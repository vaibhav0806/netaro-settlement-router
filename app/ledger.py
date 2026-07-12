from collections import defaultdict
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    AccountClass,
    AccountPurpose,
    JournalEvent,
    JournalTransaction,
    Posting,
    PostingSide,
    Settlement,
)
from app.routing import Currency


class InsufficientFunds(Exception):
    pass


def apply_posting(account: Account, side: PostingSide, amount: Decimal) -> None:
    increases = (
        account.account_class == AccountClass.ASSET and side == PostingSide.DEBIT
    ) or (
        account.account_class == AccountClass.LIABILITY and side == PostingSide.CREDIT
    )
    account.balance += amount if increases else -amount
    if account.balance < 0:
        raise InsufficientFunds


async def _locked_accounts(
    session: AsyncSession,
    keys: tuple[tuple[str, AccountPurpose], ...],
) -> dict[tuple[str, AccountPurpose], Account]:
    conditions = [
        (Account.owner_id == owner_id) & (Account.purpose == purpose)
        for owner_id, purpose in keys
    ]
    accounts = (
        await session.scalars(
            select(Account)
            .where(Account.currency == Currency.USD)
            .where(or_(*conditions))
            .order_by(Account.id)
            .with_for_update()
        )
    ).all()
    result = {(account.owner_id, account.purpose): account for account in accounts}
    if set(result) != set(keys):
        raise LookupError("required ledger account is missing")
    return result


async def _post(
    session: AsyncSession,
    settlement: Settlement,
    event: JournalEvent,
    debit_key: tuple[str, AccountPurpose],
    credit_key: tuple[str, AccountPurpose],
) -> None:
    accounts = await _locked_accounts(session, (debit_key, credit_key))
    debit = accounts[debit_key]
    credit = accounts[credit_key]
    amount = settlement.amount_usd
    if debit.balance < amount:
        raise InsufficientFunds

    journal = JournalTransaction(
        settlement_id=settlement.id,
        event=event,
        is_posted=False,
    )
    session.add(journal)
    await session.flush()
    session.add_all(
        [
            Posting(
                journal_id=journal.id,
                account_id=debit.id,
                currency=Currency.USD,
                side=PostingSide.DEBIT,
                amount=amount,
            ),
            Posting(
                journal_id=journal.id,
                account_id=credit.id,
                currency=Currency.USD,
                side=PostingSide.CREDIT,
                amount=amount,
            ),
        ]
    )
    apply_posting(debit, PostingSide.DEBIT, amount)
    apply_posting(credit, PostingSide.CREDIT, amount)
    await session.flush()
    journal.is_posted = True


async def reserve(session: AsyncSession, settlement: Settlement) -> None:
    await _post(
        session,
        settlement,
        JournalEvent.RESERVE,
        (settlement.owner_id, AccountPurpose.AVAILABLE),
        (settlement.owner_id, AccountPurpose.RESERVED),
    )


async def consume(session: AsyncSession, settlement: Settlement) -> None:
    await _post(
        session,
        settlement,
        JournalEvent.CONSUME,
        (settlement.owner_id, AccountPurpose.RESERVED),
        ("system", AccountPurpose.OMNIBUS),
    )


async def release(session: AsyncSession, settlement: Settlement) -> None:
    await _post(
        session,
        settlement,
        JournalEvent.RELEASE,
        (settlement.owner_id, AccountPurpose.RESERVED),
        (settlement.owner_id, AccountPurpose.AVAILABLE),
    )


async def assert_ledger_invariants(session: AsyncSession) -> None:
    tracked_accounts = [
        value
        for value in (*session.identity_map.values(), *session.new)
        if isinstance(value, Account)
    ]
    assert all(
        account.balance is None or account.balance >= 0 for account in tracked_accounts
    ), "negative account balance"
    await session.flush()
    accounts = (
        await session.scalars(
            select(Account)
            .order_by(Account.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    postings = (
        await session.scalars(select(Posting).execution_options(populate_existing=True))
    ).all()
    assert all(account.balance >= 0 for account in accounts), "negative account balance"
    journal_totals: dict[tuple[object, Currency], dict[PostingSide, Decimal]] = (
        defaultdict(lambda: defaultdict(Decimal))
    )
    expected_balances = {account.id: Decimal("0") for account in accounts}
    account_by_id = {account.id: account for account in accounts}
    for posting in postings:
        journal_totals[(posting.journal_id, posting.currency)][posting.side] += (
            posting.amount
        )
        account = account_by_id[posting.account_id]
        increases = (
            account.account_class == AccountClass.ASSET
            and posting.side == PostingSide.DEBIT
        ) or (
            account.account_class == AccountClass.LIABILITY
            and posting.side == PostingSide.CREDIT
        )
        expected_balances[posting.account_id] += (
            posting.amount if increases else -posting.amount
        )

    assert all(
        totals[PostingSide.DEBIT] == totals[PostingSide.CREDIT]
        for totals in journal_totals.values()
    ), "unbalanced journal currency"
    assert all(
        account.balance == expected_balances[account.id] for account in accounts
    ), "materialized account balance mismatch"
