"""Add agent_interaction_traces for per-interaction model-call timelines.

The baseline revision runs Base.metadata.create_all, so on a fresh database
the table already exists by the time this runs; skip it then.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "7c3d9e2f4a1b"
down_revision = "4f1d9c2e7a3b"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "agent_interaction_traces" in _table_names():
        return
    op.create_table(
        "agent_interaction_traces",
        sa.Column("interaction_id", sa.String(), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_calls", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_agent_interaction_traces_agent_id", "agent_interaction_traces", ["agent_id"]
    )


def downgrade() -> None:
    if "agent_interaction_traces" in _table_names():
        op.drop_table("agent_interaction_traces")
