"""Body of the flow owner-scoping migration.

This used to be a second revision file that reused the id z1a2b3c4d5e6.
Alembic keys revisions by id, so only one of the two files ever ran on a
given database. It now lives here and is applied by the reconcile revision
4f1d9c2e7a3b. Every step checks the live schema first, so it is safe to
run on a database where it already ran.

downgrade() is kept for reference and for anyone stepping a database back
by hand; the reconcile revision itself does not call it.
"""
import sqlalchemy as sa
from alembic import op


_OWNER_LEGACY_SENTINEL = "__LEGACY_UNASSIGNED__"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if "workflow_definitions" in existing:
        cols = {c["name"] for c in insp.get_columns("workflow_definitions")}
        if "owner_sub" not in cols:
            op.add_column("workflow_definitions", sa.Column("owner_sub", sa.String(), nullable=True))
            op.execute(
                sa.text(
                    "UPDATE workflow_definitions SET owner_sub = :s WHERE owner_sub IS NULL"
                ).bindparams(s=_OWNER_LEGACY_SENTINEL)
            )
            op.alter_column("workflow_definitions", "owner_sub", nullable=False)
            op.create_index(
                "ix_workflow_definitions_owner_sub", "workflow_definitions", ["owner_sub"]
            )
        if "kind" not in cols:
            op.add_column(
                "workflow_definitions",
                sa.Column("kind", sa.String(length=16), nullable=True),
            )
            op.execute("UPDATE workflow_definitions SET kind = 'playbook' WHERE kind IS NULL")
            op.alter_column("workflow_definitions", "kind", nullable=False)
        op.alter_column("workflow_definitions", "agent_id", existing_type=sa.String(), nullable=True)

    if "workflow_runs" in existing:
        cols = {c["name"] for c in insp.get_columns("workflow_runs")}
        if "owner_sub" not in cols:
            op.add_column("workflow_runs", sa.Column("owner_sub", sa.String(), nullable=True))
            op.execute(
                sa.text(
                    "UPDATE workflow_runs SET owner_sub = :s WHERE owner_sub IS NULL"
                ).bindparams(s=_OWNER_LEGACY_SENTINEL)
            )
            op.alter_column("workflow_runs", "owner_sub", nullable=False)
            op.create_index("ix_workflow_runs_owner_sub", "workflow_runs", ["owner_sub"])
        op.alter_column("workflow_runs", "agent_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing = set(insp.get_table_names())

    if "workflow_runs" in existing:
        cols = {c["name"] for c in insp.get_columns("workflow_runs")}
        if "owner_sub" in cols:
            op.drop_index("ix_workflow_runs_owner_sub", table_name="workflow_runs")
            op.drop_column("workflow_runs", "owner_sub")

    if "workflow_definitions" in existing:
        cols = {c["name"] for c in insp.get_columns("workflow_definitions")}
        if "kind" in cols:
            op.drop_column("workflow_definitions", "kind")
        if "owner_sub" in cols:
            op.drop_index("ix_workflow_definitions_owner_sub", table_name="workflow_definitions")
            op.drop_column("workflow_definitions", "owner_sub")
