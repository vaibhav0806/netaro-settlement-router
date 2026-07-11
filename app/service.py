from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ledger import consume, release, reserve
from app.models import Settlement, SettlementStatus
from app.provider import PayoutProvider, PayoutTimeout, ProviderLookup, ProviderResult
from app.routing import RateBook
from app.schemas import SettlementCreate, SettlementRead, request_fingerprint


class IdempotencyConflict(Exception):
    pass


class SettlementNotFound(Exception):
    pass


class SettlementService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rates: RateBook,
        provider: PayoutProvider,
    ) -> None:
        self._sessions = sessions
        self._rates = rates
        self._provider = provider

    async def create(
        self,
        owner_id: str,
        idempotency_key: str,
        command: SettlementCreate,
    ) -> SettlementRead:
        fingerprint = request_fingerprint(owner_id, command)
        existing = await self._find_by_key(owner_id, idempotency_key)
        if existing is not None:
            self._check_fingerprint(existing, fingerprint)
            if existing.status != SettlementStatus.RESERVED:
                return SettlementRead.from_orm_settlement(existing)
            settlement_id = existing.id
        else:
            quote = self._rates.quote(command.target_currency)
            settlement_id = uuid4()
            settlement = Settlement(
                id=settlement_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                amount_usd=command.amount_usd,
                target_currency=command.target_currency,
                route=[
                    {
                        "source": hop.source.value,
                        "target": hop.target.value,
                        "lp": hop.lp,
                        "rate": str(hop.rate),
                    }
                    for hop in quote.hops
                ],
                snapshot_version=quote.snapshot_version,
                aggregate_rate=quote.aggregate_rate,
                quoted_amount=command.amount_usd * quote.aggregate_rate,
                provider_operation_id=settlement_id,
                status=SettlementStatus.RESERVED,
            )
            try:
                async with self._sessions() as session:
                    async with session.begin():
                        session.add(settlement)
                        await session.flush()
                        await reserve(session, settlement)
            except IntegrityError as error:
                if not self._is_idempotency_violation(error):
                    raise
                winner = await self._find_by_key(owner_id, idempotency_key)
                assert winner is not None
                self._check_fingerprint(winner, fingerprint)
                if winner.status != SettlementStatus.RESERVED:
                    return SettlementRead.from_orm_settlement(winner)
                settlement_id = winner.id

        claimed = await self._claim_reserved(settlement_id)
        if claimed is None:
            return await self.get(settlement_id)

        try:
            result = await self._provider.initiate(
                claimed.id,
                claimed.amount_usd,
                claimed.target_currency,
                claimed.quoted_amount,
            )
        except PayoutTimeout:
            result = None
        return await self._finalize_initiation(settlement_id, result)

    async def get(self, settlement_id: UUID) -> SettlementRead:
        async with self._sessions() as session:
            settlement = await session.get(Settlement, settlement_id)
            if settlement is None:
                raise SettlementNotFound
            return SettlementRead.from_orm_settlement(settlement)

    async def reconcile(self, settlement_id: UUID) -> SettlementRead:
        current = await self.get(settlement_id)
        if current.status not in {
            SettlementStatus.PAYOUT_IN_PROGRESS,
            SettlementStatus.PENDING_RECONCILIATION,
        }:
            return current

        lookup = await self._provider.lookup(settlement_id)
        if lookup == ProviderLookup.PAID:
            return await self._finalize_reconciliation(
                settlement_id, ProviderResult.PAID
            )
        if lookup == ProviderLookup.UNPAID:
            return await self._finalize_reconciliation(
                settlement_id, ProviderResult.UNPAID
            )
        if lookup != ProviderLookup.NOT_FOUND:
            return await self.get(settlement_id)

        recovery = await self._load_in_progress_for_recovery(settlement_id)
        if recovery is None:
            return await self.get(settlement_id)
        try:
            result = await self._provider.initiate(
                recovery.id,
                recovery.amount_usd,
                recovery.target_currency,
                recovery.quoted_amount,
            )
        except PayoutTimeout:
            result = None
        return await self._finalize_initiation(settlement_id, result)

    async def _find_by_key(
        self, owner_id: str, idempotency_key: str
    ) -> Settlement | None:
        async with self._sessions() as session:
            return await session.scalar(
                select(Settlement).where(
                    Settlement.owner_id == owner_id,
                    Settlement.idempotency_key == idempotency_key,
                )
            )

    async def _claim_reserved(self, settlement_id: UUID) -> SettlementRead | None:
        async with self._sessions() as session:
            async with session.begin():
                settlement = await session.scalar(
                    select(Settlement)
                    .where(Settlement.id == settlement_id)
                    .with_for_update()
                )
                if settlement is None:
                    raise SettlementNotFound
                if settlement.status != SettlementStatus.RESERVED:
                    return None
                settlement.status = SettlementStatus.PAYOUT_IN_PROGRESS
                return SettlementRead.from_orm_settlement(settlement)

    async def _finalize_initiation(
        self,
        settlement_id: UUID,
        result: ProviderResult | None,
    ) -> SettlementRead:
        async with self._sessions() as session:
            async with session.begin():
                settlement = await session.scalar(
                    select(Settlement)
                    .where(Settlement.id == settlement_id)
                    .with_for_update()
                )
                if settlement is None:
                    raise SettlementNotFound
                if settlement.status != SettlementStatus.PAYOUT_IN_PROGRESS:
                    return SettlementRead.from_orm_settlement(settlement)
                if result == ProviderResult.PAID:
                    await consume(session, settlement)
                    settlement.status = SettlementStatus.SUCCESS
                elif result == ProviderResult.UNPAID:
                    await release(session, settlement)
                    settlement.status = SettlementStatus.FAILED
                else:
                    settlement.status = SettlementStatus.PENDING_RECONCILIATION
                return SettlementRead.from_orm_settlement(settlement)

    async def _finalize_reconciliation(
        self,
        settlement_id: UUID,
        result: ProviderResult,
    ) -> SettlementRead:
        async with self._sessions() as session:
            async with session.begin():
                settlement = await session.scalar(
                    select(Settlement)
                    .where(Settlement.id == settlement_id)
                    .with_for_update()
                )
                if settlement is None:
                    raise SettlementNotFound
                if settlement.status not in {
                    SettlementStatus.PAYOUT_IN_PROGRESS,
                    SettlementStatus.PENDING_RECONCILIATION,
                }:
                    return SettlementRead.from_orm_settlement(settlement)
                if result == ProviderResult.PAID:
                    await consume(session, settlement)
                    settlement.status = SettlementStatus.SUCCESS
                else:
                    await release(session, settlement)
                    settlement.status = SettlementStatus.FAILED
                return SettlementRead.from_orm_settlement(settlement)

    async def _load_in_progress_for_recovery(
        self, settlement_id: UUID
    ) -> SettlementRead | None:
        async with self._sessions() as session:
            async with session.begin():
                settlement = await session.scalar(
                    select(Settlement)
                    .where(Settlement.id == settlement_id)
                    .with_for_update()
                )
                if settlement is None:
                    raise SettlementNotFound
                if settlement.status != SettlementStatus.PAYOUT_IN_PROGRESS:
                    return None
                return SettlementRead.from_orm_settlement(settlement)

    @staticmethod
    def _check_fingerprint(settlement: Settlement, fingerprint: str) -> None:
        if settlement.request_fingerprint != fingerprint:
            raise IdempotencyConflict

    @staticmethod
    def _is_idempotency_violation(error: IntegrityError) -> bool:
        return (
            getattr(error.orig, "sqlstate", None) == "23505"
            and "settlements_owner_id_idempotency_key_key" in str(error.orig)
        )
