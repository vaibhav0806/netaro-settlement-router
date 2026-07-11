import asyncio
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.routing import Currency


class ProviderResult(StrEnum):
    PAID = "PAID"
    UNPAID = "UNPAID"


class ProviderLookup(StrEnum):
    PAID = "PAID"
    UNPAID = "UNPAID"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


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
        timeout_seconds: float = 5,
        outcomes: tuple[ProviderLookup, ...] | None = None,
        timeout_lookup: ProviderLookup = ProviderLookup.UNKNOWN,
        load_mode: bool = False,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._outcomes = outcomes
        self._timeout_lookup = timeout_lookup
        self._load_mode = load_mode
        self._operations: dict[UUID, tuple[ProviderLookup, bool]] = {}
        self._operation_count = 0
        self._lock = asyncio.Lock()

    async def initiate(
        self,
        settlement_id: UUID,
        amount_usd: Decimal,
        target_currency: Currency,
        quoted_amount: Decimal,
    ) -> ProviderResult:
        async with self._lock:
            stored = self._operations.get(settlement_id)
            if stored is None:
                stored = self._next_outcome()
                self._operations[settlement_id] = stored
                self._operation_count += 1
        operation, times_out = stored
        if times_out:
            await asyncio.sleep(self._timeout_seconds)
            raise PayoutTimeout
        if operation == ProviderLookup.PAID:
            return ProviderResult.PAID
        return ProviderResult.UNPAID

    async def lookup(self, settlement_id: UUID) -> ProviderLookup:
        async with self._lock:
            stored = self._operations.get(settlement_id)
            return stored[0] if stored is not None else ProviderLookup.NOT_FOUND

    def _next_outcome(self) -> tuple[ProviderLookup, bool]:
        if self._load_mode:
            if self._operation_count < 700:
                return ProviderLookup.PAID, False
            if self._operation_count < 850:
                return ProviderLookup.UNPAID, False
            if self._operation_count < 1000:
                return self._timeout_lookup, True
            raise RuntimeError("load provider supports exactly 1000 unique operations")
        if self._outcomes:
            outcome = self._outcomes[self._operation_count % len(self._outcomes)]
            if outcome == ProviderLookup.UNKNOWN:
                return self._timeout_lookup, True
            return outcome, False
        position = self._operation_count % 20
        if position < 14:
            return ProviderLookup.PAID, False
        if position < 17:
            return ProviderLookup.UNPAID, False
        return self._timeout_lookup, True
