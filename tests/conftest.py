import asyncio
import os
from collections import deque
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import Base
from app.provider import PayoutProvider, PayoutTimeout, ProviderLookup, ProviderResult
from app.routing import Currency, Edge, RateBook
from app.seed import seed_demo_accounts


POSTGRES_HOST_PORT = os.getenv("POSTGRES_HOST_PORT", "5432")
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://netaro:netaro@localhost:{POSTGRES_HOST_PORT}/netaro_test"
)
ADMIN_DATABASE_URL = (
    f"postgresql+asyncpg://netaro:netaro@localhost:{POSTGRES_HOST_PORT}/netaro"
)


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


class ScriptedPayoutProvider(PayoutProvider):
    def __init__(
        self,
        initial_result: ProviderResult | PayoutTimeout,
        lookup_results: tuple[ProviderLookup, ...] = (),
        *,
        paused: bool = False,
    ) -> None:
        self.initial_result = initial_result
        self.lookup_results = deque(lookup_results)
        self.initiate_calls: list[UUID] = []
        self.lookup_calls: list[UUID] = []
        self.effective_operations: dict[
            UUID, ProviderResult | PayoutTimeout
        ] = {}
        self.initiate_started = asyncio.Event()
        self.allow_initiate = asyncio.Event()
        if not paused:
            self.allow_initiate.set()
        self._lock = asyncio.Lock()
        self._initiation_condition = asyncio.Condition(self._lock)

    async def initiate(
        self,
        settlement_id: UUID,
        amount_usd: Decimal,
        target_currency: Currency,
        quoted_amount: Decimal,
    ) -> ProviderResult:
        async with self._initiation_condition:
            self.initiate_calls.append(settlement_id)
            outcome = self.effective_operations.setdefault(
                settlement_id, self.initial_result
            )
            self.initiate_started.set()
            self._initiation_condition.notify_all()
        await self.allow_initiate.wait()
        if isinstance(outcome, PayoutTimeout):
            raise PayoutTimeout(str(outcome))
        return outcome

    async def lookup(self, settlement_id: UUID) -> ProviderLookup:
        async with self._lock:
            self.lookup_calls.append(settlement_id)
            if self.lookup_results:
                return self.lookup_results.popleft()
        return ProviderLookup.UNKNOWN

    async def wait_for_initiations(self, count: int) -> None:
        async with self._initiation_condition:
            await self._initiation_condition.wait_for(
                lambda: len(self.initiate_calls) >= count
            )


@pytest_asyncio.fixture
async def rate_book() -> RateBook:
    rates = RateBook()
    rates.publish(
        (Edge(Currency.USD, Currency.PHP, "LP_TEST", Decimal("55")),),
        version=7,
    )
    return rates
