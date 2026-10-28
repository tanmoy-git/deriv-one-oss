"""REST API for Agent Workflows."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agents import Agent, AgentCollaborator
from database import SessionLocal
from tenancy import request_owner_sub
from workflow_engine import (
    advance_run,
    approve_gate,
    default_vertical_slice_graph,
    parse_graph,
)
from workflow_models import (
    WorkflowDefinition,
    WorkflowRun,
    WorkflowRunEvent,
    _json_dumps,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ensure_agent_access(db: Session, agent_id: str, owner_sub: str, email: Optional[str]) -> Agent:
    agent = db.get(Agent, agent_id)
    if not agent or agent.status == "decommissioned":
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.owner_sub == owner_sub:
        return agent
    if email:
        collab = (
            db.query(AgentCollaborator)
            .filter(
                AgentCollaborator.agent_id == agent_id,
                AgentCollaborator.user_email == email.strip().lower(),
            )
            .first()
        )
        if collab:
            return agent
    raise HTTPException(status_code=403, detail="Not allowed")


def _request_email(request: Request) -> Optional[str]:
    """Email of the verified caller, set by the auth middleware."""
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "email", None) or None


def _defn_to_dict(d: WorkflowDefinition) -> dict[str, Any]:
    return {
        "id": d.id,
        "agent_id": d.agent_id,
        "slug": d.slug,
        "name": d.name,
        "version": d.version,
        "status": d.status,
        "graph": json.loads(d.graph_json or "{}"),
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        "published_at": d.published_at.isoformat() if d.published_at else None,
    }


def _run_to_dict(run: WorkflowRun, *, include_context: bool = True) -> dict[str, Any]:
    ctx = run.context if include_context else {}
    if not include_context:
        ctx = {k: v for k, v in run.context.items() if k in ("client_id", "summary")}
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "definition_id": run.definition_id,
        "definition_version": run.definition_version,
        "status": run.status,
        "current_step": run.current_step,
        "context": ctx,
        "pending_gate": run.pending_gate,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }


class CreateWorkflowBody(BaseModel):
    slug: str = Field(..., min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=200)
    graph: Optional[dict[str, Any]] = None
    intake_channel: str = ""
    delegate_user_id: str = ""


class PublishWorkflowBody(BaseModel):
    pass


class GateDecisionBody(BaseModel):
    approved: bool = True
    message: str = ""


class SeedVerticalSliceBody(BaseModel):
    intake_channel: str = ""
    delegate_user_id: str = ""
    name: str = "Intake → Investigate → Approve → Resolve"


async def _noop_llm(*_a: Any, **_k: Any) -> str:
    return "{}"


@router.get("/api/agents/{agent_id}/workflows")
def list_workflows(
    agent_id: str,
    request: Request,
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    email = _request_email(request)
    _ensure_agent_access(db, agent_id, owner_sub, email)
    rows = (
        db.query(WorkflowDefinition)
        .filter(WorkflowDefinition.agent_id == agent_id)
        .order_by(WorkflowDefinition.updated_at.desc())
        .all()
    )
    return [_defn_to_dict(r) for r in rows]


@router.post("/api/agents/{agent_id}/workflows")
def create_workflow(
    agent_id: str,
    body: CreateWorkflowBody,
    request: Request,
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    email = _request_email(request)
    _ensure_agent_access(db, agent_id, owner_sub, email)
    graph = body.graph
    if graph is None:
        graph = default_vertical_slice_graph(
            intake_channel=body.intake_channel.strip(),
            delegate_user_id=body.delegate_user_id.strip(),
        )
    row = WorkflowDefinition(
        agent_id=agent_id,
        slug=body.slug.strip(),
        name=body.name.strip(),
        graph_json=_json_dumps(graph),
        status="draft",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _defn_to_dict(row)


@router.post("/api/agents/{agent_id}/workflows/seed-vertical-slice")
def seed_vertical_slice(
    agent_id: str,
    body: SeedVerticalSliceBody,
    request: Request,
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    email = _request_email(request)
    _ensure_agent_access(db, agent_id, owner_sub, email)
    existing = (
        db.query(WorkflowDefinition)
        .filter(
            WorkflowDefinition.agent_id == agent_id,
            WorkflowDefinition.slug == "slice-intake-delegate-hitl-resolve",
        )
        .first()
    )
    graph = default_vertical_slice_graph(
        intake_channel=body.intake_channel.strip(),
        delegate_user_id=body.delegate_user_id.strip(),
    )
    if existing:
        existing.graph_json = _json_dumps(graph)
        existing.name = body.name
        existing.updated_at = _utcnow()
        db.commit()
        db.refresh(existing)
        return _defn_to_dict(existing)
    row = WorkflowDefinition(
        agent_id=agent_id,
        slug="slice-intake-delegate-hitl-resolve",
        name=body.name,
        graph_json=_json_dumps(graph),
        status="draft",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _defn_to_dict(row)


@router.post("/api/agents/{agent_id}/workflows/{workflow_id}/publish")
def publish_workflow(
    agent_id: str,
    workflow_id: str,
    request: Request,
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    email = _request_email(request)
    _ensure_agent_access(db, agent_id, owner_sub, email)
    row = db.get(WorkflowDefinition, workflow_id)
    if not row or row.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if not parse_graph(row)["nodes"]:
        raise HTTPException(status_code=400, detail="Workflow graph is empty")
    (
        db.query(WorkflowDefinition)
        .filter(
            WorkflowDefinition.agent_id == agent_id,
            WorkflowDefinition.status == "published",
            WorkflowDefinition.id != workflow_id,
        )
        .update({"status": "draft"})
    )
    row.status = "published"
    row.published_at = _utcnow()
    row.updated_at = _utcnow()
    db.commit()
    return _defn_to_dict(row)


@router.get("/api/agents/{agent_id}/workflow-runs")
def list_agent_workflow_runs(
    agent_id: str,
    request: Request,
    status: Optional[str] = Query(None),
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    email = _request_email(request)
    _ensure_agent_access(db, agent_id, owner_sub, email)
    q = db.query(WorkflowRun).filter(WorkflowRun.agent_id == agent_id)
    if status:
        q = q.filter(WorkflowRun.status == status)
    rows = q.order_by(WorkflowRun.updated_at.desc()).limit(100).all()
    return [_run_to_dict(r, include_context=False) for r in rows]


@router.get("/api/workflow-runs/pending-gates")
def list_pending_gates(
    request: Request,
    agent_id: Optional[str] = Query(None),
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    q = db.query(WorkflowRun).filter(WorkflowRun.status == "pending_hitl")
    if agent_id:
        email = _request_email(request)
        _ensure_agent_access(db, agent_id, owner_sub, email)
        q = q.filter(WorkflowRun.agent_id == agent_id)
    else:
        agent_ids = [
            row[0]
            for row in db.query(Agent.id).filter(Agent.owner_sub == owner_sub).all()
        ]
        if not agent_ids:
            return []
        q = q.filter(WorkflowRun.agent_id.in_(agent_ids))
    rows = q.order_by(WorkflowRun.updated_at.desc()).limit(50).all()
    out = []
    for run in rows:
        gate = run.pending_gate
        if not gate:
            continue
        out.append(
            {
                "run_id": run.id,
                "agent_id": run.agent_id,
                "current_step": run.current_step,
                "gate": gate,
                "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            }
        )
    return out


@router.get("/api/workflow-runs/{run_id}")
def get_workflow_run(
    run_id: str,
    request: Request,
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    run = db.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    email = _request_email(request)
    _ensure_agent_access(db, run.agent_id, owner_sub, email)
    events = (
        db.query(WorkflowRunEvent)
        .filter(WorkflowRunEvent.run_id == run_id)
        .order_by(WorkflowRunEvent.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        **_run_to_dict(run),
        "timeline": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "step_id": e.step_id,
                "payload": json.loads(e.payload_json or "{}"),
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


@router.post("/api/workflow-runs/{run_id}/gates/{gate_id}/approve")
async def approve_workflow_gate(
    run_id: str,
    gate_id: str,
    body: GateDecisionBody,
    request: Request,
    owner_sub: str = Depends(request_owner_sub),
    db: Session = Depends(_db),
):
    run = db.get(WorkflowRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    email = _request_email(request)
    agent = _ensure_agent_access(db, run.agent_id, owner_sub, email)
    gate = run.pending_gate
    if not gate or gate.get("id") != gate_id:
        raise HTTPException(status_code=404, detail="Gate not found")
    msg = body.message.strip() or str(gate.get("payload") or "")
    try:
        approve_gate(run_id, gate_id, approved_message=msg, approved=body.approved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from api_slack_events import workflow_runtime_hooks  # noqa: WPS433

    hooks = workflow_runtime_hooks(agent)
    edge = "approved" if body.approved else "rejected"
    asyncio.create_task(
        advance_run(
            agent,
            run_id,
            llm_json=hooks["llm_json"],
            post_slack=hooks["post_slack"],
            hitl_edge=edge,
        )
    )
    return {"ok": True, "decision": edge}
