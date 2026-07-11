import hashlib
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from app.models import Settlement, SettlementStatus
from app.routing import Currency


class SettlementCreate(BaseModel):
    amount_usd: Annotated[
        Decimal, Field(gt=0, max_digits=24, decimal_places=8)
    ]
    target_currency: Currency


def request_fingerprint(owner_id: str, command: SettlementCreate) -> str:
    normalized = (
        f"{owner_id}|{command.amount_usd.normalize()}|{command.target_currency.value}"
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


class RouteHopRead(BaseModel):
    source: Currency
    target: Currency
    lp: str
    rate: Decimal


class SettlementRead(BaseModel):
    id: UUID
    status: SettlementStatus
    amount_usd: Decimal
    target_currency: Currency
    quoted_amount: Decimal
    aggregate_rate: Decimal
    snapshot_version: int
    route: tuple[RouteHopRead, ...]

    @classmethod
    def from_orm_settlement(cls, settlement: Settlement) -> "SettlementRead":
        return cls(
            id=settlement.id,
            status=settlement.status,
            amount_usd=settlement.amount_usd,
            target_currency=settlement.target_currency,
            quoted_amount=settlement.quoted_amount,
            aggregate_rate=settlement.aggregate_rate,
            snapshot_version=settlement.snapshot_version,
            route=tuple(RouteHopRead.model_validate(hop) for hop in settlement.route),
        )
