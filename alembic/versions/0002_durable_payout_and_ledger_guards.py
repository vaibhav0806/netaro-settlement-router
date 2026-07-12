"""Add durable payout state and immutable balanced journal guards."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_durable_guards"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mock_provider_operations",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "ordinal",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("submission_outcome", sa.String(length=32), nullable=False),
        sa.Column("authoritative_outcome", sa.String(length=32), nullable=False),
        sa.Column("provider_reference", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("ordinal"),
    )
    op.create_table(
        "payout_attempts",
        sa.Column("settlement_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_outcome", sa.String(length=32), nullable=True),
        sa.Column("provider_reference", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "attempt_token > 0",
            name="ck_payout_attempt_token_positive",
        ),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.PrimaryKeyConstraint("settlement_id"),
        sa.UniqueConstraint("operation_id"),
    )
    op.execute(
        """
        INSERT INTO payout_attempts (
          settlement_id, operation_id, state, attempt_token, lease_expires_at
        )
        SELECT id, provider_operation_id,
               CASE status::text
                 WHEN 'PAYOUT_IN_PROGRESS' THEN 'SUBMITTING'
                 ELSE 'RECONCILING'
               END,
               1, now()
        FROM settlements
        WHERE status::text IN ('PAYOUT_IN_PROGRESS', 'PENDING_RECONCILIATION')
        ON CONFLICT (settlement_id) DO NOTHING
        """
    )
    op.add_column(
        "journal_transactions",
        sa.Column(
            "is_posted",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )
    op.execute("UPDATE journal_transactions SET is_posted = true")
    op.alter_column("journal_transactions", "is_posted", nullable=False)

    statements = (
        """
        CREATE FUNCTION guard_posting_mutation() RETURNS trigger AS $$
        DECLARE parent_posted boolean;
        BEGIN
          SELECT is_posted INTO parent_posted
          FROM journal_transactions
          WHERE id = COALESCE(NEW.journal_id, OLD.journal_id);
          IF parent_posted THEN
            RAISE EXCEPTION 'posted journal postings are immutable'
              USING ERRCODE = '23514';
          END IF;
          RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE TRIGGER posting_mutation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON postings
        FOR EACH ROW EXECUTE FUNCTION guard_posting_mutation()
        """,
        """
        CREATE FUNCTION guard_journal_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.is_posted THEN
              RAISE EXCEPTION 'posted journals are immutable'
                USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
          END IF;
          IF OLD.is_posted THEN
            RAISE EXCEPTION 'posted journals are immutable'
              USING ERRCODE = '23514';
          END IF;
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.settlement_id IS DISTINCT FROM OLD.settlement_id
             OR NEW.event IS DISTINCT FROM OLD.event
             OR NEW.created_at IS DISTINCT FROM OLD.created_at
             OR NEW.is_posted = false THEN
            RAISE EXCEPTION 'only journal posting transition is allowed'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE TRIGGER journal_mutation_guard
        BEFORE UPDATE OR DELETE ON journal_transactions
        FOR EACH ROW EXECUTE FUNCTION guard_journal_mutation()
        """,
        """
        CREATE FUNCTION check_posted_journal() RETURNS trigger AS $$
        DECLARE current_posted boolean;
        DECLARE posting_count integer;
        BEGIN
          SELECT is_posted INTO current_posted
          FROM journal_transactions WHERE id = NEW.id;
          IF NOT current_posted THEN
            RAISE EXCEPTION 'journal must be posted before commit'
              USING ERRCODE = '23514';
          END IF;
          SELECT COUNT(*) INTO posting_count
          FROM postings WHERE journal_id = NEW.id;
          IF posting_count < 2 THEN
            RAISE EXCEPTION 'journal requires at least two postings'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT currency FROM postings WHERE journal_id = NEW.id
            GROUP BY currency
            HAVING COALESCE(SUM(amount) FILTER (WHERE side = 'DEBIT'), 0)
                <> COALESCE(SUM(amount) FILTER (WHERE side = 'CREDIT'), 0)
          ) THEN
            RAISE EXCEPTION 'journal is unbalanced'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        CREATE CONSTRAINT TRIGGER posted_journal_check
        AFTER INSERT ON journal_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_posted_journal()
        """,
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    statements = (
        "DROP TRIGGER IF EXISTS posted_journal_check ON journal_transactions",
        "DROP FUNCTION IF EXISTS check_posted_journal()",
        "DROP TRIGGER IF EXISTS journal_mutation_guard ON journal_transactions",
        "DROP FUNCTION IF EXISTS guard_journal_mutation()",
        "DROP TRIGGER IF EXISTS posting_mutation_guard ON postings",
        "DROP FUNCTION IF EXISTS guard_posting_mutation()",
    )
    for statement in statements:
        op.execute(statement)
    op.drop_column("journal_transactions", "is_posted")
    op.drop_table("payout_attempts")
    op.drop_table("mock_provider_operations")
