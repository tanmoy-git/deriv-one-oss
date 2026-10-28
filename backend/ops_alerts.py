"""Infrastructure ops alerting.

Periodic checks (run from the scheduler singleton) evaluate sandbox pool depth,
event backlog, per-agent cost spikes, and deployment health. Alerts are
debounced and posted to a private Slack channel via the platform-agent bot token.

Fails silent on collector errors — never spams about its own bugs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from database import OpsAlertState, SessionLocal, TokenUsageLog
from secret_vault import SecretVaultError, get_vault
from sqlalchemy import text

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

OPS_ALERT_ENABLED: bool = os.environ.get("OPS_ALERT_ENABLED", "1") not in (
    "0",
    "false",
    "False",
    "",
)
OPS_ALERT_SANDBOX_MIN_AVAILABLE: int = max(
    0, int(os.environ.get("OPS_ALERT_SANDBOX_MIN_AVAILABLE", "5"))
)
OPS_ALERT_EVENT_BACKLOG_MAX: int = max(
    1, int(os.environ.get("OPS_ALERT_EVENT_BACKLOG_MAX", "500"))
)
OPS_ALERT_COST_SPIKE_MULTIPLIER: float = max(
    1.0, float(os.environ.get("OPS_ALERT_COST_SPIKE_MULTIPLIER", "3.0"))
)
OPS_ALERT_COST_SPIKE_FLOOR_USD: float = max(
    0.0, float(os.environ.get("OPS_ALERT_COST_SPIKE_FLOOR_USD", "5.0"))
)
OPS_ALERT_DEBOUNCE_POLLS: int = max(
    1, int(os.environ.get("OPS_ALERT_DEBOUNCE_POLLS", "3"))
)
OPS_ALERT_RENOTIFY_MINUTES: int = max(
    1, int(os.environ.get("OPS_ALERT_RENOTIFY_MINUTES", "30"))
)
OPS_ALERTS_SLACK_CHANNEL_ID: str = os.environ.get("OPS_ALERTS_SLACK_CHANNEL_ID", "").strip()
OPS_ALERT_APP_BASE_URL: str = os.environ.get(
    "APP_BASE_URL", "http://localhost:8080"
).rstrip("/")

_NAMESPACE = os.environ.get("SANDBOX_NAMESPACE", "agent-sandbox")
_PRIMARY_SANDBOX_POOL = os.environ.get("OPS_ALERT_SANDBOX_POOL", "python-executor-pool")

_MONITORED_DEPLOYMENTS: tuple[str, ...] = (
    "backend",
    "frontend",
    "slack-consumer",
    "scheduler",
    "sandbox-router",
)


@dataclass(frozen=True)
class AlertCheck:
    """One evaluatable alert signal."""

    key: str
    title: str
    breaching: bool
    summary: str
    detail_url: str | None = None


# ── Pure helpers (unit-tested) ────────────────────────────────────────────────

def should_send_first_alert(breach_streak: int, *, debounce_polls: int) -> bool:
    return breach_streak >= debounce_polls


def should_renotify(
    last_notified_at: datetime | None,
    *,
    renotify_minutes: int,
    now: datetime | None = None,
) -> bool:
    if last_notified_at is None:
        return True
    now = now or datetime.utcnow()
    elapsed = (now - last_notified_at).total_seconds()
    return elapsed >= renotify_minutes * 60


def is_cost_spike(
    today_usd: float,
    avg_daily_usd: float,
    *,
    multiplier: float,
    floor_usd: float,
) -> bool:
    if today_usd < floor_usd:
        return False
    if avg_daily_usd <= 0:
        return False
    return today_usd >= multiplier * avg_daily_usd


# ── Collectors ────────────────────────────────────────────────────────────────

def _collect_event_backlog_check() -> AlertCheck | None:
    """Alert when inbound events are queueing up faster than they drain.

    Measured from the application's own dedup table rather than from queue
    metrics: an event is claimed when a worker starts it and marked done when
    it finishes, so a growing population of old in-progress claims means
    dispatch is falling behind. That works whatever the queue is, and needs no
    metrics API.
    """
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=5)
        with SessionLocal() as db:
            backlog = int(
                db.execute(
                    text(
                        "SELECT COUNT(*) FROM slack_event_dedup "
                        "WHERE status = 'in_progress' AND seen_at < :cutoff"
                    ),
                    {"cutoff": cutoff},
                ).scalar()
                or 0
            )
    except Exception:
        logger.warning("ops_alerts: event backlog check failed", exc_info=True)
        return None

    return AlertCheck(
        key="event_backlog",
        title="Inbound event backlog high",
        breaching=backlog > OPS_ALERT_EVENT_BACKLOG_MAX,
        summary=(
            f"{backlog} event(s) claimed more than 5 minutes ago and still "
            f"unfinished (max {OPS_ALERT_EVENT_BACKLOG_MAX})"
        ),
        detail_url=f"{OPS_ALERT_APP_BASE_URL}/admin/infra",
    )


def _collect_agent_cost_checks() -> list[AlertCheck]:
    from agents import Agent
    from api_usage import _row_cost

    checks: list[AlertCheck] = []
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    history_start = today_start - timedelta(days=7)

    try:
        with SessionLocal() as db:
            rows = (
                db.query(TokenUsageLog)
                .filter(TokenUsageLog.created_at >= history_start)
                .filter(TokenUsageLog.agent_id.isnot(None))
                .all()
            )
            agent_ids = {r.agent_id for r in rows if r.agent_id}
            agent_names: dict[str, str] = {}
            if agent_ids:
                for aid, name in db.query(Agent.id, Agent.name).filter(
                    Agent.id.in_(list(agent_ids))
                ):
                    agent_names[aid] = name

            by_agent_day: dict[str, dict[str, float]] = {}
            for row in rows:
                aid = row.agent_id
                if not aid:
                    continue
                day = row.created_at.strftime("%Y-%m-%d")
                by_agent_day.setdefault(aid, {})
                by_agent_day[aid][day] = by_agent_day[aid].get(day, 0.0) + _row_cost(row)

            today_key = today_start.strftime("%Y-%m-%d")
            for aid, day_costs in by_agent_day.items():
                today_usd = day_costs.get(today_key, 0.0)
                prior_days = [
                    cost
                    for day, cost in day_costs.items()
                    if day != today_key
                ]
                avg_daily = sum(prior_days) / 7.0
                if not is_cost_spike(
                    today_usd,
                    avg_daily,
                    multiplier=OPS_ALERT_COST_SPIKE_MULTIPLIER,
                    floor_usd=OPS_ALERT_COST_SPIKE_FLOOR_USD,
                ):
                    continue
                name = agent_names.get(aid, aid[:8])
                checks.append(
                    AlertCheck(
                        key=f"agent_cost:{aid}",
                        title=f"Agent cost spike: {name}",
                        breaching=True,
                        summary=(
                            f"`{name}` spent ${today_usd:.2f} today "
                            f"(7d avg ${avg_daily:.2f}/day, "
                            f"threshold {OPS_ALERT_COST_SPIKE_MULTIPLIER:.1f}x)"
                        ),
                        detail_url=f"{OPS_ALERT_APP_BASE_URL}/admin/usage",
                    )
                )
    except Exception:
        logger.warning("ops_alerts: agent cost check failed", exc_info=True)
    return checks


def _collect_budget_checks() -> list[AlertCheck]:
    """One check per spend budget: breaching once spend passes its soft limit.

    Every budget is reported, breaching or not, so an alert resolves when an
    admin raises the limit or a new month starts.
    """
    from database import SpendBudget
    from spend_budgets import budget_label, budget_status

    checks: list[AlertCheck] = []
    now = datetime.utcnow()
    try:
        with SessionLocal() as db:
            for budget in db.query(SpendBudget).all():
                status = budget_status(db, budget, now)
                label = budget_label(db, budget)
                summary = (
                    f"`{label}` has spent ${status.spent_usd:,.2f} of its "
                    f"${budget.monthly_limit_usd:,.2f} monthly budget"
                )
                if status.state == "exceeded":
                    summary += " and is over the limit"
                    if budget.enforce:
                        summary += "; model calls are blocked until it is raised"
                elif status.forecast_exceed_at is not None:
                    summary += (
                        f"; at this rate it reaches the limit on "
                        f"{status.forecast_exceed_at:%d %b}"
                    )
                checks.append(
                    AlertCheck(
                        key=f"budget:{budget.id}",
                        title=f"Spend budget: {label}",
                        breaching=status.state != "ok",
                        summary=summary,
                        detail_url=f"{OPS_ALERT_APP_BASE_URL}/admin",
                    )
                )
    except Exception:
        logger.warning("ops_alerts: budget check failed", exc_info=True)
    return checks


async def _collect_service_down_checks() -> list[AlertCheck]:
    checks: list[AlertCheck] = []
    try:
        # Optional: only meaningful when running under an orchestrator. Absent
        # elsewhere, and the check simply reports nothing.
        from kubernetes_asyncio import client as k8s_client, config as k8s_config  # type: ignore

        try:
            k8s_config.load_incluster_config()
        except Exception:
            await k8s_config.load_kube_config()

        async with k8s_client.ApiClient() as api:
            apps = k8s_client.AppsV1Api(api)
            for dep_name in _MONITORED_DEPLOYMENTS:
                try:
                    dep = await apps.read_namespaced_deployment(
                        name=dep_name, namespace=_NAMESPACE
                    )
                except Exception:
                    logger.warning(
                        "ops_alerts: could not read deployment %s", dep_name, exc_info=True
                    )
                    continue
                desired = int(dep.spec.replicas or 0)
                available = int(dep.status.available_replicas or 0)
                breaching = desired > 0 and available == 0
                checks.append(
                    AlertCheck(
                        key=f"service_down:{dep_name}",
                        title=f"Service down: {dep_name}",
                        breaching=breaching,
                        summary=(
                            f"Deployment `{dep_name}`: {available}/{desired} "
                            f"available replicas"
                        ),
                        detail_url=f"{OPS_ALERT_APP_BASE_URL}/admin/infra",
                    )
                )
    except Exception:
        logger.warning("ops_alerts: service health check failed", exc_info=True)
    return checks


async def _collect_all_checks() -> list[AlertCheck]:
    checks: list[AlertCheck] = []
    backlog = _collect_event_backlog_check()
    if backlog is not None:
        checks.append(backlog)
    checks.extend(_collect_agent_cost_checks())
    checks.extend(_collect_budget_checks())
    checks.extend(await _collect_service_down_checks())
    return checks


# ── Slack delivery ────────────────────────────────────────────────────────────

def _get_platform_bot_token() -> str | None:
    from agent_channel_configs import get_platform_agent

    agent = get_platform_agent()
    if not agent or not agent.xoxb_secret_ref:
        return None
    try:
        return get_vault().read(agent.xoxb_secret_ref)
    except SecretVaultError:
        return None


def _format_alert_message(
    check: AlertCheck,
    *,
    resolved: bool,
    first_breach_at: datetime | None,
) -> str:
    if resolved:
        header = f":white_check_mark: *Resolved: {check.title}*"
    else:
        header = f":rotating_light: *Platform Alert: {check.title}*"
    lines = [header, check.summary]
    if first_breach_at and not resolved:
        started = first_breach_at.replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"Started breaching: {started}")
    if check.detail_url:
        lines.append(f"Investigate: {check.detail_url}")
    return "\n".join(lines)


def _post_slack_message(
    token: str,
    channel: str,
    text: str,
    *,
    thread_ts: str | None = None,
) -> str | None:
    try:
        from slack_sdk import WebClient

        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = WebClient(token=token).chat_postMessage(**kwargs)
        return str(resp.get("ts") or "") or None
    except Exception:
        logger.warning("ops_alerts: Slack post failed", exc_info=True)
        return None


# ── State machine ─────────────────────────────────────────────────────────────

def _get_or_create_state(db, alert_key: str) -> OpsAlertState:
    state = db.query(OpsAlertState).filter(OpsAlertState.alert_key == alert_key).first()
    if state is None:
        state = OpsAlertState(alert_key=alert_key, status="ok", breach_streak=0)
        db.add(state)
        db.flush()
    return state


def process_alert_check(
    db,
    check: AlertCheck,
    *,
    channel_id: str,
    bot_token: str | None,
    debounce_polls: int = OPS_ALERT_DEBOUNCE_POLLS,
    renotify_minutes: int = OPS_ALERT_RENOTIFY_MINUTES,
) -> None:
    state = _get_or_create_state(db, check.key)
    now = datetime.utcnow()

    if check.breaching:
        state.breach_streak = (state.breach_streak or 0) + 1
        state.last_value = (check.summary or "")[:500]
        if state.status != "breaching":
            state.first_breach_at = now
        state.status = "breaching"

        if not should_send_first_alert(state.breach_streak, debounce_polls=debounce_polls):
            state.updated_at = now
            return

        if state.last_notified_at and not should_renotify(
            state.last_notified_at, renotify_minutes=renotify_minutes, now=now
        ):
            state.updated_at = now
            return

        if channel_id and bot_token:
            text = _format_alert_message(
                check, resolved=False, first_breach_at=state.first_breach_at
            )
            thread_ts = state.slack_thread_ts
            if state.last_notified_at and thread_ts:
                _post_slack_message(bot_token, channel_id, text, thread_ts=thread_ts)
            else:
                ts = _post_slack_message(bot_token, channel_id, text)
                if ts:
                    state.slack_thread_ts = ts
            state.last_notified_at = now
        else:
            logger.info(
                "ops_alerts: would alert key=%s (no channel/token configured): %s",
                check.key,
                check.summary,
            )
    else:
        if state.status == "breaching":
            if channel_id and bot_token and state.slack_thread_ts:
                text = _format_alert_message(
                    check, resolved=True, first_breach_at=state.first_breach_at
                )
                _post_slack_message(
                    bot_token, channel_id, text, thread_ts=state.slack_thread_ts
                )
            state.status = "ok"
            state.breach_streak = 0
            state.first_breach_at = None
            state.slack_thread_ts = None
        else:
            state.breach_streak = 0

    state.updated_at = now


def run_ops_alert_checks() -> None:
    """Entry point for the scheduler periodic job."""
    if not OPS_ALERT_ENABLED:
        return

    try:
        checks = asyncio.run(_collect_all_checks())
    except Exception:
        logger.warning("ops_alerts: check collection failed", exc_info=True)
        return

    channel_id = OPS_ALERTS_SLACK_CHANNEL_ID
    bot_token = _get_platform_bot_token() if channel_id else None
    if channel_id and not bot_token:
        logger.warning(
            "ops_alerts: OPS_ALERTS_SLACK_CHANNEL_ID set but platform bot token unavailable"
        )

    try:
        with SessionLocal() as db:
            for check in checks:
                process_alert_check(
                    db,
                    check,
                    channel_id=channel_id,
                    bot_token=bot_token,
                )
            db.commit()
    except Exception:
        logger.warning("ops_alerts: state processing failed", exc_info=True)
