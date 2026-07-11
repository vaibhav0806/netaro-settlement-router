from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.ledger import reserve
from app.models import JournalEvent, JournalTransaction, Settlement, SettlementStatus
from app.provider import ProviderResult
from app.routing import Currency, Edge
from app.schemas import SettlementCreate, request_fingerprint
from app.seed import seed_demo_accounts
from app.service import IdempotencyConflict, SettlementService
from conftest import ScriptedPayoutProvider


def command(amount: str = "40", target: Currency = Currency.PHP) -> SettlementCreate:
    return SettlementCreate(amount_usd=Decimal(amount), target_currency=target)


async def insert_reserved(session_factory, rate_book, key: str = "crash") -> Settlement:
    requested = command()
    quote = rate_book.quote(requested.target_currency)
    settlement_id = uuid4()
    settlement = Settlement(
        id=settlement_id,
        owner_id="customer",
        idempotency_key=key,
        request_fingerprint=request_fingerprint("customer", requested),
        amount_usd=requested.amount_usd,
        target_currency=requested.target_currency,
        route=[
            {
                "source": hop.source.value,
                "target": hop.target.value,
                "lp": hop.lp,
                "rate": str(hop.rate),
            }
            for hop in quote.hops
        ],
        snapshot_version=quote.snapshot_version,
        aggregate_rate=quote.aggregate_rate,
        quoted_amount=requested.amount_usd * quote.aggregate_rate,
        provider_operation_id=settlement_id,
        status=SettlementStatus.RESERVED,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(settlement)
            await session.flush()
            await reserve(session, settlement)
    return settlement


async def test_same_payload_replay_returns_original_without_financial_work(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    service = SettlementService(session_factory, rate_book, provider)
    first = await service.create("customer", "same", command())
    rate_book.publish(
        (Edge(Currency.USD, Currency.PHP, "LP_NEW", Decimal("99")),), version=8
    )

    replay = await service.create("customer", "same", command("40.0"))

    assert replay == first
    assert provider.initiate_calls == [first.id]
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(JournalTransaction)
            .where(
                JournalTransaction.settlement_id == first.id,
                JournalTransaction.event == JournalEvent.RESERVE,
            )
        )
    assert count == 1


@pytest.mark.parametrize(
    "changed",
    [command("41"), command("40", Currency.EUR)],
)
async def test_changed_payload_conflicts_before_quote_or_provider_work(
    clean_database, session_factory, rate_book, changed
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    service = SettlementService(session_factory, rate_book, provider)
    original = await service.create("customer", "conflict", command())

    with pytest.raises(IdempotencyConflict):
        await service.create("customer", "conflict", changed)

    assert provider.initiate_calls == [original.id]
    assert await service.get(original.id) == original


async def test_reserved_replay_claims_crash_left_settlement_once(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    crash_left = await insert_reserved(session_factory, rate_book)
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    service = SettlementService(session_factory, rate_book, provider)

    result = await service.create("customer", "crash", command())

    assert result.id == crash_left.id
    assert result.status == SettlementStatus.SUCCESS
    assert provider.initiate_calls == [crash_left.id]
    async with session_factory() as session:
        events = (
            await session.scalars(
                select(JournalTransaction.event).where(
                    JournalTransaction.settlement_id == crash_left.id
                )
            )
        ).all()
    assert events.count(JournalEvent.RESERVE) == 1
    assert events.count(JournalEvent.CONSUME) == 1


async def test_in_progress_create_replay_returns_without_provider_work(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    in_progress = await insert_reserved(session_factory, rate_book, "in-progress")
    async with session_factory() as session:
        async with session.begin():
            locked = await session.get(Settlement, in_progress.id, with_for_update=True)
            locked.status = SettlementStatus.PAYOUT_IN_PROGRESS
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    service = SettlementService(session_factory, rate_book, provider)

    replay = await service.create("customer", "in-progress", command())

    assert replay.id == in_progress.id
    assert replay.status == SettlementStatus.PAYOUT_IN_PROGRESS
    assert provider.initiate_calls == []
