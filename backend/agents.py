"""SQLAlchemy models + helpers for agent registration (Phase A).

Storage shape is trimmed to what
Phase A actually exercises. Phase-B/C-only columns (sharing, orchestration ACL,
a message queue) exist with empty defaults so wiring them later is a single
migration-free deployment.

Token material is **never** stored here — only opaque references that the
secret vault can resolve. See `backend/secret_vault.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text as sa_text,
)
from sqlalchemy.orm import relationship

from database import Base, OWNER_LEGACY_SENTINEL, SessionLocal, _uuid
from app_config import RESOURCE_PREFIX

logger = logging.getLogger(__name__)


# ── Constants / validators ────────────────────────────────────────────────────

AGENT_STATUSES = ("registered", "active", "paused", "decommissioned")
AGENT_TYPES = ("interactive", "scheduled", "orchestrator", "platform")
TRIGGER_MODES = ("mention", "dm", "both")

# Slack token shapes are documented at api.slack.com/authentication/token-types.
# We accept the well-formed prefix + payload; the real validation happens when
# the activation step calls Slack's auth.test (Phase B).
XOXB_RE = re.compile(r"^xoxb-[A-Za-z0-9\-]{10,512}$")
XAPP_RE = re.compile(r"^xapp-[A-Za-z0-9\-]{10,512}$")

# Defensive caps for free-form configuration values.
MAX_NAME = 120
MAX_DESCRIPTION = 1000
MAX_USECASE_DESCRIPTION = 1000
MAX_SOUL = 8000
MAX_AUTOMATION_REPORT_PROMPT = 4000
MAX_CHANNELS = 50
MAX_SKILLS = 100

# Slack app manifest limits (auto-create / OAuth registration path).
SLACK_MAX_APP_NAME = 35
SLACK_MAX_BOT_DISPLAY_NAME = 80
SLACK_BOT_DISPLAY_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-_.]*[a-z0-9])?$")
SLACK_MIN_LONG_DESCRIPTION = 175  # Slack: must be > 174
SLACK_MAX_LONG_DESCRIPTION = 4000
SLACK_AVATAR_MAX_BYTES = 2 * 1024 * 1024

_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_\-#]{1,80}$")  # `#name`, name, or `C0123…`
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,80}$")
_SLACK_APP_ID_RE = re.compile(r"^A[A-Z0-9]{6,20}$")  # `A0ALU7541JM`
_AGENT_DESC_PIC_RE = re.compile(r"(?i)PIC:\s+@?[\w.\-]+")
_AGENT_DESC_TEAM_RE = re.compile(
    r"(?i)(?:Team Incharge|Owning Team|Team):\s+@?[\w.\-]+"
)


def validate_agent_description(description: Optional[str]) -> Optional[str]:
    """Return a user-facing error message, or None when valid."""
    text = (description or "").strip()
    if not text:
        return "Description is required."
    has_pic = bool(_AGENT_DESC_PIC_RE.search(text))
    has_team = bool(_AGENT_DESC_TEAM_RE.search(text))
    if not has_pic and not has_team:
        return (
            "Description must include a PIC contact (e.g. PIC: @username) "
            "and an owning team (e.g. Team Incharge: @team-name)."
        )
    if not has_pic:
        return "Description must include a PIC contact (e.g. PIC: @username)."
    if not has_team:
        return "Description must include an owning team (e.g. Team Incharge: @team-name)."
    remaining = _AGENT_DESC_PIC_RE.sub("", text)
    remaining = _AGENT_DESC_TEAM_RE.sub("", remaining).strip()
    if not remaining:
        return (
            "Description must explain what the agent does, "
            "not just PIC and Team contacts."
        )
    return None


def validate_usecase_description(usecase_description: Optional[str]) -> Optional[str]:
    """Return a user-facing error message, or None when valid."""
    text = (usecase_description or "").strip()
    if not text:
        return "Usecase description is required."
    return None


def validate_slack_agent_name(name: Optional[str]) -> Optional[str]:
    """Slack OAuth auto-create: app name (max 35) + bot display charset."""
    if not isinstance(name, str) or not name.strip():
        return "Agent name is required."
    trimmed = name.strip()
    n = len(trimmed)
    if n > SLACK_MAX_APP_NAME:
        return (
            f"Agent name must be 35 characters or fewer for Slack app creation "
            f"(currently {n}/{SLACK_MAX_APP_NAME}). Shorten the name and try again."
        )
    bot_name = trimmed.lower()
    if not SLACK_BOT_DISPLAY_NAME_RE.match(bot_name):
        return (
            "Slack bot names can only contain lowercase letters, numbers, hyphens, "
            "underscores, and periods — no spaces or special characters. "
            "Example: treasury-ops-bot. Update the agent name to match."
        )
    return None


def validate_slack_long_description(description: Optional[str]) -> Optional[str]:
    """Slack OAuth auto-create: Description maps to display_information.long_description."""
    text = (description or "").strip()
    n = len(text)
    if n < SLACK_MIN_LONG_DESCRIPTION:
        return (
            f"Description must be at least {SLACK_MIN_LONG_DESCRIPTION} characters for "
            f"Slack app creation (currently {n}/{SLACK_MIN_LONG_DESCRIPTION}). "
            "Expand what the agent does, and keep your PIC and Team Incharge contacts "
            "in this field."
        )
    if n > SLACK_MAX_LONG_DESCRIPTION:
        return (
            f"Description must be at most {SLACK_MAX_LONG_DESCRIPTION} characters for Slack "
            f"(currently {n}/{SLACK_MAX_LONG_DESCRIPTION}). Shorten the Description field."
        )
    return None


def normalize_slack_manifest_name(agent_name: str) -> str:
    """Return Slack-safe app/bot display name. Caller must validate first."""
    return agent_name.strip().lower().replace('"', "").replace("'", "")


# Agent display names: letters, digits, spaces, hyphen, underscore, period, apostrophe.
# Rejects emojis and special punctuation (!, @, #, $, %, &, *, (, ), etc.).
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 _.\-']*[A-Za-z0-9])?$")
_AGENT_NAME_INVALID_MSG = (
    "Agent name cannot contain emojis or special characters "
    "(!, @, #, $, %, &, *, (, )). "
    "Use letters, numbers, spaces, hyphens, or underscores only."
)


def validate_xoxb(token: str) -> bool:
    return isinstance(token, str) and bool(XOXB_RE.match(token))


def validate_xapp(token: str) -> bool:
    return isinstance(token, str) and bool(XAPP_RE.match(token))


def validate_agent_name(name: str) -> str | None:
    """Return an error message if the agent name is invalid, else None.

    Allowed: letters, digits, spaces, hyphen, underscore, period, apostrophe.
    Must start and end with a letter or digit. Emojis and special punctuation
    are rejected.
    """
    if not isinstance(name, str):
        return "Agent name is required."
    trimmed = name.strip()
    if not trimmed:
        return "Agent name is required."
    if len(trimmed) > MAX_NAME:
        return f"Agent name must be at most {MAX_NAME} characters."
    if not _AGENT_NAME_RE.match(trimmed):
        return _AGENT_NAME_INVALID_MSG
    return None


def sanitize_skill_ids(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        if not isinstance(v, str):
            continue
        s = v.strip().lower()
        if not s or s in seen or not _SKILL_ID_RE.match(s):
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_SKILLS:
            break
    return out


def sanitize_channels(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s:
            continue
        if not _CHANNEL_RE.match(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_CHANNELS:
            break
    return out


def validate_slack_app_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SLACK_APP_ID_RE.match(value))


# ── Models ────────────────────────────────────────────────────────────────────

class Agent(Base):
    """User-owned Slack-bot agent registration.

    Status lifecycle (server-side state machine):
        registered → active → paused → active → decommissioned
                  ↘ decommissioned (any non-decommissioned can decommission)
    """

    __tablename__ = "agents"

    id = Column(String, primary_key=True, default=_uuid)

    # Identity
    name = Column(String(MAX_NAME), nullable=False)
    description = Column(String(MAX_DESCRIPTION), nullable=True)
    usecase_description = Column(Text, nullable=True)
    avatar_url = Column(String(2048), nullable=True)

    # Creator / tenant (Cloudflare Access JWT subject)
    owner_sub = Column(String, nullable=False, index=True, default=lambda: OWNER_LEGACY_SENTINEL)
    creator_email = Column(String(320), nullable=False, default="")
    creator_department = Column(String(120), nullable=False, default="")

    # Phase-B+ collaboration columns (kept empty in Phase A — JSON-encoded arrays)
    collaborator_ids = Column(Text, nullable=False, default="[]")
    shared_user_ids = Column(Text, nullable=False, default="[]")
    shared_group_ids = Column(Text, nullable=False, default="[]")

    # Slack identity
    slack_app_id = Column(String(40), nullable=True, unique=True)
    slack_bot_user_id = Column(String(40), nullable=True)
    slack_team_id = Column(String(20), nullable=True)          # workspace team_id (auto-filled for OAuth-created apps)
    slack_client_id = Column(String(255), nullable=True)       # non-secret OAuth client_id stored for install resumption
    xoxb_secret_ref = Column(String(512), nullable=True)
    xapp_secret_ref = Column(String(512), nullable=True)
    client_secret_ref = Column(String(512), nullable=True)     # for OAuth-created apps — needed for future rotation / install resumption
    signing_secret_ref = Column(String(512), nullable=True)    # Slack request signing secret for HTTP Events verification
    # Optional BYOK gateway key (SM ref). NULL → use platform MODEL_GATEWAY_API_KEY.
    gateway_api_key_ref = Column(String(512), nullable=True)

    # How this agent was registered (manual | auto_oauth)
    creation_method = Column(String(16), nullable=False, default="manual")
    # For manual/guided agents: set True once serviceaccount was
    # confirmed as a collaborator on the self-created Slack app, granting the
    # platform's App Configuration Token manifest access (name/description/
    # icon sync). Always False for auto_oauth agents (they use creation_method
    # instead — the platform created the app itself and always has access).
    manifest_access_verified = Column(Boolean, nullable=False, default=False)

    # Configuration
    skill_ids = Column(Text, nullable=False, default="[]")  # JSON array of slugs
    soul_text = Column(Text, nullable=True)
    channel_ids = Column(Text, nullable=False, default="[]")  # JSON array
    trigger_mode = Column(String(16), nullable=False, default="mention")
    agent_type = Column(String(16), nullable=False, default="interactive")
    # Canonical model slug for the main reply path, or NULL → env MODEL.
    # Allowlist: gemini-3.5-flash-lite | grok-4.5 | deepseek-v4-flash |
    llm_model = Column(String(80), nullable=True, default=None)
    # Per-agent tool-turn budget override. NULL → use the computed default.
    # Set to 0 to disable run_python entirely (pure text-reply agents).
    max_tool_turns_override = Column(Integer, nullable=True, default=None)
    # When True, approved skill artifacts are offered via call_artifact at runtime.
    artifacts_enabled = Column(Boolean, nullable=False, default=False)
    # When True, per-turn skill routing selects a subset of attached skills via skill_index_text.
    skill_routing_enabled = Column(Boolean, nullable=False, default=False)
    skill_index_text = Column(Text, nullable=True)
    skill_index_generated_at = Column(DateTime, nullable=True)
    skill_index_source_skill_ids = Column(Text, nullable=True)  # JSON array of slugs at generation time

    # Guardrails: list of {id, type, enabled[, text]} dicts.
    # Preset items reference GUARDRAIL_PRESETS by id; custom items carry a free-text rule.
    guardrails = Column(JSON, nullable=True, default=list)

    # Approved Slack bot scopes (JSON array of scope strings) for auto_oauth
    # agents. The AI-reviewed, user-approved set written into the app manifest.
    # NULL for manual/guided agents (which use the legacy default scope set).
    bot_scopes = Column(Text, nullable=True)

    # Scopes added to the manifest since last workspace reinstall. These are
    # present in the Slack app manifest but not yet granted by Slack (the bot
    # owner must reinstall the app to activate them).
    pending_scopes = Column(Text, nullable=True)

    # Phase-B/C orchestration columns (empty in Phase A)
    can_invoke = Column(Text, nullable=False, default="[]")
    can_be_invoked_by = Column(Text, nullable=False, default="[]")
    event_subscriptions = Column(Text, nullable=False, default="[]")
    event_publications = Column(Text, nullable=False, default="[]")

    # Platform agent — singleton flag; at most one row has this set to True.
    # channel_fallback_behavior: "default" → use global config in unconfigured channels
    #                            "silent"  → drop events in unconfigured channels (platform agent only)
    is_platform_agent = Column(Boolean, nullable=False, default=False)
    channel_fallback_behavior = Column(String(16), nullable=False, default="default")

    # Lifecycle
    status = Column(String(20), nullable=False, default="registered", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    paused_at = Column(DateTime, nullable=True)
    decommissioned_at = Column(DateTime, nullable=True)

    # Usage counters (incremented by Phase B runtime)
    last_active_at = Column(DateTime, nullable=True)
    session_count = Column(Integer, default=0, nullable=False)
    total_messages = Column(Integer, default=0, nullable=False)

    scheduled_tasks = relationship(
        "AgentScheduledTask",
        back_populates="agent",
        cascade="all, delete-orphan",
    )


class AgentCollaborator(Base):
    """Co-owner of an agent — has the same access as the original owner.

    Collaborators can view/edit the agent, access interaction logs, memories,
    scheduled tasks, tokens, and manage other collaborators.  They cannot
    transfer ownership or decommission the agent (owner-only actions).

    Uniqueness is enforced on (agent_id, user_email) so the same person cannot
    be added twice.
    """

    __tablename__ = "agent_collaborators"
    __table_args__ = (UniqueConstraint("agent_id", "user_email", name="uq_agent_collaborators"),)

    id = Column(String, primary_key=True, default=_uuid)
    agent_id = Column(
        String,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_email = Column(String(320), nullable=False, index=True)
    user_name = Column(String(255), nullable=True)
    added_by_email = Column(String(320), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def collaborator_to_dict(c: "AgentCollaborator") -> dict:
    return {
        "id": c.id,
        "agent_id": c.agent_id,
        "user_email": c.user_email,
        "user_name": c.user_name,
        "added_by_email": c.added_by_email,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


class AgentScheduledTask(Base):
    """Cron-driven prompt for an agent, executed by the scheduler worker."""

    __tablename__ = "agent_scheduled_tasks"

    id = Column(String, primary_key=True, default=_uuid)
    agent_id = Column(
        String,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    prompt = Column(Text, nullable=False)
    schedule = Column(String(120), nullable=False)  # 5-field cron
    timezone = Column(String(64), nullable=False, default="Asia/Dubai")
    output_channel = Column(String(120), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    report_enabled = Column(Boolean, default=False, nullable=False)
    report_prompt = Column(Text, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    last_run_status = Column(String(20), nullable=True)
    last_run_error = Column(Text, nullable=True)
    run_count = Column(Integer, default=0, nullable=False)
    author_sub = Column(String, nullable=True)
    author_email = Column(String(320), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    agent = relationship("Agent", back_populates="scheduled_tasks")


SLACK_OAUTH_SESSION_TTL_MINUTES = 30
SLACK_OAUTH_STEPS = ("config", "install", "pending_approval", "done", "error")


class SlackOAuthSession(Base):
    """Transient record tracking a Slack app auto-creation OAuth flow.

    Created when the user clicks "Create Slack App for me" and deleted once the
    Agent row is committed.  The `id` field is also used as the OAuth `state`
    parameter in both legs, providing CSRF protection.

    Sensitive credential material (client_secret, config_token) is NEVER stored
    here — only opaque vault references or non-secret metadata.
    """

    __tablename__ = "slack_oauth_sessions"

    id = Column(String, primary_key=True, default=_uuid)  # UUID, doubles as OAuth state
    owner_sub = Column(String, nullable=False, index=True)
    step = Column(String(20), nullable=False, default="config")  # see SLACK_OAUTH_STEPS
    agent_draft = Column(Text)           # JSON of pending AgentDraft fields (no tokens)
    slack_app_id = Column(String(40))      # filled after apps.manifest.create
    client_id = Column(String(255))        # filled after apps.manifest.create (non-secret)
    client_secret_ref = Column(String(512))# vault reference for the new app's client_secret
    signing_secret_ref = Column(String(512))# vault reference for the new app's signing_secret (temporary)
    agent_id = Column(String)              # filled after Agent row is created (step=done)
    error_msg = Column(String(512))        # safe user-facing error (step=error)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class AgentDraftRow(Base):
    """A single in-progress agent registration draft for a user (auto-save).

    One row per owner_sub (upserted on save). Stores the form fields the user
    has filled in so they can leave the registration page and resume later.
    No credentials — drafts are form state only (name, description, skills,
    soul, channels, trigger, guardrails, token mode).
    """

    __tablename__ = "agent_drafts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_sub = Column(String, nullable=False, unique=True, index=True)
    draft = Column(Text, nullable=False)  # JSON of form state
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False, onupdate=datetime.utcnow)


# TTL for an in-progress scope report — matches the OAuth session window so a
# report created during analysis survives until the user approves & starts.
SLACK_SCOPE_REPORT_TTL_MINUTES = 30


class SlackScopeReport(Base):
    """Transient record holding the AI scope analysis + user-approved scope set.

    Created when the user runs "Analyze scopes" in the auto-create flow and read
    back when they approve and POST ``/api/slack-oauth/start``. The ``id`` is the
    ``report_id`` shared with the frontend. Rows are pruned after
    ``SLACK_SCOPE_REPORT_TTL_MINUTES`` alongside OAuth sessions.

    Only the agent context needed to (re)run inference is stored here — no
    credentials. ``approved_scopes`` is the user's current selection (base is
    force-included server-side on read).
    """

    __tablename__ = "slack_scope_reports"

    id = Column(String, primary_key=True, default=_uuid)
    owner_sub = Column(String, nullable=False, index=True)
    # Agent context used for (re)adjudication. JSON of {name, description,
    # usecase_description, soul_text, skill_ids, trigger_mode}.
    agent_context = Column(Text, nullable=False)
    # JSON array of AI proposals: [{scope, reason, confidence, locked}].
    proposals = Column(Text, nullable=False, default="[]")
    # JSON array of the user's currently approved scope strings (conditional +
    # AI-allowed requests; base is force-included at finalize time).
    approved_scopes = Column(Text, nullable=False, default="[]")
    notes = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)


class AgentInteractionLog(Base):
    """Persisted user query + agent response for owner/admin review."""

    __tablename__ = "agent_interaction_logs"

    id = Column(String, primary_key=True, default=_uuid)
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(String, nullable=False, index=True)
    invoker_email = Column(String(320), nullable=False, index=True)
    invoker_slack_user_id = Column(String(40), nullable=True)
    trigger_type = Column(String(20), nullable=False)  # mention | dm | a2a | chat
    channel_id = Column(String(80), nullable=True)
    thread_ts = Column(String(40), nullable=True)
    source_agent_id = Column(String, nullable=True)
    source_agent_name = Column(String(120), nullable=True)
    user_message = Column(Text, nullable=False)
    assistant_message = Column(Text, nullable=False)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Audit columns added for per-invocation forensics (nullable for backward compat)
    secrets_loaded = Column(Text, nullable=True)   # JSON list of env-key names (no values)
    # JSON list of tool audit entries. Slack path may include truncated
    # code_redacted / output_redacted when AGENT_TOOL_AUDIT_BODIES is on.
    tools_called = Column(Text, nullable=True)
    routing_metadata = Column(Text, nullable=True)  # JSON skill-routing audit (selected slugs, mode, etc.)
    failure_kind = Column(String(32), nullable=True, index=True)
    failure_detail = Column(Text, nullable=True)
    resolved_model = Column(String(120), nullable=True)


class AgentInteractionTrace(Base):
    """The timeline behind one interaction: model calls and the system prompt.

    Tool calls stay in AgentInteractionLog.tools_called (stamped with t_ms).
    See agent_trace.py.
    """

    __tablename__ = "agent_interaction_traces"

    interaction_id = Column(String, primary_key=True)
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    model_calls = Column(Text, nullable=False, default="[]")  # JSON list, see AgentTrace.note_model_call
    system_prompt = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AgentThreadSkillRouting(Base):
    """Last skill-routing selection for a Slack thread (follow-up hint)."""

    __tablename__ = "agent_thread_skill_routing"

    id = Column(String, primary_key=True, default=_uuid)
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    channel_id = Column(String(80), nullable=False)
    thread_ts = Column(String(40), nullable=False)
    selected_slugs = Column(Text, nullable=False, default="[]")
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SlackEventDedup(Base):
    """Tracks Slack Events API ``event_id`` values to suppress duplicates *and*
    to recover events whose worker died mid-dispatch.

    Slack delivers at-least-once; the Cloud Run relay and Slack retries can both
    forward the same envelope.  A row is a *lease*, not a tombstone:

    - ``status="in_progress"`` with ``lease_expires_at`` in the future — a worker
      is actively handling this event; concurrent deliveries must not process it.
    - ``status="in_progress"`` with ``lease_expires_at`` in the past — the owning
      worker died before completing (OOM / SIGKILL / eviction); the lease is
      *orphaned* and the next delivery reclaims and reprocesses it.
    - ``status="done"`` — the event ran to completion; later deliveries are true
      duplicates and are ack-dropped.

    This is what prevents an abrupt process death from permanently suppressing
    redelivery (a claimed-but-never-completed row would otherwise look identical
    to a completed one).  Rows older than 24h are purged opportunistically.
    """

    __tablename__ = "slack_event_dedup"

    event_id = Column(String(32), primary_key=True)
    # "in_progress" (leased) | "done" (completed). New ORM inserts are always
    # in_progress; the DB-level default only backfills legacy rows on migration.
    status = Column(String(12), nullable=False, default="in_progress")
    # When the current in_progress lease expires. NULL for done/legacy rows.
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    seen_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SlackEphemeralReply(Base):
    """Last ephemeral (only-visible-to-user) agent reply in a thread.

    Slack does not include ephemeral messages in ``conversations.history``, so we
    store the text briefly so a follow-up "post that publicly" turn can repost it.
    """

    __tablename__ = "slack_ephemeral_replies"

    id = Column(String(36), primary_key=True, default=_uuid)
    agent_id = Column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = Column(String(80), nullable=False, index=True)
    thread_ts = Column(String(40), nullable=False)
    slack_user_id = Column(String(40), nullable=False, index=True)
    reply_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SlackBotLoopGuard(Base):
    """Sliding-window counter that rate-limits *bot-authored* Slack invocations.

    Two agents that mutually authorise each other's bots (or an agent that
    authorises its own bot) can enter an infinite reply loop: bot A DMs agent B,
    B replies, A treats the reply as a new inbound event and replies again, ad
    infinitum.  Each reply is a distinct ``event_id`` so the dedup table does not
    catch it.  This guard caps how many bot-originated invocations a given
    ``(agent_id, sender_slack_user_id)`` pair may make within a rolling window;
    bursts beyond the cap are dropped so the loop cannot sustain itself.
    """

    __tablename__ = "slack_bot_loop_guard"

    agent_id = Column(String(36), primary_key=True)
    sender_slack_user_id = Column(String(40), primary_key=True)
    window_start = Column(DateTime, nullable=False, default=datetime.utcnow)
    count = Column(Integer, nullable=False, default=0)


class AgentMemory(Base):
    """Persisted memory facts for an agent, optionally scoped to a specific user.

    scope="agent"  — general facts about how this agent should behave or context
                     it should remember across all users.
    scope="user"   — facts specific to a particular invoker (stored against their
                     email so they are retrieved when that user interacts again).

    Rows are upserted automatically after each interaction (source="auto") and can
    also be created / edited manually by the agent owner or an admin (source="manual").
    """

    __tablename__ = "agent_memories"

    id = Column(String(36), primary_key=True, default=_uuid)
    agent_id = Column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scope = Column(String(8), nullable=False)          # "agent" | "user" | "channel"
    user_email = Column(String(320), nullable=True, index=True)  # null when scope="agent"
    channel_id = Column(String(32), nullable=True, index=True)   # set when scope="channel"
    key = Column(String(200), nullable=False)
    value = Column(Text, nullable=False)               # capped at 2 KB in application code
    source = Column(String(8), nullable=False, default="auto")  # "auto" | "manual"
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)


_SLACK_EVENT_ID_RE = re.compile(r"^Ev[A-Z0-9]{8,24}$")
_SLACK_EVENT_DEDUP_TTL = timedelta(hours=24)
# Lease held by a worker while it dispatches an event. Must exceed the longest
# single dispatch (AGENT_WORKER_TIMEOUT, default 720s) plus margin so a healthy
# long-running dispatch never has its lease expire out from under it. An
# orphaned lease (owner died) is reclaimed at most this long after the crash.
_SLACK_EVENT_LEASE_TTL = timedelta(
    seconds=int(os.environ.get("AGENT_WORKER_TIMEOUT", "720")) + 60
)

# Outcomes of claim_slack_event().
CLAIM_NEW = "claimed"              # we own the lease now — dispatch
CLAIM_IN_PROGRESS = "in_progress"  # another live worker owns it — do not process
CLAIM_DONE = "done"                # already completed — ack-drop as a duplicate
_INTERACTION_CONTENT_MAX = 16_000

# ── Agent memory helpers ──────────────────────────────────────────────────────

_MEMORY_VALUE_MAX = 2_000   # chars stored per memory value (override via AGENT_MEMORY_VALUE_MAX)
_MEMORY_KEY_MAX = 200       # chars for a memory key
_MEMORY_AGENT_LIMIT = 30    # default inject cap; see load_agent_memories + AGENT_MEMORY_INJECT_* env
_MEMORY_USER_LIMIT = 20


def _utcnow_mem() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def load_agent_memories(
    agent_id: str,
    user_email: str | None = None,
    channel_id: str | None = None,
) -> list[AgentMemory]:
    """Return agent-scope memories plus (optionally) user-scope and channel-scope memories.

    When channel_id is provided and memory_scope="channel" is in effect, only
    channel-scoped memories for that channel are returned (instead of agent-scope).
    Expired memories are excluded.  Never raises — returns [] on any error.
    """
    if not agent_id:
        return []
    try:
        from memory_store.policy import inject_limit_for_scope, load_agent_memory_policy

        policy = load_agent_memory_policy()
        agent_limit = inject_limit_for_scope(policy, "agent")
        user_limit = inject_limit_for_scope(policy, "user")
        channel_limit = inject_limit_for_scope(policy, "channel")
    except Exception:
        agent_limit = _MEMORY_AGENT_LIMIT
        user_limit = _MEMORY_USER_LIMIT
        channel_limit = _MEMORY_AGENT_LIMIT
        value_max = _MEMORY_VALUE_MAX
        key_max = _MEMORY_KEY_MAX
    db = SessionLocal()
    try:
        now = _utcnow_mem()
        base = (
            db.query(AgentMemory)
            .filter(AgentMemory.agent_id == agent_id)
            .filter(
                (AgentMemory.expires_at == None) | (AgentMemory.expires_at > now)  # noqa: E711
            )
        )
        # Always load agent-scope memories (global knowledge)
        agent_scope = (
            base.filter(AgentMemory.scope == "agent")
            .order_by(AgentMemory.updated_at.desc())
            .limit(agent_limit)
            .all()
        )
        user_scope: list[AgentMemory] = []
        if user_email:
            user_scope = (
                base.filter(
                    AgentMemory.scope == "user",
                    AgentMemory.user_email == user_email.strip().lower(),
                )
                .order_by(AgentMemory.updated_at.desc())
                .limit(user_limit)
                .all()
            )
        channel_scope: list[AgentMemory] = []
        if channel_id:
            channel_scope = (
                base.filter(
                    AgentMemory.scope == "channel",
                    AgentMemory.channel_id == channel_id,
                )
                .order_by(AgentMemory.updated_at.desc())
                .limit(channel_limit)
                .all()
            )
        return list(agent_scope) + list(user_scope) + list(channel_scope)
    except Exception:
        logger.exception("load_agent_memories failed for agent_id=%s", agent_id)
        return []
    finally:
        db.close()


def upsert_agent_memory(
    *,
    agent_id: str,
    scope: str,
    key: str,
    value: str,
    user_email: str | None = None,
    channel_id: str | None = None,
    source: str = "auto",
    expires_at: datetime | None = None,
) -> AgentMemory | None:
    """Insert or update a memory fact.  Upsert key is (agent_id, scope, user_email, channel_id, key).

    scope="channel" requires channel_id.  Never raises.  Returns saved row or None.
    """
    if not agent_id or not key or not value:
        return None
    if scope not in ("agent", "user", "channel"):
        return None
    try:
        from memory_store.policy import load_agent_memory_policy

        _pol = load_agent_memory_policy()
        key = key.strip()[: _pol.key_max_chars]
        value = value.strip()[: _pol.value_max_chars]
    except Exception:
        key = key.strip()[:_MEMORY_KEY_MAX]
        value = value.strip()[:_MEMORY_VALUE_MAX]
    email = (user_email or "").strip().lower() or None
    ch_id = (channel_id or "").strip() or None
    if scope == "user" and not email:
        return None
    if scope == "channel" and not ch_id:
        return None

    if source == "auto" and expires_at is None:
        try:
            from memory_store.maintenance import default_auto_expires_at
            from memory_store.policy import load_agent_memory_policy

            expires_at = default_auto_expires_at(load_agent_memory_policy())
        except Exception:
            pass

    db = SessionLocal()
    try:
        existing = (
            db.query(AgentMemory)
            .filter(
                AgentMemory.agent_id == agent_id,
                AgentMemory.scope == scope,
                AgentMemory.key == key,
                AgentMemory.user_email == email,
                AgentMemory.channel_id == ch_id,
            )
            .first()
        )
        now = _utcnow_mem()
        if existing:
            existing.value = value
            existing.source = source
            existing.updated_at = now
            if expires_at is not None:
                existing.expires_at = expires_at
            db.commit()
            db.refresh(existing)
            return existing
        mem = AgentMemory(
            agent_id=agent_id,
            scope=scope,
            key=key,
            value=value,
            user_email=email,
            channel_id=ch_id,
            source=source,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        db.add(mem)
        db.commit()
        db.refresh(mem)
        return mem
    except Exception:
        logger.exception(
            "upsert_agent_memory failed agent_id=%s scope=%s key=%s",
            agent_id, scope, key,
        )
        db.rollback()
        return None
    finally:
        db.close()


def memory_to_dict(m: AgentMemory) -> dict:
    return {
        "id": m.id,
        "agent_id": m.agent_id,
        "scope": m.scope,
        "user_email": m.user_email,
        "channel_id": m.channel_id,
        "key": m.key,
        "value": m.value,
        "source": m.source,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        "expires_at": m.expires_at.isoformat() if m.expires_at else None,
    }


def validate_slack_event_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SLACK_EVENT_ID_RE.match(value))


def claim_slack_event(event_id: str, lease_seconds: int | None = None) -> str:
    """Claim an event for processing under a time-bounded lease.

    Returns one of:
      ``CLAIM_NEW``         — this worker now owns the lease; proceed to dispatch.
      ``CLAIM_IN_PROGRESS`` — another worker holds a *live* lease; do NOT process
                              (ask for redelivery once the lease frees).
      ``CLAIM_DONE``        — the event already ran to completion; ack-drop it.

    An ``in_progress`` row whose lease has expired is treated as *orphaned* (the
    previous owner died mid-dispatch) and is atomically reclaimed here — this is
    what stops a crash from permanently suppressing redelivery. The reclaim uses
    a conditional UPDATE guarded on the expired lease so two simultaneous
    redeliveries can never both take ownership.
    """
    from sqlalchemy.exc import IntegrityError

    from database import SessionLocal

    ttl = timedelta(seconds=lease_seconds) if lease_seconds is not None else _SLACK_EVENT_LEASE_TTL
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    lease = now + ttl

    def _insert_new(db) -> bool:
        """Try to insert a fresh in_progress lease. Returns True on success."""
        try:
            db.add(
                SlackEventDedup(
                    event_id=event_id,
                    status="in_progress",
                    lease_expires_at=lease,
                    seen_at=now,
                )
            )
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False

    db = SessionLocal()
    try:
        # Fast path: no row yet → own it.
        if _insert_new(db):
            return CLAIM_NEW

        # A row exists — completed, actively owned, or orphaned.
        row = db.get(SlackEventDedup, event_id)
        if row is None:
            # Raced with a release() delete between the failed insert and here —
            # try once more to own it fresh.
            return CLAIM_NEW if _insert_new(db) else CLAIM_IN_PROGRESS

        if row.status == "done":
            return CLAIM_DONE

        # status == "in_progress": reclaim only if the lease has expired.
        updated = (
            db.query(SlackEventDedup)
            .filter(
                SlackEventDedup.event_id == event_id,
                SlackEventDedup.status == "in_progress",
                SlackEventDedup.lease_expires_at < now,
            )
            .update(
                {"lease_expires_at": lease, "seen_at": now},
                synchronize_session=False,
            )
        )
        db.commit()
        return CLAIM_NEW if updated else CLAIM_IN_PROGRESS
    finally:
        db.close()


def refresh_slack_event_lease(event_id: str, lease_seconds: int | None = None) -> None:
    """Extend an in_progress lease while its dispatch is still running.

    Called periodically (alongside the lease heartbeat) so a
    healthy long dispatch keeps its lease alive; when the worker dies the
    heartbeat stops and the lease lapses, freeing the event for reclaim.
    """
    from database import SessionLocal

    ttl = timedelta(seconds=lease_seconds) if lease_seconds is not None else _SLACK_EVENT_LEASE_TTL
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        db.query(SlackEventDedup).filter(
            SlackEventDedup.event_id == event_id,
            SlackEventDedup.status == "in_progress",
        ).update({"lease_expires_at": now + ttl}, synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def mark_slack_event_done(event_id: str) -> None:
    """Mark a claimed event as completed so later deliveries are ack-dropped.

    Call this as soon as the user-visible Slack reply for the event has been
    posted successfully — *before* audit/memory housekeeping. That way a
    failure after the reply cannot release the claim and cause a duplicate turn.
    """
    from database import SessionLocal

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        db.query(SlackEventDedup).filter(
            SlackEventDedup.event_id == event_id
        ).update(
            {"status": "done", "lease_expires_at": None, "seen_at": now},
            synchronize_session=False,
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def is_slack_event_done(event_id: str) -> bool:
    """Return True if this event_id was already marked completed (reply committed)."""
    from database import SessionLocal

    db = SessionLocal()
    try:
        row = db.get(SlackEventDedup, event_id)
        return bool(row is not None and row.status == "done")
    except Exception:
        return False
    finally:
        db.close()


def release_slack_event_id(event_id: str) -> bool:
    """Drop an in_progress claim so the event can be redelivered after a failed dispatch.

    Never deletes a ``done`` row — the Slack reply may already have been posted,
    and releasing would allow a duplicate agent turn. Returns True if a row was
    deleted.
    """
    from database import SessionLocal

    db = SessionLocal()
    try:
        deleted = (
            db.query(SlackEventDedup)
            .filter(
                SlackEventDedup.event_id == event_id,
                SlackEventDedup.status == "in_progress",
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        return bool(deleted)
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def _truncate_interaction_text(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= _INTERACTION_CONTENT_MAX:
        return cleaned
    return cleaned[:_INTERACTION_CONTENT_MAX] + "\n… (truncated)"


def _derive_interaction_session_id(
    *,
    agent_id: str,
    trigger_type: str,
    channel_id: str | None,
    thread_ts: str | None,
) -> str:
    if trigger_type == "a2a":
        return _uuid()
    base = f"{agent_id}|{channel_id or ''}|{thread_ts or ''}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def record_agent_exchange(
    *,
    interaction_id: str | None = None,
    agent_id: str,
    user_message: str,
    assistant_message: str,
    invoker_email: str,
    trigger_type: str,
    invoker_slack_user_id: str | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    source_agent_id: str | None = None,
    source_agent_name: str | None = None,
    duration_ms: int | None = None,
    secrets_loaded: list[str] | None = None,
    tools_called: list[dict] | None = None,
    routing_metadata: dict | None = None,
    failure_kind: str | None = None,
    failure_detail: str | None = None,
    resolved_model: str | None = None,
    trace: dict | None = None,
) -> str | None:
    """Best-effort persist of one user/agent exchange. Never raises.

    ``trace`` is ``AgentTrace.to_record()``; when given, the model calls and
    system prompt are stored in agent_interaction_traces.

    Returns the interaction log id when a row was written.
    """
    if not agent_id or not invoker_email:
        return None
    user_message = _truncate_interaction_text(user_message)
    assistant_message = _truncate_interaction_text(assistant_message)
    if not user_message and not assistant_message:
        return None

    row_id = (interaction_id or "").strip() or _uuid()
    db = SessionLocal()
    try:
        session_id = _derive_interaction_session_id(
            agent_id=agent_id,
            trigger_type=trigger_type,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        is_new_session = (
            db.query(AgentInteractionLog.id)
            .filter(
                AgentInteractionLog.agent_id == agent_id,
                AgentInteractionLog.session_id == session_id,
            )
            .first()
            is None
        )
        db.add(
            AgentInteractionLog(
                id=row_id,
                agent_id=agent_id,
                session_id=session_id,
                invoker_email=invoker_email.strip().lower(),
                invoker_slack_user_id=invoker_slack_user_id or None,
                trigger_type=trigger_type,
                channel_id=channel_id or None,
                thread_ts=thread_ts or None,
                source_agent_id=source_agent_id or None,
                source_agent_name=(source_agent_name or None),
                user_message=user_message or "(empty)",
                assistant_message=assistant_message or "(empty)",
                duration_ms=duration_ms,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                secrets_loaded=json.dumps(secrets_loaded) if secrets_loaded is not None else None,
                tools_called=json.dumps(tools_called) if tools_called is not None else None,
                routing_metadata=json.dumps(routing_metadata) if routing_metadata is not None else None,
                failure_kind=failure_kind or None,
                failure_detail=failure_detail or None,
                resolved_model=resolved_model or None,
            )
        )
        if trace is not None:
            db.add(
                AgentInteractionTrace(
                    interaction_id=row_id,
                    agent_id=agent_id,
                    model_calls=json.dumps(trace.get("model_calls") or []),
                    system_prompt=trace.get("system_prompt"),
                )
            )
        agent = db.get(Agent, agent_id)
        if agent:
            agent.total_messages = (agent.total_messages or 0) + 1
            if is_new_session:
                agent.session_count = (agent.session_count or 0) + 1
            agent.last_active_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return row_id
    except Exception:
        db.rollback()
        logger.exception("Failed to record agent interaction for agent_id=%s", agent_id)
        return None
    finally:
        db.close()


def get_thread_skill_hint(agent_id: str, channel_id: str, thread_ts: str) -> str | None:
    """Return the primary slug from the last routing selection in this thread."""
    if not agent_id or not channel_id or not thread_ts:
        return None
    db = SessionLocal()
    try:
        row = (
            db.query(AgentThreadSkillRouting)
            .filter(
                AgentThreadSkillRouting.agent_id == agent_id,
                AgentThreadSkillRouting.channel_id == channel_id,
                AgentThreadSkillRouting.thread_ts == thread_ts,
            )
            .first()
        )
        if not row:
            return None
        slugs = loads(row.selected_slugs or "[]")
        return slugs[0] if slugs else None
    except Exception:
        logger.exception("get_thread_skill_hint failed agent_id=%s", agent_id)
        return None
    finally:
        db.close()


def upsert_thread_skill_hint(
    agent_id: str,
    channel_id: str,
    thread_ts: str,
    selected_slugs: list[str],
) -> None:
    """Persist routing selection for short follow-up messages in a thread."""
    if not agent_id or not channel_id or not thread_ts or not selected_slugs:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        row = (
            db.query(AgentThreadSkillRouting)
            .filter(
                AgentThreadSkillRouting.agent_id == agent_id,
                AgentThreadSkillRouting.channel_id == channel_id,
                AgentThreadSkillRouting.thread_ts == thread_ts,
            )
            .first()
        )
        payload = dumps(selected_slugs[:2])
        if row:
            row.selected_slugs = payload
            row.updated_at = now
        else:
            db.add(
                AgentThreadSkillRouting(
                    agent_id=agent_id,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    selected_slugs=payload,
                    updated_at=now,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("upsert_thread_skill_hint failed agent_id=%s", agent_id)
    finally:
        db.close()


def _parse_routing_metadata(row: AgentInteractionLog) -> dict | None:
    raw = getattr(row, "routing_metadata", None)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_tools_called(row: AgentInteractionLog) -> list[dict]:
    raw = getattr(row, "tools_called", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _tool_summary_from_tools(tools: list[dict]) -> dict:
    summary = {
        "total": 0,
        "failed": 0,
        "run_python": 0,
        "call_artifact": 0,
        "activate_skill": 0,
    }
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        tool = str(entry.get("tool") or "")
        summary["total"] += 1
        if tool in summary:
            summary[tool] += 1
        if entry.get("ok") is False:
            summary["failed"] += 1
    return summary


def _interaction_base_dict(row: AgentInteractionLog) -> dict:
    tools = _parse_tools_called(row)
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "invoker_email": row.invoker_email,
        "invoker_slack_user_id": row.invoker_slack_user_id,
        "trigger_type": row.trigger_type,
        "channel_id": row.channel_id,
        "thread_ts": row.thread_ts,
        "source_agent_id": row.source_agent_id,
        "source_agent_name": row.source_agent_name,
        "user_message": row.user_message,
        "assistant_message": row.assistant_message,
        "duration_ms": row.duration_ms,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "routing_metadata": _parse_routing_metadata(row),
        "failure_kind": getattr(row, "failure_kind", None),
        "failure_detail": getattr(row, "failure_detail", None),
        "resolved_model": getattr(row, "resolved_model", None),
        "tool_summary": _tool_summary_from_tools(tools),
    }


def interaction_to_dict(row: AgentInteractionLog) -> dict:
    return _interaction_base_dict(row)


def interaction_detail_to_dict(row: AgentInteractionLog) -> dict:
    detail = _interaction_base_dict(row)
    tools = _parse_tools_called(row)
    secrets: list[str] = []
    if getattr(row, "secrets_loaded", None):
        secrets = loads(row.secrets_loaded)
    detail["tools_called"] = tools
    detail["secrets_loaded"] = secrets
    return detail


def purge_stale_slack_event_dedup() -> None:
    """Delete dedup rows older than the TTL window.

    Only removes rows that are ``done`` or whose lease has already expired, so a
    live in_progress lease is never purged out from under an active dispatch.
    """
    from sqlalchemy import or_

    from database import SessionLocal

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - _SLACK_EVENT_DEDUP_TTL
    db = SessionLocal()
    try:
        db.query(SlackEventDedup).filter(
            SlackEventDedup.seen_at < cutoff,
            or_(
                SlackEventDedup.status == "done",
                SlackEventDedup.lease_expires_at.is_(None),
                SlackEventDedup.lease_expires_at < now,
            ),
        ).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


_EPHEMERAL_REPLY_TTL = timedelta(minutes=30)
_EPHEMERAL_REPLY_TEXT_MAX = 16_000


def store_ephemeral_reply(
    *,
    agent_id: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
    reply_text: str,
) -> None:
    """Persist the latest ephemeral reply for a thread/user (for public repost)."""
    from database import SessionLocal

    if not all([agent_id, channel_id, thread_ts, slack_user_id]):
        return
    text = (reply_text or "").strip()
    if not text:
        return
    if len(text) > _EPHEMERAL_REPLY_TEXT_MAX:
        text = text[:_EPHEMERAL_REPLY_TEXT_MAX]

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        db.query(SlackEphemeralReply).filter(
            SlackEphemeralReply.agent_id == agent_id,
            SlackEphemeralReply.channel_id == channel_id,
            SlackEphemeralReply.thread_ts == thread_ts,
            SlackEphemeralReply.slack_user_id == slack_user_id,
        ).delete(synchronize_session=False)
        db.add(
            SlackEphemeralReply(
                agent_id=agent_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                slack_user_id=slack_user_id,
                reply_text=text,
                created_at=now,
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "store_ephemeral_reply failed agent=%s channel=%s", agent_id, channel_id
        )
    finally:
        db.close()


def get_latest_ephemeral_reply(
    agent_id: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
) -> str | None:
    """Return the most recent ephemeral reply for this thread/user, if within TTL."""
    from database import SessionLocal

    if not all([agent_id, channel_id, thread_ts, slack_user_id]):
        return None
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - _EPHEMERAL_REPLY_TTL
    db = SessionLocal()
    try:
        row = (
            db.query(SlackEphemeralReply)
            .filter(
                SlackEphemeralReply.agent_id == agent_id,
                SlackEphemeralReply.channel_id == channel_id,
                SlackEphemeralReply.thread_ts == thread_ts,
                SlackEphemeralReply.slack_user_id == slack_user_id,
                SlackEphemeralReply.created_at >= cutoff,
            )
            .order_by(SlackEphemeralReply.created_at.desc())
            .first()
        )
        return row.reply_text if row else None
    except Exception:
        logger.exception(
            "get_latest_ephemeral_reply failed agent=%s channel=%s", agent_id, channel_id
        )
        return None
    finally:
        db.close()


def purge_stale_ephemeral_replies() -> None:
    """Delete ephemeral reply rows older than the TTL window."""
    from database import SessionLocal

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - _EPHEMERAL_REPLY_TTL
    db = SessionLocal()
    try:
        db.query(SlackEphemeralReply).filter(
            SlackEphemeralReply.created_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ── Slack invocation loop / flood guard ───────────────────────────────────────
# Slack agent invocations are rate-limited per (agent_id, sender_slack_user_id)
# to break runaway loops and flood storms, regardless of whether the sender is a
# bot (agent<->agent ping-pong, self-loops) or a human/script (e.g. a watchdog
# that re-mentions an agent every few seconds). Each accepted invocation would
# otherwise claim a sandbox, so an unbounded sender can exhaust the warm pool.
# Tunable via env so ops can tighten/loosen without a redeploy. Defaults allow
# normal interactive use (a handful per window) while stopping sustained bursts.
SLACK_BOT_LOOP_WINDOW_S: int = max(1, int(os.environ.get("SLACK_BOT_LOOP_WINDOW_S", "60")))
SLACK_BOT_LOOP_MAX: int = max(1, int(os.environ.get("SLACK_BOT_LOOP_MAX", "5")))


def check_slack_invocation_allowed(
    agent_id: str,
    sender_slack_user_id: str,
    *,
    window_s: int | None = None,
    max_count: int | None = None,
) -> bool:
    """Return True if a Slack invocation should proceed, False if it is
    rate-limited (loop/flood suspected).

    Applies to *any* sender (bot or human/script). Implements a per-
    ``(agent_id, sender_slack_user_id)`` sliding window: the first invocation in
    a window is always allowed; subsequent ones increment a counter and are
    blocked once the count exceeds ``max_count``.  The window resets after
    ``window_s`` seconds of the current window elapsing.

    Fails open (returns True) on any DB error — the guard must never take down
    legitimate traffic; authorisation (grant / skill ACL) remains the primary
    gate.
    """
    if not agent_id or not sender_slack_user_id:
        return True
    window_s = window_s or SLACK_BOT_LOOP_WINDOW_S
    max_count = max_count or SLACK_BOT_LOOP_MAX

    from database import SessionLocal

    now = datetime.utcnow()
    db = SessionLocal()
    try:
        row = (
            db.query(SlackBotLoopGuard)
            .filter(
                SlackBotLoopGuard.agent_id == agent_id,
                SlackBotLoopGuard.sender_slack_user_id == sender_slack_user_id,
            )
            .with_for_update()
            .first()
        )
        if row is None:
            db.add(
                SlackBotLoopGuard(
                    agent_id=agent_id,
                    sender_slack_user_id=sender_slack_user_id,
                    window_start=now,
                    count=1,
                )
            )
            db.commit()
            return True

        if (now - row.window_start).total_seconds() > window_s:
            # Window elapsed — start a fresh window.
            row.window_start = now
            row.count = 1
            db.commit()
            return True

        row.count += 1
        allowed = row.count <= max_count
        db.commit()
        if not allowed:
            logger.warning(
                "slack bot-loop guard: agent=%s sender=%s count=%s exceeds max=%s in %ss window",
                agent_id, sender_slack_user_id, row.count, max_count, window_s,
            )
        return allowed
    except Exception:
        logger.exception(
            "slack bot-loop guard check failed agent=%s sender=%s — allowing",
            agent_id, sender_slack_user_id,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return True
    finally:
        db.close()


# Backward-compatible alias — the guard now applies to all senders, not just
# bots. Retained so existing imports/tests keep working.
check_bot_invocation_allowed = check_slack_invocation_allowed


def new_oauth_session(owner_sub: str, agent_draft_json: str) -> SlackOAuthSession:
    """Create a fresh, unfilled SlackOAuthSession with the standard TTL."""
    now = datetime.now(timezone.utc)
    return SlackOAuthSession(
        id=_uuid(),
        owner_sub=owner_sub,
        step="config",
        agent_draft=agent_draft_json,
        created_at=now,
        expires_at=now + timedelta(minutes=SLACK_OAUTH_SESSION_TTL_MINUTES),
    )


def owner_sub_to_config_secret_name(owner_sub: str) -> str:
    """Deterministic, safe secret name for a user's config token in the vault."""
    slug = hashlib.sha256(owner_sub.encode("utf-8")).hexdigest()[:32]
    return f"{RESOURCE_PREFIX}-config-{slug}"


# ── Lifecycle helpers ─────────────────────────────────────────────────────────

# Allowed (from_status, action) → to_status
LIFECYCLE_TRANSITIONS: dict[tuple[str, str], str] = {
    # pending_oauth: draft created at OAuth start, waiting for Slack install
    ("pending_oauth", "register"): "registered",
    ("pending_oauth", "decommission"): "decommissioned",
    ("registered", "activate"): "active",
    ("paused", "activate"): "active",
    ("active", "pause"): "paused",
    ("registered", "decommission"): "decommissioned",
    ("active", "decommission"): "decommissioned",
    ("paused", "decommission"): "decommissioned",
}


def next_status(current: str, action: str) -> Optional[str]:
    return LIFECYCLE_TRANSITIONS.get((current, action))


def get_agent_status(agent_id: str) -> Optional[str]:
    """Return the current lifecycle status for *agent_id*, or None if not found."""
    if not agent_id:
        return None
    from database import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(Agent.status).filter(Agent.id == agent_id).first()
        return row[0] if row else None
    except Exception:
        logger.exception("get_agent_status failed agent_id=%s", agent_id)
        return None
    finally:
        db.close()


def agent_accepts_runtime_requests(status: Optional[str]) -> bool:
    """True when an agent may respond to Slack, scheduled tasks, or A2A calls."""
    return status == "active"


# ── JSON helpers (SQLite-friendly array storage) ─────────────────────────────

def loads(value: Optional[str]) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed if isinstance(x, str)]


def dumps(value: Iterable[str]) -> str:
    return json.dumps(list(value), separators=(",", ":"))


# ── DDL migration helpers ────────────────────────────────────────────────────

def _table_exists(conn, table: str) -> bool:
    if conn.dialect.name == "sqlite":
        row = conn.execute(
            sa_text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
            {"n": table},
        ).fetchone()
        return row is not None
    row = conn.execute(
        sa_text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = :n AND table_schema = 'public'"
        ),
        {"n": table},
    ).fetchone()
    return row is not None


def init_agent_tables(engine) -> None:
    """Create agent tables, add the unique index on slack_app_id, and run column migrations."""
    from agent_channel_configs import AgentChannelConfig  # noqa: F401 — registers table with Base

    Base.metadata.create_all(
        engine,
        tables=[
            Agent.__table__,
            AgentCollaborator.__table__,
            AgentScheduledTask.__table__,
            SlackOAuthSession.__table__,
            SlackScopeReport.__table__,
            SlackEventDedup.__table__,
            SlackBotLoopGuard.__table__,
            AgentInteractionLog.__table__,
            AgentMemory.__table__,
            AgentChannelConfig.__table__,
            AgentDraftRow.__table__,
        ],
    )
    with engine.begin() as conn:
        if _table_exists(conn, "agent_interaction_logs"):
            conn.execute(
                sa_text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_interaction_logs_agent_created "
                    "ON agent_interaction_logs(agent_id, created_at DESC)"
                )
            )
            # Audit columns — added post-initial-schema; safe to skip on existing tables
            # that predate this migration.
            _ail_cols = _agent_cols_for(conn, "agent_interaction_logs")
            if "secrets_loaded" not in _ail_cols:
                conn.execute(sa_text("ALTER TABLE agent_interaction_logs ADD COLUMN secrets_loaded TEXT"))
            if "tools_called" not in _ail_cols:
                conn.execute(sa_text("ALTER TABLE agent_interaction_logs ADD COLUMN tools_called TEXT"))
        if _table_exists(conn, "agent_memories"):
            conn.execute(
                sa_text(
                    "CREATE INDEX IF NOT EXISTS ix_agent_memories_agent_scope "
                    "ON agent_memories(agent_id, scope)"
                )
            )
            # Add channel_id column BEFORE any index that references it.
            _am_cols = _agent_cols_for(conn, "agent_memories")
            if "channel_id" not in _am_cols:
                conn.execute(sa_text("ALTER TABLE agent_memories ADD COLUMN channel_id TEXT"))
                conn.execute(
                    sa_text(
                        "CREATE INDEX IF NOT EXISTS ix_agent_memories_channel "
                        "ON agent_memories(agent_id, channel_id)"
                    )
                )
            # Rebuild the unique key index to include channel_id if it doesn't already.
            # Guard the DROP+CREATE behind a definition check so concurrent subprocess startups
            # don't all grab AccessExclusiveLock on the same index and deadlock each other.
            if engine.dialect.name == "postgresql":
                idx_def = conn.execute(
                    sa_text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_agent_memories_key'"
                    )
                ).scalar()
                if idx_def is None or "channel_id" not in idx_def:
                    conn.execute(sa_text("DROP INDEX IF EXISTS uq_agent_memories_key"))
                    conn.execute(
                        sa_text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_memories_key "
                            "ON agent_memories(agent_id, scope, COALESCE(user_email,''), COALESCE(channel_id,''), key)"
                        )
                    )
            else:
                # SQLite: re-create index; IF NOT EXISTS is safe since COALESCE on nullable is fine.
                try:
                    conn.execute(sa_text("DROP INDEX IF EXISTS uq_agent_memories_key"))
                except Exception:
                    pass
                conn.execute(
                    sa_text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_memories_key "
                        "ON agent_memories(agent_id, scope, COALESCE(user_email,''), COALESCE(channel_id,''), key)"
                    )
                )
        if _table_exists(conn, "agents"):
            conn.execute(
                sa_text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_slack_app_id "
                    "ON agents(slack_app_id)"
                )
            )
            conn.execute(
                sa_text(
                    "CREATE INDEX IF NOT EXISTS ix_agents_owner_status "
                    "ON agents(owner_sub, status)"
                )
            )
            existing = _agent_cols(conn)
            if "slack_team_id" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN slack_team_id TEXT"))
            if "client_secret_ref" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN client_secret_ref TEXT"))
            if "creation_method" not in existing:
                conn.execute(
                    sa_text(
                        "ALTER TABLE agents ADD COLUMN creation_method TEXT NOT NULL DEFAULT 'manual'"
                    )
                )
            if "manifest_access_verified" not in existing:
                _bool_type = "BOOLEAN NOT NULL DEFAULT FALSE" if engine.dialect.name == "postgresql" else "INTEGER NOT NULL DEFAULT 0"
                conn.execute(
                    sa_text(f"ALTER TABLE agents ADD COLUMN manifest_access_verified {_bool_type}")
                )
            if "signing_secret_ref" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN signing_secret_ref TEXT"))
            if "slack_client_id" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN slack_client_id TEXT"))
            if "is_platform_agent" not in existing:
                # Use BOOLEAN on PostgreSQL; SQLite stores any type as INTEGER internally.
                _bool_type = "BOOLEAN NOT NULL DEFAULT FALSE" if engine.dialect.name == "postgresql" else "INTEGER NOT NULL DEFAULT 0"
                conn.execute(
                    sa_text(f"ALTER TABLE agents ADD COLUMN is_platform_agent {_bool_type}")
                )
            else:
                # Upgrade INTEGER → BOOLEAN if the column was added as INTEGER
                # by a previous deploy. On PostgreSQL, `ALTER COLUMN ... TYPE
                # boolean USING col::boolean` FAILS if the column has a DEFAULT
                # that can't be cast automatically (e.g. DEFAULT 0). We must
                # DROP the default first, cast, then re-set a boolean default.
                # Without this, the column stays INTEGER and every auto_oauth
                # Agent INSERT crashes with DatatypeMismatch (the Boolean model
                # emits `false`, which integer columns reject).
                if engine.dialect.name == "postgresql":
                    conn.execute(sa_text("SAVEPOINT sp_alter_bool"))
                    try:
                        conn.execute(sa_text("ALTER TABLE agents ALTER COLUMN is_platform_agent DROP DEFAULT"))
                        conn.execute(
                            sa_text(
                                "ALTER TABLE agents ALTER COLUMN is_platform_agent "
                                "TYPE boolean USING is_platform_agent::boolean"
                            )
                        )
                        conn.execute(sa_text("ALTER TABLE agents ALTER COLUMN is_platform_agent SET DEFAULT FALSE"))
                        conn.execute(sa_text("RELEASE SAVEPOINT sp_alter_bool"))
                    except Exception:
                        conn.execute(sa_text("ROLLBACK TO SAVEPOINT sp_alter_bool"))
                        # Already BOOLEAN (or unsupported) — safe to continue
            if "channel_fallback_behavior" not in existing:
                conn.execute(
                    sa_text(
                        "ALTER TABLE agents ADD COLUMN channel_fallback_behavior TEXT NOT NULL DEFAULT 'silent'"
                    )
                )
            if "bot_scopes" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN bot_scopes TEXT"))
            if "pending_scopes" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN pending_scopes TEXT"))
            if "usecase_description" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN usecase_description TEXT"))
            if "llm_model" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN llm_model TEXT"))
            if "gateway_api_key_ref" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN gateway_api_key_ref TEXT"))
            if "artifacts_enabled" not in existing:
                _bool_type = (
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                    if engine.dialect.name == "postgresql"
                    else "INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    sa_text(f"ALTER TABLE agents ADD COLUMN artifacts_enabled {_bool_type}")
                )
            if "skill_routing_enabled" not in existing:
                _bool_type = (
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                    if engine.dialect.name == "postgresql"
                    else "INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    sa_text(f"ALTER TABLE agents ADD COLUMN skill_routing_enabled {_bool_type}")
                )
            if "skill_index_text" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN skill_index_text TEXT"))
            if "skill_index_generated_at" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN skill_index_generated_at TIMESTAMP"))
            if "skill_index_source_skill_ids" not in existing:
                conn.execute(sa_text("ALTER TABLE agents ADD COLUMN skill_index_source_skill_ids TEXT"))
        if _table_exists(conn, "agent_interaction_logs"):
            log_cols = _agent_cols_for(conn, "agent_interaction_logs")
            if "routing_metadata" not in log_cols:
                conn.execute(sa_text("ALTER TABLE agent_interaction_logs ADD COLUMN routing_metadata TEXT"))
            if "failure_kind" not in log_cols:
                conn.execute(sa_text("ALTER TABLE agent_interaction_logs ADD COLUMN failure_kind VARCHAR(32)"))
            if "failure_detail" not in log_cols:
                conn.execute(sa_text("ALTER TABLE agent_interaction_logs ADD COLUMN failure_detail TEXT"))
            if "resolved_model" not in log_cols:
                conn.execute(sa_text("ALTER TABLE agent_interaction_logs ADD COLUMN resolved_model VARCHAR(120)"))
        if _table_exists(conn, "agent_scheduled_tasks"):
            task_cols = _agent_cols_for(conn, "agent_scheduled_tasks")
            if "report_enabled" not in task_cols:
                _bool_type = (
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                    if engine.dialect.name == "postgresql"
                    else "INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    sa_text(f"ALTER TABLE agent_scheduled_tasks ADD COLUMN report_enabled {_bool_type}")
                )
            if "report_prompt" not in task_cols:
                conn.execute(sa_text("ALTER TABLE agent_scheduled_tasks ADD COLUMN report_prompt TEXT"))
            if "last_run_error" not in task_cols:
                conn.execute(sa_text("ALTER TABLE agent_scheduled_tasks ADD COLUMN last_run_error TEXT"))
            if "author_sub" not in task_cols:
                conn.execute(sa_text("ALTER TABLE agent_scheduled_tasks ADD COLUMN author_sub TEXT"))
            if "author_email" not in task_cols:
                conn.execute(sa_text("ALTER TABLE agent_scheduled_tasks ADD COLUMN author_email TEXT"))
        # Migrate slack_oauth_sessions table
        if _table_exists(conn, "slack_oauth_sessions"):
            sess_cols = _agent_cols_for(conn, "slack_oauth_sessions")
            if "signing_secret_ref" not in sess_cols:
                conn.execute(sa_text("ALTER TABLE slack_oauth_sessions ADD COLUMN signing_secret_ref TEXT"))
        # Migrate slack_event_dedup → leased dedup (status + lease_expires_at).
        # Pre-existing rows predate the lease scheme; backfill them as 'done' so
        # they behave exactly like the old tombstone (ack-drop duplicates) and
        # age out within the 24h TTL. New rows are inserted as in_progress.
        if _table_exists(conn, "slack_event_dedup"):
            _dedup_cols = _agent_cols_for(conn, "slack_event_dedup")
            if "status" not in _dedup_cols:
                conn.execute(
                    sa_text(
                        "ALTER TABLE slack_event_dedup ADD COLUMN status "
                        "TEXT NOT NULL DEFAULT 'done'"
                    )
                )
            if "lease_expires_at" not in _dedup_cols:
                _ts_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
                conn.execute(
                    sa_text(f"ALTER TABLE slack_event_dedup ADD COLUMN lease_expires_at {_ts_type}")
                )
            conn.execute(
                sa_text(
                    "CREATE INDEX IF NOT EXISTS ix_slack_event_dedup_lease "
                    "ON slack_event_dedup(lease_expires_at)"
                )
            )


def _agent_cols(conn) -> set[str]:
    return _agent_cols_for(conn, "agents")


def _agent_cols_for(conn, table: str) -> set[str]:
    from database import table_cols
    return table_cols(conn, table)


# ── Serialisation ────────────────────────────────────────────────────────────

def agent_to_public_dict(a: Agent) -> dict:
    """Minimal agent metadata for callers without owner/collaborator/approved access."""
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description,
        "avatar_url": a.avatar_url,
        "status": a.status,
        "agent_type": a.agent_type,
        "trigger_mode": a.trigger_mode,
        "creation_method": getattr(a, "creation_method", "manual"),
        "is_platform_agent": bool(getattr(a, "is_platform_agent", False)),
        "session_count": a.session_count,
        "scheduled_task_count": len(a.scheduled_tasks) if a.scheduled_tasks else 0,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
        "has_signing_secret": bool(getattr(a, "signing_secret_ref", None)),
        "has_gateway_api_key": bool(getattr(a, "gateway_api_key_ref", None)),
    }


def _privileged_agent_secret_fields(a: Agent) -> dict[str, object]:
    """Vault resource names — only for owners/collaborators on private agent routes."""
    return {
        "xoxb_secret_ref": a.xoxb_secret_ref,
        "xapp_secret_ref": a.xapp_secret_ref,
        "signing_secret_ref": getattr(a, "signing_secret_ref", None),
        "gateway_api_key_ref": getattr(a, "gateway_api_key_ref", None),
    }


def agent_dict_for_access(a: Agent, access: str) -> dict:
    """Return full configuration only for privileged access levels."""
    if access in ("owner", "collaborator"):
        d = agent_to_dict(a)
        d.update(_privileged_agent_secret_fields(a))
        return d
    if access == "approved":
        d = agent_to_dict(a)
        return d
    return agent_to_public_dict(a)


def agent_to_dict(a: Agent) -> dict:
    """Public representation of an agent. NEVER includes raw token material."""
    return {
        "id": a.id,
        "name": a.name,
        "description": a.description,
        "usecase_description": getattr(a, "usecase_description", None),
        "avatar_url": a.avatar_url,
        "creator_email": a.creator_email,
        "creator_department": a.creator_department,
        "slack_app_id": a.slack_app_id,
        "slack_bot_user_id": a.slack_bot_user_id,
        "slack_team_id": a.slack_team_id,
        "has_xoxb_secret": bool(a.xoxb_secret_ref),
        "has_xapp_secret": bool(a.xapp_secret_ref),
        "has_gateway_api_key": bool(getattr(a, "gateway_api_key_ref", None)),
        "has_signing_secret": bool(getattr(a, "signing_secret_ref", None)),
        "creation_method": getattr(a, "creation_method", "manual"),
        "manifest_access_verified": bool(getattr(a, "manifest_access_verified", False)),
        "skill_ids": loads(a.skill_ids),
        "soul_text": a.soul_text,
        "channel_ids": loads(a.channel_ids),
        "trigger_mode": a.trigger_mode,
        "agent_type": a.agent_type,
        "llm_model": getattr(a, "llm_model", None),
        "artifacts_enabled": bool(getattr(a, "artifacts_enabled", False)),
        "skill_routing_enabled": bool(getattr(a, "skill_routing_enabled", False)),
        "skill_index_text": getattr(a, "skill_index_text", None),
        "skill_index_generated_at": (
            a.skill_index_generated_at.isoformat()
            if getattr(a, "skill_index_generated_at", None)
            else None
        ),
        "skill_index_source_skill_ids": loads(a.skill_index_source_skill_ids or "[]")
        if getattr(a, "skill_index_source_skill_ids", None)
        else [],
        "can_invoke": loads(a.can_invoke or "[]"),
        "can_be_invoked_by": loads(a.can_be_invoked_by or "[]"),
        "guardrails": a.guardrails or [],
        "bot_scopes": loads(a.bot_scopes) if getattr(a, "bot_scopes", None) else None,
        "pending_scopes": loads(a.pending_scopes) if getattr(a, "pending_scopes", None) else [],
        "status": a.status,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
        "activated_at": a.activated_at.isoformat() if a.activated_at else None,
        "paused_at": a.paused_at.isoformat() if a.paused_at else None,
        "decommissioned_at": a.decommissioned_at.isoformat() if a.decommissioned_at else None,
        "last_active_at": a.last_active_at.isoformat() if a.last_active_at else None,
        "session_count": a.session_count,
        "total_messages": a.total_messages,
        # Phase-A: scheduled tasks summary count only
        "scheduled_task_count": len(a.scheduled_tasks),
        # Phase-A clearly signals the runtime is not connected yet
        "runtime": "stubbed",
    }


def task_to_dict(t: AgentScheduledTask) -> dict:
    from automation_prompts import DEFAULT_AUTOMATION_REPORT_PROMPT

    return {
        "id": t.id,
        "agent_id": t.agent_id,
        "prompt": t.prompt,
        "schedule": t.schedule,
        "timezone": t.timezone,
        "output_channel": t.output_channel,
        "is_active": t.is_active,
        "report_enabled": bool(getattr(t, "report_enabled", False)),
        "report_prompt": getattr(t, "report_prompt", None),
        "default_report_prompt": DEFAULT_AUTOMATION_REPORT_PROMPT,
        "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
        "last_run_status": t.last_run_status,
        "last_run_error": getattr(t, "last_run_error", None),
        "run_count": t.run_count,
        "created_at": t.created_at.isoformat(),
    }


# ── Knowledge base ────────────────────────────────────────────────────────────
#
# One storage namespace + one retrieval namespace per agent, provisioned only
# when the owner enables the capability. See AGENT_KNOWLEDGE_BASE_SPEC.md.

# Statuses for AgentKnowledgeBase.status
KB_STATUS_DISABLED = "disabled"
KB_STATUS_PROVISIONING = "provisioning"
KB_STATUS_READY = "ready"
KB_STATUS_ERROR = "error"
KB_STATUS_DEPROVISIONING = "deprovisioning"

# Statuses for AgentKnowledgeBaseDocument.index_status
KB_DOC_PENDING = "pending"
KB_DOC_IMPORTING = "importing"
KB_DOC_INDEXED = "indexed"
KB_DOC_FAILED = "failed"

KB_DOC_TERMINAL_STATUSES = frozenset({KB_DOC_INDEXED, KB_DOC_FAILED})


class AgentKnowledgeBase(Base):
    """Per-agent knowledge base.

    Opt-in: no row (or an existing row left at ``disabled``) means no blob store
    and no retrieval namespace exist for the agent, and no cost is
    incurred. Enabling provisions both; disabling destroys both (it is a hard
    delete of the underlying stores, not a pause).

    ``storage_namespace`` / ``data_store_id`` are resolved once at provisioning
    time and persisted rather than recomputed on read, so that a later change to
    the naming scheme can never orphan an already-provisioned store.
    """

    __tablename__ = "agent_knowledge_bases"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_agent_knowledge_bases_agent"),)

    id = Column(String, primary_key=True, default=_uuid)
    agent_id = Column(
        String,
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    enabled = Column(Boolean, nullable=False, default=False)
    status = Column(String(20), nullable=False, default=KB_STATUS_DISABLED, index=True)
    error_message = Column(Text, nullable=True)

    # Resolved at provisioning time; NULL whenever status is disabled.
    storage_namespace = Column(String(63), nullable=True)   # the blob store hard limit: 63 chars
    data_store_id = Column(String(63), nullable=True)     # the retrieval provider limit: 63 chars
    data_store_location = Column(String(16), nullable=False, default="us")  # global | us | eu

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    documents = relationship(
        "AgentKnowledgeBaseDocument",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )

    @property
    def is_usable(self) -> bool:
        """True when the runtime may offer search_knowledge_base for this agent."""
        return bool(self.enabled) and self.status == KB_STATUS_READY and bool(self.data_store_id)


class AgentKnowledgeBaseDocument(Base):
    """A single uploaded document within an agent's knowledge base.

    ``content_hash`` is the de-dup key within a knowledge base: re-uploading
    identical bytes is rejected rather than creating a second the retrieval provider
    document that would dilute retrieval.

    ``index_document_id`` is only known after the import LRO completes and
    is reconciled back by the ingest worker — the retrieval provider generates it
    server-side as a hash, so it cannot be predicted at upload time.
    """

    __tablename__ = "agent_knowledge_base_documents"
    __table_args__ = (
        UniqueConstraint(
            "knowledge_base_id", "content_hash", name="uq_agent_kb_documents_hash"
        ),
    )

    id = Column(String, primary_key=True, default=_uuid)
    knowledge_base_id = Column(
        String,
        ForeignKey("agent_knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    uploaded_by_email = Column(String(320), nullable=False, default="")
    original_filename = Column(String(512), nullable=False)
    object_path = Column(String(1024), nullable=False)  # object path within the bucket
    content_hash = Column(String(64), nullable=False)       # sha256 hex
    size_bytes = Column(Integer, nullable=False, default=0)
    mime_type = Column(String(255), nullable=False, default="")

    index_document_id = Column(String(255), nullable=True)
    index_status = Column(String(20), nullable=False, default=KB_DOC_PENDING, index=True)
    index_error = Column(Text, nullable=True)

    # Lease columns for the ingest worker — mirrors the overnight-worker pattern
    # so multiple replicas can poll the same table without double-importing.
    lease_owner = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    indexed_at = Column(DateTime, nullable=True)

    knowledge_base = relationship("AgentKnowledgeBase", back_populates="documents")


def kb_to_dict(kb: "AgentKnowledgeBase | None") -> dict:
    """Serialise a knowledge base for the API. None → the disabled shape."""
    if kb is None:
        return {
            "enabled": False,
            "status": KB_STATUS_DISABLED,
            "error_message": None,
            "data_store_location": None,
            "document_count": 0,
        }
    return {
        "enabled": bool(kb.enabled),
        "status": kb.status,
        "error_message": kb.error_message,
        "data_store_location": kb.data_store_location,
        # Bucket / data store IDs are deliberately not exposed to clients —
        # they are infrastructure identifiers, not user-facing data.
        "created_at": kb.created_at.isoformat() if kb.created_at else None,
        "updated_at": kb.updated_at.isoformat() if kb.updated_at else None,
    }


def kb_document_to_dict(d: "AgentKnowledgeBaseDocument") -> dict:
    # The import LRO completing does not mean the document is searchable —
    # the retrieval provider keeps indexing asynchronously afterwards. The worker
    # therefore stamps ``indexed_at`` a short way into the future, and this
    # reports the document as still importing until that moment passes, so
    # "Indexed" in the UI is not claimed before a search would actually find it.
    status = d.index_status
    if status == KB_DOC_INDEXED and d.indexed_at and d.indexed_at > datetime.utcnow():
        status = KB_DOC_IMPORTING

    return {
        "id": d.id,
        "filename": d.original_filename,
        "size_bytes": d.size_bytes,
        "mime_type": d.mime_type,
        "index_status": status,
        "index_error": d.index_error,
        "uploaded_by_email": d.uploaded_by_email,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "indexed_at": d.indexed_at.isoformat() if d.indexed_at else None,
    }


class KnowledgeBaseUsageLog(Base):
    """One row per billable the retrieval provider operation, attributed to an agent.

    the retrieval provider bills queries and indexed storage at *project* level by SKU,
    and ``user_labels`` on a search request are not a documented billing-export
    dimension — so per-agent cost cannot be read back out of billing the way
    per-bucket storage cost can. This table is the attribution source instead:
    counts here are multiplied by published unit pricing to produce the per-agent
    estimate, the same approach ``token_usage_logs`` takes for LLM cost.
    """

    __tablename__ = "kb_usage_logs"

    id = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    agent_id = Column(
        String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "search" | "import"
    event_type = Column(String(20), nullable=False, index=True)
    # Searches: 1 per query. Imports: number of documents in the batch.
    unit_count = Column(Integer, nullable=False, default=1)
