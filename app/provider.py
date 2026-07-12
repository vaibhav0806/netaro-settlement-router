import asyncio
import hashlib
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import MockProviderOperation
from app.routing import Currency


class ProviderResult(StrEnum):
    PAID = "PAID"
    UNPAID = "UNPAID"
    AMBIGUOUS = "AMBIGUOUS_503"


class ProviderLookup(StrEnum):
    PAID = "PAID"
    UNPAID = "UNPAID"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    PROCESSING = "PROCESSING"
    UNAVAILABLE = "UNAVAILABLE"


class PayoutTimeout(TimeoutError):
    pass


class PayoutProvider(Protocol):
    async def initiate(
        self,
        settlement_id: UUID,
        amount_usd: Decimal,
        target_currency: Currency,
        quoted_amount: Decimal,
    ) -> ProviderResult: ...

    async def lookup(self, settlement_id: UUID) -> ProviderLookup: ...


class MockPayoutProvider:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        timeout_seconds: float = 5,
        outcomes: tuple[ProviderLookup, ...] | None = None,
        timeout_lookup: ProviderLookup = ProviderLookup.UNKNOWN,
        load_mode: bool = False,
    ) -> None:
        self._sessions = sessions
        self._timeout_seconds = timeout_seconds
        self._outcomes = outcomes
        self._timeout_lookup = timeout_lookup
        self._load_mode = load_mode
        self._operations: dict[
            UUID, tuple[ProviderResult | None, ProviderLookup, bool]
        ] = {}
        self._operation_count = 0
        self._lock = asyncio.Lock()

    async def initiate(
        self,
        settlement_id: UUID,
        amount_usd: Decimal,
        target_currency: Currency,
        quoted_amount: Decimal,
    ) -> ProviderResult:
        if self._sessions is not None:
            submission, operation, times_out = await self._durable_outcome(
                settlement_id
            )
        else:
            async with self._lock:
                stored = self._operations.get(settlement_id)
                if stored is None:
                    stored = self._next_outcome()
                    self._operations[settlement_id] = stored
                    self._operation_count += 1
            submission, operation, times_out = stored
        if times_out:
            await asyncio.sleep(self._timeout_seconds)
            raise PayoutTimeout
        assert submission is not None
        return submission

    async def lookup(self, settlement_id: UUID) -> ProviderLookup:
        if self._sessions is not None:
            async with self._sessions() as session:
                operation = await session.get(MockProviderOperation, settlement_id)
                if operation is None:
                    return ProviderLookup.NOT_FOUND
                return ProviderLookup(operation.authoritative_outcome)
        async with self._lock:
            stored = self._operations.get(settlement_id)
            return stored[1] if stored is not None else ProviderLookup.NOT_FOUND

    def _next_outcome(
        self,
    ) -> tuple[ProviderResult | None, ProviderLookup, bool]:
        return self._outcome_for_position(self._operation_count)

    def _outcome_for_position(
        self, position: int
    ) -> tuple[ProviderResult | None, ProviderLookup, bool]:
        if self._load_mode:
            if position < 700:
                return ProviderResult.PAID, ProviderLookup.PAID, False
            if position < 850:
                return (
                    ProviderResult.AMBIGUOUS,
                    self._ambiguous_authority(position),
                    False,
                )
            if position < 1000:
                return None, self._ambiguous_authority(position), True
            raise RuntimeError("load provider supports exactly 1000 unique operations")
        if self._outcomes:
            outcome = self._outcomes[position % len(self._outcomes)]
            if outcome == ProviderLookup.UNKNOWN:
                return None, self._timeout_lookup, True
            result = (
                ProviderResult.PAID
                if outcome == ProviderLookup.PAID
                else ProviderResult.UNPAID
            )
            return result, outcome, False
        position %= 20
        if position < 14:
            return ProviderResult.PAID, ProviderLookup.PAID, False
        if position < 17:
            return (
                ProviderResult.AMBIGUOUS,
                self._ambiguous_authority(position),
                False,
            )
        return None, self._ambiguous_authority(position), True

    @staticmethod
    def _ambiguous_authority(position: int) -> ProviderLookup:
        return ProviderLookup.PAID if position % 2 == 0 else ProviderLookup.UNPAID

    async def _durable_outcome(
        self, settlement_id: UUID
    ) -> tuple[ProviderResult | None, ProviderLookup, bool]:
        assert self._sessions is not None
        reference = (
            "provider-" + hashlib.sha256(str(settlement_id).encode()).hexdigest()[:24]
        )
        async with self._sessions() as session:
            async with session.begin():
                ordinal = await session.scalar(
                    insert(MockProviderOperation)
                    .values(
                        operation_id=settlement_id,
                        submission_outcome="ALLOCATING",
                        authoritative_outcome=ProviderLookup.UNKNOWN.value,
                        provider_reference=reference,
                    )
                    .on_conflict_do_nothing(
                        index_elements=(MockProviderOperation.operation_id,)
                    )
                    .returning(MockProviderOperation.ordinal)
                )
                if ordinal is not None:
                    submission, authority, times_out = self._outcome_for_position(
                        ordinal - 1
                    )
                    if times_out:
                        submission_value = "TIMEOUT"
                    else:
                        assert submission is not None
                        submission_value = submission.value
                    await session.execute(
                        update(MockProviderOperation)
                        .where(MockProviderOperation.operation_id == settlement_id)
                        .values(
                            submission_outcome=submission_value,
                            authoritative_outcome=authority.value,
                        )
                    )
                operation = await session.scalar(
                    select(MockProviderOperation).where(
                        MockProviderOperation.operation_id == settlement_id
                    )
                )
                assert operation is not None
                authority = ProviderLookup(operation.authoritative_outcome)
                if operation.submission_outcome == "TIMEOUT":
                    return None, authority, True
                return ProviderResult(operation.submission_outcome), authority, False
