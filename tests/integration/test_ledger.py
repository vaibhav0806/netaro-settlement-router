import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
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
    AccountClass,
    AccountPurpose,
    JournalEvent,
    JournalTransaction,
    Posting,
    PostingSide,
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


async def test_invariant_check_reports_positive_materialized_balance_mismatch(
    seeded_accounts, session
):
    account = await session.scalar(
        select(Account).where(
            Account.owner_id == "customer",
            Account.purpose == AccountPurpose.AVAILABLE,
        )
    )
    account.balance = Decimal("999")

    with pytest.raises(AssertionError, match="materialized account balance mismatch"):
        await assert_ledger_invariants(session)
    await session.rollback()


async def test_application_generated_journals_have_two_balanced_positive_postings(
    seeded_accounts, session
):
    consumed = await make_settlement(session, Decimal("40"), "consumed")
    await reserve(session, consumed)
    await consume(session, consumed)
    released = await make_settlement(session, Decimal("30"), "released")
    await reserve(session, released)
    await release(session, released)
    await session.commit()

    journals = (await session.scalars(select(JournalTransaction))).all()
    assert {journal.event for journal in journals} == set(JournalEvent)
    for journal in journals:
        postings = (
            await session.scalars(
                select(Posting).where(Posting.journal_id == journal.id)
            )
        ).all()
        assert len(postings) == 2
        assert all(posting.amount > 0 for posting in postings)
        assert len({posting.currency for posting in postings}) == 1
        assert {posting.side for posting in postings} == {
            PostingSide.DEBIT,
            PostingSide.CREDIT,
        }


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-1")])
async def test_postgresql_rejects_nonpositive_posting_amount(
    seeded_accounts, session, amount
):
    journal = await session.scalar(
        select(JournalTransaction).where(
            JournalTransaction.event == JournalEvent.OPENING
        )
    )
    account = await session.scalar(select(Account).limit(1))
    session.add(
        Posting(
            journal_id=journal.id,
            account_id=account.id,
            currency=account.currency,
            side=PostingSide.DEBIT,
            amount=amount,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
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


async def test_opening_journal_rejects_settlement_id(session):
    settlement = await make_settlement(session, amount=Decimal("40"))
    session.add(
        JournalTransaction(
            settlement_id=settlement.id,
            event=JournalEvent.OPENING,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_nonopening_journal_requires_settlement_id(session):
    session.add(
        JournalTransaction(
            settlement_id=None,
            event=JournalEvent.RESERVE,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_invariant_audit_waits_for_concurrent_ledger_commit(
    session_factory, seeded_accounts
):
    writer_locked = asyncio.Event()
    allow_commit = asyncio.Event()
    auditor_pid = asyncio.get_running_loop().create_future()

    async def write_reserve() -> None:
        async with session_factory() as writer:
            settlement = await make_settlement(
                writer, amount=Decimal("40"), key="concurrent-audit"
            )
            await reserve(writer, settlement)
            await writer.flush()
            writer_locked.set()
            await allow_commit.wait()
            await writer.commit()

    async def audit() -> None:
        async with session_factory() as auditor:
            isolation = await auditor.scalar(text("SHOW transaction_isolation"))
            assert isolation == "read committed"
            preloaded = (
                await auditor.scalars(select(Account).order_by(Account.id))
            ).all()
            available = next(
                account
                for account in preloaded
                if account.owner_id == "customer"
                and account.purpose == AccountPurpose.AVAILABLE
            )
            reserved = next(
                account
                for account in preloaded
                if account.owner_id == "customer"
                and account.purpose == AccountPurpose.RESERVED
            )
            assert available.balance == Decimal("1000")
            assert reserved.balance == Decimal("0")
            auditor_pid.set_result(
                await auditor.scalar(text("SELECT pg_backend_pid()"))
            )
            await assert_ledger_invariants(auditor)
            assert available.balance == Decimal("960")
            assert reserved.balance == Decimal("40")
            await auditor.rollback()

    writer_task = asyncio.create_task(write_reserve())
    ready_task = asyncio.create_task(writer_locked.wait())
    audit_task = None
    try:
        done, _ = await asyncio.wait(
            (writer_task, ready_task),
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if writer_task in done:
            await writer_task
        if ready_task not in done:
            raise TimeoutError("writer did not acquire account locks")
        audit_task = asyncio.create_task(audit())
        pid = await asyncio.wait_for(auditor_pid, timeout=1)
        async with session_factory() as observer:
            async with asyncio.timeout(2):
                while True:
                    wait_event_type = await observer.scalar(
                        text(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": pid},
                    )
                    if wait_event_type == "Lock":
                        break
                    if audit_task.done():
                        await audit_task
                        pytest.fail("audit completed without waiting for account locks")
                    await asyncio.sleep(0.01)

        allow_commit.set()
        await asyncio.wait_for(writer_task, timeout=1)
        await asyncio.wait_for(audit_task, timeout=1)
    finally:
        allow_commit.set()
        tasks = [
            task
            for task in (writer_task, ready_task, audit_task)
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_seed_rejects_missing_omnibus_account(session):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()
    await session.execute(
        delete(Account).where(
            Account.owner_id == "system",
            Account.currency == Currency.USDC,
            Account.purpose == AccountPurpose.OMNIBUS,
        )
    )
    await session.commit()

    with pytest.raises(RuntimeError, match="seed state is inconsistent"):
        await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.rollback()


async def test_seed_rejects_missing_opening_effect(session):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()
    opening_id = await session.scalar(
        select(JournalTransaction.id).where(
            JournalTransaction.event == JournalEvent.OPENING
        )
    )
    await session.execute(delete(Posting).where(Posting.journal_id == opening_id))
    await session.execute(
        delete(JournalTransaction).where(JournalTransaction.id == opening_id)
    )
    await session.commit()

    with pytest.raises(RuntimeError, match="seed state is inconsistent"):
        await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.rollback()


async def test_seed_rejects_corrupt_opening_effect(session):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()
    opening_id = await session.scalar(
        select(JournalTransaction.id).where(
            JournalTransaction.event == JournalEvent.OPENING
        )
    )
    await session.execute(
        update(Posting)
        .where(
            Posting.journal_id == opening_id,
            Posting.side == "CREDIT",
        )
        .values(amount=Decimal("999"))
    )
    await session.commit()

    with pytest.raises(RuntimeError, match="seed state is inconsistent"):
        await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.rollback()


async def test_seed_rejects_duplicate_opening_journal(session):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()
    session.add(
        JournalTransaction(settlement_id=None, event=JournalEvent.OPENING)
    )
    await session.commit()

    with pytest.raises(RuntimeError, match="seed state is inconsistent"):
        await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.rollback()


async def test_seed_rejects_partial_system_accounts(session):
    session.add(
        Account(
            owner_id="system",
            currency=Currency.USD,
            account_class=AccountClass.ASSET,
            purpose=AccountPurpose.OMNIBUS,
            balance=Decimal("0"),
        )
    )
    await session.commit()

    with pytest.raises(RuntimeError, match="seed state is inconsistent"):
        await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.rollback()


async def test_invariant_check_accepts_pending_account_default_balance(session):
    session.add(
        Account(
            owner_id="pending-default",
            currency=Currency.USD,
            account_class=AccountClass.LIABILITY,
            purpose=AccountPurpose.AVAILABLE,
        )
    )

    await assert_ledger_invariants(session)
    await session.rollback()


@pytest.mark.parametrize("terminal_event", [None, "consume", "release"])
async def test_seed_is_noop_after_valid_ledger_evolution(session, terminal_event):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    settlement = await make_settlement(
        session, amount=Decimal("40"), key=f"seed-replay-{terminal_event}"
    )
    await reserve(session, settlement)
    if terminal_event == "consume":
        await consume(session, settlement)
    elif terminal_event == "release":
        await release(session, settlement)
    await session.commit()

    before_balances = dict(
        (
            await session.execute(
                select(Account.id, Account.balance).order_by(Account.id)
            )
        ).all()
    )
    before_journals = await session.scalar(
        select(func.count()).select_from(JournalTransaction)
    )
    before_postings = await session.scalar(select(func.count()).select_from(Posting))

    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()

    assert dict(
        (
            await session.execute(
                select(Account.id, Account.balance).order_by(Account.id)
            )
        ).all()
    ) == before_balances
    assert (
        await session.scalar(select(func.count()).select_from(JournalTransaction))
        == before_journals
    )
    assert await session.scalar(select(func.count()).select_from(Posting)) == before_postings


async def test_seed_rejects_corrupt_current_balance(session):
    await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.commit()
    await session.execute(
        update(Account)
        .where(
            Account.owner_id == "customer",
            Account.purpose == AccountPurpose.AVAILABLE,
        )
        .values(balance=Decimal("999"))
    )
    await session.commit()

    with pytest.raises(RuntimeError, match="seed state is inconsistent"):
        await seed_demo_accounts(session, "customer", Decimal("1000"))
    await session.rollback()


async def test_invariant_check_reports_negative_pending_new_account(session):
    session.add(
        Account(
            owner_id="pending",
            currency=Currency.USD,
            account_class=AccountClass.LIABILITY,
            purpose=AccountPurpose.AVAILABLE,
            balance=Decimal("-1"),
        )
    )

    with pytest.raises(AssertionError, match="negative account balance"):
        await assert_ledger_invariants(session)
    await session.rollback()
