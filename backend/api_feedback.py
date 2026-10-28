"""Feedback annotate mode: users file feedback on a page element; admins triage it."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth_middleware import get_current_user
from auth_policy import AuthContext
from database import DB, Feedback

router = APIRouter()

_FEEDBACK_CATEGORIES = frozenset({"bug", "feature", "ux", "other"})
_FEEDBACK_STATUSES = frozenset({"open", "triaged", "in_progress", "done", "wont_do"})
_FEEDBACK_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def _sanitize_element_context(raw: dict) -> dict:
    """Allow only known keys; cap string lengths."""
    allowed = {"sectionId", "selector", "label", "rect", "viewport", "tag"}
    out: dict = {}
    for k in allowed:
        v = raw.get(k)
        if v is None:
            out[k] = None
        elif k in ("sectionId", "selector", "label", "tag"):
            out[k] = str(v)[:240] if v else None
        elif k == "rect" and isinstance(v, dict):
            out[k] = {
                "x": float(v.get("x", 0)),
                "y": float(v.get("y", 0)),
                "w": float(v.get("w", 0)),
                "h": float(v.get("h", 0)),
            }
        elif k == "viewport" and isinstance(v, dict):
            out[k] = {
                "w": int(v.get("w", 0)),
                "h": int(v.get("h", 0)),
                "dpr": float(v.get("dpr", 1)),
            }
        else:
            out[k] = None
    return out


class FeedbackCreate(BaseModel):
    route: str
    element_context: dict
    user_message: str
    category: str


class FeedbackPatch(BaseModel):
    status: Optional[str] = None
    resolution_note: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[str] = None


def _feedback_to_dict(f: Feedback) -> dict:
    import json as _json
    return {
        "id": f.id,
        "created_at": f.created_at.isoformat(),
        "created_by_email": f.email,
        "route": f.route,
        "element_context": _json.loads(f.element_context) if f.element_context else {},
        "user_message": f.user_message,
        "category": f.category,
        "status": f.status,
        "assignee": f.assignee,
        "priority": f.priority,
        "resolution_note": f.resolution_note,
        "triaged_at": f.triaged_at.isoformat() if f.triaged_at else None,
    }


def _feedback_admin_assignees_sync_fallback(auth: AuthContext) -> list[dict]:
    """Local mock-auth fallback: current admin only (mirrors JWT group, not env roster)."""
    from cf_access import ENABLE_MOCK_AUTH

    if ENABLE_MOCK_AUTH and auth.is_admin and auth.email:
        email = auth.email.lower().strip()
        name = email.split("@")[0].replace(".", " ").replace("-", " ").title()
        return [{"email": email, "name": name}]
    return []


async def _feedback_admin_assignees(auth: AuthContext) -> list[dict]:
    """Who a feedback ticket can be assigned to: the administrators.

    Resolved from the directory by group membership. When no directory is
    configured the directory returns nobody, and the caller falls back to the
    requesting administrator so the page still works on a fresh deployment.
    """
    from auth_policy import ADMIN_GROUPS
    from org_directory import get_admin_group_members

    try:
        assignees = await get_admin_group_members(tuple(ADMIN_GROUPS))
    except Exception:
        assignees = []
    if assignees:
        return [{"email": p.email, "name": p.name or p.email} for p in assignees]

    return _feedback_admin_assignees_sync_fallback(auth)


@router.get("/api/admin/feedback/assignees")
async def list_feedback_assignees(auth: AuthContext = Depends(get_current_user)):
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    assignees = await _feedback_admin_assignees(auth)
    if not assignees:
        raise HTTPException(
            status_code=503,
            detail=(
                "No admin assignees found. Configure a directory provider "
                "(DIRECTORY_PROVIDER) so the backend can resolve members of the "
                "groups named in ADMIN_GROUPS."
            ),
        )
    return {"assignees": assignees}


@router.post("/api/feedback", status_code=201)
def create_feedback(body: FeedbackCreate, db: DB, auth: AuthContext = Depends(get_current_user)):
    import json as _json
    from cf_access import ENABLE_MOCK_AUTH

    if body.category not in _FEEDBACK_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Invalid category. Must be one of: {sorted(_FEEDBACK_CATEGORIES)}")

    message = _strip_html(body.user_message.strip())
    if not message:
        raise HTTPException(status_code=422, detail="user_message cannot be empty")
    if len(message) > 4000:
        raise HTTPException(status_code=422, detail="user_message must be <= 4000 characters")

    safe_ctx = _sanitize_element_context(body.element_context)
    route = str(body.route)[:1000]

    if ENABLE_MOCK_AUTH:
        from mock_feedback import create_mock_feedback

        return create_mock_feedback({
            "email": auth.email,
            "route": route,
            "element_context": safe_ctx,
            "user_message": message,
            "category": body.category,
        })

    row = Feedback(
        owner_sub=auth.sub,
        email=auth.email,
        route=route,
        element_context=_json.dumps(safe_ctx),
        user_message=message,
        category=body.category,
        status="open",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _feedback_to_dict(row)


@router.get("/api/feedback")
def list_feedback(db: DB, auth: AuthContext = Depends(get_current_user), limit: int = 200):
    from cf_access import ENABLE_MOCK_AUTH

    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if ENABLE_MOCK_AUTH:
        from mock_feedback import list_mock_feedback

        return list_mock_feedback()[:limit]
    rows = (
        db.query(Feedback)
        .order_by(Feedback.created_at.desc())
        .limit(limit)
        .all()
    )
    return [_feedback_to_dict(r) for r in rows]


@router.patch("/api/feedback/{feedback_id}")
async def update_feedback(
    feedback_id: str, body: FeedbackPatch, db: DB, auth: AuthContext = Depends(get_current_user)
):
    if not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    if body.status is not None and body.status not in _FEEDBACK_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid status. Must be one of: {sorted(_FEEDBACK_STATUSES)}")
    if body.priority is not None and body.priority != "" and body.priority not in _FEEDBACK_PRIORITIES:
        raise HTTPException(status_code=422, detail=f"Invalid priority. Must be one of: {sorted(_FEEDBACK_PRIORITIES)}")

    if body.assignee is not None and body.assignee.strip():
        allowed = {a["email"].lower() for a in await _feedback_admin_assignees(auth)}
        if allowed and body.assignee.strip().lower() not in allowed:
            raise HTTPException(status_code=422, detail="Assignee must be a member of a group named in ADMIN_GROUPS")

    from cf_access import ENABLE_MOCK_AUTH

    if ENABLE_MOCK_AUTH:
        from mock_feedback import patch_mock_feedback

        patch_payload: dict = {}
        if body.status is not None:
            patch_payload["status"] = body.status
        if body.resolution_note is not None:
            patch_payload["resolution_note"] = body.resolution_note.strip()[:2000] or None
        if body.assignee is not None:
            patch_payload["assignee"] = body.assignee.strip()[:320] or None
        if body.priority is not None:
            patch_payload["priority"] = body.priority.strip() or None
        updated = patch_mock_feedback(feedback_id, patch_payload)
        if not updated:
            raise HTTPException(status_code=404, detail="Feedback entry not found")
        return updated

    row = db.get(Feedback, feedback_id)
    if not row:
        raise HTTPException(status_code=404, detail="Feedback entry not found")
    if body.status is not None:
        row.status = body.status
        if body.status in ("triaged", "in_progress", "done") and row.triaged_at is None:
            row.triaged_at = datetime.now(timezone.utc)
    if body.resolution_note is not None:
        row.resolution_note = body.resolution_note.strip()[:2000] or None
    if body.assignee is not None:
        row.assignee = body.assignee.strip()[:320] or None
    if body.priority is not None:
        row.priority = body.priority.strip() or None
    db.commit()
    return _feedback_to_dict(row)
