"""Create settlement ledger schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


currency = postgresql.ENUM(
    "USD", "USDC", "EUR", "PHP", "AED", name="currency", create_type=False
)
account_class = postgresql.ENUM(
    "ASSET", "LIABILITY", name="account_class", create_type=False
)
account_purpose = postgresql.ENUM(
    "AVAILABLE", "RESERVED", "OMNIBUS", name="account_purpose", create_type=False
)
posting_side = postgresql.ENUM(
    "DEBIT", "CREDIT", name="posting_side", create_type=False
)
journal_event = postgresql.ENUM(
    "OPENING",
    "RESERVE",
    "CONSUME",
    "RELEASE",
    name="journal_event",
    create_type=False,
)
settlement_status = postgresql.ENUM(
    "RESERVED",
    "PAYOUT_IN_PROGRESS",
    "SUCCESS",
    "FAILED",
    "PENDING_RECONCILIATION",
    name="settlement_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (
        currency,
        account_class,
        account_purpose,
        posting_side,
        journal_event,
        settlement_status,
    ):
        enum.create(bind, checkfirst=False)

    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("currency", currency, nullable=False),
        sa.Column("account_class", account_class, nullable=False),
        sa.Column("purpose", account_purpose, nullable=False),
        sa.Column("balance", sa.Numeric(24, 8), nullable=False),
        sa.CheckConstraint("balance >= 0", name="ck_accounts_balance_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "currency", "purpose"),
    )
    op.create_table(
        "settlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("request_fingerprint", sa.String(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(24, 8), nullable=False),
        sa.Column("target_currency", currency, nullable=False),
        sa.Column("route", sa.JSON(), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_rate", sa.Numeric(24, 8), nullable=False),
        sa.Column("quoted_amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("provider_operation_id", sa.Uuid(), nullable=False),
        sa.Column("status", settlement_status, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("aggregate_rate > 0", name="ck_settlements_rate_positive"),
        sa.CheckConstraint("amount_usd > 0", name="ck_settlements_amount_positive"),
        sa.CheckConstraint(
            "provider_operation_id = id",
            name="ck_settlements_provider_operation_matches_id",
        ),
        sa.CheckConstraint("quoted_amount > 0", name="ck_settlements_quote_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
    )
    op.create_table(
        "journal_transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("settlement_id", sa.Uuid(), nullable=True),
        sa.Column("event", journal_event, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(event = 'OPENING' AND settlement_id IS NULL) OR "
            "(event <> 'OPENING' AND settlement_id IS NOT NULL)",
            name="ck_journal_transactions_event_settlement",
        ),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_journal_transactions_settlement_event",
        "journal_transactions",
        ["settlement_id", "event"],
        unique=True,
        postgresql_where=sa.text("settlement_id IS NOT NULL"),
    )
    op.create_table(
        "postings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("journal_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("currency", currency, nullable=False),
        sa.Column("side", posting_side, nullable=False),
        sa.Column("amount", sa.Numeric(24, 8), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_postings_amount_positive"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["journal_id"], ["journal_transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("postings")
    op.drop_index(
        "uq_journal_transactions_settlement_event",
        table_name="journal_transactions",
        postgresql_where=sa.text("settlement_id IS NOT NULL"),
    )
    op.drop_table("journal_transactions")
    op.drop_table("settlements")
    op.drop_table("accounts")
    bind = op.get_bind()
    for enum in (
        settlement_status,
        journal_event,
        posting_side,
        account_purpose,
        account_class,
        currency,
    ):
        enum.drop(bind, checkfirst=False)
