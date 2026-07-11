from collections.abc import AsyncIterator
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import Base
from app.seed import seed_demo_accounts


TEST_DATABASE_URL = "postgresql+asyncpg://netaro:netaro@localhost:5432/netaro_test"
ADMIN_DATABASE_URL = "postgresql+asyncpg://netaro:netaro@localhost:5432/netaro"


@pytest_asyncio.fixture(scope="session")
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    admin_engine = create_async_engine(
        ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    async with admin_engine.connect() as connection:
        await connection.execute(text("DROP DATABASE IF EXISTS netaro_test WITH (FORCE)"))
        await connection.execute(text("CREATE DATABASE netaro_test"))
    await admin_engine.dispose()

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def clean_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    async with session_factory() as session:
        await session.execute(
            text(
                "TRUNCATE TABLE postings, journal_transactions, settlements, accounts "
                "RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    yield


@pytest_asyncio.fixture
async def session(
    clean_database,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as value:
        yield value


@pytest_asyncio.fixture
async def seeded_accounts(
    clean_database,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await seed_demo_accounts(session, "customer", Decimal("1000"))
        await session.commit()
