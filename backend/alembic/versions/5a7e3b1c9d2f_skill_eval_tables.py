"""add skill eval case and run tables

Revision ID: 5a7e3b1c9d2f
Revises: 4f1d9c2e7a3b
Create Date: 2026-10-28 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5a7e3b1c9d2f"
down_revision: Union[str, None] = "4f1d9c2e7a3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {ix["name"] for ix in insp.get_indexes(table)}


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if name not in _index_names(table):
        op.create_index(name, table, columns, unique=False)


def upgrade() -> None:
    # The baseline revision runs create_all, so on a fresh database these
    # tables can already exist by the time this revision runs.
    names = _table_names()

    if "skill_eval_cases" not in names:
        op.create_table(
            "skill_eval_cases",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("input_prompt", sa.Text(), nullable=False),
            sa.Column("grader", sa.String(length=16), nullable=False),
            sa.Column("expected", sa.Text(), nullable=False),
            sa.Column("created_by_sub", sa.String(), nullable=True),
            sa.Column("created_by_email", sa.String(length=320), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "skill_eval_runs" not in names:
        op.create_table(
            "skill_eval_runs",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("slug", sa.String(length=64), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=256), nullable=False),
            sa.Column("trigger", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("submission_id", sa.String(), nullable=True),
            sa.Column("started_by_sub", sa.String(), nullable=True),
            sa.Column("passed", sa.Integer(), nullable=False),
            sa.Column("failed", sa.Integer(), nullable=False),
            sa.Column("errored", sa.Integer(), nullable=False),
            sa.Column("total", sa.Integer(), nullable=False),
            sa.Column("skipped", sa.Integer(), nullable=False),
            sa.Column("results_json", sa.Text(), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )

    _create_index_if_missing("ix_skill_eval_cases_slug", "skill_eval_cases", ["slug"])
    _create_index_if_missing("ix_skill_eval_runs_slug", "skill_eval_runs", ["slug"])
    _create_index_if_missing("ix_skill_eval_runs_content_hash", "skill_eval_runs", ["content_hash"])


def downgrade() -> None:
    names = _table_names()
    for table in ("skill_eval_runs", "skill_eval_cases"):
        if table in names:
            op.drop_table(table)
