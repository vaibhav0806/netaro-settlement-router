import asyncio
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from app.ledger import InsufficientFunds, reserve
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
from app.service import SettlementService
from conftest import ScriptedPayoutProvider


COMMAND = SettlementCreate(amount_usd=Decimal("20"), target_currency=Currency.PHP)


async def reserve_amount(session_factory, key: str, amount: Decimal) -> None:
    settlement_id = uuid4()
    async with session_factory() as session:
        settlement = Settlement(
            id=settlement_id,
            owner_id="customer",
            idempotency_key=key,
            request_fingerprint=f"fingerprint-{key}",
            amount_usd=amount,
            target_currency=Currency.PHP,
            route=[],
            snapshot_version=1,
            aggregate_rate=Decimal("55"),
            quoted_amount=amount * Decimal("55"),
            provider_operation_id=settlement_id,
            status=SettlementStatus.RESERVED,
        )
        session.add(settlement)
        try:
            await session.flush()
            await reserve(session, settlement)
            await session.commit()
        except InsufficientFunds:
            await session.rollback()
            raise


async def test_scripted_provider_reuses_stored_result_for_replayed_operation():
    settlement_id = uuid4()
    provider = ScriptedPayoutProvider(ProviderResult.PAID)

    first = await provider.initiate(
        settlement_id, Decimal("20"), Currency.PHP, Decimal("1100")
    )
    provider.initial_result = ProviderResult.UNPAID
    replay = await provider.initiate(
        settlement_id, Decimal("20"), Currency.PHP, Decimal("1100")
    )

    assert first == replay == ProviderResult.PAID
    assert provider.initiate_calls == [settlement_id, settlement_id]
    assert provider.effective_operations == {settlement_id: ProviderResult.PAID}


async def test_one_hundred_same_key_calls_create_one_reserve_and_initiation(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("1000"))
        await session.commit()
    provider = ScriptedPayoutProvider(ProviderResult.PAID, paused=True)
    service = SettlementService(session_factory, rate_book, provider)
    tasks = [
        asyncio.create_task(service.create("customer", "same", COMMAND))
        for _ in range(100)
    ]
    try:
        await asyncio.wait_for(provider.wait_for_initiations(1), timeout=10)
        async with session_factory() as session:
            settlement_count = await session.scalar(
                select(func.count()).select_from(Settlement)
            )
            reserve_count = await session.scalar(
                select(func.count())
                .select_from(JournalTransaction)
                .where(JournalTransaction.event == JournalEvent.RESERVE)
            )
        assert settlement_count == reserve_count == 1
    finally:
        provider.allow_initiate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)

    assert len({result.id for result in results}) == 1
    assert len(provider.initiate_calls) == 1


async def test_unique_requests_cannot_overspend_while_payouts_are_paused(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("1000"))
        await session.commit()
    provider = ScriptedPayoutProvider(ProviderResult.PAID, paused=True)
    service = SettlementService(session_factory, rate_book, provider)
    tasks = [
        asyncio.create_task(service.create("customer", f"unique-{index}", COMMAND))
        for index in range(100)
    ]
    try:
        await asyncio.wait_for(provider.wait_for_initiations(50), timeout=20)
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
            reserve_count = await session.scalar(
                select(func.count())
                .select_from(JournalTransaction)
                .where(JournalTransaction.event == JournalEvent.RESERVE)
            )
        assert balances[AccountPurpose.AVAILABLE] == Decimal("0")
        assert balances[AccountPurpose.RESERVED] == Decimal("1000")
        assert reserve_count == 50
    finally:
        provider.allow_initiate.set()
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=20
    )

    assert sum(isinstance(result, InsufficientFunds) for result in results) == 50
    assert sum(
        not isinstance(result, BaseException)
        and result.status == SettlementStatus.SUCCESS
        for result in results
    ) == 50
    assert len(provider.initiate_calls) == 50


async def test_mixed_concurrent_wave_exhausts_balance_and_rejects_remainder(
    clean_database, session_factory
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("1000"))
        await session.commit()
    amounts = [
        Decimal("375"),
        Decimal("250"),
        Decimal("125"),
        Decimal("100"),
        Decimal("75"),
        Decimal("50"),
        Decimal("25"),
        Decimal("1001"),
    ]

    results = await asyncio.gather(
        *(
            reserve_amount(session_factory, f"mixed-{index}", amount)
            for index, amount in enumerate(amounts)
        ),
        return_exceptions=True,
    )

    assert results[:-1] == [None] * 7
    assert isinstance(results[-1], InsufficientFunds)
    async with session_factory() as session:
        balances = (await session.scalars(select(Account.balance))).all()
        reserved = await session.scalar(
            select(Account.balance).where(
                Account.owner_id == "customer",
                Account.purpose == AccountPurpose.RESERVED,
            )
        )
    assert reserved == Decimal("1000")
    assert all(value >= 0 for value in balances)


async def test_fifty_competing_success_finalizers_consume_once(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(
        PayoutTimeout(), tuple([ProviderLookup.PAID] * 50)
    )
    service = SettlementService(session_factory, rate_book, provider)
    pending = await service.create("customer", "pending", COMMAND)

    results = await asyncio.gather(
        *(service.reconcile(pending.id) for _ in range(50))
    )

    assert all(result.status == SettlementStatus.SUCCESS for result in results)
    async with session_factory() as session:
        consume_count = await session.scalar(
            select(func.count())
            .select_from(JournalTransaction)
            .where(
                JournalTransaction.settlement_id == pending.id,
                JournalTransaction.event == JournalEvent.CONSUME,
            )
        )
    assert consume_count == 1


async def test_concurrent_not_found_recovery_has_one_effective_operation(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    quote = rate_book.quote(Currency.PHP)
    settlement_id = uuid4()
    settlement = Settlement(
        id=settlement_id,
        owner_id="customer",
        idempotency_key="recovery-race",
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
    provider = ScriptedPayoutProvider(
        ProviderResult.PAID,
        tuple([ProviderLookup.NOT_FOUND] * 10),
        paused=True,
    )
    service = SettlementService(session_factory, rate_book, provider)
    tasks = [asyncio.create_task(service.reconcile(settlement_id)) for _ in range(10)]
    try:
        await asyncio.wait_for(provider.wait_for_initiations(10), timeout=10)
        assert len(provider.effective_operations) == 1
    finally:
        provider.allow_initiate.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)

    assert all(result == results[0] for result in results)
    assert results[0].status == SettlementStatus.SUCCESS
    assert provider.initiate_calls == [settlement_id] * 10
    assert provider.effective_operations == {settlement_id: ProviderResult.PAID}
