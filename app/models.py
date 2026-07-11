from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.routing import Currency


class AccountClass(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"


class AccountPurpose(StrEnum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    OMNIBUS = "OMNIBUS"


class PostingSide(StrEnum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class JournalEvent(StrEnum):
    OPENING = "OPENING"
    RESERVE = "RESERVE"
    CONSUME = "CONSUME"
    RELEASE = "RELEASE"


class SettlementStatus(StrEnum):
    RESERVED = "RESERVED"
    PAYOUT_IN_PROGRESS = "PAYOUT_IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"


class Base(DeclarativeBase):
    pass


currency_enum = Enum(Currency, name="currency")
account_class_enum = Enum(AccountClass, name="account_class")
account_purpose_enum = Enum(AccountPurpose, name="account_purpose")
posting_side_enum = Enum(PostingSide, name="posting_side")
journal_event_enum = Enum(JournalEvent, name="journal_event")
settlement_status_enum = Enum(SettlementStatus, name="settlement_status")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("owner_id", "currency", "purpose"),
        CheckConstraint("balance >= 0", name="ck_accounts_balance_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    currency: Mapped[Currency] = mapped_column(currency_enum, nullable=False)
    account_class: Mapped[AccountClass] = mapped_column(account_class_enum, nullable=False)
    purpose: Mapped[AccountPurpose] = mapped_column(account_purpose_enum, nullable=False)
    balance: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal("0")
    )


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "idempotency_key",
            name="uq_settlements_owner_idempotency_key",
        ),
        CheckConstraint("amount_usd > 0", name="ck_settlements_amount_positive"),
        CheckConstraint("aggregate_rate > 0", name="ck_settlements_rate_positive"),
        CheckConstraint("quoted_amount > 0", name="ck_settlements_quote_positive"),
        CheckConstraint(
            "provider_operation_id = id",
            name="ck_settlements_provider_operation_matches_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    target_currency: Mapped[Currency] = mapped_column(currency_enum, nullable=False)
    route: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    snapshot_version: Mapped[int] = mapped_column(nullable=False)
    aggregate_rate: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    quoted_amount: Mapped[Decimal] = mapped_column(Numeric(32, 8), nullable=False)
    provider_operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    status: Mapped[SettlementStatus] = mapped_column(settlement_status_enum, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class JournalTransaction(Base):
    __tablename__ = "journal_transactions"
    __table_args__ = (
        CheckConstraint(
            "(event = 'OPENING' AND settlement_id IS NULL) OR "
            "(event <> 'OPENING' AND settlement_id IS NOT NULL)",
            name="ck_journal_transactions_event_settlement",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    settlement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("settlements.id"), nullable=True
    )
    event: Mapped[JournalEvent] = mapped_column(journal_event_enum, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    postings: Mapped[list["Posting"]] = relationship(
        back_populates="journal", cascade="all, delete-orphan"
    )


Index(
    "uq_journal_transactions_settlement_event",
    JournalTransaction.settlement_id,
    JournalTransaction.event,
    unique=True,
    postgresql_where=JournalTransaction.settlement_id.is_not(None),
)


class Posting(Base):
    __tablename__ = "postings"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_postings_amount_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    journal_id: Mapped[UUID] = mapped_column(
        ForeignKey("journal_transactions.id"), nullable=False
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    currency: Mapped[Currency] = mapped_column(currency_enum, nullable=False)
    side: Mapped[PostingSide] = mapped_column(posting_side_enum, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    journal: Mapped[JournalTransaction] = relationship(back_populates="postings")
