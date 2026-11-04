"""GET /api/admin/agent-map -- the agent -> skill -> system/secret/channel graph.

Admin only. The graph is built by ``agent_map.build_agent_map``; this module
just loads the rows. Skill secrets are queried by column (slug, env_key) so the
vault reference is never even loaded.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from agent_channel_configs import AgentChannelConfig
from agent_map import build_agent_map
from agents import Agent
from auth_middleware import get_current_user
from database import DB
from skill_submission_models import SkillSecret
from skill_system_links_model import SkillSystemLink

logger = logging.getLogger(__name__)
router = APIRouter()


class AgentMapNode(BaseModel):
    id: str
    type: Literal["agent", "skill", "system", "secret", "channel"]
    label: str
    status: Optional[str] = None
    risk_level: Optional[str] = None
    detail: Optional[str] = None


class AgentMapEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: Literal["uses_skill", "touches_system", "needs_secret", "in_channel", "holds_credential"]
    direction: Optional[str] = None


class AgentMapResponse(BaseModel):
    nodes: list[AgentMapNode]
    edges: list[AgentMapEdge]
    focus: Optional[str] = None
    totals: dict[str, int]


@router.get("/api/admin/agent-map", response_model=AgentMapResponse)
def admin_agent_map(
    request: Request,
    db: DB,
    focus: Optional[str] = Query(
        None,
        description="Node id such as system:Salesforce or secret:SALESFORCE_TOKEN; "
        "returns only what is upstream or downstream of it.",
        max_length=300,
    ),
):
    ctx = get_current_user(request)
    if not ctx.is_admin:
        raise HTTPException(403, "Admin access required.")

    import system_catalog
    from skills_acl import all_skill_acls, skill_display_name

    try:
        catalog = system_catalog.get_system_map()
    except Exception:
        logger.exception("agent_map: system catalogue lookup failed")
        catalog = {}

    acls = all_skill_acls()
    try:
        graph = build_agent_map(
            agents=db.query(Agent).all(),
            channel_configs=db.query(AgentChannelConfig).all(),
            system_links=db.query(SkillSystemLink).all(),
            skill_secret_names=db.query(SkillSecret.slug, SkillSecret.env_key).all(),
            skill_names={slug: skill_display_name(acl, slug) for slug, acl in acls.items()},
            skill_risk={slug: acl.risk_level for slug, acl in acls.items()},
            catalog=catalog,
            focus=focus,
        )
    except KeyError:
        raise HTTPException(404, "No node with that id in the agent map.")
    return graph
