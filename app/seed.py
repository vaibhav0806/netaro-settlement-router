import asyncio
import os
from decimal import Decimal

from sqlalchemy import func, select
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


async def seed_demo_accounts(
    session: AsyncSession, owner_id: str, amount: Decimal
) -> None:
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("netaro:demo-account-seed")))
    )
    required_purposes = {AccountPurpose.AVAILABLE, AccountPurpose.RESERVED}
    customer_accounts = (
        await session.scalars(
            select(Account).where(
                Account.owner_id == owner_id,
                Account.currency == Currency.USD,
                Account.purpose.in_(required_purposes),
            )
        )
    ).all()
    existing_purposes = {account.purpose for account in customer_accounts}
    if existing_purposes == required_purposes:
        return
    if existing_purposes:
        raise RuntimeError("customer seed accounts are incomplete")

    system_accounts = {
        account.currency: account
        for account in (
            await session.scalars(
                select(Account).where(
                    Account.owner_id == "system",
                    Account.purpose == AccountPurpose.OMNIBUS,
                )
            )
        ).all()
    }
    omnibus_usd = system_accounts.get(Currency.USD) or Account(
        owner_id="system",
        currency=Currency.USD,
        account_class=AccountClass.ASSET,
        purpose=AccountPurpose.OMNIBUS,
        balance=Decimal("0"),
    )
    omnibus_usdc = system_accounts.get(Currency.USDC) or Account(
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


async def main() -> None:
    owner_id = os.getenv("DEMO_OWNER_ID", "demo-customer")
    amount = Decimal(os.getenv("DEMO_BALANCE_USD", "100000"))
    async with SessionFactory() as session:
        async with session.begin():
            await seed_demo_accounts(session, owner_id, amount)


if __name__ == "__main__":
    asyncio.run(main())
