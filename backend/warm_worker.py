"""Warm async fleet worker — coroutine-per-event queue consumer.

Architecture: AGENT_EXECUTION_ARCHITECTURE.md §2–§7

Replaces slack_consumer_worker.py + the spawn-per-event path for Slack event
dispatch.  Automations and scheduled tasks still use agent_worker.spawn_*.

Run:
    python -m warm_worker

Key differences from slack_consumer_worker:
  - One coroutine per event, with concurrency capped by a semaphore rather than
    by how fast events arrive.
  - Coroutine per event (vs OS subprocess per event) — ~0 startup, ~KB RAM overhead.
  - Session table (agent_sessions) for durable multi-turn conversation state.
  - OCC versioned write + transactional outbox (reply_outbox) — no dual-write gap.
  - Placeholder posted at <300ms; chat.update replaces it with the final answer.
  - The handler returns only AFTER the outbox is committed AND the Slack message
    is delivered; returning is what acknowledges the event.
  - Idempotency table (turn_idempotency) guards against duplicate redelivery.
  - Compaction fires ONLY when recent window exceeds token budget (not every turn).
  - Invoker-scoped secrets stored in ContextVar; dropped on coroutine exit.

Flow control: MAX_CONCURRENT_MESSAGES limits how many events the worker
delivers before pausing — caps coroutine concurrency at pod level without a separate
semaphore.  Target: ~75 concurrent coroutines per pod.
"""

from __future__ import annotations

from app_config import APP_NAME

import asyncio
import contextvars
import json
import logging
import os
import signal
import uuid
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import text as _sql_text
from providers import get_message_queue
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8080").rstrip("/")

load_dotenv()

from logging_setup import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger("warm_worker")

# ── Config ────────────────────────────────────────────────────────────────────

SLACK_EVENTS_TOPIC: str = os.environ.get("SLACK_EVENTS_TOPIC", "slack.events")
# Flow control: max concurrent coroutines per pod (§3 — ~75 per pod).
MAX_CONCURRENT_MESSAGES: int = int(os.environ.get("WARM_MAX_CONCURRENT", "75"))
# Rough char budget for recent verbatim window before compaction fires (§5).
# 80 000 chars ≈ 20 000 tokens.  Tune via SESSION_TOKEN_BUDGET_CHARS env var.
_TOKEN_BUDGET_CHARS: int = int(os.environ.get("SESSION_TOKEN_BUDGET_CHARS", "80000"))
# Max OCC retries before giving up (ordering keys make conflicts extremely rare).
_OCC_MAX_RETRIES: int = 3
# Per-agent config cache TTL seconds (§4 — "re-read on TTL, not per turn").
_AGENT_CACHE_TTL_S: float = float(os.environ.get("AGENT_CACHE_TTL_SECONDS", "60"))
# Outbox sweeper: how often to scan for stuck 'pending' rows (§4 sweeper).
_SWEEPER_INTERVAL_S: float = float(os.environ.get("OUTBOX_SWEEPER_INTERVAL_S", "30"))
# Only sweep rows older than this many seconds — prevents racing live coroutines.
_SWEEPER_MIN_AGE_S: int = int(os.environ.get("OUTBOX_SWEEPER_MIN_AGE_S", "60"))

# ── Per-agent process cache (§4 — "agent config: soul, guardrails, skill_ids") ─
# Dict keyed on Slack app_id → (Agent object, monotonic timestamp).
# CPython dict operations are GIL-safe; worst-case two threads both fetch from DB
# on a cache miss, which is harmless.
_agent_cache: dict[str, tuple] = {}


def _get_agent_cached_sync(slack_app_id: str):
    """Look up agent by Slack app_id with a process-level TTL cache (§4).

    Re-reads from DB only when the TTL has expired or the entry is absent.
    Avoids a DB round-trip on every message for the common case where the
    agent config has not changed.
    """
    import time as _t
    entry = _agent_cache.get(slack_app_id)
    if entry is not None:
        agent, cached_at = entry
        if _t.monotonic() - cached_at < _AGENT_CACHE_TTL_S:
            return agent
    from api_slack_events import _get_agent_by_app_id
    agent = _get_agent_by_app_id(slack_app_id)
    if agent is not None:
        _agent_cache[slack_app_id] = (agent, _t.monotonic())
    return agent


# ── Per-coroutine ContextVar for invoker-scoped secrets (§4) ─────────────────
# Populated at turn start from the vault; automatically dropped when the
# coroutine exits — provides logical isolation without OS process separation.
_invoker_secrets: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "invoker_secrets", default={}
)

# ── Shared event loop and shutdown flag ───────────────────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None
_shutdown_requested: bool = False


def _handle_shutdown(signum, frame) -> None:
    global _shutdown_requested
    logger.info("Received signal %d — requesting graceful shutdown", signum)
    _shutdown_requested = True


# ── Session ID ────────────────────────────────────────────────────────────────

def _make_session_id(agent_id: str, channel: str, thread_ts: str) -> str:
    """Stable session key for one (agent, channel, thread) conversation."""
    return f"{agent_id}:{channel}:{thread_ts}"


# ── DB helpers (sync — called via run_in_executor) ───────────────────────────

def _read_session_sync(session_id: str) -> dict:
    """Read session row and immediately release the DB connection (§2 read-release).

    Returns {'state', 'version', 'summary', 'history_cursor'}.
    version=-1 signals no session exists yet (first turn).
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        row = db.execute(
            _sql_text(
                "SELECT state, version, summary, history_cursor"
                " FROM agent_sessions WHERE session_id = :sid"
            ),
            {"sid": session_id},
        ).fetchone()
        db.commit()
    finally:
        db.close()

    if row is None:
        return {"state": {}, "version": -1, "summary": None, "history_cursor": None}
    return {
        "state": row.state or {},
        "version": row.version,
        "summary": row.summary,
        "history_cursor": row.history_cursor,
    }


def _occ_write_sync(
    *,
    session_id: str,
    agent_id: str,
    new_state: dict,
    orig_version: int,
    event_id: str,
    outbox_payload: str,
    new_summary: Optional[str] = None,
    new_cursor: Optional[str] = None,
) -> str:
    """Atomically write session + reply_outbox + idempotency in one ~2ms txn (§2, §5).

    Returns "ok" on success, "conflict" on OCC version mismatch.
    Raises on unexpected DB errors (let the caller nack).
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        if orig_version == -1:
            # ── First turn: INSERT new session row ────────────────────────────
            result = db.execute(
                _sql_text(
                    "INSERT INTO agent_sessions"
                    " (session_id, agent_id, state, version, summary,"
                    "  history_cursor, working_state, created_at, updated_at)"
                    " VALUES (:sid, :aid, CAST(:state AS jsonb), 0, :summary,"
                    "  :cursor, '{}'::jsonb, :now, :now)"
                    " ON CONFLICT (session_id) DO NOTHING"
                ),
                {
                    "sid": session_id,
                    "aid": agent_id,
                    "state": json.dumps(new_state),
                    "summary": new_summary,
                    "cursor": new_cursor,
                    "now": now,
                },
            )
        else:
            # ── Subsequent turns: OCC UPDATE ──────────────────────────────────
            extra_cols = ""
            update_params: dict = {
                "state": json.dumps(new_state),
                "now": now,
                "sid": session_id,
                "orig": orig_version,
            }
            if new_summary is not None:
                extra_cols += ", summary = :summary"
                update_params["summary"] = new_summary
            if new_cursor is not None:
                extra_cols += ", history_cursor = :cursor"
                update_params["cursor"] = new_cursor
            result = db.execute(
                _sql_text(
                    f"UPDATE agent_sessions"
                    f" SET state = CAST(:state AS jsonb), version = version + 1, updated_at = :now{extra_cols}"
                    f" WHERE session_id = :sid AND version = :orig"
                ),
                update_params,
            )

        if result.rowcount == 0:
            db.rollback()
            return "conflict"

        # ── INSERT reply_outbox (idempotent on event_id) ──────────────────────
        db.execute(
            _sql_text(
                "INSERT INTO reply_outbox (id, event_id, session_id, payload, status, created_at)"
                " VALUES (:id, :eid, :sid, :payload, 'pending', :now)"
                " ON CONFLICT (event_id) DO NOTHING"
            ),
            {
                "id": str(uuid.uuid4()),
                "eid": event_id,
                "sid": session_id,
                "payload": outbox_payload,
                "now": now,
            },
        )

        # ── INSERT turn_idempotency (idempotent on event_id) ──────────────────
        db.execute(
            _sql_text(
                "INSERT INTO turn_idempotency (event_id, session_id, committed_at)"
                " VALUES (:eid, :sid, :now)"
                " ON CONFLICT (event_id) DO NOTHING"
            ),
            {"eid": event_id, "sid": session_id, "now": now},
        )

        db.commit()
        return "ok"
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _mark_outbox_delivered_sync(event_id: str, slack_ts: Optional[str]) -> None:
    """Mark reply_outbox row delivered after successful chat.update (idempotent)."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(
            _sql_text(
                "UPDATE reply_outbox"
                " SET status = 'delivered', slack_ts = :ts, delivered_at = :now"
                " WHERE event_id = :eid AND status = 'pending'"
            ),
            {"ts": slack_ts, "now": datetime.now(timezone.utc), "eid": event_id},
        )
        db.commit()
    finally:
        db.close()


def _sweep_pending_outbox_sync() -> list[dict]:
    """Fetch reply_outbox rows stuck in 'pending' older than the min-age threshold (§4).

    Using a fixed age guard prevents the sweeper from racing a coroutine that is
    still in-flight processing the same event_id.  Ordering by created_at ensures
    oldest stuck messages are recovered first.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        rows = db.execute(
            _sql_text(
                "SELECT event_id, session_id, payload"
                " FROM reply_outbox"
                " WHERE status = 'pending'"
                "   AND created_at < NOW() - (:age_s || ' seconds')::interval"
                " ORDER BY created_at ASC"
                " LIMIT 50"
            ),
            {"age_s": str(_SWEEPER_MIN_AGE_S)},
        ).fetchall()
        db.commit()
    finally:
        db.close()
    return [
        {"event_id": r.event_id, "session_id": r.session_id, "payload": r.payload}
        for r in rows
    ]


def _get_agent_by_id_sync(agent_id: str):
    """Load agent row by primary key (used by sweeper, which has no Slack app_id)."""
    from database import SessionLocal
    from agents import Agent
    db = SessionLocal()
    try:
        return db.query(Agent).filter(Agent.id == agent_id).first()
    except Exception:
        logger.exception("Could not load agent_id=%s", agent_id)
        return None
    finally:
        db.close()


def _sweeper_post_sync(xoxb: str, channel: str, thread_ts: str, text: str) -> Optional[str]:
    """Post a recovered reply via chat.postMessage (no placeholder_ts available).

    The sweeper never has the original placeholder ts — it posts a fresh message
    in the correct thread.  Idempotency is handled at the outbox level (keyed on
    event_id), so a duplicate post is visible but harmless in the failure cases
    where this code runs.
    """
    from slack_sdk import WebClient
    from api_slack_events import _strip_tool_call_blocks
    try:
        resp = WebClient(token=xoxb).chat_postMessage(
            channel=channel,
            text=_strip_tool_call_blocks(text),
            thread_ts=thread_ts or None,
        )
        return (resp or {}).get("ts")
    except Exception:
        logger.exception("Sweeper: failed to post to channel=%s", channel)
        return None


def _check_idempotency_sync(event_id: str) -> Optional[dict]:
    """Check turn_idempotency + reply_outbox for crash-recovery on redelivery (§6).

    Returns None if not yet committed.
    Returns dict with 'status', 'payload', 'slack_ts' if already committed.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        row = db.execute(
            _sql_text(
                "SELECT t.session_id, r.status, r.payload, r.slack_ts"
                " FROM turn_idempotency t"
                " LEFT JOIN reply_outbox r ON r.event_id = t.event_id"
                " WHERE t.event_id = :eid"
            ),
            {"eid": event_id},
        ).fetchone()
        db.commit()
    finally:
        db.close()

    if row is None:
        return None
    return {
        "session_id": row.session_id,
        "status": row.status,   # 'pending' | 'delivered' | None (outbox row missing)
        "payload": row.payload,
        "slack_ts": row.slack_ts,
    }


# ── Slack helpers ─────────────────────────────────────────────────────────────

def _post_placeholder_sync(xoxb: str, channel: str, thread_ts: str) -> Optional[str]:
    """Post 'working…' placeholder; return its Slack ts for later chat.update."""
    from slack_sdk import WebClient
    try:
        resp = WebClient(token=xoxb).chat_postMessage(
            channel=channel,
            text=":hourglass_flowing_sand: Working\u2026",
            thread_ts=thread_ts or None,
        )
        return (resp or {}).get("ts")
    except Exception:
        logger.exception("Failed to post placeholder channel=%s", channel)
        return None


def _update_slack_message_sync(xoxb: str, channel: str, ts: str, text: str) -> Optional[str]:
    """Replace placeholder with final reply via chat.update. Returns updated ts."""
    from slack_sdk import WebClient
    from api_slack_events import _strip_tool_call_blocks
    try:
        resp = WebClient(token=xoxb).chat_update(
            channel=channel,
            ts=ts,
            text=_strip_tool_call_blocks(text),
        )
        return (resp or {}).get("ts")
    except Exception:
        logger.exception("Failed to update Slack message channel=%s ts=%s", channel, ts)
        return None


def _fetch_thread_tail_sync(
    xoxb: str,
    channel: str,
    thread_ts: str,
    since_ts: Optional[str],
) -> list[dict]:
    """Fetch recent verbatim thread messages (§4: 'recent verbatim tail').

    When since_ts (history_cursor) is set, only messages AFTER that timestamp
    are returned — the earlier messages are already captured in session.summary.
    """
    from slack_sdk import WebClient
    try:
        kwargs: dict = {"channel": channel, "ts": thread_ts, "limit": 50}
        if since_ts:
            kwargs["oldest"] = since_ts
        result = WebClient(token=xoxb).conversations_replies(**kwargs)
        messages = result.get("messages", [])
        history: list[dict] = []
        for msg in messages[:-1]:  # exclude the triggering message (last item)
            if since_ts and float(msg.get("ts", "0")) <= float(since_ts):
                continue
            role = "assistant" if msg.get("bot_id") else "user"
            content = (msg.get("text") or "").strip()
            if content:
                history.append({"role": role, "content": content})
        return history
    except Exception:
        logger.warning(
            "Could not fetch thread tail channel=%s ts=%s since=%s",
            channel, thread_ts, since_ts,
        )
        return []


# ── Compaction (§5: only on token budget exceeded) ───────────────────────────

def _history_chars(history: list[dict]) -> int:
    return sum(len(m.get("content", "")) for m in history)


_COMPACTION_PROMPT = """\
You are summarising a long AI-agent conversation to reduce context length.
Given the existing summary (may be empty) and a batch of conversation messages,
write a concise updated summary capturing key facts, decisions, and context.
Keep the summary under 2000 characters. Output only the summary text, no preamble.

Existing summary:
{current_summary}

Messages to fold in:
{messages}
"""


async def _compact_history(
    history: list[dict],
    current_summary: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Compact the oldest half of history into a new LLM summary (§5).

    Returns (new_summary, new_cursor) or (None, None) on failure.
    new_cursor is None for this revision — the session table stores the Slack
    thread_ts cursor; cursor tracking is deferred to a future phase.
    """
    import anthropic as _anthropic
    from api_slack_events import _MODEL_GATEWAY_API_KEY, _MODEL_GATEWAY_URL, _MODEL

    if not _MODEL_GATEWAY_API_KEY or len(history) < 4:
        return None, None

    split = max(2, len(history) // 2)
    to_compact = history[:split]

    msgs_text = "\n".join(
        f"{m['role'].upper()}: {m.get('content', '')[:500]}"
        for m in to_compact
    )
    prompt = _COMPACTION_PROMPT.format(
        current_summary=current_summary or "(none — first compaction)",
        messages=msgs_text,
    )

    try:
        client = _anthropic.AsyncAnthropic(
            api_key=_MODEL_GATEWAY_API_KEY,
            base_url=_MODEL_GATEWAY_URL,
        )
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        new_summary = "".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        ).strip()
        return new_summary or None, None
    except Exception:
        logger.exception("Compaction LLM call failed")
        return None, None


# ── Outbox sweeper (Phase 4 / §6) ────────────────────────────────────────────

async def _sweep_one_row(row: dict, loop: asyncio.AbstractEventLoop) -> None:
    """Redeliver one stuck reply_outbox row.

    Failure modes handled (§6):
      - Coroutine committed the outbox row (status='pending') but crashed before
        chat.update and before the event is acknowledged.
      - Pod was killed between commit and delivery.

    Recovery:
      1. Parse session_id to extract agent_id, channel, thread_ts.
      2. Load the agent's bot token.
      3. Post via chat.postMessage (no placeholder_ts available).
      4. Mark outbox row delivered.
    """
    from api_slack_events import _read_secret

    event_id: str = row["event_id"]
    session_id: str = row["session_id"]
    payload: str = row["payload"]

    # session_id = "{agent_id}:{channel}:{thread_ts}"
    parts = session_id.split(":", 2)
    if len(parts) != 3:
        logger.warning("Sweeper: malformed session_id=%r event_id=%s — skipping", session_id, event_id[:20])
        return
    agent_id, channel, thread_ts = parts

    agent = await loop.run_in_executor(None, _get_agent_by_id_sync, agent_id)
    if not agent:
        logger.warning(
            "Sweeper: agent_id=%s not found for event_id=%s — skipping", agent_id, event_id[:20]
        )
        return

    xoxb = _read_secret(agent.xoxb_secret_ref)
    if not xoxb:
        logger.warning(
            "Sweeper: no xoxb for agent_id=%s event_id=%s — skipping", agent_id, event_id[:20]
        )
        return

    slack_ts = await loop.run_in_executor(
        None, _sweeper_post_sync, xoxb, channel, thread_ts, payload
    )

    await loop.run_in_executor(None, _mark_outbox_delivered_sync, event_id, slack_ts)

    logger.info(
        "Sweeper: recovered event_id=%s session=%s slack_ts=%s",
        event_id[:20], session_id[:50], slack_ts,
    )


async def _run_sweeper_pass() -> None:
    """One sweep pass: find all stuck pending rows and redeliver them."""
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _sweep_pending_outbox_sync)
    if not rows:
        return
    logger.info("Outbox sweeper: found %d row(s) to redeliver", len(rows))
    for row in rows:
        try:
            await _sweep_one_row(row, loop)
        except Exception:
            logger.exception("Sweeper: failed on event_id=%s", row.get("event_id", "?"))


async def _sweeper_loop() -> None:
    """Background task: periodically scan reply_outbox for stuck 'pending' rows (§4, §6).

    Runs every _SWEEPER_INTERVAL_S seconds (default 30s).  Only touches rows
    older than _SWEEPER_MIN_AGE_S seconds (default 60s) to avoid racing live
    coroutines that are still completing their turn.

    This closes the crash-between-commit-and-delivery failure mode from §6:
      'committed not delivered -> outbox sweeper/redeliver posts it'
    """
    logger.info(
        "Outbox sweeper started — interval=%ds min_age=%ds",
        int(_SWEEPER_INTERVAL_S), _SWEEPER_MIN_AGE_S,
    )
    while not _shutdown_requested:
        await asyncio.sleep(_SWEEPER_INTERVAL_S)
        try:
            await _run_sweeper_pass()
        except Exception:
            logger.exception("Outbox sweeper pass raised an unhandled exception")


# ── Core dispatch coroutine (§2 full sequence) ────────────────────────────────

async def _dispatch_warm(agent, event: dict, event_id: str) -> bool:
    """Run one agent turn for one Slack event.

    Implements the full §2 sequence:
      1. Idempotency check (crash recovery)
      2. Post placeholder <300ms
      3. Rehydrate session (read-release)
      4. Load invoker secrets → ContextVar
      5. LLM tool loop (no DB connection held)
      6. Compaction check (only if over budget)
      7. OCC write + outbox + idempotency co-commit
      8. chat.update final reply
      9. Mark outbox delivered
      10. Ack

    Returns True to ack, False to nack.
    """
    # Deferred imports so the module-level load stays fast
    import json as _json
    import time as _time

    from api_slack_events import (
        _DERIV_ONE_BASE_URL,
        _INTERNAL_SIGNING_SECRET,
        _MODEL_GATEWAY_API_KEY,
        _MODEL_GATEWAY_URL,
        _MODEL,
        _SLACK_QUERY_SKILL,
        _audit_user_message,
        _build_memory_context,
        _build_slack_attachment_blocks,
        _call_llm,
        _check_agent_access,
        _check_slack_app_grant,
        _check_slack_workflow_grant,
        _slack_workflow_identity_candidates,
        _compose_user_message_content,
        _fetch_sender_profile,
        _filter_accessible_skills,
        _handle_channel_instruction,
        _handle_owner_config_command,
        _is_owner_or_collaborator,
        _load_agent_skills,
        _load_skill_secrets_for_agent,
        _looks_like_channel_instruction,
        _looks_like_config_command,
        _max_tool_turns_for_agent,
        _post_agent_access_denied_sync,
        _post_slack_reply,
        _read_secret,
        _resolve_channel_config,
        _sign_invocation_context,
        _strip_tool_call_blocks,
    )

    event_type: str = event.get("type", "")
    channel: str = event.get("channel", "")
    thread_ts: str = event.get("thread_ts") or event.get("ts", "")
    user_text: str = event.get("text", "").strip()
    slack_files: list = event.get("files") or []
    slack_user_id: str = event.get("user", "")

    # Strip bot-mention prefix from app_mention events
    if event_type == "app_mention" and user_text.startswith("<@"):
        user_text = user_text.split(">", 1)[-1].strip()

    if not channel or (not user_text and not slack_files):
        return True  # nothing to dispatch

    agent_id = str(agent.id)
    session_id = _make_session_id(agent_id, channel, thread_ts)
    loop = asyncio.get_running_loop()

    # ── 1. Idempotency check (§6 crash-recovery path) ─────────────────────────
    if event_id:
        idem = await loop.run_in_executor(None, _check_idempotency_sync, event_id)
        if idem is not None:
            if idem.get("status") == "delivered":
                # Already committed + delivered — safe to ack without reprocessing.
                logger.info(
                    "Event already committed+delivered event_id=%s — acking",
                    event_id[:20],
                )
                return True
            if idem.get("status") == "pending":
                # Committed but not yet delivered (crash between commit and post).
                # Re-post from outbox then ack.
                logger.info(
                    "Event committed but not delivered event_id=%s — redelivering outbox",
                    event_id[:20],
                )
                payload = idem.get("payload") or ""
                if payload:
                    xoxb_repost = _read_secret(agent.xoxb_secret_ref)
                    if xoxb_repost and channel and thread_ts:
                        ph_ts = await loop.run_in_executor(
                            None, _post_placeholder_sync, xoxb_repost, channel, thread_ts
                        )
                        if ph_ts:
                            final_ts = await loop.run_in_executor(
                                None, _update_slack_message_sync,
                                xoxb_repost, channel, ph_ts, payload,
                            )
                            await loop.run_in_executor(
                                None, _mark_outbox_delivered_sync, event_id, final_ts
                            )
                return True

    # ── Channel config resolution (mirrors _dispatch_event) ───────────────────
    channel_cfg = _resolve_channel_config(agent, channel)
    fallback = getattr(agent, "channel_fallback_behavior", "default") or "default"
    if channel_cfg is None and (
        getattr(agent, "is_platform_agent", False) or fallback == "silent"
    ):
        logger.debug(
            "warm_worker: no channel config agent=%s channel=%s — dropping", agent_id, channel
        )
        return True

    effective_soul = (channel_cfg.soul_text if channel_cfg else None) or agent.soul_text
    effective_display_name = (
        (channel_cfg.display_name if channel_cfg and channel_cfg.display_name else None)
        or agent.name
    )
    effective_guardrails = (
        channel_cfg.guardrails
        if (channel_cfg and channel_cfg.guardrails is not None)
        else (agent.guardrails or [])
    )
    if channel_cfg and channel_cfg.skill_ids:
        try:
            effective_skill_ids: list[str] = _json.loads(channel_cfg.skill_ids)
        except Exception:
            effective_skill_ids = _json.loads(agent.skill_ids or "[]")
    else:
        effective_skill_ids = _json.loads(agent.skill_ids or "[]")

    effective_memory_scope = (channel_cfg.memory_scope if channel_cfg else None) or "global"
    _is_platform_for_memory = bool(getattr(agent, "is_platform_agent", False))
    effective_channel_id_for_memory: Optional[str] = (
        channel if (_is_platform_for_memory or effective_memory_scope == "channel") else None
    )

    # ── Load bot token ─────────────────────────────────────────────────────────
    xoxb = _read_secret(agent.xoxb_secret_ref)
    if not xoxb:
        logger.error("Agent %s has no readable xoxb token — nacking", agent_id)
        return False

    # ── Ambient gate (cheap classifier) ────────────────────────────────────────
    # A plain (non-mention, non-DM) message only reaches here because the
    # caller (_handle_event) already confirmed the channel is
    # "ambient" for this agent. Route it through the same cheap/fast gate the
    # legacy webhook path uses before doing ANY expensive work — no
    # placeholder, no session read, no LLM tool loop. Fails closed.
    is_ambient: bool = event_type == "message" and event.get("channel_type") != "im"
    if is_ambient:
        from ambient_message_gate import should_handle_ambient_message

        gate_history = await loop.run_in_executor(
            None, _fetch_thread_tail_sync, xoxb, channel, thread_ts, None
        )
        should_handle, ambient_reason = await loop.run_in_executor(
            None,
            should_handle_ambient_message,
            effective_display_name,
            (channel_cfg.channel_name if channel_cfg else "") or "",
            user_text,
            gate_history,
        )
        if not should_handle:
            logger.info(
                "ambient gate: skipping agent=%s channel=%s reason=%s",
                agent_id, channel, ambient_reason,
            )
            return True
        logger.info(
            "ambient gate: handling agent=%s channel=%s reason=%s",
            agent_id, channel, ambient_reason,
        )

    # ── Gate 0 — lifecycle status ────────────────────────────────────────────
    # Pause/activate only flip DB status today; this file had no check for it
    # at all (unlike _dispatch_event's Gate 0 and slack_consumer_worker.py) —
    # a paused agent kept dispatching. Re-fetch from DB so a pause that lands
    # after the cached agent was loaded still takes effect.
    from agents import agent_accepts_runtime_requests, get_agent_status

    runtime_status = await loop.run_in_executor(None, get_agent_status, agent_id)
    if not agent_accepts_runtime_requests(runtime_status):
        is_bot_sender = bool(event.get("bot_id") or event.get("subtype") == "bot_message")
        logger.info(
            "Agent not accepting Slack events agent=%s status=%s sender_bot=%s — dropping",
            agent_id, runtime_status, is_bot_sender,
        )
        if not is_bot_sender:
            from api_slack_events import _post_agent_unavailable_sync

            await loop.run_in_executor(
                None,
                _post_agent_unavailable_sync,
                xoxb,
                channel,
                thread_ts,
                effective_display_name,
                runtime_status or getattr(agent, "status", "unknown"),
            )
        return True

    # ── 2. Post placeholder immediately (<300ms target) ───────────────────────
    placeholder_ts: Optional[str] = await loop.run_in_executor(
        None, _post_placeholder_sync, xoxb, channel, thread_ts
    )
    if not placeholder_ts:
        logger.warning(
            "Could not post placeholder agent=%s channel=%s — continuing", agent_id, channel
        )

    # ── 3. Rehydrate session: BEGIN; SELECT; COMMIT (read-release §2) ─────────
    session_data = await loop.run_in_executor(None, _read_session_sync, session_id)
    orig_version: int = session_data["version"]
    session_summary: Optional[str] = session_data["summary"]
    session_cursor: Optional[str] = session_data["history_cursor"]

    # ── 4. Concurrently fetch thread tail + sender profile + attachments ───────
    history, sender_profile, attachment_blocks = await asyncio.gather(
        loop.run_in_executor(
            None, _fetch_thread_tail_sync, xoxb, channel, thread_ts, session_cursor
        ),
        loop.run_in_executor(None, _fetch_sender_profile, xoxb, slack_user_id),
        loop.run_in_executor(None, _build_slack_attachment_blocks, xoxb, slack_files),
    )

    user_content = _compose_user_message_content(user_text, attachment_blocks)
    user_message_audit = _audit_user_message(user_text, slack_files)

    sender_email: str = sender_profile.get("email", "")
    sender_name: str = (
        sender_profile.get("real_name", "") or sender_profile.get("display_name", "")
    )
    is_bot_sender: bool = bool(sender_profile.get("is_bot", False))
    sender_slack_user_id: str = sender_profile.get("slack_user_id", slack_user_id)

    # Handle unprocessable attachments
    if slack_files and not attachment_blocks and not user_text:
        logger.warning(
            "Slack files present but none processable agent=%s channel=%s", agent_id, channel
        )
        if placeholder_ts:
            await loop.run_in_executor(
                None,
                _update_slack_message_sync,
                xoxb, channel, placeholder_ts,
                (
                    "I received your file(s) but couldn't process them. "
                    "Please share a JPEG, PNG, GIF, WebP, PDF, or Word (.docx) file."
                ),
            )
        return True

    # ── Identity gate ──────────────────────────────────────────────────────────
    if not sender_email and not is_bot_sender:
        logger.warning(
            "Could not resolve email for user_id=%s agent=%s — denying",
            slack_user_id, agent_id,
        )
        if placeholder_ts:
            await loop.run_in_executor(
                None,
                _update_slack_message_sync,
                xoxb, channel, placeholder_ts,
                (
                    "I wasn't able to verify your identity automatically.\n\n"
                    "Please make sure your Slack profile has a work email set, "
                    f"or contact your {APP_NAME} admin for help."
                ),
            )
        return True

    # ── Access gates ───────────────────────────────────────────────────────────
    accessible_skill_slugs: list[str]
    restricted_skill_slugs: list[str]

    if is_bot_sender:
        grant_ok = await loop.run_in_executor(
            None, _check_slack_app_grant, str(agent.id), sender_slack_user_id
        )
        workflow_candidates = _slack_workflow_identity_candidates(event, xoxb=xoxb)
        workflow_grant_ok = False
        if not grant_ok:
            workflow_grant_ok = await loop.run_in_executor(
                None,
                _check_slack_workflow_grant,
                str(agent.id),
                workflow_candidates,
                xoxb,
            )
        if not grant_ok and not workflow_grant_ok:
            logger.info(
                "Slack app not authorised: user=%s agent=%s workflow_candidates=%s — dropping",
                sender_slack_user_id,
                agent_id,
                sorted(workflow_candidates),
            )
            if placeholder_ts:
                # Update placeholder to blank so the user doesn't see a stale spinner
                await loop.run_in_executor(
                    None, _update_slack_message_sync, xoxb, channel, placeholder_ts,
                    "(message removed)",
                )
            return True
        sender_email = f"bot:{sender_slack_user_id}"
        sender_name = sender_name or f"Slack App ({sender_slack_user_id})"
        accessible_skill_slugs = effective_skill_ids
        restricted_skill_slugs = []
    else:
        _is_platform = bool(getattr(agent, "is_platform_agent", False))
        _channel_access_mode = (
            (channel_cfg.access_policy or {}).get("mode", "open") if channel_cfg else "open"
        )
        if _is_platform and _channel_access_mode != "allowlist":
            agent_allowed = True
        else:
            agent_allowed = await _check_agent_access(sender_email, agent)

        if not agent_allowed:
            logger.info("Agent access denied email=%s agent=%s", sender_email, agent_id)
            await loop.run_in_executor(
                None,
                _post_agent_access_denied_sync,
                xoxb, channel, thread_ts, agent.name, str(agent.id),
            )
            if placeholder_ts:
                await loop.run_in_executor(
                    None, _update_slack_message_sync, xoxb, channel, placeholder_ts,
                    "(message removed)",
                )
            return True

        # Channel allowlist gate
        if channel_cfg:
            access_policy = channel_cfg.access_policy or {}
            if access_policy.get("mode") == "allowlist":
                allowed_emails = [
                    e.strip().lower() for e in (access_policy.get("emails") or [])
                ]
                if sender_email.lower() not in allowed_emails:
                    logger.info(
                        "Channel allowlist denied email=%s agent=%s channel=%s",
                        sender_email, agent_id, channel,
                    )
                    if placeholder_ts:
                        await loop.run_in_executor(
                            None,
                            _update_slack_message_sync,
                            xoxb, channel, placeholder_ts,
                            (
                                "You don't have access to this agent in this channel. "
                                f"Visit *<{APP_BASE_URL}|the platform>* to request access."
                            ),
                        )
                    return True

        accessible_skill_slugs = await _filter_accessible_skills(
            sender_email, agent, skill_override=effective_skill_ids
        )
        restricted_skill_slugs = [
            s for s in effective_skill_ids if s not in accessible_skill_slugs
        ]
        logger.debug(
            "Skill filter email=%s agent=%s accessible=%s restricted=%s",
            sender_email, agent_id, accessible_skill_slugs, restricted_skill_slugs,
        )

    # ── Owner config commands (bypass main LLM path) ──────────────────────────
    if _is_owner_or_collaborator(agent, sender_email) and _looks_like_config_command(user_text):
        logger.info("Config command from owner email=%s agent=%s", sender_email, agent_id)
        if placeholder_ts:
            await loop.run_in_executor(
                None, _update_slack_message_sync, xoxb, channel, placeholder_ts, "\u2026"
            )
        await _handle_owner_config_command(
            agent=agent,
            user_text=user_text,
            xoxb=xoxb,
            channel=channel,
            thread_ts=thread_ts,
            sender_email=sender_email,
        )
        return True

    # ── Platform-agent channel instruction (bypass main LLM path) ─────────────
    _is_platform = bool(getattr(agent, "is_platform_agent", False))
    if _is_platform and _looks_like_channel_instruction(user_text):
        _cfg_channel_name = (channel_cfg.channel_name if channel_cfg else None) or channel
        logger.info("Channel instruction email=%s agent=%s", sender_email, agent_id)
        if placeholder_ts:
            await loop.run_in_executor(
                None, _update_slack_message_sync, xoxb, channel, placeholder_ts, "\u2026"
            )
        await _handle_channel_instruction(
            agent=agent,
            channel_id=channel,
            channel_name=_cfg_channel_name,
            user_text=user_text,
            xoxb=xoxb,
            thread_ts=thread_ts,
            sender_email=sender_email,
        )
        return True

    # ── Build system prompt ────────────────────────────────────────────────────
    identity = (
        f"You are {effective_display_name}, a Slack bot assistant running inside this Slack workspace. "
        "You have full access to your assigned tools and skills — never claim you lack access. "
        "Do NOT say you are an AI or disclaim Slack integration. Just answer helpfully.\n\n"
        "FORMATTING RULES — follow these exactly, every reply:\n"
        "- Use Slack mrkdwn, NOT standard markdown.\n"
        "- Bold: *text* (single asterisk). Never use **double asterisks**.\n"
        "- Italic: _text_ (underscore).\n"
        "- Inline code: `code`.\n"
        "- Bullet lists: start each item with \u2022 or - on its own line.\n"
        "- Section headers: *Header* on its own line, followed by a blank line.\n"
        "- DO NOT use markdown tables (| col | col |) — Slack does not render them. "
        "Use a bullet list or numbered list instead.\n"
        "- DO NOT wrap responses in a code block unless showing actual code.\n"
        "- Keep replies concise. Avoid disclaimers, warnings, and filler phrases.\n\n"
    )
    if sender_email and not sender_email.startswith("bot:"):
        name_part = f"{sender_name} " if sender_name else ""
        identity += f"The person asking is: {name_part}<{sender_email}>\n\n"

    # Inject compacted conversation summary (§4 — loaded once per turn from DB).
    # Recent verbatim tail is in the message history passed to _call_llm below.
    if session_summary:
        identity += (
            "## Conversation History (earlier turns, compacted)\n"
            f"{session_summary}\n\n"
            "Note: The messages above summarise earlier turns. "
            "Recent turns follow in the message history below.\n\n"
        )

    soul = (effective_soul or "").strip()
    skill_context = _load_agent_skills(agent, skill_slugs=accessible_skill_slugs)

    skill_secrets = await loop.run_in_executor(
        None, _load_skill_secrets_for_agent, agent, accessible_skill_slugs
    )

    system_prompt = identity
    if soul:
        system_prompt += soul + "\n\n"

    active_guardrails = [g for g in (effective_guardrails or []) if g.get("enabled")]
    if active_guardrails:
        from api_agents import _resolve_guardrail_text
        rules = "\n".join(
            f"- {_resolve_guardrail_text(g)}"
            for g in active_guardrails
            if _resolve_guardrail_text(g)
        )
        if rules:
            system_prompt += (
                "## Guardrails\nYou must follow these rules at all times:\n" + rules + "\n\n"
            )

    if skill_context:
        system_prompt += skill_context

    if restricted_skill_slugs:
        from skills_acl import get_skill_acl
        restricted_lines: list[str] = []
        for slug in restricted_skill_slugs:
            acl = get_skill_acl(slug)
            desc = acl.description if acl else slug
            restricted_lines.append(f"- **{slug}**: {desc}")
        system_prompt += (
            "\n\n## Skills Required But Not Accessible\n\n"
            "The following skills are attached to this agent but "
            "**this user does not have access** to them. "
            "You have been given their descriptions only — no implementation details or API keys. "
            "Do NOT attempt to call run_python or make any API calls for these skills.\n\n"
            + "\n".join(restricted_lines)
            + "\n\nIf the user's request requires any of these skills, tell them which skill "
            f"they need and instruct them to visit {APP_BASE_URL} to request access."
        )

    # Inject agent + user memories
    mem_ctx = _build_memory_context(agent_id, sender_email, channel_id=effective_channel_id_for_memory)
    if mem_ctx:
        system_prompt += "\n\n" + mem_ctx

    # ── Assemble sandbox env secrets (stored in ContextVar) ───────────────────
    merged_secrets: dict[str, str] = dict(skill_secrets or {})
    if sender_email:
        merged_secrets["SENDER_EMAIL"] = sender_email
    if sender_name:
        merged_secrets["SENDER_NAME"] = sender_name
    if slack_user_id:
        merged_secrets["SLACK_USER_ID"] = slack_user_id
    merged_secrets["SLACK_BOT_TOKEN"] = xoxb
    if channel:
        merged_secrets["SLACK_CHANNEL_ID"] = channel
    if thread_ts:
        merged_secrets["SLACK_THREAD_TS"] = thread_ts
    merged_secrets["AGENT_ID"] = agent_id
    merged_secrets["PLATFORM_INTERNAL_BASE_URL"] = _DERIV_ONE_BASE_URL
    merged_secrets["DERIV_ONE_BASE_URL"] = _DERIV_ONE_BASE_URL
    if _INTERNAL_SIGNING_SECRET and sender_email and not sender_email.startswith("bot:"):
        try:
            merged_secrets["DERIV_ONE_CTX_TOKEN"] = _sign_invocation_context(
                sender_email=sender_email,
                source_agent_id=agent_id,
            )
        except Exception:
            logger.warning("Could not sign invocation context for agent %s", agent_id)

    # Invokable agents context
    can_invoke_ids: list[str] = []
    try:
        can_invoke_ids = _json.loads(agent.can_invoke or "[]")
    except (ValueError, TypeError):
        pass
    if can_invoke_ids:
        from database import SessionLocal
        from agents import Agent as _Agent
        _db = SessionLocal()
        try:
            invokable_agents = _db.query(_Agent).filter(_Agent.id.in_(can_invoke_ids)).all()
        except Exception:
            invokable_agents = []
            logger.warning("Could not load invokable agents for agent %s", agent_id)
        finally:
            _db.close()
        if invokable_agents:
            agent_lines = "\n".join(
                f"- *{a.name}* (id: `{a.id}`): {(a.description or 'no description').strip()}"
                for a in invokable_agents
            )
            system_prompt += (
                "\n\n---\n\n*Agent-to-Agent Calls*\n"
                "You can delegate sub-tasks to other agents via `run_python`. "
                "Use this pattern:\n\n"
                "```python\n"
                "import os, requests\n"
                "resp = requests.post(\n"
                "    os.environ.get(\"PLATFORM_INTERNAL_BASE_URL\") or os.environ[\"DERIV_ONE_BASE_URL\"]"
                " + \"/internal/agents/invoke\",\n"
                "    json={\"target_agent_id\": \"<id or name>\", \"message\": \"<task>\"},\n"
                "    headers={\"X-Invoke-Context\": os.environ[\"DERIV_ONE_CTX_TOKEN\"]},\n"
                "    timeout=120,\n"
                ")\n"
                "print(resp.json()[\"reply\"])\n"
                "```\n\n"
                "Available agents you may call:\n" + agent_lines
            )

    system_prompt += _SLACK_QUERY_SKILL

    # Store secrets in ContextVar (dropped automatically when coroutine exits)
    _invoker_secrets.set(merged_secrets)

    # ── 5. LLM tool loop (no DB connection held) ──────────────────────────────
    llm_started = _time.time()
    _tool_audit: list[dict] = []

    llm_outcome = await _call_llm(
        system_prompt=system_prompt,
        user_content=user_content,
        agent=agent,
        history=history,
        skill_secrets=merged_secrets,
        tool_audit=_tool_audit,
    )
    reply_text = llm_outcome.text
    llm_duration_ms = int((_time.time() - llm_started) * 1000)

    # ── 6. Compaction check — ONLY if token budget exceeded (§5) ──────────────
    new_summary: Optional[str] = None
    new_cursor: Optional[str] = None

    total_chars = _history_chars(history) + len(user_text) + len(reply_text)
    if total_chars > _TOKEN_BUDGET_CHARS:
        logger.info(
            "Session %s: history chars=%d over budget=%d — running compaction",
            session_id[:50], total_chars, _TOKEN_BUDGET_CHARS,
        )
        new_summary, new_cursor = await _compact_history(history, session_summary)
        if new_summary:
            logger.info(
                "Compaction complete session=%s new_summary_len=%d",
                session_id[:50], len(new_summary),
            )

    # ── 7. OCC write + outbox co-commit (retry on conflict) ───────────────────
    # With ordering keys + EOD, conflicts should be extremely rare.
    write_result = "conflict"
    cur_version = orig_version
    for _attempt in range(_OCC_MAX_RETRIES):
        write_result = await loop.run_in_executor(
            None,
            lambda v=cur_version: _occ_write_sync(
                session_id=session_id,
                agent_id=agent_id,
                new_state={},           # state managed via summary/cursor
                orig_version=v,
                event_id=event_id or str(uuid.uuid4()),
                outbox_payload=reply_text,
                new_summary=new_summary,
                new_cursor=new_cursor,
            ),
        )
        if write_result == "ok":
            break
        logger.warning(
            "OCC conflict attempt=%d session=%s event=%s — re-reading and retrying",
            _attempt + 1, session_id[:50], (event_id or "")[:20],
        )
        # Re-read latest version and retry the write (no need to re-run LLM —
        # state={} and summary/cursor are append-only, so the write is safe to retry).
        refreshed = await loop.run_in_executor(None, _read_session_sync, session_id)
        cur_version = refreshed["version"]

    if write_result != "ok":
        logger.error(
            "OCC failed after %d attempts session=%s event=%s — nacking for redelivery",
            _OCC_MAX_RETRIES, session_id[:50], (event_id or "")[:20],
        )
        if placeholder_ts:
            await loop.run_in_executor(
                None, _update_slack_message_sync, xoxb, channel, placeholder_ts,
                "I encountered an internal error. Please try again.",
            )
        return False

    # ── 8. Deliver via chat.update ─────────────────────────────────────────────
    final_ts: Optional[str] = None
    if placeholder_ts:
        final_ts = await loop.run_in_executor(
            None, _update_slack_message_sync, xoxb, channel, placeholder_ts, reply_text
        )
    else:
        # No placeholder — fall back to postMessage
        await _post_slack_reply(xoxb=xoxb, channel=channel, thread_ts=thread_ts, text=reply_text)

    # ── 9. Mark outbox delivered ──────────────────────────────────────────────
    if event_id:
        await loop.run_in_executor(
            None, _mark_outbox_delivered_sync, event_id, final_ts
        )

    # ── Post-turn side effects (fire-and-forget; do not block ack) ─────────────
    from api_slack_events import _extract_and_store_memories
    from agents import record_agent_exchange

    trigger_type = "dm" if event.get("channel_type") == "im" else "mention"
    _secrets_loaded = sorted(merged_secrets.keys())

    async def _side_effects() -> None:
        try:
            await loop.run_in_executor(
                None,
                lambda: record_agent_exchange(
                    agent_id=agent.id,
                    user_message=user_message_audit,
                    assistant_message=reply_text,
                    invoker_email=sender_email,
                    invoker_slack_user_id=slack_user_id or None,
                    trigger_type=trigger_type,
                    channel_id=channel or None,
                    thread_ts=thread_ts or None,
                    duration_ms=llm_duration_ms,
                    secrets_loaded=_secrets_loaded,
                    tools_called=_tool_audit or None,
                ),
            )
            await _extract_and_store_memories(
                agent_id=agent_id,
                user_email=sender_email,
                user_msg=user_text,
                agent_msg=reply_text,
                channel_id=effective_channel_id_for_memory,
            )
        except Exception:
            logger.exception("Side-effects task failed for agent=%s event=%s", agent_id, event_id)

    asyncio.create_task(_side_effects())

    # Clear ContextVar (contents will be GC'd when token is no longer reachable)
    _invoker_secrets.set({})

    return True  # ack — §2: ack ONLY after outbox committed AND delivered


# ── Event handler ───────────────────────────────────────────────────

async def _handle_event(data: dict) -> None:
    """Filter and dispatch one inbound event.

    Acknowledgement is the queue's business: returning normally means the event
    is finished with, raising means it should be redelivered. See
    ``providers.queue`` for the contract.
    """
    from api_slack_events import _is_bot_joining_channel, _handle_channel_join, _is_ambient_channel

    try:
        if isinstance(data, (bytes, bytearray, str)):
            data = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("Malformed queue message: %s — discarding poison pill", exc)
        return

    if not isinstance(data, dict):
        logger.error("queue message is not a JSON object — discarding")
        return

    msg_type = data.get("type", "")
    if msg_type == "url_verification":
        return
    if msg_type != "event_callback":
        return

    slack_app_id: str = data.get("api_app_id", "").strip()
    event_id: str = (data.get("event_id") or data.get("slack_event_id") or "").strip()
    event: dict = data.get("event", {})
    event_type: str = event.get("type", "")
    channel_type: str = event.get("channel_type", "")
    channel: str = event.get("channel", "")

    # Filter bot non-mention events to prevent reply loops
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        if event_type != "app_mention":
            return

    if not slack_app_id:
        logger.warning("No api_app_id in the event — discarding")
        return

    loop = asyncio.get_running_loop()
    # Use the process-level TTL cache (§4) — avoids a DB round-trip on every message.
    agent = await loop.run_in_executor(None, _get_agent_cached_sync, slack_app_id)
    if not agent:
        logger.warning("No agent for slack_app_id=%s — discarding", slack_app_id)
        return

    # A plain channel message (not a mention, not a DM) only qualifies for
    # dispatch when the agent has opted into "ambient" mode for this specific
    # channel. A message that also mentions this bot is excluded here — that
    # same text already fires its own app_mention event, which is the one
    # that dispatches (mirrors the dedup guard in api_slack_events.slack_events).
    own_bot_user_id = str(getattr(agent, "slack_bot_user_id", "") or "").strip()
    mentions_this_bot = bool(
        own_bot_user_id and f"<@{own_bot_user_id}>" in (event.get("text") or "")
    )
    is_plain_channel_message = (
        event_type == "message"
        and channel_type != "im"
        and not event.get("subtype")
        and not mentions_this_bot
    )
    is_ambient_ok = (
        await loop.run_in_executor(None, _is_ambient_channel, agent, channel)
        if is_plain_channel_message
        else False
    )
    should_dispatch = (
        event_type == "app_mention"
        or (event_type == "message" and channel_type == "im")
        or event_type == "member_joined_channel"
        or (is_plain_channel_message and is_ambient_ok)
    )
    if not should_dispatch:
        return

    logger.info(
        "Dispatching event app=%s agent=%s event_id=%s event_type=%s",
        slack_app_id, agent.id, event_id or "(none)", event_type,
    )

    # Channel join: use existing handler — no session table interaction needed
    if event_type == "member_joined_channel":
        if not _is_bot_joining_channel(agent, event):
            return
        try:
            await _handle_channel_join(agent, event)
        except Exception:
            logger.exception("_handle_channel_join failed agent=%s", agent.id)
        return

    # In-flight dedup lease — _check_idempotency_sync inside _dispatch_warm only
    # catches a REDELIVERY of an already-committed turn (crash recovery); it
    # does nothing for two deliveries of the same event_id arriving close
    # together while the first is still mid-flight (e.g. old+new pod briefly
    # overlapping during a rolling deploy, or a slow webhook ack). Claim the
    # event up front so a concurrent duplicate is nacked instead of running
    # the full (multi-turn, multi-sandbox) pipeline twice in parallel.
    claimed_event = False
    if event_id:
        from agents import CLAIM_DONE, CLAIM_IN_PROGRESS, claim_slack_event, validate_slack_event_id

        if not validate_slack_event_id(event_id):
            logger.warning(
                "Unexpected event_id shape app=%s event_id=%s — skipping dedup lease",
                slack_app_id, event_id,
            )
        else:
            outcome = await loop.run_in_executor(None, claim_slack_event, event_id)
            if outcome == CLAIM_DONE:
                logger.info(
                    "Duplicate event ignored (already completed) app=%s event_id=%s",
                    slack_app_id, event_id,
                )
                return
            if outcome == CLAIM_IN_PROGRESS:
                logger.info(
                    "Event already in progress on another worker app=%s event_id=%s — nacking",
                    slack_app_id, event_id,
                )
                raise RuntimeError(
                    f"event_id={event_id} claimed by another worker — redeliver"
                )
            claimed_event = True

    # Normal event: coroutine-per-event dispatch (§3 concurrency model)
    try:
        should_ack = await _dispatch_warm(agent, event, event_id)
    except Exception:
        logger.exception(
            "Unhandled exception in _dispatch_warm app=%s event_id=%s",
            slack_app_id, event_id,
        )
        should_ack = False
    finally:
        if claimed_event:
            from agents import is_slack_event_done, mark_slack_event_done, release_slack_event_id

            if should_ack:
                await loop.run_in_executor(None, mark_slack_event_done, event_id)
            elif not await loop.run_in_executor(None, is_slack_event_done, event_id):
                # Only release if the reply was never actually posted — if it
                # was (housekeeping failed after), releasing would let a
                # redelivery double-post.
                await loop.run_in_executor(None, release_slack_event_id, event_id)

    # Housekeeping has run; now tell the queue what happened. Raising is how a
    # handler asks for redelivery (see providers.queue).
    if not should_ack:
        raise RuntimeError(
            f"dispatch did not complete for event_id={event_id} — redeliver"
        )


# ── Pod-level startup tasks (run once per pod, §4) ───────────────────────────

async def _startup() -> None:
    """Initialise pod-wide shared resources after the event loop starts."""
    # Redis skill-invalidation subscriber (pick up edits without pod restart)
    try:
        from skills_invalidate import run_subscriber as _run_skills_subscriber
        asyncio.create_task(_run_skills_subscriber())
        logger.info("Skills invalidation subscriber started")
    except Exception:
        logger.exception("Could not start skills invalidation subscriber — 60s poll fallback active")

    # Pre-warm IAM cache for all active grant holders
    async def _warm_iam_cache() -> None:
        try:
            from database import SessionLocal
            from sqlalchemy import text as _t
            from org_directory import get_employee_by_email as _iam_get
            db = SessionLocal()
            try:
                rows = db.execute(
                    _t("SELECT DISTINCT user_email FROM agent_access_grants WHERE status='approved'")
                ).fetchall()
                emails = [r[0] for r in rows if r[0]]
            finally:
                db.close()
            for email in emails:
                try:
                    await _iam_get(email)
                except Exception:
                    pass
            logger.info("IAM cache pre-warmed for %d active grant holders", len(emails))
        except Exception:
            logger.exception("IAM cache pre-warm failed — stale fallback will populate lazily")

    asyncio.create_task(_warm_iam_cache())

    # Periodic 60-second the blob store skill sync (fallback when Redis subscriber is down)
    async def _periodic_skills_sync() -> None:
        while not _shutdown_requested:
            await asyncio.sleep(60)
            try:
                from skills_store import pull_from_store
                pull_from_store()
            except Exception:
                logger.exception("Periodic skills sync failed")

    asyncio.create_task(_periodic_skills_sync())

    # Outbox sweeper — recovers replies committed but not delivered (§4, §6)
    asyncio.create_task(_sweeper_loop())

    logger.info(
        "warm_worker startup complete — max_concurrent=%d token_budget_chars=%d sweeper_interval=%ds",
        MAX_CONCURRENT_MESSAGES,
        _TOKEN_BUDGET_CHARS,
        int(_SWEEPER_INTERVAL_S),
    )


# ── Main consumer loop ────────────────────────────────────────────────────────

async def _run_warm_worker() -> None:
    """Consume events from the queue and process them as coroutines."""
    await _startup()

    queue = get_message_queue()
    # Cap in-flight work rather than letting arrival rate set concurrency.
    gate = asyncio.Semaphore(MAX_CONCURRENT_MESSAGES)

    async def _handle(payload: dict) -> None:
        if _shutdown_requested:
            raise RuntimeError("shutting down — redeliver this event")
        async with gate:
            await _handle_event(payload)

    logger.info(
        "Starting warm worker topic=%s max_concurrent=%d",
        SLACK_EVENTS_TOPIC,
        MAX_CONCURRENT_MESSAGES,
    )

    await queue.subscribe(SLACK_EVENTS_TOPIC, _handle)
    await queue.start()
    try:
        while not _shutdown_requested:
            await asyncio.sleep(1)
    finally:
        await queue.stop()
        logger.info("Warm worker queue subscription closed")


def main() -> None:
    global _loop

    logger.info("warm_worker starting up")

    from database import init_db
    from skill_submission_models import init_skill_submission_tables
    init_db()
    init_skill_submission_tables()

    try:
        from skills_store import pull_from_store
        pull_from_store()
        logger.info("Skills pull complete")
    except Exception:
        logger.exception("Skills pull failed — continuing with seed ACL")

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    try:
        _loop.run_until_complete(_run_warm_worker())
    finally:
        _loop.close()

    logger.info("warm_worker stopped")


if __name__ == "__main__":
    main()
