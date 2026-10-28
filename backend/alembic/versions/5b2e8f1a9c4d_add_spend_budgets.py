"""Add spend_budgets table for monthly spend limits.

The baseline revision runs Base.metadata.create_all, so on a fresh database
the table already exists by the time this runs; skip it then.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "5b2e8f1a9c4d"
down_revision = "4f1d9c2e7a3b"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "spend_budgets" in _table_names():
        return
    op.create_table(
        "spend_budgets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(length=10), nullable=False),
        sa.Column("scope_id", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("monthly_limit_usd", sa.Float(), nullable=False),
        sa.Column("soft_limit_pct", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("enforce", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scope", "scope_id", name="uq_spend_budgets_scope"),
    )


def downgrade() -> None:
    if "spend_budgets" in _table_names():
        op.drop_table("spend_budgets")
