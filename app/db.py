import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://netaro:netaro@localhost:5432/netaro",
)

engine = create_async_engine(DATABASE_URL)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


class DatabaseUnavailable(Exception):
    pass


async def check_database(
    sessions: async_sessionmaker[AsyncSession] = SessionFactory,
) -> None:
    try:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        raise DatabaseUnavailable from error


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session
