"""Admin endpoints for monthly spend budgets.

GET    /api/admin/budgets        every budget with its month-to-date status
PUT    /api/admin/budgets        create, or update the budget for that scope
PATCH  /api/admin/budgets/{id}   change the limit, alert threshold or enforcement
DELETE /api/admin/budgets/{id}
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agents import Agent
from auth_middleware import get_current_user
from database import DB, SpendBudget
from model_pricing import refresh_pricing_cache
from spend_budgets import (
    budget_label,
    budget_status,
    month_end,
    month_start,
    normalize_scope_id,
)

router = APIRouter()


def _admin(request: Request):
    ctx = get_current_user(request)
    if not ctx.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return ctx


class BudgetUpsert(BaseModel):
    scope: Literal["org", "agent", "user"]
    scope_id: Optional[str] = None
    monthly_limit_usd: float = Field(gt=0, le=10_000_000)
    soft_limit_pct: int = Field(default=80, ge=1, le=100)
    enforce: bool = True


class BudgetPatch(BaseModel):
    monthly_limit_usd: Optional[float] = Field(default=None, gt=0, le=10_000_000)
    soft_limit_pct: Optional[int] = Field(default=None, ge=1, le=100)
    enforce: Optional[bool] = None


def _to_dict(db, budget: SpendBudget, now: datetime) -> dict[str, Any]:
    return {
        "id": budget.id,
        "scope": budget.scope,
        "scope_id": budget.scope_id,
        "label": budget_label(db, budget),
        "monthly_limit_usd": budget.monthly_limit_usd,
        "soft_limit_pct": budget.soft_limit_pct,
        "enforce": budget.enforce,
        "updated_at": budget.updated_at.isoformat(),
        "status": budget_status(db, budget, now).to_dict(),
    }


@router.get("/api/admin/budgets")
async def list_budgets(db: DB, _: Any = Depends(_admin)) -> dict[str, Any]:
    await refresh_pricing_cache()
    now = datetime.utcnow()
    order = {"org": 0, "agent": 1, "user": 2}
    budgets = sorted(
        db.query(SpendBudget).all(),
        key=lambda b: (order.get(b.scope, 3), b.scope_id),
    )
    return {
        "period_start": month_start(now).isoformat(),
        "period_end": month_end(now).isoformat(),
        "budgets": [_to_dict(db, b, now) for b in budgets],
    }


@router.put("/api/admin/budgets")
def upsert_budget(body: BudgetUpsert, db: DB, ctx: Any = Depends(_admin)) -> dict[str, Any]:
    scope_id = normalize_scope_id(body.scope, body.scope_id)
    if body.scope != "org" and not scope_id:
        raise HTTPException(status_code=422, detail=f"scope_id is required for a {body.scope} budget.")
    if body.scope == "agent" and db.get(Agent, scope_id) is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    if body.scope == "user" and "@" not in scope_id:
        raise HTTPException(status_code=422, detail="A user budget needs the user's email address.")

    now = datetime.utcnow()
    budget = (
        db.query(SpendBudget)
        .filter(SpendBudget.scope == body.scope, SpendBudget.scope_id == scope_id)
        .first()
    )
    if budget is None:
        budget = SpendBudget(scope=body.scope, scope_id=scope_id, created_by=ctx.email, created_at=now)
        db.add(budget)
    budget.monthly_limit_usd = body.monthly_limit_usd
    budget.soft_limit_pct = body.soft_limit_pct
    budget.enforce = body.enforce
    budget.updated_at = now
    db.commit()
    db.refresh(budget)
    return _to_dict(db, budget, now)


@router.patch("/api/admin/budgets/{budget_id}")
def patch_budget(budget_id: str, body: BudgetPatch, db: DB, _: Any = Depends(_admin)) -> dict[str, Any]:
    budget = db.get(SpendBudget, budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="Budget not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(budget, field, value)
    now = datetime.utcnow()
    budget.updated_at = now
    db.commit()
    db.refresh(budget)
    return _to_dict(db, budget, now)


@router.delete("/api/admin/budgets/{budget_id}", status_code=204)
def delete_budget(budget_id: str, db: DB, _: Any = Depends(_admin)) -> None:
    budget = db.get(SpendBudget, budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="Budget not found.")
    db.delete(budget)
    db.commit()
