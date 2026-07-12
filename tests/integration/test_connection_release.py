import asyncio
from decimal import Decimal

from conftest import TEST_DATABASE_URL, ScriptedPayoutProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.provider import ProviderResult
from app.routing import Currency
from app.schemas import SettlementCreate
from app.seed import seed_demo_accounts
from app.service import SettlementService


async def test_provider_wait_holds_no_database_connection(clean_database, rate_book):
    engine = create_async_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            await seed_demo_accounts(session, "customer", Decimal("100"))
            await session.commit()
        provider = ScriptedPayoutProvider(ProviderResult.PAID, paused=True)
        service = SettlementService(sessions, rate_book, provider)
        create_task = asyncio.create_task(
            service.create(
                "customer",
                "connection-release",
                SettlementCreate(
                    amount_usd=Decimal("10"),
                    target_currency=Currency.PHP,
                ),
            )
        )
        try:
            await asyncio.wait_for(provider.initiate_started.wait(), timeout=2)
            async with sessions() as session:
                assert (
                    await asyncio.wait_for(
                        session.scalar(text("SELECT 1")),
                        timeout=0.5,
                    )
                    == 1
                )
        finally:
            provider.allow_initiate.set()
        await asyncio.wait_for(create_task, timeout=2)
    finally:
        await engine.dispose()
