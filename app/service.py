import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ledger import consume, release, reserve
from app.models import PayoutAttempt, Settlement, SettlementStatus
from app.provider import PayoutProvider, ProviderLookup, ProviderResult
from app.routing import RateBook
from app.schemas import SettlementCreate, SettlementRead, request_fingerprint

IDEMPOTENCY_CONSTRAINT = "uq_settlements_owner_idempotency_key"
logger = logging.getLogger(__name__)


class IdempotencyConflict(Exception):
    pass


class SettlementNotFound(Exception):
    pass


class StaleAttemptToken(Exception):
    pass


@dataclass(frozen=True)
class PayoutClaim:
    settlement: SettlementRead
    attempt_token: int


class SettlementService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        rates: RateBook,
        provider: PayoutProvider,
        *,
        lease_seconds: float = 10,
    ) -> None:
        self._sessions = sessions
        self._rates = rates
        self._provider = provider
        self._lease_seconds = lease_seconds

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

        claim = await self._claim_reserved(settlement_id)
        if claim is None:
            return await self.get(settlement_id)

        try:
            result = await self._provider.initiate(
                claim.settlement.id,
                claim.settlement.amount_usd,
                claim.settlement.target_currency,
                claim.settlement.quoted_amount,
            )
        except Exception:
            result = None
        try:
            return await self._finalize_attempt(
                settlement_id,
                claim.attempt_token,
                result,
            )
        except StaleAttemptToken:
            return await self.get(settlement_id)

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
        claim = await self._claim_for_reconciliation(
            settlement_id,
            respect_schedule=False,
        )
        if claim is None:
            return await self.get(settlement_id)
        return await self._reconcile_claim(claim, propagate_errors=True)

    async def run_reconciliation_once(self, *, limit: int = 50) -> int:
        claims = await self._claim_due_batch(limit=limit)
        await asyncio.gather(
            *(self._reconcile_claim(claim, propagate_errors=False) for claim in claims)
        )
        return len(claims)

    async def reconciliation_loop(
        self,
        stop_event: asyncio.Event,
        *,
        interval_seconds: float = 0.25,
    ) -> None:
        while not stop_event.is_set():
            try:
                await self.run_reconciliation_once()
            except Exception:
                logger.exception("reconciliation pass failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=interval_seconds,
                )
            except TimeoutError:
                continue

    async def _find_by_key(
        self, owner_id: str, idempotency_key: str
    ) -> Settlement | None:
        async with self._sessions() as session:
            settlement: Settlement | None = await session.scalar(
                select(Settlement).where(
                    Settlement.owner_id == owner_id,
                    Settlement.idempotency_key == idempotency_key,
                )
            )
            return settlement

    async def _claim_reserved(self, settlement_id: UUID) -> PayoutClaim | None:
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
                token = 1
                session.add(
                    PayoutAttempt(
                        settlement_id=settlement.id,
                        operation_id=settlement.provider_operation_id,
                        state="SUBMITTING",
                        attempt_token=token,
                        lease_expires_at=self._lease_deadline(),
                    )
                )
                return PayoutClaim(
                    SettlementRead.from_orm_settlement(settlement),
                    token,
                )

    async def _claim_for_reconciliation(
        self,
        settlement_id: UUID,
        *,
        respect_schedule: bool,
    ) -> PayoutClaim | None:
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
                    return None
                attempt = await session.get(
                    PayoutAttempt,
                    settlement_id,
                    with_for_update=True,
                )
                now = datetime.now(UTC)
                if attempt is None:
                    attempt = PayoutAttempt(
                        settlement_id=settlement.id,
                        operation_id=settlement.provider_operation_id,
                        state="RECONCILING",
                        attempt_token=1,
                        lease_expires_at=now,
                    )
                    session.add(attempt)
                    token = 1
                else:
                    if attempt.state == "CLAIMED" and attempt.lease_expires_at > now:
                        return None
                    if respect_schedule and attempt.lease_expires_at > now:
                        return None
                    attempt.attempt_token += 1
                    token = attempt.attempt_token
                attempt.state = "CLAIMED"
                attempt.lease_expires_at = self._lease_deadline()
                return PayoutClaim(
                    SettlementRead.from_orm_settlement(settlement),
                    token,
                )

    async def _claim_due_batch(self, *, limit: int) -> tuple[PayoutClaim, ...]:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        select(Settlement, PayoutAttempt)
                        .join(
                            PayoutAttempt,
                            PayoutAttempt.settlement_id == Settlement.id,
                        )
                        .where(
                            Settlement.status.in_(
                                (
                                    SettlementStatus.PAYOUT_IN_PROGRESS,
                                    SettlementStatus.PENDING_RECONCILIATION,
                                )
                            ),
                            PayoutAttempt.lease_expires_at <= now,
                            PayoutAttempt.state.in_(("SUBMITTING", "RECONCILING")),
                        )
                        .order_by(PayoutAttempt.lease_expires_at, Settlement.id)
                        .limit(limit)
                        .with_for_update(
                            of=(Settlement, PayoutAttempt), skip_locked=True
                        )
                    )
                ).all()
                claims = []
                for settlement, attempt in rows:
                    attempt.attempt_token += 1
                    attempt.state = "CLAIMED"
                    attempt.lease_expires_at = self._lease_deadline()
                    claims.append(
                        PayoutClaim(
                            SettlementRead.from_orm_settlement(settlement),
                            attempt.attempt_token,
                        )
                    )
                return tuple(claims)

    async def _reconcile_claim(
        self,
        claim: PayoutClaim,
        *,
        propagate_errors: bool,
    ) -> SettlementRead:
        try:
            lookup = await self._provider.lookup(claim.settlement.id)
            if lookup == ProviderLookup.PAID:
                result = ProviderResult.PAID
            elif lookup == ProviderLookup.UNPAID:
                result = ProviderResult.UNPAID
            elif lookup == ProviderLookup.NOT_FOUND:
                try:
                    result = await self._provider.initiate(
                        claim.settlement.id,
                        claim.settlement.amount_usd,
                        claim.settlement.target_currency,
                        claim.settlement.quoted_amount,
                    )
                except Exception:
                    result = None
            else:
                result = None
            return await self._finalize_attempt(
                claim.settlement.id,
                claim.attempt_token,
                result,
            )
        except StaleAttemptToken:
            return await self.get(claim.settlement.id)
        except Exception:
            try:
                await self._finalize_attempt(
                    claim.settlement.id,
                    claim.attempt_token,
                    None,
                )
            except StaleAttemptToken:
                pass
            if propagate_errors:
                raise
            return await self.get(claim.settlement.id)

    async def _finalize_attempt(
        self,
        settlement_id: UUID,
        attempt_token: int,
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
                attempt = await session.get(
                    PayoutAttempt,
                    settlement_id,
                    with_for_update=True,
                )
                if attempt is None:
                    raise SettlementNotFound
                if attempt.attempt_token != attempt_token:
                    raise StaleAttemptToken
                if settlement.status in {
                    SettlementStatus.SUCCESS,
                    SettlementStatus.FAILED,
                }:
                    return SettlementRead.from_orm_settlement(settlement)
                if result == ProviderResult.PAID:
                    await consume(session, settlement)
                    settlement.status = SettlementStatus.SUCCESS
                    attempt.state = "COMPLETED"
                    attempt.last_outcome = ProviderLookup.PAID.value
                elif result == ProviderResult.UNPAID:
                    await release(session, settlement)
                    settlement.status = SettlementStatus.FAILED
                    attempt.state = "COMPLETED"
                    attempt.last_outcome = ProviderLookup.UNPAID.value
                else:
                    settlement.status = SettlementStatus.PENDING_RECONCILIATION
                    attempt.state = "RECONCILING"
                    attempt.last_outcome = (
                        ProviderResult.AMBIGUOUS.value
                        if result == ProviderResult.AMBIGUOUS
                        else ProviderLookup.UNKNOWN.value
                    )
                    attempt.lease_expires_at = datetime.now(UTC)
                return SettlementRead.from_orm_settlement(settlement)

    def _lease_deadline(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self._lease_seconds)

    @staticmethod
    def _check_fingerprint(settlement: Settlement, fingerprint: str) -> None:
        if settlement.request_fingerprint != fingerprint:
            raise IdempotencyConflict

    @staticmethod
    def _is_idempotency_violation(error: IntegrityError) -> bool:
        return getattr(
            error.orig, "sqlstate", None
        ) == "23505" and f'constraint "{IDEMPOTENCY_CONSTRAINT}"' in str(error.orig)
