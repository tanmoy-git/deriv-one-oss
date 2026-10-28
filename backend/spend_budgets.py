"""Monthly spend budgets for the platform, an agent or a user.

Spend is never counted separately: it is the month-to-date sum of
token_usage_logs, priced per model with model_pricing, the same way
/api/admin/usage prices it. A budget only stores its limit.

Two things read budgets:

* ``check_spend_allowed`` runs before a model call. It blocks the call when an
  enforced budget that applies to it is used up. It fails open: if the check
  itself errors, the call goes ahead and a warning is logged, so a budget bug
  can never take chat down.
* ops_alerts posts a Slack alert when spend passes a budget's soft limit.

Months are calendar months in UTC, matching token_usage_logs.created_at.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import SessionLocal, SpendBudget, TokenUsageLog
from model_pricing import cost_for_model

logger = logging.getLogger(__name__)

SCOPES = ("org", "agent", "user")


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_end(now: datetime) -> datetime:
    start = month_start(now)
    return start + timedelta(days=calendar.monthrange(start.year, start.month)[1])


def normalize_scope_id(scope: str, scope_id: Optional[str]) -> str:
    if scope == "org":
        return ""
    value = (scope_id or "").strip()
    return value.lower() if scope == "user" else value


def spend_since(db: Session, since: datetime, *, scope: str, scope_id: str = "") -> float:
    """US dollars spent since ``since`` for one scope."""
    q = db.query(
        TokenUsageLog.model,
        func.coalesce(func.sum(TokenUsageLog.input_tokens), 0),
        func.coalesce(func.sum(TokenUsageLog.output_tokens), 0),
        func.coalesce(func.sum(TokenUsageLog.cache_read_tokens), 0),
        func.coalesce(func.sum(TokenUsageLog.cache_creation_tokens), 0),
    ).filter(TokenUsageLog.created_at >= since)
    if scope == "agent":
        q = q.filter(TokenUsageLog.agent_id == scope_id)
    elif scope == "user":
        q = q.filter(func.lower(TokenUsageLog.user_email) == scope_id)
    total = 0.0
    for model, inp, out, cr, cc in q.group_by(TokenUsageLog.model):
        total += cost_for_model(model or "", int(inp), int(out), int(cr), int(cc))
    return total


@dataclass(frozen=True)
class BudgetStatus:
    spent_usd: float
    limit_usd: float
    soft_limit_usd: float
    # "ok", "soft" (past the alert threshold) or "exceeded".
    state: str
    # Spend by the end of the month if the month-to-date daily rate holds.
    forecast_usd: float
    # When the limit will be reached at that rate; None if it will not be
    # reached this month or already has been.
    forecast_exceed_at: Optional[datetime]

    def to_dict(self) -> dict:
        return {
            "spent_usd": round(self.spent_usd, 4),
            "limit_usd": self.limit_usd,
            "soft_limit_usd": round(self.soft_limit_usd, 4),
            "used_pct": round(100 * self.spent_usd / self.limit_usd, 1) if self.limit_usd else 0.0,
            "state": self.state,
            "forecast_usd": round(self.forecast_usd, 4),
            "forecast_exceed_at": (
                self.forecast_exceed_at.isoformat() if self.forecast_exceed_at else None
            ),
        }


def evaluate(
    spent_usd: float,
    limit_usd: float,
    soft_limit_pct: int,
    now: datetime,
) -> BudgetStatus:
    """Pure: where a budget stands and where it is heading this month."""
    start, end = month_start(now), month_end(now)
    soft = limit_usd * soft_limit_pct / 100
    if spent_usd >= limit_usd:
        state = "exceeded"
    elif spent_usd >= soft:
        state = "soft"
    else:
        state = "ok"

    # At least an hour of history, so a call at 00:01 on the 1st does not
    # project a month of spend from one minute.
    elapsed = max((now - start).total_seconds(), 3600.0)
    rate_per_second = spent_usd / elapsed
    forecast = rate_per_second * (end - start).total_seconds()

    exceed_at = None
    if state != "exceeded" and rate_per_second > 0 and forecast >= limit_usd:
        exceed_at = start + timedelta(seconds=limit_usd / rate_per_second)
        exceed_at = max(exceed_at, now)
    return BudgetStatus(spent_usd, limit_usd, soft, state, forecast, exceed_at)


def budget_label(db: Session, budget: SpendBudget) -> str:
    """What an admin calls this budget: the agent's name, the email, or the platform."""
    if budget.scope == "org":
        return "Whole platform"
    if budget.scope == "agent":
        from agents import Agent

        name = db.query(Agent.name).filter(Agent.id == budget.scope_id).scalar()
        return name or budget.scope_id
    return budget.scope_id


def budget_status(db: Session, budget: SpendBudget, now: Optional[datetime] = None) -> BudgetStatus:
    now = now or datetime.utcnow()
    spent = spend_since(db, month_start(now), scope=budget.scope, scope_id=budget.scope_id)
    return evaluate(spent, budget.monthly_limit_usd, budget.soft_limit_pct, now)


@dataclass(frozen=True)
class SpendDecision:
    allowed: bool
    message: str = ""
    budget_id: Optional[str] = None


_SCOPE_LABEL = {
    "org": "The platform",
    "agent": "This agent",
    "user": "Your account",
}


def _applicable(db: Session, agent_id: Optional[str], user_email: Optional[str]) -> list[SpendBudget]:
    keys = [("org", "")]
    if agent_id:
        keys.append(("agent", str(agent_id)))
    if user_email:
        keys.append(("user", user_email.strip().lower()))
    budgets = db.query(SpendBudget).filter(SpendBudget.enforce.is_(True)).all()
    wanted = set(keys)
    # Most specific first, so the message names the budget closest to the caller.
    order = {"user": 0, "agent": 1, "org": 2}
    return sorted(
        (b for b in budgets if (b.scope, b.scope_id) in wanted),
        key=lambda b: order.get(b.scope, 3),
    )


def check_spend_allowed(
    *,
    agent_id: Optional[str] = None,
    user_email: Optional[str] = None,
    now: Optional[datetime] = None,
) -> SpendDecision:
    """Whether a model call for this agent and user may go ahead."""
    try:
        now = now or datetime.utcnow()
        with SessionLocal() as db:
            for budget in _applicable(db, agent_id, user_email):
                status = budget_status(db, budget, now)
                if status.state == "exceeded":
                    who = _SCOPE_LABEL.get(budget.scope, "This scope")
                    return SpendDecision(
                        allowed=False,
                        budget_id=budget.id,
                        message=(
                            f"{who} has reached its monthly spend limit of "
                            f"${budget.monthly_limit_usd:,.2f}. "
                            "An admin can raise the limit under Admin > Budgets. "
                            "It resets on the 1st of next month (UTC)."
                        ),
                    )
    except Exception:
        logger.warning("spend_budgets: check failed; allowing the call", exc_info=True)
    return SpendDecision(allowed=True)
