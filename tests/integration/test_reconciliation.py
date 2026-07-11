import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ledger import reserve
from app.models import (
    Account,
    AccountPurpose,
    JournalEvent,
    JournalTransaction,
    Settlement,
    SettlementStatus,
)
from app.provider import PayoutTimeout, ProviderLookup, ProviderResult
from app.routing import Currency
from app.schemas import SettlementCreate, request_fingerprint
from app.seed import seed_demo_accounts
from app.service import SettlementNotFound, SettlementService
from conftest import ScriptedPayoutProvider


COMMAND = SettlementCreate(amount_usd=Decimal("40"), target_currency=Currency.PHP)


class LookupFailureProvider:
    async def initiate(
        self, settlement_id, amount_usd, target_currency, quoted_amount
    ):
        raise AssertionError("initiate must not be called")

    async def lookup(self, settlement_id):
        raise RuntimeError("provider lookup failed")


async def insert_in_progress(session_factory, rate_book) -> Settlement:
    quote = rate_book.quote(Currency.PHP)
    settlement_id = uuid4()
    settlement = Settlement(
        id=settlement_id,
        owner_id="customer",
        idempotency_key="recover",
        request_fingerprint=request_fingerprint("customer", COMMAND),
        amount_usd=COMMAND.amount_usd,
        target_currency=COMMAND.target_currency,
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
        quoted_amount=COMMAND.amount_usd * quote.aggregate_rate,
        provider_operation_id=settlement_id,
        status=SettlementStatus.PAYOUT_IN_PROGRESS,
    )
    async with session_factory() as session:
        async with session.begin():
            session.add(settlement)
            await session.flush()
            await reserve(session, settlement)
    return settlement


async def create_pending(session_factory, rate_book, provider):
    service = SettlementService(session_factory, rate_book, provider)
    settlement = await service.create("customer", "pending", COMMAND)
    assert settlement.status == SettlementStatus.PENDING_RECONCILIATION
    return service, settlement


async def test_unknown_reconciliation_preserves_pending_reservation(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(
        PayoutTimeout(), (ProviderLookup.UNKNOWN,)
    )
    service, pending = await create_pending(session_factory, rate_book, provider)

    result = await service.reconcile(pending.id)

    assert result.status == SettlementStatus.PENDING_RECONCILIATION
    assert provider.initiate_calls == [pending.id]
    assert provider.lookup_calls == [pending.id]
    async with session_factory() as session:
        reserved = await session.scalar(
            select(Account.balance).where(
                Account.owner_id == "customer",
                Account.purpose == AccountPurpose.RESERVED,
            )
        )
    assert reserved == Decimal("40")


async def test_repeated_unknown_reconciliation_is_a_noop(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(
        PayoutTimeout(), tuple([ProviderLookup.UNKNOWN] * 3)
    )
    service, pending = await create_pending(session_factory, rate_book, provider)

    for _ in range(3):
        result = await service.reconcile(pending.id)
        assert result.status == SettlementStatus.PENDING_RECONCILIATION
        async with session_factory() as session:
            balances = dict(
                (
                    await session.execute(
                        select(Account.purpose, Account.balance).where(
                            Account.owner_id == "customer"
                        )
                    )
                ).all()
            )
            events = (
                await session.scalars(
                    select(JournalTransaction.event).where(
                        JournalTransaction.settlement_id == pending.id
                    )
                )
            ).all()
        assert balances[AccountPurpose.AVAILABLE] == Decimal("60")
        assert balances[AccountPurpose.RESERVED] == Decimal("40")
        assert events == [JournalEvent.RESERVE]


async def test_concurrent_unpaid_reconciliation_releases_once(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(
        PayoutTimeout(), tuple([ProviderLookup.UNPAID] * 50)
    )
    service, pending = await create_pending(session_factory, rate_book, provider)

    results = await asyncio.wait_for(
        asyncio.gather(*(service.reconcile(pending.id) for _ in range(50))),
        timeout=20,
    )

    assert all(result.status == SettlementStatus.FAILED for result in results)
    async with session_factory() as session:
        balances = dict(
            (
                await session.execute(
                    select(Account.purpose, Account.balance).where(
                        Account.owner_id == "customer"
                    )
                )
            ).all()
        )
        events = (
            await session.scalars(
                select(JournalTransaction.event).where(
                    JournalTransaction.settlement_id == pending.id
                )
            )
        ).all()
    assert balances[AccountPurpose.AVAILABLE] == Decimal("100")
    assert balances[AccountPurpose.RESERVED] == Decimal("0")
    assert events.count(JournalEvent.RESERVE) == 1
    assert events.count(JournalEvent.RELEASE) == 1
    assert events.count(JournalEvent.CONSUME) == 0


async def test_unexpected_lookup_failure_preserves_pending_reservation(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    _, pending = await create_pending(
        session_factory, rate_book, ScriptedPayoutProvider(PayoutTimeout())
    )
    service = SettlementService(session_factory, rate_book, LookupFailureProvider())

    with pytest.raises(RuntimeError, match="provider lookup failed"):
        await service.reconcile(pending.id)

    persisted = await service.get(pending.id)
    assert persisted.status == SettlementStatus.PENDING_RECONCILIATION
    async with session_factory() as session:
        balances = dict(
            (
                await session.execute(
                    select(Account.purpose, Account.balance).where(
                        Account.owner_id == "customer"
                    )
                )
            ).all()
        )
        events = (
            await session.scalars(
                select(JournalTransaction.event).where(
                    JournalTransaction.settlement_id == pending.id
                )
            )
        ).all()
    assert balances[AccountPurpose.AVAILABLE] == Decimal("60")
    assert balances[AccountPurpose.RESERVED] == Decimal("40")
    assert events == [JournalEvent.RESERVE]


@pytest.mark.parametrize(
    ("lookup", "status", "terminal_event", "available", "reserved"),
    [
        (
            ProviderLookup.PAID,
            SettlementStatus.SUCCESS,
            JournalEvent.CONSUME,
            "60",
            "0",
        ),
        (
            ProviderLookup.UNPAID,
            SettlementStatus.FAILED,
            JournalEvent.RELEASE,
            "100",
            "0",
        ),
    ],
)
async def test_definitive_reconciliation_finalizes_exactly_once(
    clean_database,
    session_factory,
    rate_book,
    lookup,
    status,
    terminal_event,
    available,
    reserved,
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(PayoutTimeout(), (lookup,))
    service, pending = await create_pending(session_factory, rate_book, provider)

    first = await service.reconcile(pending.id)
    replay = await service.reconcile(pending.id)

    assert first.status == replay.status == status
    assert provider.lookup_calls == [pending.id]
    async with session_factory() as session:
        balances = dict(
            (
                await session.execute(
                    select(Account.purpose, Account.balance).where(
                        Account.owner_id == "customer"
                    )
                )
            ).all()
        )
        events = (
            await session.scalars(
                select(JournalTransaction.event).where(
                    JournalTransaction.settlement_id == pending.id
                )
            )
        ).all()
    assert balances[AccountPurpose.AVAILABLE] == Decimal(available)
    assert balances[AccountPurpose.RESERVED] == Decimal(reserved)
    assert events.count(terminal_event) == 1


@pytest.mark.parametrize(
    ("lookup", "status", "initiation_count"),
    [
        (ProviderLookup.PAID, SettlementStatus.SUCCESS, 0),
        (ProviderLookup.UNPAID, SettlementStatus.FAILED, 0),
        (ProviderLookup.UNKNOWN, SettlementStatus.PAYOUT_IN_PROGRESS, 0),
        (ProviderLookup.NOT_FOUND, SettlementStatus.SUCCESS, 1),
    ],
)
async def test_in_progress_recovery_looks_up_before_optional_idempotent_submit(
    clean_database,
    session_factory,
    rate_book,
    lookup,
    status,
    initiation_count,
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    crash_left = await insert_in_progress(session_factory, rate_book)
    provider = ScriptedPayoutProvider(ProviderResult.PAID, (lookup,))
    service = SettlementService(session_factory, rate_book, provider)

    result = await service.reconcile(crash_left.id)

    assert result.status == status
    assert provider.lookup_calls == [crash_left.id]
    assert len(provider.initiate_calls) == initiation_count
    if initiation_count:
        assert provider.initiate_calls == [crash_left.id]


async def test_missing_settlement_raises_without_provider_work(
    clean_database, session_factory, rate_book
):
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    service = SettlementService(session_factory, rate_book, provider)
    missing = uuid4()

    with pytest.raises(SettlementNotFound):
        await service.get(missing)
    with pytest.raises(SettlementNotFound):
        await service.reconcile(missing)

    assert provider.lookup_calls == []
    assert provider.initiate_calls == []
