"""re-apply the flow owner-scoping migration that shared revision id z1a2b3c4d5e6

Two revision files used to declare revision z1a2b3c4d5e6 with the same parent:
browser task scheduling and flow owner scoping. Alembic keeps only one script
per id, so each database ran whichever file won the load order and silently
skipped the other. Alembic also saw two heads, so `alembic upgrade head`
failed outright.

The browser file is now the only z1a2b3c4d5e6. Its tables are dropped later by
aa1b2c3d4e5f, so skipping it left nothing behind. Skipping the flow file did:
workflow_definitions and workflow_runs can be missing owner_sub and kind,
which api_flows.py needs. This revision runs the flow body again. Every step
checks the live schema first, so it is a no-op where the flow file already ran.

Revision ID: 4f1d9c2e7a3b
Revises: cc3d4e5f6a7b
Create Date: 2026-10-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic_shared import flow_owner_scoping

revision: str = "4f1d9c2e7a3b"
down_revision: Union[str, None] = "cc3d4e5f6a7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    flow_owner_scoping.upgrade()


def downgrade() -> None:
    # Left in place: the flow columns belong to the schema at every revision
    # since z1a2b3c4d5e6, so dropping them here would break api_flows.py on a
    # database that is only being stepped back one revision.
    pass
