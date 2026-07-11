import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger import (
    InsufficientFunds,
    assert_ledger_invariants,
    consume,
    release,
    reserve,
)
from app.models import (
    Account,
    AccountPurpose,
    JournalEvent,
    JournalTransaction,
    Posting,
    Settlement,
    SettlementStatus,
)
from app.routing import Currency
from app.seed import seed_demo_accounts


async def make_settlement(
    session: AsyncSession,
    amount: Decimal,
    key: str = "settlement",
) -> Settlement:
    settlement_id = uuid4()
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
    await session.flush()
    return settlement


async def balance(session: AsyncSession, purpose: str) -> Decimal:
    return await session.scalar(
        select(Account.balance).where(
            Account.owner_id == "customer",
            Account.currency == Currency.USD,
            Account.purpose == AccountPurpose(purpose),
        )
    )


async def test_reserve_is_balanced_and_moves_available_to_reserved(
    seeded_accounts,
    session,
):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)
    await session.commit()
    assert await balance(session, "AVAILABLE") == Decimal("960")
    assert await balance(session, "RESERVED") == Decimal("40")
    await assert_ledger_invariants(session)


async def reserve_in_new_session(session_factory, key: str, amount: Decimal):
    async with session_factory() as session:
        settlement = await make_settlement(session, amount, key)
        try:
            await reserve(session, settlement)
            await session.commit()
        except InsufficientFunds:
            await session.rollback()
            raise


async def available_balance(session_factory) -> Decimal:
    async with session_factory() as session:
        return await balance(session, "AVAILABLE")


async def test_two_concurrent_spends_cannot_overspend(session_factory, seeded_accounts):
    results = await asyncio.gather(
        reserve_in_new_session(session_factory, "a", Decimal("700")),
        reserve_in_new_session(session_factory, "b", Decimal("700")),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, InsufficientFunds) for result in results) == 1
    assert await available_balance(session_factory) == Decimal("300")


async def test_insufficient_funds_writes_no_journal(seeded_accounts, session):
    settlement = await make_settlement(session, amount=Decimal("1001"))

    with pytest.raises(InsufficientFunds):
        await reserve(session, settlement)
    await session.commit()

    count = await session.scalar(
        select(func.count()).select_from(JournalTransaction).where(
            JournalTransaction.event == JournalEvent.RESERVE
        )
    )
    assert count == 0


async def test_consume_credits_omnibus_usd_and_clears_reserved(
    seeded_accounts, session
):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)
    await consume(session, settlement)
    await session.commit()

    assert await balance(session, "AVAILABLE") == Decimal("960")
    assert await balance(session, "RESERVED") == Decimal("0")
    omnibus = await session.scalar(
        select(Account.balance).where(
            Account.owner_id == "system",
            Account.currency == Currency.USD,
            Account.purpose == AccountPurpose.OMNIBUS,
        )
    )
    assert omnibus == Decimal("960")
    await assert_ledger_invariants(session)


async def test_release_restores_available(seeded_accounts, session):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)
    await release(session, settlement)
    await session.commit()

    assert await balance(session, "AVAILABLE") == Decimal("1000")
    assert await balance(session, "RESERVED") == Decimal("0")
    await assert_ledger_invariants(session)


async def test_duplicate_settlement_event_is_rejected(seeded_accounts, session):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)

    with pytest.raises(IntegrityError):
        await reserve(session, settlement)
    await session.rollback()


async def test_every_journal_balances_per_currency(seeded_accounts, session):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)
    await session.commit()
    await assert_ledger_invariants(session)

    credit = await session.scalar(
        select(Posting).where(
            Posting.side == "CREDIT",
            Posting.journal_id
            == select(JournalTransaction.id)
            .where(JournalTransaction.event == JournalEvent.RESERVE)
            .scalar_subquery(),
        )
    )
    await session.execute(
        update(Posting).where(Posting.id == credit.id).values(amount=Decimal("39"))
    )
    with pytest.raises(AssertionError, match="unbalanced journal currency"):
        await assert_ledger_invariants(session)
    await session.rollback()


async def test_seed_is_idempotent_and_balanced(session):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()
    before = await session.scalar(select(func.count()).select_from(JournalTransaction))

    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()

    assert (
        await session.scalar(select(func.count()).select_from(JournalTransaction))
        == before
    )
    assert await balance(session, "AVAILABLE") == Decimal("1000")
    await assert_ledger_invariants(session)


async def test_concurrent_seed_is_idempotent(session_factory, clean_database):
    async def seed_once() -> None:
        async with session_factory() as session:
            await seed_demo_accounts(session, "customer", Decimal("1000"))
            await session.commit()

    await asyncio.gather(seed_once(), seed_once())

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Account)) == 4
        assert (
            await session.scalar(select(func.count()).select_from(JournalTransaction))
            == 1
        )
        await assert_ledger_invariants(session)


async def test_invariant_check_reports_negative_pending_balance(seeded_accounts, session):
    account = await session.scalar(
        select(Account).where(
            Account.owner_id == "customer",
            Account.purpose == AccountPurpose.AVAILABLE,
        )
    )
    account.balance = Decimal("-1")

    with pytest.raises(AssertionError, match="negative account balance"):
        await assert_ledger_invariants(session)
    await session.rollback()


async def test_invariant_check_accepts_valid_uncommitted_postings(
    seeded_accounts, session
):
    settlement = await make_settlement(session, amount=Decimal("40"))
    await reserve(session, settlement)

    await assert_ledger_invariants(session)
    await session.rollback()
