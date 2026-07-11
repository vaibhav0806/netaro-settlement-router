import asyncio
import os
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.ledger import apply_posting
from app.models import (
    Account,
    AccountClass,
    AccountPurpose,
    JournalEvent,
    JournalTransaction,
    Posting,
    PostingSide,
)
from app.routing import Currency


AccountKey = tuple[str, Currency, AccountPurpose]


def _seed_account_keys(owner_id: str) -> set[AccountKey]:
    return {
        (owner_id, Currency.USD, AccountPurpose.AVAILABLE),
        (owner_id, Currency.USD, AccountPurpose.RESERVED),
        ("system", Currency.USD, AccountPurpose.OMNIBUS),
        ("system", Currency.USDC, AccountPurpose.OMNIBUS),
    }


async def _load_seed_accounts(
    session: AsyncSession, owner_id: str
) -> dict[AccountKey, Account]:
    keys = _seed_account_keys(owner_id)
    accounts = (
        await session.scalars(
            select(Account).where(
                or_(
                    *(
                        (Account.owner_id == key_owner)
                        & (Account.currency == currency)
                        & (Account.purpose == purpose)
                        for key_owner, currency, purpose in keys
                    )
                )
            )
        )
    ).all()
    return {
        (account.owner_id, account.currency, account.purpose): account
        for account in accounts
    }


async def _validate_existing_seed(
    session: AsyncSession,
    owner_id: str,
    amount: Decimal,
    accounts: dict[AccountKey, Account],
) -> None:
    keys = _seed_account_keys(owner_id)
    if set(accounts) != keys:
        raise RuntimeError("seed state is inconsistent")

    available = accounts[(owner_id, Currency.USD, AccountPurpose.AVAILABLE)]
    reserved = accounts[(owner_id, Currency.USD, AccountPurpose.RESERVED)]
    omnibus_usd = accounts[("system", Currency.USD, AccountPurpose.OMNIBUS)]
    omnibus_usdc = accounts[("system", Currency.USDC, AccountPurpose.OMNIBUS)]
    expected_accounts = (
        (available, AccountClass.LIABILITY, amount),
        (reserved, AccountClass.LIABILITY, Decimal("0")),
        (omnibus_usd, AccountClass.ASSET, amount),
        (omnibus_usdc, AccountClass.ASSET, Decimal("0")),
    )
    if any(
        account.account_class != account_class or account.balance != balance
        for account, account_class, balance in expected_accounts
    ):
        raise RuntimeError("seed state is inconsistent")

    opening_ids = (
        await session.scalars(
            select(JournalTransaction.id).where(
                JournalTransaction.event == JournalEvent.OPENING,
                JournalTransaction.settlement_id.is_(None),
            )
        )
    ).all()
    if len(opening_ids) != 1:
        raise RuntimeError("seed state is inconsistent")
    postings = (
        await session.scalars(
            select(Posting).where(Posting.journal_id == opening_ids[0])
        )
    ).all()
    actual = sorted(
        (posting.account_id, posting.currency, posting.side, posting.amount)
        for posting in postings
    )
    expected = sorted(
        [
            (omnibus_usd.id, Currency.USD, PostingSide.DEBIT, amount),
            (available.id, Currency.USD, PostingSide.CREDIT, amount),
        ]
    )
    if actual != expected:
        raise RuntimeError("seed state is inconsistent")


async def seed_demo_accounts(
    session: AsyncSession, owner_id: str, amount: Decimal
) -> None:
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("netaro:demo-account-seed")))
    )
    accounts = await _load_seed_accounts(session, owner_id)
    customer_keys = {
        (owner_id, Currency.USD, AccountPurpose.AVAILABLE),
        (owner_id, Currency.USD, AccountPurpose.RESERVED),
    }
    existing_customer_keys = set(accounts) & customer_keys
    if existing_customer_keys == customer_keys:
        await _validate_existing_seed(session, owner_id, amount, accounts)
        return
    opening_count = await session.scalar(
        select(func.count())
        .select_from(JournalTransaction)
        .where(JournalTransaction.event == JournalEvent.OPENING)
    )
    if accounts or opening_count:
        raise RuntimeError("seed state is inconsistent")

    omnibus_usd = Account(
        owner_id="system",
        currency=Currency.USD,
        account_class=AccountClass.ASSET,
        purpose=AccountPurpose.OMNIBUS,
        balance=Decimal("0"),
    )
    omnibus_usdc = Account(
        owner_id="system",
        currency=Currency.USDC,
        account_class=AccountClass.ASSET,
        purpose=AccountPurpose.OMNIBUS,
        balance=Decimal("0"),
    )
    available = Account(
        owner_id=owner_id,
        currency=Currency.USD,
        account_class=AccountClass.LIABILITY,
        purpose=AccountPurpose.AVAILABLE,
        balance=Decimal("0"),
    )
    reserved = Account(
        owner_id=owner_id,
        currency=Currency.USD,
        account_class=AccountClass.LIABILITY,
        purpose=AccountPurpose.RESERVED,
        balance=Decimal("0"),
    )
    session.add_all([omnibus_usd, omnibus_usdc, available, reserved])
    await session.flush()

    journal = JournalTransaction(settlement_id=None, event=JournalEvent.OPENING)
    session.add(journal)
    await session.flush()
    session.add_all(
        [
            Posting(
                journal_id=journal.id,
                account_id=omnibus_usd.id,
                currency=Currency.USD,
                side=PostingSide.DEBIT,
                amount=amount,
            ),
            Posting(
                journal_id=journal.id,
                account_id=available.id,
                currency=Currency.USD,
                side=PostingSide.CREDIT,
                amount=amount,
            ),
        ]
    )
    apply_posting(omnibus_usd, PostingSide.DEBIT, amount)
    apply_posting(available, PostingSide.CREDIT, amount)
    await session.flush()
    await _validate_existing_seed(
        session,
        owner_id,
        amount,
        await _load_seed_accounts(session, owner_id),
    )


async def main() -> None:
    owner_id = os.getenv("DEMO_OWNER_ID", "demo-customer")
    amount = Decimal(os.getenv("DEMO_BALANCE_USD", "100000"))
    async with SessionFactory() as session:
        async with session.begin():
            await seed_demo_accounts(session, owner_id, amount)


if __name__ == "__main__":
    asyncio.run(main())
