from decimal import Decimal
from uuid import uuid4

import pytest

from sqlalchemy import func, select

from app.models import (
    Account,
    AccountPurpose,
    JournalEvent,
    JournalTransaction,
    Settlement,
    SettlementStatus,
)
from app.provider import (
    MockPayoutProvider,
    PayoutTimeout,
    ProviderLookup,
    ProviderResult,
)
from app.routing import Currency
from app.schemas import SettlementCreate
from app.seed import seed_demo_accounts
from app.service import SettlementService
from conftest import ScriptedPayoutProvider


class InitiationFailureProvider:
    async def initiate(
        self, settlement_id, amount_usd, target_currency, quoted_amount
    ):
        raise RuntimeError("provider initiation failed")

    async def lookup(self, settlement_id):
        return ProviderLookup.UNKNOWN


async def test_paid_settlement_consumes_reservation_once(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(ProviderResult.PAID)
    service = SettlementService(session_factory, rate_book, provider)

    result = await service.create(
        "customer",
        "paid-1",
        SettlementCreate(amount_usd=Decimal("40"), target_currency=Currency.PHP),
    )

    assert result.status.value == "SUCCESS"
    assert result.amount_usd == Decimal("40")
    assert result.quoted_amount == Decimal("2200")
    assert result.aggregate_rate == Decimal("55")
    assert result.snapshot_version == 7
    assert result.route[0].lp == "LP_TEST"
    assert provider.initiate_calls == [result.id]
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
        events = dict(
            (
                await session.execute(
                    select(JournalTransaction.event, func.count())
                    .where(JournalTransaction.settlement_id == result.id)
                    .group_by(JournalTransaction.event)
                )
            ).all()
        )
    assert balances[AccountPurpose.AVAILABLE] == Decimal("60")
    assert balances[AccountPurpose.RESERVED] == Decimal("0")
    assert events == {JournalEvent.RESERVE: 1, JournalEvent.CONSUME: 1}


async def test_unpaid_settlement_releases_reservation_once(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(ProviderResult.UNPAID)
    service = SettlementService(session_factory, rate_book, provider)

    result = await service.create(
        "customer",
        "unpaid-1",
        SettlementCreate(amount_usd=Decimal("40"), target_currency=Currency.PHP),
    )

    assert result.status.value == "FAILED"
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
        events = set(
            await session.scalars(
                select(JournalTransaction.event).where(
                    JournalTransaction.settlement_id == result.id
                )
            )
        )
    assert balances[AccountPurpose.AVAILABLE] == Decimal("100")
    assert balances[AccountPurpose.RESERVED] == Decimal("0")
    assert events == {JournalEvent.RESERVE, JournalEvent.RELEASE}


async def test_timeout_preserves_reservation_for_reconciliation(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    provider = ScriptedPayoutProvider(PayoutTimeout())
    service = SettlementService(session_factory, rate_book, provider)

    result = await service.create(
        "customer",
        "timeout-1",
        SettlementCreate(amount_usd=Decimal("40"), target_currency=Currency.PHP),
    )

    assert result.status.value == "PENDING_RECONCILIATION"
    assert provider.initiate_calls == [result.id]
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
        events = set(
            await session.scalars(
                select(JournalTransaction.event).where(
                    JournalTransaction.settlement_id == result.id
                )
            )
        )
    assert balances[AccountPurpose.AVAILABLE] == Decimal("60")
    assert balances[AccountPurpose.RESERVED] == Decimal("40")
    assert events == {JournalEvent.RESERVE}


async def test_unexpected_initiation_failure_preserves_in_progress_reservation(
    clean_database, session_factory, rate_book
):
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("100"))
        await session.commit()
    service = SettlementService(
        session_factory, rate_book, InitiationFailureProvider()
    )

    with pytest.raises(RuntimeError, match="provider initiation failed"):
        await service.create(
            "customer",
            "runtime-failure",
            SettlementCreate(
                amount_usd=Decimal("40"), target_currency=Currency.PHP
            ),
        )

    async with session_factory() as session:
        settlement = await session.scalar(
            select(Settlement).where(
                Settlement.idempotency_key == "runtime-failure"
            )
        )
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
                    JournalTransaction.settlement_id == settlement.id
                )
            )
        ).all()
    assert settlement.status == SettlementStatus.PAYOUT_IN_PROGRESS
    assert balances[AccountPurpose.AVAILABLE] == Decimal("60")
    assert balances[AccountPurpose.RESERVED] == Decimal("40")
    assert events == [JournalEvent.RESERVE]


async def test_mock_provider_deduplicates_timeout_operation_by_settlement_id():
    provider = MockPayoutProvider(
        timeout_seconds=0,
        outcomes=(ProviderLookup.UNKNOWN,),
    )
    settlement_id = uuid4()

    for _ in range(2):
        with pytest.raises(PayoutTimeout):
            await provider.initiate(
                settlement_id,
                Decimal("40"),
                Currency.PHP,
                Decimal("2200"),
            )

    assert await provider.lookup(settlement_id) == ProviderLookup.UNKNOWN
    assert await provider.lookup(uuid4()) == ProviderLookup.NOT_FOUND


@pytest.mark.parametrize(
    "internal_result",
    [ProviderLookup.PAID, ProviderLookup.UNPAID, ProviderLookup.UNKNOWN],
)
async def test_mock_provider_timeout_retains_configured_internal_result(
    internal_result,
):
    provider = MockPayoutProvider(
        timeout_seconds=0,
        outcomes=(ProviderLookup.UNKNOWN,),
        timeout_lookup=internal_result,
    )
    settlement_id = uuid4()

    with pytest.raises(PayoutTimeout):
        await provider.initiate(
            settlement_id, Decimal("1"), Currency.PHP, Decimal("55")
        )

    assert await provider.lookup(settlement_id) == internal_result


async def test_mock_provider_default_demo_distribution_is_deterministic():
    provider = MockPayoutProvider(timeout_seconds=0)
    results = []
    for _ in range(20):
        settlement_id = uuid4()
        try:
            results.append(
                await provider.initiate(
                    settlement_id,
                    Decimal("1"),
                    Currency.PHP,
                    Decimal("55"),
                )
            )
        except PayoutTimeout:
            results.append(None)

    assert results.count(ProviderResult.PAID) == 14
    assert results.count(ProviderResult.UNPAID) == 3
    assert results.count(None) == 3
