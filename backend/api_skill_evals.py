"""Skill eval cases and runs.

Endpoints (skill owner, collaborator or admin; the same people who can edit the skill)
─────────
GET    /api/skills/{slug}/evals                     Cases, latest run and gate settings
POST   /api/skills/{slug}/evals/cases               Add a case
PUT    /api/skills/{slug}/evals/cases/{case_id}     Update a case
DELETE /api/skills/{slug}/evals/cases/{case_id}     Delete a case
POST   /api/skills/{slug}/evals/runs                Start a run against the published text
GET    /api/skills/{slug}/evals/runs                Recent runs with per-case results
GET    /api/skills/{slug}/evals/runs/{run_id}       One run
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

import skill_evals
from auth_middleware import get_current_user
from database import DB, SkillEvalCase, SkillEvalRun
from skill_collaborators import is_skill_owner_or_collaborator

logger = logging.getLogger(__name__)
router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$")
# A run left in "running" longer than this is treated as abandoned (process restart).
_STALE_RUN_AFTER = timedelta(minutes=30)
_MAX_RUNS_LISTED = 50

GraderName = Literal["contains", "regex", "exact", "llm_judge"]


class EvalCaseIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    input_prompt: str = Field(..., min_length=1, max_length=8000)
    grader: GraderName
    expected: str = Field(..., min_length=1, max_length=4000)


class EvalCaseOut(BaseModel):
    id: str
    slug: str
    name: str
    input_prompt: str
    grader: str
    expected: str
    created_by_email: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class EvalCaseResultOut(BaseModel):
    case_id: str
    name: str
    grader: str
    status: str
    reason: str
    output_excerpt: str
    duration_ms: int


class EvalRunOut(BaseModel):
    id: str
    slug: str
    content_hash: str
    model: str
    trigger: str
    status: str
    submission_id: Optional[str] = None
    passed: int
    failed: int
    errored: int
    total: int
    skipped: int
    pass_rate: Optional[float] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    results: list[EvalCaseResultOut] = Field(default_factory=list)


class EvalOverviewOut(BaseModel):
    cases: list[EvalCaseOut]
    last_run: Optional[EvalRunOut] = None
    live_content_hash: str
    gate_enabled: bool
    max_cases_per_run: int
    max_cases_per_skill: int


class EvalRunListOut(BaseModel):
    runs: list[EvalRunOut]


def _skills_dir() -> Path:
    from api_skill_publish import _skills_dir as publish_skills_dir

    return publish_skills_dir()


def _open_session():
    # Background runs outlive the request session; tests replace this.
    from database import SessionLocal

    return SessionLocal()


def _skill_path(slug: str) -> Path:
    return _skills_dir() / slug / "skill.md"


def _require_manage(request: Request, db, slug: str):
    """Resolve the skill and check the caller may edit it; returns (auth, slug)."""
    slug = slug.strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=422, detail="Invalid slug.")
    auth = get_current_user(request)
    if not _skill_path(slug).exists():
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found.")
    if not auth.is_admin and not is_skill_owner_or_collaborator(db, slug, auth.sub, auth.email):
        raise HTTPException(status_code=403, detail="Skill management access required.")
    return auth, slug


def _case_to_dict(c: SkillEvalCase) -> dict:
    return {
        "id": c.id,
        "slug": c.slug,
        "name": c.name,
        "input_prompt": c.input_prompt,
        "grader": c.grader,
        "expected": c.expected,
        "created_by_email": c.created_by_email,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _validated(body: EvalCaseIn) -> EvalCaseIn:
    problem = skill_evals.validate_case(body.grader, body.expected)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    return body


def _get_case(db, slug: str, case_id: str) -> SkillEvalCase:
    row = db.query(SkillEvalCase).filter(SkillEvalCase.id == case_id, SkillEvalCase.slug == slug).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Eval case not found.")
    return row


@router.get("/api/skills/{slug}/evals", response_model=EvalOverviewOut)
def get_evals(slug: str, request: Request, db: DB):
    _, slug = _require_manage(request, db, slug)
    live = _skill_path(slug).read_text(encoding="utf-8")
    last = (
        db.query(SkillEvalRun)
        .filter(SkillEvalRun.slug == slug)
        .order_by(SkillEvalRun.started_at.desc())
        .first()
    )
    return {
        "cases": [_case_to_dict(c) for c in skill_evals.list_cases(db, slug)],
        "last_run": skill_evals.run_to_dict(last) if last else None,
        "live_content_hash": skill_evals.content_hash(live),
        "gate_enabled": skill_evals.gate_enabled(),
        "max_cases_per_run": skill_evals.max_cases_per_run(),
        "max_cases_per_skill": skill_evals.MAX_CASES_PER_SKILL,
    }


@router.post("/api/skills/{slug}/evals/cases", response_model=EvalCaseOut, status_code=201)
def create_eval_case(slug: str, body: EvalCaseIn, request: Request, db: DB):
    auth, slug = _require_manage(request, db, slug)
    _validated(body)
    count = db.query(SkillEvalCase).filter(SkillEvalCase.slug == slug).count()
    if count >= skill_evals.MAX_CASES_PER_SKILL:
        raise HTTPException(
            status_code=422,
            detail=f"A skill can have at most {skill_evals.MAX_CASES_PER_SKILL} eval cases.",
        )
    now = datetime.utcnow()
    row = SkillEvalCase(
        slug=slug,
        name=body.name.strip(),
        input_prompt=body.input_prompt,
        grader=body.grader,
        expected=body.expected,
        created_by_sub=auth.sub,
        created_by_email=auth.email,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _case_to_dict(row)


@router.put("/api/skills/{slug}/evals/cases/{case_id}", response_model=EvalCaseOut)
def update_eval_case(slug: str, case_id: str, body: EvalCaseIn, request: Request, db: DB):
    _, slug = _require_manage(request, db, slug)
    _validated(body)
    row = _get_case(db, slug, case_id)
    row.name = body.name.strip()
    row.input_prompt = body.input_prompt
    row.grader = body.grader
    row.expected = body.expected
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _case_to_dict(row)


@router.delete("/api/skills/{slug}/evals/cases/{case_id}")
def delete_eval_case(slug: str, case_id: str, request: Request, db: DB):
    _, slug = _require_manage(request, db, slug)
    row = _get_case(db, slug, case_id)
    db.delete(row)
    db.commit()
    return {"deleted": case_id}


def _execute_run_in_background(run_id: str, slug: str) -> None:
    db = _open_session()
    try:
        run = db.get(SkillEvalRun, run_id)
        if run is None:
            return
        path = _skill_path(slug)
        if not path.exists():
            run.status = "failed"
            run.error = "Skill file no longer exists."
            run.finished_at = datetime.utcnow()
            db.commit()
            return
        content = path.read_text(encoding="utf-8")
        skill_evals.execute_run(db, run, content, references_dir=path.parent / "references")
    except Exception:
        logger.exception("skill_evals: background run %s failed", run_id)
    finally:
        db.close()


@router.post("/api/skills/{slug}/evals/runs", response_model=EvalRunOut, status_code=202)
def start_eval_run(slug: str, request: Request, background_tasks: BackgroundTasks, db: DB):
    auth, slug = _require_manage(request, db, slug)
    if not db.query(SkillEvalCase.id).filter(SkillEvalCase.slug == slug).first():
        raise HTTPException(status_code=422, detail="Add at least one eval case first.")
    in_flight = (
        db.query(SkillEvalRun.id)
        .filter(
            SkillEvalRun.slug == slug,
            SkillEvalRun.status == "running",
            SkillEvalRun.started_at > datetime.utcnow() - _STALE_RUN_AFTER,
        )
        .first()
    )
    if in_flight:
        raise HTTPException(status_code=409, detail="An eval run for this skill is already in progress.")

    content = _skill_path(slug).read_text(encoding="utf-8")
    run = skill_evals.start_run(db, slug, content, trigger="manual", started_by_sub=auth.sub)
    background_tasks.add_task(_execute_run_in_background, run.id, slug)
    return skill_evals.run_to_dict(run)


@router.get("/api/skills/{slug}/evals/runs", response_model=EvalRunListOut)
def list_eval_runs(slug: str, request: Request, db: DB, limit: int = 10):
    _, slug = _require_manage(request, db, slug)
    limit = max(1, min(limit, _MAX_RUNS_LISTED))
    rows = (
        db.query(SkillEvalRun)
        .filter(SkillEvalRun.slug == slug)
        .order_by(SkillEvalRun.started_at.desc())
        .limit(limit)
        .all()
    )
    return {"runs": [skill_evals.run_to_dict(r) for r in rows]}


@router.get("/api/skills/{slug}/evals/runs/{run_id}", response_model=EvalRunOut)
def get_eval_run(slug: str, run_id: str, request: Request, db: DB):
    _, slug = _require_manage(request, db, slug)
    row = db.query(SkillEvalRun).filter(SkillEvalRun.id == run_id, SkillEvalRun.slug == slug).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Eval run not found.")
    return skill_evals.run_to_dict(row)
