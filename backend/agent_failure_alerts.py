"""Slack alerts when agent LLM replies fall back to generic failure messages.

Posts to a dedicated channel and @tags the agent creator (creator_email).
Debounced per agent + failure kind via OpsAlertState.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime

from agents import Agent
from database import OpsAlertState, SessionLocal
from ops_alerts import should_renotify

logger = logging.getLogger(__name__)

AGENT_FORMAT_FAILURE_MESSAGE = (
    "I completed the investigation but could not format the final response. "
    "Please ask me for a concise summary."
)
AGENT_TIMEOUT_FAILURE_MESSAGE = (
    "I was unable to complete this in time. Please try breaking your request into smaller steps."
)

FAILURE_KIND_FORMAT = "format_failure"
FAILURE_KIND_TIMEOUT = "timeout_no_answer"
# A spend budget blocked the call. Recorded on the interaction, never alerted:
# the budget alert in ops_alerts already covers it.
FAILURE_KIND_BUDGET = "budget_exceeded"

_AGENT_FAILURE_ALERTS_ENABLED: bool = os.environ.get(
    "AGENT_FAILURE_ALERTS_ENABLED", "1"
) not in ("0", "false", "False", "")
_AGENT_FAILURE_ALERTS_CHANNEL_ID: str = os.environ.get(
    "AGENT_FAILURE_ALERTS_CHANNEL_ID", ""
).strip()
_AGENT_FAILURE_ALERT_DEBOUNCE_MINUTES: int = max(
    1, int(os.environ.get("AGENT_FAILURE_ALERT_DEBOUNCE_MINUTES", "15"))
)
_APP_BASE_URL: str = os.environ.get(
    "APP_BASE_URL", "http://localhost:8080"
).rstrip("/")


@dataclass(frozen=True)
class AgentLlmOutcome:
    """Result of an agent LLM tool loop."""

    text: str
    failure_kind: str | None = None
    failure_detail: str | None = None
    resolved_model: str | None = None

    @property
    def is_alertable_failure(self) -> bool:
        return self.failure_kind in (FAILURE_KIND_FORMAT, FAILURE_KIND_TIMEOUT)


def _get_platform_bot_token() -> str | None:
    from ops_alerts import _get_platform_bot_token as _ops_token

    return _ops_token()


def _lookup_slack_user_id_by_email(token: str, email: str) -> str | None:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    try:
        from slack_sdk import WebClient

        resp = WebClient(token=token).users_lookupByEmail(email=email)
        user = resp.get("user") if isinstance(resp, dict) else getattr(resp, "user", None)
        if not user:
            return None
        uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
        return str(uid or "").strip() or None
    except Exception:
        logger.warning(
            "agent_failure_alerts: users.lookupByEmail failed for %s",
            email,
            exc_info=True,
        )
        return None


def _format_owner_mention(token: str, creator_email: str) -> str:
    uid = _lookup_slack_user_id_by_email(token, creator_email)
    if uid:
        return f"<@{uid}>"
    if creator_email:
        return creator_email
    return "(owner unknown)"


def _failure_kind_label(kind: str) -> str:
    if kind == FAILURE_KIND_FORMAT:
        return "Could not format final response (empty model text)"
    if kind == FAILURE_KIND_TIMEOUT:
        return "Tool/time budget exhausted with no final answer"
    return kind


def _build_alert_message(
    agent: Agent,
    outcome: AgentLlmOutcome,
    *,
    owner_mention: str,
    source: str,
    channel_id: str | None,
    thread_ts: str | None,
    scheduled_task_id: str | None,
    interaction_id: str | None = None,
) -> str:
    agent_name = getattr(agent, "name", None) or "Agent"
    agent_id = str(agent.id)
    lines = [
        f":warning: *Agent response failure — {agent_name}*",
        f"Type: {_failure_kind_label(outcome.failure_kind or '')}",
    ]
    if outcome.failure_detail:
        lines.append(f"Detail: {outcome.failure_detail}")
    if outcome.resolved_model:
        lines.append(f"Model: {outcome.resolved_model}")
    lines.append(f"Owner: {owner_mention}")
    lines.append(f"Source: {source}")
    if channel_id:
        ctx = f"Slack channel `{channel_id}`"
        if thread_ts:
            ctx += f" thread `{thread_ts}`"
        lines.append(f"Context: {ctx}")
    if scheduled_task_id:
        lines.append(f"Scheduled task: `{scheduled_task_id}`")
    lines.append(f"User saw: \"{outcome.text[:120]}{'…' if len(outcome.text) > 120 else ''}\"")
    if interaction_id:
        lines.append(
            f"Run: {_APP_BASE_URL}/agents/{agent_id}?tab=interactions&interaction={interaction_id}"
        )
    else:
        lines.append(f"Agent: {_APP_BASE_URL}/agents/{agent_id}")
    return "\n".join(lines)


def _post_slack_message(token: str, channel: str, text: str) -> bool:
    try:
        from slack_sdk import WebClient

        WebClient(token=token).chat_postMessage(channel=channel, text=text)
        return True
    except Exception:
        logger.warning("agent_failure_alerts: Slack post failed", exc_info=True)
        return False


def _should_notify(db, alert_key: str) -> bool:
    state = db.query(OpsAlertState).filter(OpsAlertState.alert_key == alert_key).first()
    if state is None:
        return True
    return should_renotify(
        state.last_notified_at,
        renotify_minutes=_AGENT_FAILURE_ALERT_DEBOUNCE_MINUTES,
    )


def _record_notification(db, alert_key: str, summary: str) -> None:
    now = datetime.utcnow()
    state = db.query(OpsAlertState).filter(OpsAlertState.alert_key == alert_key).first()
    if state is None:
        state = OpsAlertState(
            alert_key=alert_key,
            status="breaching",
            breach_streak=1,
            first_breach_at=now,
        )
        db.add(state)
    state.status = "breaching"
    state.last_notified_at = now
    state.last_value = (summary or "")[:500]
    state.updated_at = now


def _notify_agent_failure_sync(
    agent: Agent,
    outcome: AgentLlmOutcome,
    *,
    source: str,
    channel_id: str | None,
    thread_ts: str | None,
    scheduled_task_id: str | None,
    interaction_id: str | None = None,
) -> None:
    if not _AGENT_FAILURE_ALERTS_ENABLED or not outcome.is_alertable_failure:
        return

    channel = _AGENT_FAILURE_ALERTS_CHANNEL_ID
    token = _get_platform_bot_token()
    if not channel or not token:
        logger.info(
            "agent_failure_alerts: skipped (channel=%s token=%s) agent=%s kind=%s",
            bool(channel),
            bool(token),
            agent.id,
            outcome.failure_kind,
        )
        return

    alert_key = f"agent_failure:{agent.id}:{outcome.failure_kind}"
    db = SessionLocal()
    try:
        if not _should_notify(db, alert_key):
            return
        owner_mention = _format_owner_mention(token, getattr(agent, "creator_email", "") or "")
        text = _build_alert_message(
            agent,
            outcome,
            owner_mention=owner_mention,
            source=source,
            channel_id=channel_id,
            thread_ts=thread_ts,
            scheduled_task_id=scheduled_task_id,
            interaction_id=interaction_id,
        )
        if _post_slack_message(token, channel, text):
            _record_notification(db, alert_key, outcome.failure_detail or outcome.failure_kind or "")
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        logger.exception(
            "agent_failure_alerts: failed for agent=%s kind=%s",
            agent.id,
            outcome.failure_kind,
        )
    finally:
        db.close()


async def notify_agent_failure(
    agent: Agent,
    outcome: AgentLlmOutcome,
    *,
    source: str,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    scheduled_task_id: str | None = None,
    interaction_id: str | None = None,
) -> None:
    """Best-effort alert; never raises."""
    if not outcome.is_alertable_failure:
        return
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: _notify_agent_failure_sync(
            agent,
            outcome,
            source=source,
            channel_id=channel_id,
            thread_ts=thread_ts,
            scheduled_task_id=scheduled_task_id,
            interaction_id=interaction_id,
        ),
    )
