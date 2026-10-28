"""HTTP handler for Slack Events API — routes incoming events to the correct agent.

Cloud Run forwards all Slack event POSTs to POST /api/slack/events.
This endpoint:

1. Reads the raw request body and extracts ``api_app_id`` from the JSON payload.
2. Looks up the registered ``Agent`` row by ``slack_app_id``.
3. Verifies the ``X-Slack-Signature`` header using the per-agent signing secret
   stored in the vault (HMAC-SHA256, v0 scheme).
4. Rejects requests with a timestamp older than 5 minutes (replay protection).
5. Handles Slack's ``url_verification`` challenge immediately.
6. Deduplicates on Slack ``event_id`` (24h TTL) before dispatching.
7. Dispatches ``app_mention`` and DM ``message`` events to a background task
   that calls the LLM with the agent's soul + skills and replies via the bot token.

Security notes
──────────────
- This endpoint is exempt from the Cloudflare Access JWT gate because Slack
  cannot present one.  All authentication is via Slack's own signing secret.
- The signing secret is fetched from the vault on every request so
  any secret rotation takes effect immediately.
- No raw secret values are logged; only error codes and app IDs appear.
- A 200 is returned immediately for event dispatches; the LLM call happens in
  a detached background task so Slack's 3-second ACK deadline is always met.
"""

from __future__ import annotations

from app_config import APP_NAME
from platform_urls import platform_internal_base_url

import asyncio
import base64
import hashlib
import hmac
import json as _json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import anthropic
import jwt as _pyjwt
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from model_catalog import platform_default_model
from agents import (
    Agent,
    CLAIM_DONE,
    CLAIM_IN_PROGRESS,
    agent_accepts_runtime_requests,
    check_slack_invocation_allowed,
    claim_slack_event,
    get_agent_status,
    get_latest_ephemeral_reply,
    get_thread_skill_hint,
    load_agent_memories,
    mark_slack_event_done,
    purge_stale_ephemeral_replies,
    purge_stale_slack_event_dedup,
    record_agent_exchange,
    release_slack_event_id,
    store_ephemeral_reply,
    upsert_agent_memory,
    upsert_thread_skill_hint,
    validate_slack_event_id,
)
from access_models import AgentAccessGrant, CrossDepartmentGrant
from database import SessionLocal
from secret_vault import SecretVaultError, get_vault
from skills_acl import resolve_tier1_slugs
from agent_channel_configs import AgentChannelConfig, get_channel_config, upsert_channel_config
from ambient_message_gate import should_handle_ambient_message
from agent_failure_alerts import (
    AGENT_FORMAT_FAILURE_MESSAGE,
    AGENT_TIMEOUT_FAILURE_MESSAGE,
    AgentLlmOutcome,
    FAILURE_KIND_FORMAT,
    FAILURE_KIND_TIMEOUT,
    notify_agent_failure,
)
from slack_reply_gate import (
    EPHEMERAL_SENTINEL,
    NO_REPLY_SENTINEL,
    bot_turn_limit_reached,
    is_near_duplicate_reply,
    should_respond_to_bot,
    strip_ephemeral_flag,
    strip_no_reply,
)


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime for SQLite comparisons.

    SQLite strips timezone info on write and returns naive datetimes on read.
    Using an offset-aware datetime in comparisons raises TypeError.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


logger = logging.getLogger(__name__)

router = APIRouter()

# ── Config ────────────────────────────────────────────────────────────────────

_MODEL_GATEWAY_API_KEY: Optional[str] = os.environ.get("MODEL_GATEWAY_API_KEY")
_MODEL_GATEWAY_URL_RAW: str = os.environ.get("MODEL_GATEWAY_URL", "http://localhost:4000").rstrip("/")
# Strip trailing /v1 — the Anthropic SDK appends /v1/messages itself
_MODEL_GATEWAY_URL: str = _MODEL_GATEWAY_URL_RAW[:-3] if _MODEL_GATEWAY_URL_RAW.endswith("/v1") else _MODEL_GATEWAY_URL_RAW
_MODEL: str = platform_default_model()
_APP_BASE_URL: str = os.environ.get("APP_BASE_URL", "http://localhost:8080").rstrip("/")
_SLACK_TIMESTAMP_TOLERANCE_SECS = 300  # 5 minutes
_MAX_TOOL_TURNS = max(1, int(os.environ.get("AGENT_MAX_TOOL_TURNS", "40")))
# Per-skill tool-turn overrides, for skills whose work genuinely needs more (or
# fewer) turns than the default. Format: "slug=turns,slug=turns".
def _parse_turn_overrides(raw: str) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for pair in (raw or "").split(","):
        slug, _, turns = pair.partition("=")
        slug = slug.strip().lower()
        if not slug or not turns.strip().isdigit():
            continue
        overrides[slug] = max(1, int(turns.strip()))
    return overrides


_SKILL_TOOL_TURN_OVERRIDES: dict[str, int] = _parse_turn_overrides(
    os.environ.get("SKILL_TOOL_TURN_OVERRIDES", "")
)
# Sandbox comms failures are infrastructure, not agent behaviour. Allow this many
# such turns per run without charging them to the turn or time budget.
_MAX_FREE_INFRA_RETRIES = max(0, int(os.environ.get("AGENT_MAX_FREE_INFRA_RETRIES", "6")))
# Marker the tool loop uses to recognise its own sandbox-unavailable message.
_SANDBOX_UNAVAILABLE_MARKER = "Sandbox temporarily unavailable"
_SANDBOX_INFRA_MARKERS = (
    "failed to communicate with the sandbox",
    "name or service not known",
    "connection refused",
    "connection reset",
    "no sandboxes available",
    "sandboxrequesterror",
)
# Cap on a single tool result handed back to the model.
_AGENT_TOOL_OUTPUT_MAX_CHARS = max(
    2_000, int(os.environ.get("AGENT_TOOL_OUTPUT_MAX_CHARS", "12000"))
)
# Tool-use turns can emit large run_python payloads; 2048 is too low for skill-heavy agents.
_AGENT_LLM_MAX_TOKENS = int(os.environ.get("AGENT_LLM_MAX_TOKENS", "8192"))
# Stop fetching early enough to reserve time for a user-facing synthesis.
_AGENT_LLM_TOOL_TIME_BUDGET_SECS = max(
    30.0, float(os.environ.get("AGENT_LLM_TOOL_TIME_BUDGET_SECS", "420"))
)

# Slack file attachments — Slack allows up to 50 files/message; 100 MB/file
_SLACK_FILE_MAX_BYTES = 100 * 1024 * 1024       # 100 MB per file
_SLACK_FILE_MAX_COUNT = 50
_SLACK_FILE_MAX_BATCH_BYTES = 200 * 1024 * 1024  # 200 MB total across all files in one message
_SLACK_FILE_DOWNLOAD_TIMEOUT_SECS = 180          # per-chunk read timeout for large downloads
_SLACK_FILE_ALLOWED_MIMES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/zip",
})
_SLACK_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SLACK_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SLACK_ZIP_MIME = "application/zip"
# Large Slack attachments are staged to the per-invocation the blob store workspace instead of inlined.
_SLACK_WORKSPACE_STAGE_THRESHOLD_BYTES = 256 * 1024
_WORKSPACE_MAX_BYTES = 50 * 1024 * 1024
_WORKSPACE_SIGNED_URL_MINUTES = 30
_SLACK_WORKSPACE_UPLOAD_MAX_FILES = 10
_SLACK_WORKSPACE_UPLOAD_MAX_BYTES = 50 * 1024 * 1024
_SLACK_FILE_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"%PDF", "application/pdf"),
]

# Anthropic rejects any single base64 image/document part over 5 MB with a 400.
# The Slack-side caps above are far larger, so oversized attachments used to be
# inlined verbatim and fail the whole request — and because the attachment is
# replayed from thread history on every turn, the thread stayed broken.
_LLM_ATTACHMENT_MAX_B64_BYTES = 5 * 1024 * 1024
# Headroom for the surrounding JSON envelope so we never land right on the cap.
_LLM_ATTACHMENT_B64_SAFETY_BYTES = 64 * 1024
_LLM_ATTACHMENT_B64_BUDGET = (
    _LLM_ATTACHMENT_MAX_B64_BYTES - _LLM_ATTACHMENT_B64_SAFETY_BYTES
)
# Downscale ladder: each step shrinks both dimensions, and every step tries the
# JPEG qualities below before shrinking further, so we give up detail last.
_LLM_IMAGE_SHRINK_STEP_SCALE = 0.7
_LLM_IMAGE_SHRINK_MAX_STEPS = 8
_LLM_IMAGE_SHRINK_QUALITIES = (85, 70, 55, 40)

# A2A signing secret — only loaded here; never injected into subprocesses directly.
# Subprocesses receive a short-lived signed JWT (DERIV_ONE_CTX_TOKEN) instead.
_INTERNAL_SIGNING_SECRET: str = os.environ.get("INTERNAL_SIGNING_SECRET", "")
_PLATFORM_INTERNAL_BASE_URL: str = platform_internal_base_url()
# Legacy alias — worker modules import this name.
_DERIV_ONE_BASE_URL: str = _PLATFORM_INTERNAL_BASE_URL


def _sign_invocation_context(
    sender_email: str,
    source_agent_id: str,
    ttl_seconds: int = 300,
) -> str:
    """Return a signed JWT for the subprocess to use when calling /internal/agents/invoke."""
    if not _INTERNAL_SIGNING_SECRET:
        raise RuntimeError("INTERNAL_SIGNING_SECRET not set")
    now = int(time.time())
    payload = {
        "sender_email": sender_email,
        "source_agent_id": source_agent_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return _pyjwt.encode(payload, _INTERNAL_SIGNING_SECRET, algorithm="HS256")

SKILLS_DIR = Path(__file__).parent.parent / "src" / "data" / "skills"

# ── Slack query skill injected into the system prompt ─────────────────────────
#
# This block is appended whenever the agent has a valid bot token, giving the
# LLM working Python patterns to query the Slack workspace via `run_python`.
# The bot token is injected as SLACK_BOT_TOKEN in the subprocess env.
#
# Scoping rules (enforced by Slack API — NOT by us):
#   - The bot can only read channels it is a member of.
#   - search.messages requires `search:read` scope (user tokens only); we
#     use conversations.history + conversations.list as a safe alternative
#     for bot tokens which have `channels:history` / `channels:read`.
#
_SLACK_QUERY_SKILL = """\
---

## Slack Workspace Queries

You have a `SLACK_BOT_TOKEN` environment variable and `slack_sdk` installed.
Use `run_python` with these patterns to answer questions about Slack messages,
channels, users, or threads.  Always use real data — never guess channel IDs or user IDs.

```python
import os
from slack_sdk import WebClient
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

# ── List channels the bot is in ───────────────────────────────────────────────
result = slack.conversations_list(types="public_channel,private_channel", limit=200)
channels = [(c["id"], c["name"]) for c in result["channels"]]
print(channels)

# ── Read recent messages from a channel ──────────────────────────────────────
result = slack.conversations_history(channel="CXXXXXXXX", limit=20)
for msg in result["messages"]:
    print(msg.get("text", ""))

# ── Read a specific thread ────────────────────────────────────────────────────
result = slack.conversations_replies(channel="CXXXXXXXX", ts="1234567890.123456")
for msg in result["messages"]:
    print(msg.get("text", ""))

# ── Look up a user by email ───────────────────────────────────────────────────
result = slack.users_lookupByEmail(email="user@example.com")
print(result["user"]["id"], result["user"]["real_name"])

# ── Look up a user by Slack ID ────────────────────────────────────────────────
result = slack.users_info(user="UXXXXXXXX")
print(result["user"]["profile"]["real_name"], result["user"]["profile"]["email"])

# ── Get your own channel + thread (from the current conversation) ─────────────
current_channel = os.environ.get("SLACK_CHANNEL_ID", "")
current_thread  = os.environ.get("SLACK_THREAD_TS", "")
result = slack.conversations_replies(channel=current_channel, ts=current_thread, limit=50)
for msg in result["messages"]:
    role = "bot" if msg.get("bot_id") else "user"
    print(f"{role}: {msg.get('text','')}")

# ── Search for messages (requires search:read user-token scope; may 403 on bot token) ─
# If search fails, fall back to conversations_history on the relevant channel.
try:
    result = slack.search_messages(query="your search term", count=10)
    for match in result["messages"]["matches"]:
        print(match["channel"]["name"], match["text"])
except Exception as e:
    print(f"Search not available: {e} — use conversations_history instead")
```

Tips:
- `SLACK_CHANNEL_ID` and `SLACK_THREAD_TS` are always set to the channel/thread
  of the current conversation — use them to read context from the active thread.
- Do NOT post to `SLACK_CHANNEL_ID` / `SLACK_THREAD_TS` with chat.postMessage —
  the platform posts your final reply automatically. Posting there duplicates messages.
- Resolve user IDs with `users_info` before displaying names.
- If a channel history call returns `not_in_channel`, the bot needs to join first
  or the user should invite it.
- Paginate with `cursor=result["response_metadata"]["next_cursor"]` when needed.
"""

# ── Tool definition (mirrors main.py RUN_PYTHON_TOOL) ─────────────────────────

_RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": (
        "Execute Python code in the backend environment and return the raw stdout output. "
        "Use this to make API calls, run queries, and perform computations. "
        "ALWAYS use this tool to fetch real data — never generate placeholder output. "
        "The environment has: requests, json, os, urllib. "
        "Skill API keys and secrets are available as environment variables. "
        "Print raw data to stdout (json.dumps or simple prints). "
        "After the tool returns, YOU must format the results using Slack mrkdwn — "
        "*bold* with single asterisks, bullet lists with • or -, NO markdown tables. "
        "Never wrap your response in a code block."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute. Print results to stdout.",
            }
        },
        "required": ["code"],
    },
}

_RUN_PYTHON_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "run_python",
        "description": _RUN_PYTHON_TOOL["description"],
        "parameters": _RUN_PYTHON_TOOL["input_schema"],
    },
}

_CALL_ARTIFACT_TOOL = {
    "name": "call_artifact",
    "description": (
        "Run a pre-approved Python artifact attached to this agent's skills. "
        "Prefer this over run_python when an artifact covers the task."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Artifact module name."},
            "params": {
                "type": "object",
                "description": "Parameters for the artifact entrypoint.",
            },
        },
        "required": ["name"],
    },
}

_CALL_ARTIFACT_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "call_artifact",
        "description": _CALL_ARTIFACT_TOOL["description"],
        "parameters": _CALL_ARTIFACT_TOOL["input_schema"],
    },
}

_ACTIVATE_SKILL_TOOL = {
    "name": "activate_skill",
    "description": (
        "Load an additional attached skill's instructions and secrets mid-run. "
        "Call this BEFORE run_python or call_artifact when the Skill Index or task "
        "requires a skill that is not yet loaded. Only activates skills attached "
        "to this agent that you have access to."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "Skill slug from the Skill Index to load.",
            }
        },
        "required": ["slug"],
    },
}

_ACTIVATE_SKILL_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "activate_skill",
        "description": _ACTIVATE_SKILL_TOOL["description"],
        "parameters": _ACTIVATE_SKILL_TOOL["input_schema"],
    },
}

_RESERVED_SKILL_SECRET_KEYS = frozenset({"SENDER_EMAIL", "SENDER_NAME", "SLACK_USER_ID"})

_SKILL_ARTIFACTS_ENABLED = os.environ.get("SKILL_ARTIFACTS_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
)
_SLACK_SKILL_ROUTER_MODE = os.environ.get("SLACK_SKILL_ROUTER_MODE", "off").lower()
_SLACK_SKILL_ACTIVATE_MAX = max(1, int(os.environ.get("SLACK_SKILL_ACTIVATE_MAX", "3")))
# Truncated redacted bodies stored on agent_interaction_logs.tools_called for debugging.
# Set AGENT_TOOL_AUDIT_BODIES=0 to keep metadata-only (legacy).
_AGENT_TOOL_AUDIT_BODIES = os.environ.get("AGENT_TOOL_AUDIT_BODIES", "1").lower() in (
    "1",
    "true",
    "yes",
)
_AGENT_TOOL_AUDIT_BODY_MAX_CHARS = max(
    256, int(os.environ.get("AGENT_TOOL_AUDIT_BODY_MAX_CHARS", "2000"))
)


def _audit_body_snippet(text: str | None) -> str:
    """Redact + truncate tool code/output for persistence on interaction logs."""
    from redaction import redact_text

    raw = text or ""
    redacted = redact_text(raw)
    max_chars = _AGENT_TOOL_AUDIT_BODY_MAX_CHARS
    if len(redacted) <= max_chars:
        return redacted
    return redacted[:max_chars] + f"…[truncated {len(redacted) - max_chars} chars]"


def _append_run_python_audit(tool_audit: list | None, code: str, output: str) -> None:
    if tool_audit is None:
        return
    from code_fingerprint import code_sha256, shape_sha256

    ok = not (output or "").startswith("[error]")
    entry = {
        "tool": "run_python",
        "code_len": len(code),
        "output_len": len(output or ""),
        "ok": ok,
        "code_sha256": code_sha256(code),
        "shape_sha256": shape_sha256(code),
    }
    if _AGENT_TOOL_AUDIT_BODIES:
        entry["code_redacted"] = _audit_body_snippet(code)
        entry["output_redacted"] = _audit_body_snippet(output)
    tool_audit.append(entry)


def _append_call_artifact_audit(
    tool_audit: list | None,
    *,
    name: str,
    version: int,
    output: str,
) -> None:
    if tool_audit is None:
        return
    ok = not (output or "").startswith("[error]")
    entry = {
        "tool": "call_artifact",
        "artifact": name,
        "version": version,
        "output_len": len(output or ""),
        "ok": ok,
    }
    if _AGENT_TOOL_AUDIT_BODIES:
        entry["output_redacted"] = _audit_body_snippet(output)
    tool_audit.append(entry)


def _append_activate_skill_audit(
    tool_audit: list | None,
    *,
    slug: str,
    ok: bool,
    secret_keys: list[str] | None = None,
    artifact_names: list[str] | None = None,
    error: str | None = None,
) -> None:
    if tool_audit is None:
        return
    entry: dict = {
        "tool": "activate_skill",
        "slug": slug,
        "ok": ok,
    }
    if secret_keys is not None:
        entry["secret_keys"] = list(secret_keys)
    if artifact_names is not None:
        entry["artifact_names"] = list(artifact_names)
    if error:
        entry["error"] = error
    tool_audit.append(entry)


def _should_offer_activate_skill(agent: Agent, pool_skill_slugs: list[str] | None) -> bool:
    """True when mid-run activation can load skills the router may have skipped."""
    if not pool_skill_slugs or len(pool_skill_slugs) <= 1:
        return False
    if not getattr(agent, "skill_routing_enabled", False):
        return False
    return _SLACK_SKILL_ROUTER_MODE in ("on", "shadow")


def _activate_skill_system_guidance(
    active_slugs: list[str],
    pool_slugs: list[str],
) -> str:
    unloaded = [s for s in pool_slugs if s not in active_slugs]
    if not unloaded:
        return ""
    loaded = ", ".join(active_slugs) if active_slugs else "(none)"
    available = ", ".join(unloaded)
    return (
        "\n\n## Mid-run skill activation\n"
        f"Currently loaded skills: {loaded}.\n"
        f"Additional attached skills available to load: {available}.\n"
        "If a task requires an unloaded skill, call activate_skill with its slug "
        "BEFORE run_python or call_artifact for that skill. Do not activate preemptively.\n"
    )


def _merge_skill_secrets(target: dict[str, str], incoming: dict[str, str]) -> None:
    for key, value in incoming.items():
        if key not in _RESERVED_SKILL_SECRET_KEYS:
            target[key] = value


def _artifact_section_for_slugs(slugs: list[str]) -> tuple[str, list[str]]:
    """Return (prompt_section, artifact_names) for approved artifacts on *slugs*."""
    if not slugs:
        return "", []
    db = SessionLocal()
    try:
        from artifact_runtime import build_artifact_prompt_section, load_approved_artifacts

        arts = load_approved_artifacts(db, slugs)
        if not arts:
            return "", []
        section = build_artifact_prompt_section(arts)
        names = [a.name for a in arts]
        return section, names
    except Exception:
        logger.exception("artifact lookup failed for slugs=%s", slugs)
        return "", []
    finally:
        db.close()


async def _activate_skill_mid_run(
    *,
    slug: str,
    agent: Agent,
    pool_skill_slugs: list[str],
    active_skill_slugs: list[str],
    skill_secrets: dict[str, str],
    system_prompt_ref: list[str],
    activation_count: list[int],
    tool_audit: list | None,
    channel_id: str | None,
    thread_ts: str | None,
    anthropic_tools: list[dict] | None = None,
    offer_call_artifact_ref: list[bool] | None = None,
    use_artifacts: bool = False,
) -> str:
    """Activate an ACL-accessible skill mid-run; mutate secrets/slugs/system in place."""
    slug = (slug or "").strip()
    if not slug:
        err = "[error] activate_skill requires a non-empty slug."
        _append_activate_skill_audit(tool_audit, slug=slug or "?", ok=False, error=err)
        return err

    if slug not in pool_skill_slugs:
        err = f"[error] Skill '{slug}' is not attached or not accessible to this user."
        logger.info(
            "activate_skill rejected slug=%s agent=%s reason=not_in_pool",
            slug,
            agent.id,
        )
        _append_activate_skill_audit(tool_audit, slug=slug, ok=False, error=err)
        return err

    if slug in active_skill_slugs:
        err = f"[error] Skill '{slug}' is already loaded."
        _append_activate_skill_audit(tool_audit, slug=slug, ok=False, error=err)
        return err

    if activation_count[0] >= _SLACK_SKILL_ACTIVATE_MAX:
        err = (
            f"[error] Maximum mid-run skill activations ({_SLACK_SKILL_ACTIVATE_MAX}) "
            "reached for this reply."
        )
        _append_activate_skill_audit(tool_audit, slug=slug, ok=False, error=err)
        return err

    loop = asyncio.get_event_loop()
    body = await loop.run_in_executor(None, _load_agent_skills, agent, [slug])
    if not body.strip():
        err = f"[error] Skill '{slug}' could not be loaded."
        logger.warning("activate_skill missing body slug=%s agent=%s", slug, agent.id)
        _append_activate_skill_audit(tool_audit, slug=slug, ok=False, error=err)
        return err

    new_secrets = await loop.run_in_executor(
        None, _load_skill_secrets_for_agent, agent, [slug]
    )
    _merge_skill_secrets(skill_secrets, new_secrets)
    secret_keys = sorted(new_secrets.keys())

    active_skill_slugs.append(slug)
    activation_count[0] += 1

    activated_section = f"\n\n---\n\n## Activated skill: {slug}\n\n{body}"
    system_prompt_ref[0] += activated_section

    artifact_names: list[str] = []
    if use_artifacts:
        art_section, artifact_names = _artifact_section_for_slugs([slug])
        if art_section.strip():
            system_prompt_ref[0] += "\n\n" + art_section.strip()
            if offer_call_artifact_ref is not None and not offer_call_artifact_ref[0]:
                offer_call_artifact_ref[0] = True
                if anthropic_tools is not None:
                    tool_names = {t.get("name") for t in anthropic_tools}
                    if "call_artifact" not in tool_names:
                        anthropic_tools.append(_CALL_ARTIFACT_TOOL)

    if channel_id and thread_ts:
        upsert_thread_skill_hint(str(agent.id), channel_id, thread_ts, active_skill_slugs[:2])

    keys_line = ", ".join(secret_keys) if secret_keys else "(none)"
    artifacts_line = ", ".join(artifact_names) if artifact_names else "(none)"
    tool_text = (
        f"Activated skill: {slug}\n"
        f"Secret keys now available: {keys_line}\n"
        f"Approved artifacts: {artifacts_line}\n\n"
        f"## Skill body\n\n{body}"
    )
    _append_activate_skill_audit(
        tool_audit,
        slug=slug,
        ok=True,
        secret_keys=secret_keys,
        artifact_names=artifact_names,
    )
    logger.info(
        "activate_skill ok slug=%s agent=%s active_slugs=%s secret_keys=%s",
        slug,
        agent.id,
        active_skill_slugs,
        secret_keys,
    )
    return tool_text


# ── Signature verification ────────────────────────────────────────────────────

def _verify_slack_signature(signing_secret: str, timestamp: str, raw_body: bytes, provided_sig: str) -> bool:
    """Return True iff the provided X-Slack-Signature is valid.

    Uses HMAC-SHA256 over the canonical string ``v0:{timestamp}:{body}``.
    Constant-time comparison prevents timing oracle attacks.
    """
    try:
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        return False
    if abs(time.time() - ts_int) > _SLACK_TIMESTAMP_TOLERANCE_SECS:
        return False
    sig_base = f"v0:{timestamp}:{raw_body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        sig_base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided_sig)


# ── Agent + secret helpers ────────────────────────────────────────────────────

def _get_agent_by_app_id(slack_app_id: str) -> Optional[Agent]:
    db = SessionLocal()
    try:
        return db.query(Agent).filter(Agent.slack_app_id == slack_app_id).first()
    finally:
        db.close()


def _read_secret(ref: Optional[str]) -> Optional[str]:
    if not ref:
        return None
    try:
        return get_vault().read(ref)
    except SecretVaultError:
        return None


# ── Skill loading ─────────────────────────────────────────────────────────────

def _load_agent_skills(agent: Agent, skill_slugs: list[str] | None = None) -> str:
    """Load skill.md + references for each slug in *skill_slugs* (or agent.skill_ids).

    Pass *skill_slugs* to override the default agent skill list — used when the
    caller has already filtered slugs to the user's accessible subset.

    Strips YAML frontmatter and appends all reference docs, mirroring the
    _load_skill() logic in main.py.  Returns an empty string if no skills.
    """
    if skill_slugs is None:
        try:
            skill_slugs = _json.loads(agent.skill_ids or "[]")
        except (ValueError, TypeError):
            return ""

    parts: list[str] = []
    for slug in skill_slugs:
        path = SKILLS_DIR / slug / "skill.md"
        if not path.exists():
            logger.debug("Skill file not found for slug=%s", slug)
            continue
        content = path.read_text(encoding="utf-8")
        # Strip YAML frontmatter block
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                content = content[end + 4:].strip()
        # Append reference docs
        refs_dir = SKILLS_DIR / slug / "references"
        if refs_dir.is_dir():
            for ref in sorted(refs_dir.glob("*.md")):
                try:
                    ref.resolve().relative_to(refs_dir.resolve())
                except ValueError:
                    continue
                content += f"\n\n---\n\n## {ref.stem}\n\n{ref.resolve().read_text(encoding='utf-8')}"
        parts.append(content)

    return "\n\n---\n\n".join(parts)


def _slack_recent_user_messages(history: list[dict], *, limit: int = 4) -> list[str]:
    texts: list[str] = []
    for msg in history or []:
        if msg.get("role") != "user":
            continue
        text = (msg.get("content") or "").strip()
        if text:
            texts.append(text)
    return texts[-limit:]


async def _resolve_slack_skill_selection(
    *,
    agent: Agent,
    accessible_skill_slugs: list[str],
    user_text: str,
    history: list[dict],
    channel: str,
    thread_ts: str,
) -> tuple[list[str], dict]:
    """Pick skill bodies to load; return (slugs, routing audit metadata)."""
    routing_meta: dict = {
        "mode": _SLACK_SKILL_ROUTER_MODE,
        "enabled": bool(getattr(agent, "skill_routing_enabled", False)),
        "candidate_slugs": list(accessible_skill_slugs),
        "selected_slugs": list(accessible_skill_slugs),
        "fallback": True,
        "reason": "routing_disabled",
        "shadow_only": False,
        "index_present": bool((getattr(agent, "skill_index_text", None) or "").strip()),
    }
    if not getattr(agent, "skill_routing_enabled", False) or not accessible_skill_slugs:
        return list(accessible_skill_slugs), routing_meta
    if _SLACK_SKILL_ROUTER_MODE == "off":
        routing_meta["reason"] = "global_mode_off"
        return list(accessible_skill_slugs), routing_meta

    from skill_router import build_accessible_catalog_from_slugs, suggest_skills_for_message

    catalog = build_accessible_catalog_from_slugs(accessible_skill_slugs)
    active_hint = get_thread_skill_hint(str(agent.id), channel, thread_ts)
    selected = list(accessible_skill_slugs)
    used_fallback = True
    confidence = 0.0
    reason = ""

    if _MODEL_GATEWAY_API_KEY and catalog:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=_MODEL_GATEWAY_API_KEY, base_url=_MODEL_GATEWAY_URL)
        try:
            picked, confidence, reason, used_fallback = await suggest_skills_for_message(
                user_text,
                catalog,
                client,
                recent_messages=_slack_recent_user_messages(history),
                active_skill_hint=active_hint,
                skill_index_text=getattr(agent, "skill_index_text", None),
            )
        except Exception:
            logger.exception("slack skill routing failed agent=%s", agent.id)
            picked = []
            used_fallback = True
            reason = "router_error"

        if not used_fallback and picked:
            selected = picked

    routing_meta.update({
        "selected_slugs": selected,
        "shadow_only": _SLACK_SKILL_ROUTER_MODE == "shadow",
        "fallback": used_fallback or _SLACK_SKILL_ROUTER_MODE == "shadow",
        "confidence": confidence,
        "reason": reason or routing_meta.get("reason"),
        "active_skill_hint": active_hint,
    })

    if _SLACK_SKILL_ROUTER_MODE == "shadow":
        return list(accessible_skill_slugs), routing_meta

    if selected and not used_fallback:
        upsert_thread_skill_hint(str(agent.id), channel, thread_ts, selected[:2])
    return selected, routing_meta


def _load_skill_secrets_for_agent(
    agent: Agent,
    skill_slugs: list[str] | None = None,
) -> dict[str, str]:
    """Collect env-var secrets for skills accessible to the requesting user.

    Pass *skill_slugs* to restrict secret loading to the filtered (accessible)
    subset of the agent's skills.  When omitted, falls back to all agent
    skill_ids — but callers MUST pass the filtered list to prevent secrets for
    inaccessible skills from leaking into the sandbox environment.

    Reads SkillSecret rows from the DB and resolves them vithe vault.
    Returns an empty dict (never raises) so missing secrets don't break replies.
    """
    try:
        from skill_submission_models import SkillSecret
    except ImportError:
        return {}

    if skill_slugs is not None:
        skill_ids = skill_slugs
    else:
        try:
            skill_ids = _json.loads(agent.skill_ids or "[]")
        except (ValueError, TypeError):
            return {}

    if not skill_ids:
        return {}

    db = SessionLocal()
    try:
        rows = db.query(SkillSecret).filter(SkillSecret.slug.in_(skill_ids)).all()
    except Exception:
        return {}
    finally:
        db.close()

    if not rows:
        return {}

    try:
        vault = get_vault()
    except Exception:
        return {}

    result: dict[str, str] = {}
    for row in rows:
        try:
            result[row.env_key] = vault.read(row.secret_ref)
        except SecretVaultError:
            logger.warning("Could not read skill secret env_key=%s slug=%s", row.env_key, row.slug)
    return result


# ── Code execution sandbox ───────────────────────────────────────────────────

from executor import execute_in_sandbox, truncate_tool_output


def _is_sandbox_infra_error(detail: str) -> bool:
    """True when a sandbox failure is transient infrastructure, not user code."""
    low = (detail or "").lower()
    return any(marker in low for marker in _SANDBOX_INFRA_MARKERS)


def _is_infra_failure_output(output: str) -> bool:
    """True when a tool result is our own sandbox-unavailable message."""
    return bool(output) and _SANDBOX_UNAVAILABLE_MARKER in output


# Run artefacts live in ``run_workspace`` so the browser agent's download path
# reuses the same naming and link-signing. These aliases keep the call sites in
# this module unchanged.
import run_workspace

_workspace_storage_available = run_workspace.storage_available
_workspace_safe_name = run_workspace.safe_name
_workspace_blob_name = run_workspace.blob_name
_workspace_upload_from_file = run_workspace.upload_from_file
_workspace_upload_bytes = run_workspace.upload_bytes
_workspace_download_to_file = run_workspace.download_to_file
_workspace_download_bytes = run_workspace.download_bytes
_workspace_cleanup = run_workspace.cleanup


def _workspace_signed_url(prefix: str, filename: str) -> str:
    return run_workspace.download_url(
        prefix, filename, minutes=_WORKSPACE_SIGNED_URL_MINUTES
    )


async def _maybe_cleanup_workspace(loop: asyncio.AbstractEventLoop, prefix: str | None) -> None:
    """Delete a per-invocation the blob store workspace prefix (best-effort)."""
    if prefix:
        try:
            await loop.run_in_executor(None, _workspace_cleanup, prefix)
        except Exception:
            logger.warning("Workspace cleanup failed for %s", prefix, exc_info=True)


_WORKSPACE_ZIP_MAX_MEMBERS = 50
_WORKSPACE_ZIP_MAX_UNCOMPRESSED = _WORKSPACE_MAX_BYTES


def _workspace_stage_bytes(
    prefix: str,
    filename: str,
    data: bytes,
    manifest: dict[str, str],
    total_bytes: list[int],
) -> bool:
    """Upload bytes to workspace and register a signed URL in *manifest*.

    All the blob store calls here are synchronous. Callers running on the async loop
    MUST wrap this in ``loop.run_in_executor``.
    """
    safe = _workspace_safe_name(filename)
    if total_bytes[0] + len(data) > _WORKSPACE_MAX_BYTES:
        logger.warning("Workspace size cap exceeded, skipping %s", safe)
        return False
    try:
        _workspace_upload_bytes(prefix, safe, data)
        url = _workspace_signed_url(prefix, safe)
        if not url:
            logger.warning("Could not sign workspace URL for %s", safe)
            return False
        manifest[safe] = url
        total_bytes[0] += len(data)
        return True
    except Exception:
        logger.warning("Failed to stage workspace file %s", safe, exc_info=True)
        return False


def _format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / (1024 * 1024):.1f} MB"


async def _execute_code(
    code: str,
    env_extras: dict[str, str] | None = None,
    artifacts: dict[str, str] | None = None,
    workspace_manifest: dict[str, str] | None = None,
    workspace_prefix: str | None = None,
    workspace_outputs: set[str] | None = None,
    workspace_total_bytes: list[int] | None = None,
) -> str:
    """Run Python code in an isolated sandbox.

    Calls execute_in_sandbox directly (async) to avoid the asyncio.run() /
    event-loop-closed bug that occurs when the sync wrapper is called from a
    thread via run_in_executor.  Returns combined stdout/stderr.

    When *workspace_manifest* is set, injects WORKSPACE_FILES into the sandbox
    and persists any <|FILE|> markers emitted to the the blob store workspace.
    """
    workspace_dir = "/tmp"
    if workspace_prefix:
        workspace_dir = tempfile.mkdtemp(prefix="slack_ws_")
    run_env = dict(env_extras or {})
    if workspace_manifest is not None:
        run_env["WORKSPACE_FILES"] = _json.dumps(workspace_manifest)
    loop = asyncio.get_event_loop()
    try:
        result = await execute_in_sandbox(
            code, workspace_dir, run_env or None, artifacts=artifacts
        )
        if (
            workspace_prefix
            and workspace_manifest is not None
            and workspace_total_bytes is not None
        ):
            def _upload_generated(p: str, s: str, pf: str) -> str | None:
                try:
                    _workspace_upload_from_file(p, s, pf)
                    return _workspace_signed_url(p, s)
                except Exception:
                    logger.warning("Failed to stage generated file %s", s, exc_info=True)
                    return None

            for finfo in result.get("files") or []:
                fname = str(finfo.get("filename") or "").strip()
                if not fname:
                    continue
                fpath = Path(workspace_dir) / fname
                if not fpath.is_file():
                    continue
                safe = _workspace_safe_name(fname)
                file_size = fpath.stat().st_size
                if workspace_total_bytes[0] + file_size > _WORKSPACE_MAX_BYTES:
                    logger.warning("Workspace size cap exceeded, skipping %s", safe)
                    continue

                url = await loop.run_in_executor(
                    None, _upload_generated, workspace_prefix, safe, str(fpath)
                )
                if url:
                    workspace_manifest[safe] = url
                    workspace_total_bytes[0] += file_size
                    if workspace_outputs is not None:
                        workspace_outputs.add(safe)
        parts = []
        if result.get("stdout"):
            parts.append(result["stdout"].strip())
        if result.get("stderr"):
            parts.append(f"[stderr]\n{result['stderr'].strip()}")
        output = "\n".join(parts) or "(no output)"
    except TimeoutError:
        return "[error] Code execution timed out."
    except Exception as exc:
        if _is_sandbox_infra_error(str(exc)):
            logger.warning("Sandbox unavailable after retries: %s", exc)
            return (
                f"[error] {_SANDBOX_UNAVAILABLE_MARKER} — this is an infrastructure "
                "fault, not a problem with your code. The attempt was not charged to "
                "your tool budget. Re-send the exact same code once."
            )
        return f"[error] Could not execute code: {exc}"
    finally:
        if workspace_dir != "/tmp":
            shutil.rmtree(workspace_dir, ignore_errors=True)

    return truncate_tool_output(output, _AGENT_TOOL_OUTPUT_MAX_CHARS)


async def _execute_call_artifact(
    name: str,
    params: dict,
    *,
    accessible_skill_slugs: list[str],
    skill_secrets: dict[str, str],
    agent_id: str,
    tool_audit: list | None,
    workspace_manifest: dict[str, str] | None = None,
    workspace_prefix: str | None = None,
    workspace_outputs: set[str] | None = None,
    workspace_total_bytes: list[int] | None = None,
) -> str:
    from artifact_params import ParamsValidationError
    from artifact_runtime import (
        build_call_artifact_code,
        prepare_call,
        record_run,
        resolve_artifact,
        scoped_secrets,
    )

    def _resolve() -> tuple[str | None, object | None, dict[str, str] | None]:
        db = SessionLocal()
        try:
            art = resolve_artifact(db, accessible_skill_slugs, name)
            if art is None:
                return f"[error] Unknown or unavailable artifact: {name}", None, None
            try:
                bound, declared_keys = prepare_call(art, params)
            except (ParamsValidationError, ValueError) as exc:
                return f"[error] {exc}", art, None
            env = scoped_secrets(skill_secrets, declared_keys)
            code = build_call_artifact_code(art, bound)
            return None, art, {"code": code, "env": env, "module": art.name, "source": art.code}
        finally:
            db.close()

    loop = asyncio.get_event_loop()
    err, art, payload = await loop.run_in_executor(None, _resolve)
    if err:
        if art is not None:
            _append_call_artifact_audit(tool_audit, name=name, version=art.version, output=err)
        return err
    assert payload is not None and art is not None

    output = await _execute_code(
        payload["code"],
        payload["env"],
        artifacts={payload["module"]: payload["source"]},
        workspace_manifest=workspace_manifest,
        workspace_prefix=workspace_prefix,
        workspace_outputs=workspace_outputs,
        workspace_total_bytes=workspace_total_bytes,
    )
    ok = not output.startswith("[error]")
    _append_call_artifact_audit(tool_audit, name=name, version=art.version, output=output)

    artifact_id = art.id

    def _record() -> None:
        from artifact_models import SkillArtifact

        db = SessionLocal()
        try:
            row = db.query(SkillArtifact).filter(SkillArtifact.id == artifact_id).first()
            if row:
                record_run(db, row, ok)
        finally:
            db.close()

    await loop.run_in_executor(None, _record)
    return output


# ── Slack thread history ──────────────────────────────────────────────────────

def _fetch_thread_history(xoxb: str, channel: str, thread_ts: str) -> list[dict]:
    """Fetch prior messages in a Slack thread as [{role, content, sender_id}] pairs.

    Excludes the triggering message itself (the last item in the replies list).
    Falls back to an empty list on any error so the LLM call still proceeds.
    """
    from slack_sdk import WebClient

    client = WebClient(token=xoxb)
    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts, limit=20)
        messages = result.get("messages", [])
        history: list[dict] = []
        for msg in messages[:-1]:  # exclude the triggering message
            role = "assistant" if msg.get("bot_id") else "user"
            text = _plain_text_from_slack_message(msg)
            if text:
                sender_id = str(msg.get("user") or msg.get("bot_id") or "").strip()
                entry = {"role": role, "content": text, "sender_id": sender_id}
                if msg.get("files"):
                    entry["files"] = msg["files"]
                history.append(entry)
        return history
    except Exception:
        logger.warning(
            "Could not fetch thread history for channel=%s ts=%s", channel, thread_ts
        )
        return []


def _slack_history_for_llm(history: list[dict] | None) -> list[dict]:
    """Drop Slack-only metadata before Anthropic/OpenAI message APIs."""
    out: list[dict] = []
    for msg in history or []:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        if "content" not in msg:
            continue
        out.append({"role": role, "content": msg["content"]})
    return out


# ── Slack file attachments ────────────────────────────────────────────────────

def _detect_ooxml_mime(data: bytes) -> str | None:
    """Detect Office Open XML or generic ZIP from PK archive contents."""
    if not data.startswith(b"PK\x03\x04"):
        return None
    import zipfile
    from io import BytesIO
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            names = set(zf.namelist())
            if "word/document.xml" in names:
                return _SLACK_DOCX_MIME
            if "xl/workbook.xml" in names:
                return _SLACK_XLSX_MIME
    except zipfile.BadZipFile:
        return None
    return _SLACK_ZIP_MIME


def _detect_slack_file_mime(data: bytes, filename: str = "") -> str | None:
    """Detect MIME type from magic bytes (do not trust Slack-declared mimetype)."""
    for magic, mime in _SLACK_FILE_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    ooxml_mime = _detect_ooxml_mime(data)
    if ooxml_mime:
        return ooxml_mime
    low_name = filename.lower()
    if data.startswith(b"PK\x03\x04"):
        if low_name.endswith(".docx"):
            return _SLACK_DOCX_MIME
        if low_name.endswith(".xlsx"):
            return _SLACK_XLSX_MIME
        if low_name.endswith(".zip"):
            return _SLACK_ZIP_MIME
    return None


_SLACK_ALLOWED_HOST_SUFFIXES = (".slack.com", ".slack-edge.com")


def _is_slack_url(url: str) -> bool:
    """Return True only when *url* points to a known Slack hostname."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return any(host == s.lstrip(".") or host.endswith(s) for s in _SLACK_ALLOWED_HOST_SUFFIXES)
    except Exception:
        return False


def _slack_file_download_url(xoxb: str, file_obj: dict) -> str | None:
    """Resolve a private download URL for a Slack file object.

    Validates the URL hostname before returning to prevent bot token exfiltration
    via a crafted url_private_download in a spoofed event payload.
    """
    url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if url:
        if not _is_slack_url(url):
            logger.warning(
                "Slack file id=%s has non-Slack URL — refusing to forward bot token",
                file_obj.get("id"),
            )
            return None
        return url
    file_id = file_obj.get("id")
    if not file_id:
        return None
    from slack_sdk import WebClient
    try:
        info = WebClient(token=xoxb).files_info(file=file_id)
        slack_file = info.get("file") or {}
        resolved = slack_file.get("url_private_download") or slack_file.get("url_private")
        if resolved and not _is_slack_url(resolved):
            logger.warning("files.info returned non-Slack URL for file_id=%s — skipping", file_id)
            return None
        return resolved
    except Exception:
        logger.warning("files.info failed for Slack file_id=%s", file_id, exc_info=True)
        return None


def _download_slack_file(xoxb: str, file_obj: dict) -> tuple[bytes, str] | None:
    """Download raw bytes for one Slack attachment. Returns (data, mime) or None."""
    import requests

    url = _slack_file_download_url(xoxb, file_obj)
    if not url:
        logger.warning(
            "No download URL for Slack file id=%s name=%s",
            file_obj.get("id"), file_obj.get("name"),
        )
        return None
    try:
        with requests.get(
            url,
            headers={"Authorization": f"Bearer {xoxb}"},
            timeout=(10, _SLACK_FILE_DOWNLOAD_TIMEOUT_SECS),
            stream=True,
        ) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > _SLACK_FILE_MAX_BYTES:
                    logger.warning(
                        "Slack file id=%s exceeds %d byte limit — skipping",
                        file_obj.get("id"), _SLACK_FILE_MAX_BYTES,
                    )
                    return None
                chunks.append(chunk)
            data = b"".join(chunks)
    except Exception:
        logger.warning(
            "Failed to download Slack file id=%s name=%s",
            file_obj.get("id"), file_obj.get("name"),
            exc_info=True,
        )
        return None

    mime = _detect_slack_file_mime(
        data,
        str(file_obj.get("name") or file_obj.get("title") or ""),
    )
    if mime not in _SLACK_FILE_ALLOWED_MIMES:
        logger.info(
            "Unsupported Slack attachment id=%s name=%s mime=%s",
            file_obj.get("id"), file_obj.get("name"), mime,
        )
        return None
    return data, mime


_PDF_PASSWORD_PROTECTED = "\x00__PDF_PASSWORD_PROTECTED__\x00"


def _extract_pdf_text(data: bytes) -> str:
    """Extract plain text from a PDF.

    Returns extracted text on success, ``_PDF_PASSWORD_PROTECTED`` if the PDF
    is encrypted/password-locked, or ``""`` for scanned/image-only PDFs.
    """
    try:
        from io import BytesIO
        from pdfminer.high_level import extract_text
        from pdfminer.pdfdocument import PDFPasswordIncorrect
        from document_extract import cap_extracted_text, validate_pdf_for_extraction

        ok, reason = validate_pdf_for_extraction(data)
        if not ok:
            logger.warning("PDF rejected before extraction: %s", reason)
            return ""
        try:
            text = cap_extracted_text((extract_text(BytesIO(data)) or "").strip())
            return text
        except PDFPasswordIncorrect:
            logger.warning("PDF text extraction skipped: document is password protected")
            return _PDF_PASSWORD_PROTECTED
    except Exception:
        logger.warning("PDF text extraction failed", exc_info=True)
        return ""


def _extract_docx_text(data: bytes) -> str:
    """Extract plain text from a Word (.docx) document."""
    try:
        from io import BytesIO
        from docx import Document
        from document_extract import MAX_DOCX_PARTS, cap_extracted_text, validate_zip_archive

        ok, reason = validate_zip_archive(data)
        if not ok:
            logger.warning("DOCX rejected before extraction: %s", reason)
            return ""

        doc = Document(BytesIO(data))
        parts: list[str] = []
        for paragraph in doc.paragraphs:
            text = paragraph.text.strip()
            if text:
                parts.append(text)
                if len(parts) >= MAX_DOCX_PARTS:
                    break
        if len(parts) < MAX_DOCX_PARTS:
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        text = cell.text.strip()
                        if text:
                            parts.append(text)
                            if len(parts) >= MAX_DOCX_PARTS:
                                break
                    if len(parts) >= MAX_DOCX_PARTS:
                        break
                if len(parts) >= MAX_DOCX_PARTS:
                    break
        text = cap_extracted_text("\n".join(parts).strip())
        return text
    except Exception:
        logger.warning("DOCX text extraction failed", exc_info=True)
        return ""


def _extract_xlsx_text(data: bytes) -> str:
    """Extract plain text from an Excel workbook."""
    try:
        from io import BytesIO
        from openpyxl import load_workbook
        from document_extract import MAX_DOCX_PARTS, cap_extracted_text, validate_zip_archive

        ok, reason = validate_zip_archive(data)
        if not ok:
            logger.warning("XLSX rejected before extraction: %s", reason)
            return ""

        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
        parts: list[str] = []

        for ws in wb.worksheets:
            parts.append(f"--- Sheet: {ws.title} ---")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    parts.append("\t".join(cells))
                    if len(parts) >= MAX_DOCX_PARTS:
                        break
            if len(parts) >= MAX_DOCX_PARTS:
                break
        return cap_extracted_text("\n".join(parts).strip())
    except Exception:
        logger.warning("XLSX text extraction failed", exc_info=True)
        return ""


def _validate_zip_safety(zf) -> tuple[bool, str]:
    """Pre-scan a ZipFile for Zip Bomb / Zip Slip before extraction.

    Returns (safe, reason).
    """
    cumulative = 0
    count = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        count += 1
        if count > _WORKSPACE_ZIP_MAX_MEMBERS:
            return False, f"too many members ({count} > {_WORKSPACE_ZIP_MAX_MEMBERS})"
        cumulative += info.file_size
        if cumulative > _WORKSPACE_ZIP_MAX_UNCOMPRESSED:
            return False, (
                f"cumulative uncompressed size {_format_bytes(cumulative)} "
                f"exceeds {_format_bytes(_WORKSPACE_ZIP_MAX_UNCOMPRESSED)} limit"
            )
        resolved = os.path.normpath(info.filename)
        if resolved.startswith("..") or os.path.isabs(resolved):
            return False, f"path traversal detected in member: {info.filename!r}"
    return True, ""


def _extract_zip_text_blocks(name: str, data: bytes) -> list[dict]:
    """Inline small ZIP members as text blocks for the LLM."""
    import zipfile
    from io import BytesIO

    blocks: list[dict] = []
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            safe, reason = _validate_zip_safety(zf)
            if not safe:
                logger.warning("ZIP %s rejected: %s", name, reason)
                return [{"type": "text", "text": f"=== {name} (ZIP) ===\n(rejected: {reason})"}]

            for info in zf.infolist():
                if info.is_dir():
                    continue
                member = _workspace_safe_name(info.filename)
                if not member:
                    continue
                if info.file_size > _SLACK_WORKSPACE_STAGE_THRESHOLD_BYTES:
                    blocks.append({
                        "type": "text",
                        "text": f"(member {member} too large to inline — use WORKSPACE_FILES)",
                    })
                    continue
                raw = zf.read(info.filename)
                low = member.lower()
                if low.endswith(".json"):
                    try:
                        text = raw.decode("utf-8")
                        blocks.append({
                            "type": "text",
                            "text": f"=== {member} ===\n{text}",
                        })
                    except Exception:
                        pass
                elif low.endswith((".txt", ".csv", ".md")):
                    try:
                        blocks.append({
                            "type": "text",
                            "text": f"=== {member} ===\n{raw.decode('utf-8')}",
                        })
                    except Exception:
                        pass
    except Exception:
        logger.warning("ZIP text extraction failed for %s", name, exc_info=True)
        return []
    if blocks:
        blocks.insert(0, {"type": "text", "text": f"=== {name} (ZIP) ==="})
    return blocks


def _stage_zip_members_to_workspace(
    zip_name: str,
    data: bytes,
    prefix: str,
    manifest: dict[str, str],
    total_bytes: list[int],
) -> list[str]:
    """Extract ZIP members into the the blob store workspace. Returns human-readable member list.

    Enforces member count, cumulative size, and path traversal checks.
    All the blob store calls are synchronous; callers on the async loop MUST wrap in
    ``loop.run_in_executor``.
    """
    import zipfile
    from io import BytesIO

    staged: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            safe, reason = _validate_zip_safety(zf)
            if not safe:
                logger.warning("ZIP %s rejected: %s", zip_name, reason)
                return [f"(rejected: {reason})"]

            for info in zf.infolist():
                if info.is_dir():
                    continue
                member = _workspace_safe_name(info.filename)
                if not member:
                    continue
                if info.file_size > _SLACK_FILE_MAX_BYTES:
                    staged.append(f"{member} (skipped — exceeds file size limit)")
                    continue
                member_data = zf.read(info.filename)
                if _workspace_stage_bytes(prefix, member, member_data, manifest, total_bytes):
                    staged.append(f"{member} ({_format_bytes(len(member_data))})")
                else:
                    staged.append(f"{member} (failed to stage)")
    except Exception:
        logger.warning("Failed to stage ZIP %s to workspace", zip_name, exc_info=True)
        return []
    return staged


_WORKSPACE_STAGED_SUMMARY_MAX_MEMBERS = 20


def _workspace_staged_attachment_block(name: str, size: int, members: list[str]) -> dict:
    """Build a text block describing files staged to the workspace.

    Truncates the member list in the LLM prompt when it exceeds
    ``_WORKSPACE_STAGED_SUMMARY_MAX_MEMBERS`` to avoid consuming too many
    prompt tokens.
    """
    lines = [
        f'File "{name}" ({_format_bytes(size)}) staged to workspace.',
    ]
    if members:
        shown = members[:_WORKSPACE_STAGED_SUMMARY_MAX_MEMBERS]
        summary = ", ".join(shown)
        if len(members) > _WORKSPACE_STAGED_SUMMARY_MAX_MEMBERS:
            summary += f" ... and {len(members) - _WORKSPACE_STAGED_SUMMARY_MAX_MEMBERS} more"
        lines.append(f"Contents ({len(members)} files): {summary}")
    else:
        lines.append("Contents: (no extractable members)")
    lines.append(
        "These files are available in run_python/call_artifact via the "
        "WORKSPACE_FILES environment variable (JSON map of filename to signed URL). "
        "Files are also auto-downloaded to /tmp/_workspace/ in the sandbox."
    )
    return {"type": "text", "text": "\n".join(lines)}


_LLM_ERROR_DETAIL_MAX_CHARS = 300
_GENERIC_LLM_FAILURE_MESSAGE = (
    "I encountered an error processing your request. Please try again later."
)


def _extract_llm_error_detail(exc: BaseException) -> str:
    """Pull the upstream provider message out of a model gateway/Anthropic error.

    the model gateway wraps the real 400 as a JSON string inside ``error.message`` and then
    appends its own fallback-routing dump, which is noise to the user.  Keep the
    upstream message and drop the rest.
    """
    raw = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            raw = str(err.get("message") or "")
        elif isinstance(err, str):
            raw = err
    if not raw:
        raw = str(exc)
    raw = re.split(r"No fallback model group found", raw, maxsplit=1)[0]
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if match:
        try:
            raw = _json.loads(f'"{match.group(1)}"')
        except Exception:
            raw = match.group(1)
    raw = " ".join(raw.split()).strip().strip("{}").strip()
    if len(raw) > _LLM_ERROR_DETAIL_MAX_CHARS:
        raw = raw[:_LLM_ERROR_DETAIL_MAX_CHARS].rstrip() + "..."
    return raw


def _llm_failure_reply(exc: BaseException) -> tuple[str, str]:
    """Return ``(user_facing_text, short_detail)`` for a failed LLM call."""
    detail = _extract_llm_error_detail(exc)
    if not detail:
        return _GENERIC_LLM_FAILURE_MESSAGE, ""
    status = getattr(exc, "status_code", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    # 4xx means the request itself was rejected — an oversized attachment, a bad
    # model name, a blown context window. Retrying changes nothing, so say why.
    if status_int is not None and 400 <= status_int < 500:
        return f"I couldn't process that request: {detail}", detail
    return f"I encountered an error processing your request ({detail}).", detail


def _b64_encoded_len(raw_len: int) -> int:
    """Length of the base64 encoding of *raw_len* bytes, without encoding it."""
    return ((raw_len + 2) // 3) * 4


def _fits_llm_attachment_budget(raw_len: int) -> bool:
    return _b64_encoded_len(raw_len) <= _LLM_ATTACHMENT_B64_BUDGET


def _shrink_image_for_llm(label: str, data: bytes, mime: str) -> tuple[bytes, str] | None:
    """Re-encode an image so its base64 payload fits the model's 5 MB cap.

    Returns ``(data, mime)`` — unchanged when it already fits — or ``None`` when
    the image cannot be decoded or cannot be shrunk far enough.  Animated GIFs
    collapse to their first frame, which is the best a still-image part can carry.
    """
    if _fits_llm_attachment_budget(len(data)):
        return data, mime
    try:
        from PIL import Image
    except Exception:
        logger.warning("Pillow unavailable — cannot shrink oversized image %r", label)
        return None
    from io import BytesIO

    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
            # JPEG has no alpha channel; flatten onto white so transparent
            # screenshots do not come back with black backgrounds.
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                base = flat
            else:
                base = img.convert("RGB")
    except Exception:
        logger.warning("Could not decode image %r for downscaling", label, exc_info=True)
        return None

    width, height = base.size
    # Byte count tracks pixel area, so start near sqrt(target ratio) instead of
    # walking down from full size — usually one or two encodes instead of eight.
    ratio = _LLM_ATTACHMENT_B64_BUDGET / max(1, _b64_encoded_len(len(data)))
    scale = min(1.0, (ratio ** 0.5) * 1.1)
    for _ in range(_LLM_IMAGE_SHRINK_MAX_STEPS):
        w = max(1, int(width * scale))
        h = max(1, int(height * scale))
        resized = base if (w, h) == (width, height) else base.resize((w, h), Image.LANCZOS)
        for quality in _LLM_IMAGE_SHRINK_QUALITIES:
            buf = BytesIO()
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if _fits_llm_attachment_budget(len(out)):
                logger.info(
                    "Downscaled oversized image %r: %s %dx%d -> %s JPEG q%d %dx%d",
                    label, _format_bytes(len(data)), width, height,
                    _format_bytes(len(out)), quality, w, h,
                )
                return out, "image/jpeg"
        scale *= _LLM_IMAGE_SHRINK_STEP_SCALE
    logger.warning(
        "Could not shrink image %r (%s) under the %s attachment budget",
        label, _format_bytes(len(data)), _format_bytes(_LLM_ATTACHMENT_B64_BUDGET),
    )
    return None


def _slack_file_to_content_blocks(name: str, data: bytes, mime: str) -> list[dict]:
    """Convert one downloaded Slack file into Anthropic message content blocks."""
    label = name or "attachment"
    if mime.startswith("image/"):
        shrunk = _shrink_image_for_llm(label, data, mime)
        if shrunk is None:
            return [{"type": "text", "text": (
                f"=== {label} (image, {_format_bytes(len(data))}) ===\n"
                "(This image is too large to send to the model and could not be "
                "downscaled to fit. Tell the user the image was too large to read "
                "and ask them to re-upload a smaller or cropped version, then "
                "continue with any other evidence in the thread.)"
            )}]
        data, mime = shrunk
        b64 = base64.b64encode(data).decode("ascii")
        return [{
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        }]

    if mime == "application/pdf":
        extracted = _extract_pdf_text(data)
        if extracted == _PDF_PASSWORD_PROTECTED:
            logger.info("Skipping password-protected PDF %r — notifying LLM in context", label)
            return [{"type": "text", "text": (
                f"=== {label} (PDF) ===\n"
                "(This PDF is password-protected and could not be read. "
                "Acknowledge this to the user and continue the investigation "
                "using any other evidence available in the thread.)"
            )}]
        if extracted:
            return [{"type": "text", "text": f"=== {label} (PDF) ===\n{extracted}"}]
        # Scanned/image PDFs — pass the document to the vision-capable model.
        if not _fits_llm_attachment_budget(len(data)):
            logger.warning(
                "Scanned PDF %r (%s) exceeds the attachment budget — sending a note instead",
                label, _format_bytes(len(data)),
            )
            return [{"type": "text", "text": (
                f"=== {label} (PDF, {_format_bytes(len(data))}) ===\n"
                "(This PDF has no extractable text and is too large to send as an "
                "image. Tell the user it could not be read and ask them to upload "
                "a smaller extract or screenshots of the relevant pages, then "
                "continue with any other evidence in the thread.)"
            )}]
        b64 = base64.b64encode(data).decode("ascii")
        return [{
            "type": "document",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        }]

    if mime == _SLACK_DOCX_MIME:
        extracted = _extract_docx_text(data)
        if extracted:
            return [{"type": "text", "text": f"=== {label} (Word) ===\n{extracted}"}]
        return [{"type": "text", "text": f"=== {label} (Word) ===\n(no extractable text)"}]

    if mime == _SLACK_XLSX_MIME:
        extracted = _extract_xlsx_text(data)
        if extracted:
            return [{"type": "text", "text": f"=== {label} (Excel) ===\n{extracted}"}]
        return [{"type": "text", "text": f"=== {label} (Excel) ===\n(no extractable text)"}]

    if mime == _SLACK_ZIP_MIME:
        return _extract_zip_text_blocks(label, data)

    return []


def _build_slack_attachment_blocks(
    xoxb: str,
    files: list,
    *,
    workspace_prefix: str | None = None,
    workspace_manifest: dict[str, str] | None = None,
    workspace_total_bytes: list[int] | None = None,
) -> list[dict]:
    """Download Slack files and build multimodal content blocks for the LLM."""
    blocks: list[dict] = []
    total_batch_bytes = 0
    for file_obj in files[:_SLACK_FILE_MAX_COUNT]:
        if not isinstance(file_obj, dict):
            continue
        if total_batch_bytes >= _SLACK_FILE_MAX_BATCH_BYTES:
            logger.info(
                "_build_slack_attachment_blocks: batch cap %d bytes reached, dropping remaining files",
                _SLACK_FILE_MAX_BATCH_BYTES,
            )
            break
        downloaded = _download_slack_file(xoxb, file_obj)
        if not downloaded:
            continue
        data, mime = downloaded
        total_batch_bytes += len(data)
        if total_batch_bytes > _SLACK_FILE_MAX_BATCH_BYTES:
            logger.info(
                "_build_slack_attachment_blocks: batch cap exceeded after file %s, dropping",
                file_obj.get("name") or file_obj.get("id"),
            )
            break
        name = str(file_obj.get("name") or file_obj.get("title") or "attachment")
        # Oversized images get downscaled for vision below, which loses detail.
        # Stage the original too, so run_python/call_artifact can still read it
        # at full resolution (OCR, pixel inspection, crops).
        if (
            workspace_prefix
            and workspace_manifest is not None
            and workspace_total_bytes is not None
            and _workspace_storage_available()
            and mime.startswith("image/")
            and not _fits_llm_attachment_budget(len(data))
            and _workspace_stage_bytes(
                workspace_prefix, name, data, workspace_manifest, workspace_total_bytes
            )
        ):
            blocks.append(_workspace_staged_attachment_block(name, len(data), [name]))
            blocks.append({"type": "text", "text": (
                f'"{name}" is too large to send at full resolution, so the image '
                "part below is a downscaled copy. Read the staged original from "
                "the workspace if you need finer detail."
            )})
        if (
            workspace_prefix
            and workspace_manifest is not None
            and workspace_total_bytes is not None
            and _workspace_storage_available()
            and len(data) >= _SLACK_WORKSPACE_STAGE_THRESHOLD_BYTES
            and mime in (_SLACK_ZIP_MIME, _SLACK_XLSX_MIME)
        ):
            if mime == _SLACK_ZIP_MIME:
                members = _stage_zip_members_to_workspace(
                    name, data, workspace_prefix, workspace_manifest, workspace_total_bytes
                )
                blocks.append(_workspace_staged_attachment_block(name, len(data), members))
            elif _workspace_stage_bytes(
                workspace_prefix, name, data, workspace_manifest, workspace_total_bytes
            ):
                blocks.append(_workspace_staged_attachment_block(name, len(data), [name]))
            continue
        blocks.extend(_slack_file_to_content_blocks(name, data, mime))
    return blocks


_SLACK_WORKSPACE_BUNDLE_THRESHOLD = 3


def _deliver_workspace_outputs_to_slack(
    xoxb: str,
    channel: str,
    thread_ts: str,
    workspace_prefix: str,
    workspace_outputs: set[str],
) -> None:
    """Upload generated workspace files to the Slack thread (best-effort).

    Downloads directly from the blob store via the internal client (no signed URL hop).
    When the output count exceeds ``_SLACK_WORKSPACE_BUNDLE_THRESHOLD``, files
    are bundled into a single ZIP archive to conserve Slack rate-limit budget.

    This function is synchronous and MUST be called from ``run_in_executor``.
    """
    if not workspace_outputs or not _workspace_storage_available():
        return
    from slack_sdk import WebClient
    import zipfile

    client = WebClient(token=xoxb)
    tmp_dir = tempfile.mkdtemp(prefix="ws_deliver_")

    try:
        downloaded: list[tuple[str, str]] = []
        total_bytes = 0
        for filename in sorted(workspace_outputs):
            if len(downloaded) >= _SLACK_WORKSPACE_UPLOAD_MAX_FILES:
                logger.info("Slack workspace delivery: file count cap reached")
                break
            try:
                local_path = os.path.join(tmp_dir, _workspace_safe_name(filename))
                ok = _workspace_download_to_file(workspace_prefix, filename, local_path)
                if not ok:
                    continue
                fsize = os.path.getsize(local_path)
                if total_bytes + fsize > _SLACK_WORKSPACE_UPLOAD_MAX_BYTES:
                    logger.info("Slack workspace delivery: byte cap reached")
                    break
                downloaded.append((filename, local_path))
                total_bytes += fsize
            except Exception:
                logger.warning("Failed to download workspace file %s", filename, exc_info=True)

        if not downloaded:
            return

        if len(downloaded) > _SLACK_WORKSPACE_BUNDLE_THRESHOLD:
            bundle_path = os.path.join(tmp_dir, "output.zip")
            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for fname, lpath in downloaded:
                    zf.write(lpath, arcname=_workspace_safe_name(fname))
            try:
                client.files_upload_v2(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    file=bundle_path,
                    filename="output.zip",
                    title="Generated files",
                )
            except Exception:
                logger.warning("Failed to upload bundled output.zip to Slack", exc_info=True)
        else:
            for fname, lpath in downloaded:
                try:
                    client.files_upload_v2(
                        channel=channel,
                        thread_ts=thread_ts or None,
                        file=lpath,
                        filename=fname,
                        title=fname,
                    )
                except Exception:
                    logger.warning("Failed to upload workspace file %s to Slack", fname, exc_info=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _compose_user_message_content(user_text: str, attachment_blocks: list[dict]) -> str | list:
    """Merge optional text with attachment blocks into an LLM user message."""
    if not attachment_blocks:
        return user_text
    content: list[dict] = list(attachment_blocks)
    if user_text:
        content.append({"type": "text", "text": user_text})
    else:
        content.append({
            "type": "text",
            "text": "The user shared the attached file(s) with no accompanying message.",
        })
    return content


def _audit_user_message(user_text: str, files: list) -> str:
    """Human-readable summary for exchange logging when files are present."""
    if not files:
        return user_text
    names = [
        str(f.get("name") or f.get("title") or "file")
        for f in files[:_SLACK_FILE_MAX_COUNT]
        if isinstance(f, dict)
    ]
    prefix = f"[attachments: {', '.join(names)}]" if names else "[attachments]"
    return f"{prefix} {user_text}".strip()


# ── Agent-level access control ────────────────────────────────────────────────

async def _check_agent_access(email: str, agent: Agent) -> bool:
    """Return True if *email* may interact with this agent at all.

    Pass-through conditions (no DB hit required):
    - Super-user allowlist
    - Agent creator (creator_email match)

    Otherwise: check for an active, non-expired AgentAccessGrant.
    Fail-closed: any DB exception returns False.
    """
    from skills_acl import is_super_user
    if is_super_user(email):
        return True
    from agent_access_helpers import creator_email_matches

    if creator_email_matches(agent.creator_email, email):
        return True
    try:
        db = SessionLocal()
        try:
            now = _utcnow()
            grant = (
                db.query(AgentAccessGrant)
                .filter(
                    AgentAccessGrant.agent_id == str(agent.id),
                    AgentAccessGrant.user_email == email,
                    AgentAccessGrant.status == "approved",
                )
                .filter(
                    (AgentAccessGrant.expires_at == None) |  # noqa: E711
                    (AgentAccessGrant.expires_at > now)
                )
                .first()
            )
            return grant is not None
        finally:
            db.close()
    except Exception:
        logger.exception("Agent access check failed for email=%s agent=%s — denying", email, agent.id)
        return False


def _post_agent_access_denied_sync(xoxb: str, channel: str, thread_ts: str, agent_name: str, agent_id: str) -> None:
    """Post an agent access denied message (synchronous, for executor)."""
    from slack_sdk import WebClient
    text = (
        f"You don't have access to *{agent_name}*.\n\n"
        f"Visit *<{_APP_BASE_URL}/agents|the Agents page>* to request access. "
        "Once approved, you'll be able to interact with this agent."
    )
    try:
        WebClient(token=xoxb).chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts or None,
        )
    except Exception:
        logger.exception("Failed to post agent-access-denied message to channel=%s", channel)


def _agent_unavailable_notice(agent_name: str, status: str) -> str:
    """User-visible message when an agent cannot accept Slack interactions."""
    if status == "paused":
        return (
            f"*{agent_name}* is currently paused.\n\n"
            f"Visit *<{_APP_BASE_URL}/agents|the Agents page>* and activate "
            "the agent to resume Slack interactions."
        )
    if status == "registered":
        return (
            f"*{agent_name}* is not active yet.\n\n"
            f"Activate it from *<{_APP_BASE_URL}/agents|the Agents page>* "
            "before interacting on Slack."
        )
    return (
        f"*{agent_name}* is not available right now.\n\n"
        f"Check its status in *<{_APP_BASE_URL}/agents|the Agents page>*."
    )


def _post_agent_unavailable_sync(
    xoxb: str,
    channel: str,
    thread_ts: str,
    agent_name: str,
    status: str,
) -> None:
    """Tell a human the agent is paused / not active (synchronous, for executor)."""
    from slack_sdk import WebClient

    try:
        WebClient(token=xoxb).chat_postMessage(
            channel=channel,
            text=_agent_unavailable_notice(agent_name, status),
            thread_ts=thread_ts or None,
        )
    except Exception:
        logger.exception("Failed to post agent-unavailable message to channel=%s", channel)


def _notify_agent_unavailable_from_event(agent: Agent, event: dict) -> None:
    """Best-effort Slack notice when discarding an event for a non-active agent."""
    channel = (event.get("channel") or "").strip()
    if not channel:
        return
    xoxb = _read_secret(agent.xoxb_secret_ref)
    if not xoxb:
        return
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    _post_agent_unavailable_sync(
        xoxb,
        channel,
        thread_ts,
        getattr(agent, "name", "Agent"),
        getattr(agent, "status", "unknown"),
    )


def _check_slack_app_grant(agent_id: str, slack_user_id: str) -> bool:
    """Return True if the Slack app identified by *slack_user_id* has an explicit
    grant to invoke *agent_id*.

    Intentionally synchronous — called from the async dispatch path via a thread
    executor so the DB call does not block the event loop.
    """
    if not slack_user_id:
        return False
    try:
        from access_models import AgentSlackAppGrant
        db = SessionLocal()
        try:
            grant = (
                db.query(AgentSlackAppGrant)
                .filter(
                    AgentSlackAppGrant.agent_id == str(agent_id),
                    AgentSlackAppGrant.slack_user_id == slack_user_id,
                )
                .first()
            )
            return grant is not None
        finally:
            db.close()
    except Exception:
        logger.exception(
            "Error checking Slack app grant for agent=%s slack_user=%s — denying",
            agent_id, slack_user_id,
        )
        return False


_WORKFLOW_APP_ID_CACHE: dict[str, str] = {}


def _normalise_workflow_identity(raw: str) -> str:
    """Canonicalise a Workflow Builder / app ID for grant matching."""
    value = (raw or "").strip()
    if not value:
        return ""
    if value.startswith(("Wf", "WF", "Wt", "WT")):
        return value[0].upper() + value[1].lower() + value[2:]
    if value.startswith("a") and len(value) > 1 and value[1:].isalnum():
        return "A" + value[1:]
    return value


def _lookup_app_id_by_bot_id(bot_id: str, xoxb: str | None = None) -> str:
    """Return the Slack app_id (A…) for a bot_id (B…), when available."""
    bot_id = (bot_id or "").strip()
    if not bot_id:
        return ""
    try:
        from slack_app_registry import SlackAppRecord

        db = SessionLocal()
        try:
            rec = db.query(SlackAppRecord).filter(SlackAppRecord.bot_id == bot_id).first()
            if rec and rec.app_id:
                return str(rec.app_id).strip()
        finally:
            db.close()
    except Exception:
        logger.warning("Registry lookup failed for bot_id=%s app_id", bot_id, exc_info=True)

    if not xoxb:
        return ""
    try:
        from slack_sdk import WebClient

        result = WebClient(token=xoxb).bots_info(bot=bot_id)
        bot = result.get("bot", {}) if isinstance(result.get("bot"), dict) else {}
        return str(bot.get("app_id") or "").strip()
    except Exception as exc:
        logger.warning("bots.info failed for bot_id=%s app_id lookup: %s", bot_id, exc)
        return ""


def _resolve_workflow_app_id(workflow_id: str, xoxb: str | None = None) -> str:
    """Best-effort map a Workflow Builder Wf… ID to its workflow app_id (A…)."""
    wf_id = _normalise_workflow_identity(workflow_id)
    if not wf_id:
        return ""
    if wf_id.startswith("A"):
        return wf_id
    if not wf_id.startswith("Wf"):
        return ""

    cached = _WORKFLOW_APP_ID_CACHE.get(wf_id)
    if cached:
        return cached

    token = (xoxb or "").strip()
    if not token:
        return ""
    try:
        from slack_sdk import WebClient

        client = WebClient(token=token)
        resp = client.api_call(
            api_method="functions.workflows.steps.list",
            json={"workflow_id": wf_id},
        )
        data = resp.data if hasattr(resp, "data") else resp
        app_id = ""
        if isinstance(data, dict):
            app_id = str(data.get("workflow_app_id") or "").strip()
            if not app_id:
                workflow = data.get("workflow")
                if isinstance(workflow, dict):
                    app_id = str(
                        workflow.get("workflow_app_id") or workflow.get("app_id") or ""
                    ).strip()
        app_id = _normalise_workflow_identity(app_id)
        if app_id:
            _WORKFLOW_APP_ID_CACHE[wf_id] = app_id
        return app_id
    except Exception as exc:
        logger.warning("Could not resolve workflow app_id for %s: %s", wf_id, exc)
        return ""


def _persist_workflow_resolved_app_id(grant_id: str, app_id: str) -> None:
    """Cache a resolved workflow app_id on the grant row."""
    app_id = _normalise_workflow_identity(app_id)
    if not app_id:
        return
    try:
        from access_models import AgentSlackWorkflowGrant

        db = SessionLocal()
        try:
            grant = db.get(AgentSlackWorkflowGrant, grant_id)
            if grant and not grant.resolved_app_id:
                grant.resolved_app_id = app_id
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.warning(
            "Failed to persist resolved_app_id grant=%s app_id=%s",
            grant_id,
            app_id,
            exc_info=True,
        )


def _slack_workflow_identity_candidates(event: dict, xoxb: str | None = None) -> set[str]:
    """Collect workflow/app IDs from a bot-authored Slack event."""
    ids: set[str] = set()
    for key in ("app_id",):
        value = _normalise_workflow_identity(str(event.get(key) or ""))
        if value:
            ids.add(value)

    bot_profile = event.get("bot_profile")
    if isinstance(bot_profile, dict):
        value = _normalise_workflow_identity(str(bot_profile.get("app_id") or ""))
        if value:
            ids.add(value)

    bot_id = str(event.get("bot_id") or "").strip()
    if bot_id:
        app_id = _lookup_app_id_by_bot_id(bot_id, xoxb=xoxb)
        if app_id:
            ids.add(_normalise_workflow_identity(app_id))

    return ids


def _check_slack_workflow_grant(
    agent_id: str,
    candidate_ids: set[str],
    xoxb: str | None = None,
) -> bool:
    """Return True when an authorised Workflow Builder workflow invoked the agent."""
    normalised = {_normalise_workflow_identity(c) for c in candidate_ids if c}
    normalised.discard("")
    if not normalised:
        return False
    try:
        from access_models import AgentSlackWorkflowGrant

        db = SessionLocal()
        try:
            grants = (
                db.query(AgentSlackWorkflowGrant)
                .filter(AgentSlackWorkflowGrant.agent_id == str(agent_id))
                .all()
            )
            for grant in grants:
                grant_id = _normalise_workflow_identity(grant.workflow_id or "")
                resolved = _normalise_workflow_identity(grant.resolved_app_id or "")
                if grant_id in normalised or (resolved and resolved in normalised):
                    return True
                if grant_id.startswith("Wf"):
                    if not resolved:
                        resolved = _resolve_workflow_app_id(grant_id, xoxb=xoxb)
                        if resolved:
                            _persist_workflow_resolved_app_id(str(grant.id), resolved)
                    if resolved and resolved in normalised:
                        return True
            return False
        finally:
            db.close()
    except Exception:
        logger.exception(
            "Error checking Slack workflow grant for agent=%s candidates=%s — denying",
            agent_id,
            sorted(normalised),
        )
        return False


async def _filter_accessible_skills(
    email: str,
    agent: Agent,
    skill_override: list[str] | None = None,
) -> list[str]:
    """Return the subset of the agent's skill_ids the sender can access.

    When skill_override is provided (e.g. per-channel effective skill list),
    the ACL check runs against that list instead of agent.skill_ids.
    Skills the user lacks access to are silently filtered out; the agent runs
    with the reduced skill set rather than being denied entirely.
    """
    import json as _j
    if skill_override is not None:
        all_slugs = skill_override
    else:
        try:
            all_slugs = _j.loads(agent.skill_ids or "[]")
        except (ValueError, TypeError):
            return []
    if not all_slugs:
        return []
    missing = await _check_agent_skill_access(email, agent, skill_ids=all_slugs)
    missing_set = set(missing)
    return [s for s in all_slugs if s not in missing_set]


# ── Sender identity + skill access control ───────────────────────────────────

def _fetch_sender_profile(xoxb: str, slack_user_id: str) -> dict:
    """Resolve a Slack user ID to their email, real name, and display name.

    Calls ``users.info`` using the bot token.  Requires the ``users:read`` and
    ``users:read.email`` scopes on the Slack app.

    Returns an empty dict on any error so the caller can gate on the empty email.
    Also returns ``is_bot`` and ``bot_user_id`` so the event handler can route
    Slack app senders through the dedicated bot-access path.
    """
    if not slack_user_id:
        return {}
    from slack_sdk import WebClient
    try:
        result = WebClient(token=xoxb).users_info(user=slack_user_id)
        user = result.get("user", {})
        profile = user.get("profile", {})
        email = profile.get("email", "")
        is_bot: bool = bool(user.get("is_bot", False))
        bot_user_id: str = user.get("profile", {}).get("bot_id", "") or ""
        logger.info(
            "Resolved Slack profile user_id=%s email=%s is_bot=%s",
            slack_user_id, email or "(none)", is_bot,
        )
        return {
            "email": email,
            "real_name": profile.get("real_name", ""),
            "display_name": profile.get("display_name", ""),
            "is_bot": is_bot,
            "bot_user_id": bot_user_id,
            # Pass through the Slack user_id so the bot path can look up grants
            "slack_user_id": slack_user_id,
        }
    except Exception as exc:
        logger.warning(
            "Could not fetch Slack profile for user_id=%s: %s", slack_user_id, exc
        )
        return {}


def _event_is_bot_authored(event: dict) -> bool:
    """Return True when Slack marks the message as bot/integration-authored."""
    return bool(event.get("bot_id")) or event.get("subtype") == "bot_message"


def _lookup_slack_user_id_by_app_id(app_id: str) -> str:
    """Map a Slack ``app_id`` (A…) to the bot member ``user_id`` (U…) via registry."""
    app_id = (app_id or "").strip()
    if not app_id:
        return ""
    try:
        from slack_app_registry import SlackAppRecord

        db = SessionLocal()
        try:
            rec = (
                db.query(SlackAppRecord)
                .filter(SlackAppRecord.app_id == app_id)
                .filter(SlackAppRecord.user_id.isnot(None))
                .first()
            )
            if rec and rec.user_id:
                return str(rec.user_id)
        finally:
            db.close()
    except Exception:
        logger.warning("Registry lookup failed for app_id=%s", app_id, exc_info=True)
    return ""


def _lookup_slack_user_id_by_bot_id(bot_id: str, xoxb: str | None = None) -> str:
    """Map a Slack ``bot_id`` (B…) to the bot member ``user_id`` (U…).

    Resolution order:
      1. Local app registry by ``bot_id``
      2. ``bots.info`` ``user_id`` (when Slack returns one)
      3. ``bots.info`` ``app_id`` → local registry by ``app_id``

    Step 3 covers apps (e.g. Security Hooks) whose message events carry a
    secondary ``bot_id`` that ``bots.info`` does not link to a ``user_id``,
    while ``users.list`` / the registry still have the canonical U… under the
    same ``app_id``. Without this, authorized bot mentions are dropped as
    "unresolvable sender" before the grant check can run.
    """
    bot_id = (bot_id or "").strip()
    if not bot_id:
        return ""
    try:
        from slack_app_registry import SlackAppRecord

        db = SessionLocal()
        try:
            rec = db.query(SlackAppRecord).filter(SlackAppRecord.bot_id == bot_id).first()
            if rec and rec.user_id:
                return str(rec.user_id)
        finally:
            db.close()
    except Exception:
        logger.warning("Registry lookup failed for bot_id=%s", bot_id, exc_info=True)

    if not xoxb:
        return ""
    try:
        from slack_sdk import WebClient

        result = WebClient(token=xoxb).bots_info(bot=bot_id)
        bot = result.get("bot", {}) if isinstance(result.get("bot"), dict) else {}
        user_id = str(bot.get("user_id") or "").strip()
        if user_id:
            logger.info("Resolved slack user_id=%s via bots.info for bot_id=%s", user_id, bot_id)
            return user_id

        app_id = str(bot.get("app_id") or "").strip()
        if app_id:
            via_app = _lookup_slack_user_id_by_app_id(app_id)
            if via_app:
                logger.info(
                    "Resolved slack user_id=%s via bots.info app_id=%s for bot_id=%s",
                    via_app, app_id, bot_id,
                )
                return via_app
        return ""
    except Exception as exc:
        logger.warning("bots.info failed for bot_id=%s: %s", bot_id, exc)
        return ""


def _resolve_sender_slack_user_id(event: dict, xoxb: str | None = None) -> str:
    """Return the sender's Slack member ID from an inbound event.

    Slack bot-authored ``app_mention`` events often omit ``user`` and carry
    ``bot_id`` instead — resolve through the registry / ``bots.info``.
    """
    slack_user_id = str(event.get("user") or "").strip()
    if slack_user_id:
        return slack_user_id
    event_bot_id = str(event.get("bot_id") or "").strip()
    if not event_bot_id:
        return ""
    resolved = _lookup_slack_user_id_by_bot_id(event_bot_id, xoxb=xoxb)
    if resolved:
        logger.info(
            "Resolved sender slack_user_id=%s from event bot_id=%s",
            resolved,
            event_bot_id,
        )
    return resolved


def _max_tool_turns_for_agent(agent: Agent) -> int:
    """Return the tool-use turn budget for *agent*.

    Priority:
    1. agent.max_tool_turns_override — explicit per-agent setting (0 = no tools).
    2. A per-skill override from SKILL_TOOL_TURN_OVERRIDES.
    3. Default _MAX_TOOL_TURNS.
    """
    override = getattr(agent, "max_tool_turns_override", None)
    if override is not None:
        return int(override)
    try:
        skill_ids: list[str] = _json.loads(agent.skill_ids or "[]")
    except (ValueError, TypeError):
        return _MAX_TOOL_TURNS
    # Most permissive override wins, so attaching a turn-hungry skill is never
    # made worse by also attaching a cheap one.
    matched = [
        _SKILL_TOOL_TURN_OVERRIDES[normalized]
        for slug in skill_ids
        if (normalized := (slug or "").strip().lower()) in _SKILL_TOOL_TURN_OVERRIDES
    ]
    return max(matched) if matched else _MAX_TOOL_TURNS


async def _check_agent_skill_access(
    email: str,
    agent: Agent,
    *,
    skill_ids: list[str] | None = None,
) -> list[str]:
    """Return slugs of skills attached to this agent that the sender cannot access.

    Checks Tier 1 (department / executive) first via ``skills_acl``, then falls
    back to approved cross-department grants (Tier 2) stored in the DB.

    When ``skill_ids`` is provided, the ACL check runs against that list instead
    of ``agent.skill_ids`` (used for per-channel skill overrides).

    Returns an empty list when all skills are accessible or no email is known.
    """
    if not email:
        return []

    if skill_ids is None:
        try:
            skill_ids = _json.loads(agent.skill_ids or "[]")
        except (ValueError, TypeError):
            return []

    if not skill_ids:
        return []

    # Resolve department + job title from IAM
    department = ""
    job_title: str | None = None
    try:
        from org_directory import get_employee_by_email
        employee = await get_employee_by_email(email)
        if employee:
            department = employee.department or ""
            job_title = employee.role or None
    except Exception:
        logger.warning("IAM lookup failed for email=%s — treating as no-department user", email)

    # Resolve owned + collaborator slugs for critical-risk Tier 1 access.
    owned_slugs: frozenset[str] = frozenset()
    collaborator_slugs: frozenset[str] = frozenset()
    try:
        from skill_submission_models import SkillSubmission
        from skill_submission_models import REVIEW_STATUS_PASS as _PASS
        from skill_collaborators import collaborator_slugs_for_email
        _db_check = SessionLocal()
        try:
            owned_slugs = frozenset(
                r.proposed_slug
                for r in _db_check.query(SkillSubmission.proposed_slug).filter(
                    SkillSubmission.user_email == email,
                    SkillSubmission.review_status == _PASS,
                ).all()
            )
            collaborator_slugs = collaborator_slugs_for_email(_db_check, email)
        finally:
            _db_check.close()
    except Exception:
        logger.debug("Could not resolve owned/collaborator slugs for %s", email)

    # Tier 1: slugs accessible automatically by department / executive status
    try:
        tier1_slugs = resolve_tier1_slugs(
            department,
            job_title=job_title,
            owned_slugs=owned_slugs,
            collaborator_slugs=collaborator_slugs,
            email=email,
        )
    except Exception:
        tier1_slugs = set()

    missing: list[str] = []
    db = SessionLocal()
    try:
        for slug in skill_ids:
            if slug in tier1_slugs:
                continue
            # Tier 2: check for an active approved cross-department grant by email
            grant = (
                db.query(CrossDepartmentGrant)
                .filter(
                    CrossDepartmentGrant.user_email == email,
                    CrossDepartmentGrant.skill_slug == slug,
                    CrossDepartmentGrant.status == "approved",
                )
                .filter(
                    # Allow grants with no expiry OR not yet expired
                    (CrossDepartmentGrant.expires_at == None) |  # noqa: E711
                    (CrossDepartmentGrant.expires_at > _utcnow())
                )
                .first()
            )
            if not grant:
                missing.append(slug)
    except Exception:
        # Fail closed: if the access check errors, deny all skills rather than
        # allowing an unchecked request through.
        logger.exception("DB grant check failed for email=%s — denying all skills", email)
        return list(skill_ids)
    finally:
        db.close()

    return missing


def _post_skill_access_denied_sync(
    xoxb: str, channel: str, thread_ts: str, missing_slugs: list[str]
) -> None:
    """Post a skill access denied message to Slack (synchronous, for executor)."""
    from slack_sdk import WebClient

    bullet_lines = "\n".join(
        f"• *{slug}* — go to Skills Hub → find _{slug}_ → click *Request Access*"
        for slug in missing_slugs
    )
    text = (
        "You don't have access to one or more skills required by this agent.\n\n"
        f"*Missing access:*\n{bullet_lines}\n\n"
        f"Visit *<{_APP_BASE_URL}|the platform>* to request access. "
        "Once approved, you'll be able to use this agent."
    )
    try:
        WebClient(token=xoxb).chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts or None,
        )
    except Exception:
        logger.exception(
            "Failed to post skill-access-denied message to channel=%s", channel
        )


# ── Owner self-configuration ─────────────────────────────────────────────────

_CONFIG_KEYWORDS: frozenset[str] = frozenset({
    "remember", "forget", "don't remember", "your personality",
    "change how you", "update your", "your instructions",
    "your soul", "your skill", "clear your memory",
    "add to your memory", "remove from your memory",
    "your behavior", "your behaviour", "change your", "reset your memory",
})


def _is_owner_or_collaborator(agent: Agent, sender_email: str) -> bool:
    """Return True if *sender_email* is the agent creator (or a collaborator).

    collaborator_ids currently stores Cloudflare sub IDs; email-based
    collaborator support can be wired in later without changing the gate logic.
    """
    if not sender_email:
        return False
    from agent_access_helpers import creator_email_matches

    return creator_email_matches(agent.creator_email, sender_email)


def _looks_like_config_command(text: str) -> bool:
    """Lightweight keyword pre-filter — avoids an LLM call for normal messages."""
    low = text.lower()
    return any(kw in low for kw in _CONFIG_KEYWORDS)


_CONFIG_EXTRACTION_PROMPT = """\
You are a configuration assistant for an AI agent. The agent's owner has sent a \
natural-language configuration command. Extract the intended change and output \
ONLY valid JSON — no explanation, no code fences.

Schema (include only keys relevant to the chosen action):
{{
  "action":        "upsert_memory" | "delete_memory" | "reset_memories" | "update_soul" | "update_skill",
  "key":           "<memory key, ≤40 chars>",
  "value":         "<memory value, ≤500 chars>",
  "scope":         "agent",
  "soul_patch":    "<text to add or replace in personality/soul>",
  "soul_mode":     "append" | "replace",
  "skill_slug":    "<slug of skill to update — must be one of the agent's assigned skills>",
  "skill_patch":   "<new instructions to add or replace in the skill file>",
  "skill_mode":    "append" | "replace",
  "human_summary": "<one sentence confirming what you did, in first person>"
}}

Owner message: {user_msg}
Agent name: {agent_name}
Agent skill slugs: {skill_slugs}
"""


# ── Channel instruction handling (platform agent) ─────────────────────────────

_CHANNEL_INSTRUCTION_KEYWORDS: frozenset[str] = frozenset({
    "from now on", "in this channel", "your role here", "you should focus",
    "you are supposed to", "only answer", "always respond", "for this channel",
    "your purpose here", "going forward", "from here on", "in this channel you",
    "you should answer", "you should only", "you should be", "respond about",
    "answer questions about", "help with", "you are here to", "your job here",
    "from now", "in here", "this channel is for", "focus on",
})

_CHANNEL_INSTRUCTION_EXTRACTION_PROMPT = """\
A user is telling an AI agent how to behave in a specific Slack channel.
Extract the instruction and output ONLY valid JSON — no explanation, no code fences.

Schema:
{{
  "soul_patch": "<condensed instruction text to add to the agent's channel personality, ≤400 chars>",
  "memory_key": "<snake_case key for this instruction, ≤40 chars>",
  "memory_value": "<concise summary of the instruction, ≤200 chars>",
  "human_summary": "<one sentence confirming what was saved, in first person>"
}}

Channel: #{channel_name}
Agent: {agent_name}
User instruction: {user_msg}
"""


def _looks_like_channel_instruction(text: str) -> bool:
    """Return True if the message looks like a channel-scoped configuration instruction."""
    low = text.lower()
    return any(kw in low for kw in _CHANNEL_INSTRUCTION_KEYWORDS)


async def _handle_channel_instruction(
    *,
    agent: "Agent",
    channel_id: str,
    channel_name: str,
    user_text: str,
    xoxb: str,
    thread_ts: str,
    sender_email: str,
) -> None:
    """Handle a channel-scoped instruction from an agent owner/collaborator.

    Extracts the instruction via LLM, appends it to the channel's soul_text, and
    stores it as a channel-scoped memory so it persists across sessions.
    """
    import json as _json2

    loop = asyncio.get_running_loop()

    async def _post(text: str) -> None:
        from slack_sdk import WebClient as _WC
        await loop.run_in_executor(
            None,
            lambda: _WC(token=xoxb).chat_postMessage(
                channel=channel_id, thread_ts=thread_ts or None, text=text
            ),
        )

    if not _MODEL_GATEWAY_API_KEY:
        return

    try:
        prompt = _CHANNEL_INSTRUCTION_EXTRACTION_PROMPT.format(
            channel_name=channel_name,
            agent_name=agent.name or APP_NAME,
            user_msg=user_text[:1500],
        )
        client = anthropic.AsyncAnthropic(
            api_key=_MODEL_GATEWAY_API_KEY,
            base_url=_MODEL_GATEWAY_URL,
        )
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system="You are a helpful assistant. Output only valid JSON, no code fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        extracted: dict = _json2.loads(raw)
        soul_patch: str = str(extracted.get("soul_patch", "")).strip()
        memory_key: str = str(extracted.get("memory_key", "channel_instruction")).strip()[:40]
        memory_value: str = str(extracted.get("memory_value", user_text[:200])).strip()
        human_summary: str = str(extracted.get("human_summary", "Got it, I've updated my behaviour for this channel.")).strip()

        if not soul_patch:
            return

        # Append to channel soul_text
        from agent_channel_configs import upsert_channel_config
        existing_cfg = _resolve_channel_config(agent, channel_id)
        existing_soul = (existing_cfg.soul_text if existing_cfg else None) or ""
        new_soul = (existing_soul.rstrip() + "\n\n" + soul_patch).strip() if existing_soul else soul_patch
        upsert_channel_config(
            str(agent.id), channel_id,
            soul_text=new_soul,
        )

        # Store as channel-scoped memory
        upsert_agent_memory(
            agent_id=str(agent.id),
            scope="channel",
            key=memory_key,
            value=memory_value,
            channel_id=channel_id,
            user_email=sender_email,
            source="manual",
        )

        logger.info(
            "_handle_channel_instruction: updated soul+memory for agent=%s channel=%s by=%s",
            agent.id, channel_id, sender_email,
        )
        await _post(f":white_check_mark: {human_summary}")

    except Exception:
        logger.exception(
            "_handle_channel_instruction: failed for agent=%s channel=%s", agent.id, channel_id
        )


# ── Agent memory helpers ──────────────────────────────────────────────────────

_MEMORY_CONTEXT_CAP = 2048   # hard cap on injected memory text (chars)

_MEMORY_EXTRACTION_PROMPT = """\
You are a memory-extraction assistant. Given a single conversation exchange between a user and an AI agent, extract facts worth remembering for future conversations.

Rules:
- Output a JSON array of objects with keys "key", "value", "scope".
  - scope MUST always be "user" (facts about the specific user only).
- If nothing is worth remembering, output an empty array: []
- Be concise. key ≤ 40 chars, value ≤ 200 chars.
- Do NOT extract: one-off data lookups, temporary results, error messages.
- Do NOT extract agent-wide or channel-wide instructions — only user-specific preferences and context.
- Output ONLY the JSON array — no explanation, no code fences.

Exchange:
User: {user_msg}

Agent: {agent_msg}
"""


def _build_memory_context(agent_id: str, user_email: str, channel_id: str | None = None) -> str:
    """Build a compact memory block to prepend to the system prompt.

    Loads agent-scope, user-scope, and (when channel_id is provided) channel-scope
    memories.  Returns an empty string when the agent has no memories yet.  Never raises.
    """
    mems = load_agent_memories(agent_id, user_email or None, channel_id=channel_id)
    if not mems:
        return ""
    lines = ["## Memory"]
    for m in mems:
        if m.scope == "user" and m.user_email:
            prefix = f"[{m.user_email}] "
        elif m.scope == "channel" and m.channel_id:
            prefix = f"[channel:{m.channel_id}] "
        else:
            prefix = ""
        lines.append(f"- {prefix}{m.key}: {m.value}")
    ctx = "\n".join(lines)
    return ctx[:_MEMORY_CONTEXT_CAP]


async def _extract_and_store_memories(
    agent_id: str,
    user_email: str,
    user_msg: str,
    agent_msg: str,
    channel_id: str | None = None,
) -> None:
    """Fire-and-forget: ask the LLM to extract user-scoped memorable facts.

    Automatic extraction never writes agent-wide or channel-wide memory.
    All errors are silently swallowed.
    """
    if not _MODEL_GATEWAY_API_KEY or not user_email or not user_msg or not agent_msg:
        return
    try:
        prompt = _MEMORY_EXTRACTION_PROMPT.format(
            user_msg=user_msg[:1000],
            agent_msg=agent_msg[:1000],
        )
        client = anthropic.AsyncAnthropic(
            api_key=_MODEL_GATEWAY_API_KEY,
            base_url=_MODEL_GATEWAY_URL,
        )
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system="You are a helpful assistant. Output only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        from token_tracking import record_token_usage as _record_usage
        _record_usage("memory", _MODEL, response.usage, user_email=user_email)
        raw = "".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        ).strip()
        if not raw:
            return
        # Strip code fences if the model wrapped the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        facts: list[dict] = _json.loads(raw)
        if not isinstance(facts, list):
            return
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            key = str(fact.get("key", "")).strip()
            value = str(fact.get("value", "")).strip()
            if not key or not value:
                continue
            upsert_agent_memory(
                agent_id=agent_id,
                scope="user",
                key=key,
                value=value,
                user_email=user_email,
                channel_id=None,
                source="auto",
            )
    except Exception:
        logger.debug("Memory extraction silently failed for agent %s", agent_id)


# ── Owner config command handler ─────────────────────────────────────────────

async def _handle_owner_config_command(
    *,
    agent: Agent,
    user_text: str,
    xoxb: str,
    channel: str,
    thread_ts: str,
    sender_email: str,
) -> None:
    """Apply a natural-language configuration change issued by the agent owner.

    Runs entirely in the slack-consumer process — no sandbox involved.
    All DB writes and pushes happen synchronously before the reply is posted.
    All errors are caught and surfaced as a Slack reply, never silently swallowed.
    """
    import json as _json2

    loop = asyncio.get_running_loop()

    async def _post(text: str) -> None:
        from slack_sdk import WebClient as _WC
        await loop.run_in_executor(
            None,
            lambda: _WC(token=xoxb).chat_postMessage(
                channel=channel, thread_ts=thread_ts or None, text=text
            ),
        )

    try:
        skill_slugs = _json.loads(agent.skill_ids or "[]")

        # ── Step 1: LLM extraction (no tools) ────────────────────────────────
        prompt = _CONFIG_EXTRACTION_PROMPT.format(
            user_msg=user_text[:1500],
            agent_name=agent.name,
            skill_slugs=", ".join(skill_slugs) or "(none)",
        )
        client = anthropic.AsyncAnthropic(
            api_key=_MODEL_GATEWAY_API_KEY,
            base_url=_MODEL_GATEWAY_URL,
        )
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system="You are a helpful assistant. Output only valid JSON, no code fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(
            b.text for b in response.content if hasattr(b, "text") and b.text
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        cfg: dict = _json2.loads(raw)
        action: str = cfg.get("action", "")
        summary: str = cfg.get("human_summary", "Done.")

        # ── Step 2: Apply the change ──────────────────────────────────────────
        db = SessionLocal()
        try:
            # Re-fetch agent in this thread's session so writes are committed cleanly
            fresh_agent = db.query(Agent).filter(Agent.id == agent.id).first()

            if action == "upsert_memory":
                key = str(cfg.get("key", "")).strip()[:200]
                value = str(cfg.get("value", "")).strip()[:2000]
                if not key or not value:
                    await _post("I couldn't extract a valid key/value from your message. Please be more specific.")
                    return
                upsert_agent_memory(
                    agent_id=str(agent.id),
                    scope="agent",
                    key=key,
                    value=value,
                    user_email=None,
                    source="manual",
                )
                logger.info("Config: upsert_memory key=%s agent=%s by=%s", key, agent.id, sender_email)

            elif action == "delete_memory":
                key = str(cfg.get("key", "")).strip()
                if key:
                    from agents import AgentMemory as _AM
                    db.query(_AM).filter(
                        _AM.agent_id == str(agent.id),
                        _AM.key == key,
                    ).delete(synchronize_session=False)
                    db.commit()
                    logger.info("Config: delete_memory key=%s agent=%s by=%s", key, agent.id, sender_email)

            elif action == "reset_memories":
                from agents import AgentMemory as _AM
                db.query(_AM).filter(
                    _AM.agent_id == str(agent.id),
                    _AM.scope == "agent",
                ).delete(synchronize_session=False)
                db.commit()
                logger.info("Config: reset_memories agent=%s by=%s", agent.id, sender_email)

            elif action == "update_soul":
                patch = str(cfg.get("soul_patch", "")).strip()
                mode = cfg.get("soul_mode", "append")
                if not patch:
                    await _post("I couldn't extract personality text from your message. Please be more specific.")
                    return
                if mode == "replace":
                    fresh_agent.soul_text = patch
                else:
                    existing = (fresh_agent.soul_text or "").rstrip()
                    fresh_agent.soul_text = (existing + "\n" + patch).strip()
                db.commit()
                logger.info("Config: update_soul mode=%s agent=%s by=%s", mode, agent.id, sender_email)

            elif action == "update_skill":
                slug = str(cfg.get("skill_slug", "")).strip()
                patch = str(cfg.get("skill_patch", "")).strip()
                mode = cfg.get("skill_mode", "append")
                # Security gate 1: slug must be assigned to this specific agent.
                if slug not in skill_slugs:
                    await _post(
                        f"I can only update skills assigned to me. "
                        f"'{slug}' is not in my skill list ({', '.join(skill_slugs) or 'none'})."
                    )
                    return
                # Security gate 2: no other agent (different creator) may share this
                # skill slug. Skill files are stored one-per-slug on disk and in the blob store;
                # allowing a write here would silently overwrite another owner's agent.
                conflict = (
                    db.query(Agent.id)
                    .filter(
                        Agent.skill_ids.contains(slug),
                        Agent.creator_email != (agent.creator_email or ""),
                        Agent.id != agent.id,
                    )
                    .first()
                )
                if conflict:
                    await _post(
                        f"Skill '{slug}' is shared with agents owned by other users. "
                        "I can't update it to avoid overwriting their configuration. "
                        "Create a private copy of this skill and assign it to me instead."
                    )
                    logger.warning(
                        "Config: update_skill blocked — slug=%s is shared across owners; "
                        "agent=%s creator=%s conflicting_agent=%s",
                        slug, agent.id, agent.creator_email, conflict[0],
                    )
                    return
                if not patch:
                    await _post("I couldn't extract the skill instruction text. Please be more specific.")
                    return
                from pathlib import Path as _Path
                skills_dir = _Path(__file__).parent.parent / "src" / "data" / "skills"
                skill_file = skills_dir / slug / "skill.md"
                if not skill_file.exists():
                    await _post(f"Skill file for '{slug}' not found locally. Try again after a pull.")
                    return
                if mode == "replace":
                    new_content = patch
                else:
                    existing_content = skill_file.read_text(encoding="utf-8").rstrip()
                    new_content = existing_content + "\n\n" + patch
                skill_file.write_text(new_content, encoding="utf-8")
                # Push to the blob store so it survives pod restarts
                try:
                    from skills_store import push_skill_file
                    await loop.run_in_executor(None, push_skill_file, slug, skill_file)
                except Exception:
                    logger.warning("Config: push failed for slug=%s — local file updated only", slug)
                logger.info("Config: update_skill slug=%s mode=%s agent=%s by=%s", slug, mode, agent.id, sender_email)

            else:
                await _post(f"I understood a config request but couldn't map it to a known action (got: '{action}'). Please rephrase.")
                return

        finally:
            db.close()

        # ── Step 3: Confirm ───────────────────────────────────────────────────
        await _post(summary)

    except Exception:
        logger.exception("_handle_owner_config_command failed for agent %s", agent.id)
        await _post("Something went wrong applying your config change. Check the logs.")




# ── Channel config resolution ─────────────────────────────────────────────────

def _resolve_channel_config(agent: Agent, channel_id: str) -> AgentChannelConfig | None:
    """Return the active per-channel config for this agent+channel, or None."""
    if not channel_id:
        return None
    return get_channel_config(str(agent.id), channel_id)


def _is_ambient_channel(agent: Agent, channel_id: str) -> bool:
    """True when this agent is configured to receive ALL messages in this channel.

    Gated by ``AgentChannelConfig.join_behavior.proactivity == "ambient"``. A
    message from an ambient channel still must pass ``should_handle_ambient_message``
    before it is actually processed — this only controls whether it reaches
    the dispatcher at all.
    """
    cfg = _resolve_channel_config(agent, channel_id)
    if not cfg:
        return False
    return (cfg.join_behavior or {}).get("proactivity") == "ambient"


# ── Channel join flow ─────────────────────────────────────────────────────────

_BROADCAST_NAME_RE = re.compile(
    r"(announce|broadcast|alert|all.?staff|company.?wide|general|news|notif)",
    re.IGNORECASE,
)
_BROADCAST_MEMBER_THRESHOLD = 200
_PERSONAL_MEMBER_THRESHOLD = 2

_SOUL_GEN_PROMPT = """You are configuring an AI assistant that will serve a Slack channel.
Given the channel's name, topic, and a sample of recent messages, write a focused personality
(soul_text) for this assistant. Be specific to the team's domain and work patterns.
Write in second person ("You are..."). Maximum 400 words. Output only the soul text, no preamble.

Channel name: {channel_name}
Channel topic: {channel_topic}
Recent message sample:
{message_sample}
"""

_MEMORY_HARVEST_PROMPT = """Extract durable, factual knowledge from these Slack messages.
Return a JSON array of objects with keys: "key" (string, snake_case), "value" (string, concise fact).
Focus on: team members + roles, recurring workflows, key projects, standing decisions, tools used.
Output only valid JSON, no preamble. Maximum 15 facts.

Messages:
{message_sample}
"""

_CHANNEL_SUMMARY_PROMPT = """You just joined a Slack channel and read its recent messages.
Write a concise summary (3-6 bullet points) of what you learned about this channel:
what the team works on, any active topics or decisions, open questions, and the overall vibe.
Be specific — reference actual topics from the messages, not generic statements.
Write in first person ("I noticed...", "The team is working on...").
Keep each bullet to one sentence. Output only the bullet list, no header or preamble.

Channel: #{channel_name}
Topic: {channel_topic}
Recent messages:
{message_sample}
"""


def _classify_channel(channel_name: str, channel_topic: str, member_count: int, is_private: bool) -> str:
    """Return BROADCAST, PERSONAL, or TEAM based on channel characteristics."""
    if member_count > _BROADCAST_MEMBER_THRESHOLD or _BROADCAST_NAME_RE.search(channel_name):
        return "BROADCAST"
    if member_count <= _PERSONAL_MEMBER_THRESHOLD:
        return "PERSONAL"
    return "TEAM"


def _channel_is_public_for_ephemeral(
    event: dict,
    channel_cfg: AgentChannelConfig | None,
) -> bool:
    """True when the conversation is a public channel (eligible for ephemeral replies)."""
    if event.get("channel_type") == "im":
        return False
    if channel_cfg and isinstance(channel_cfg.channel_metadata, dict):
        if "is_private" in channel_cfg.channel_metadata:
            return not bool(channel_cfg.channel_metadata.get("is_private"))
    return False


def _classify_skill_risk_slugs(skill_slugs: list[str]) -> tuple[list[str], list[str]]:
    """Return (elevated_or_critical_slugs, critical_only_slugs)."""
    from skills_acl import get_skill_acl

    sensitive: list[str] = []
    critical: list[str] = []
    for slug in skill_slugs:
        acl = get_skill_acl(slug)
        if not acl:
            continue
        if acl.risk_level in ("elevated", "critical"):
            sensitive.append(slug)
        if acl.risk_level == "critical":
            critical.append(slug)
    return sensitive, critical


def _is_bot_joining_channel(agent: Agent, event: dict) -> bool:
    """Return True only when member_joined_channel refers to this agent's bot."""
    if event.get("type") != "member_joined_channel":
        return False
    joining_user = str(event.get("user") or "").strip()
    bot_user_id = str(agent.slack_bot_user_id or "").strip()
    if not bot_user_id:
        logger.warning(
            "member_joined_channel ignored for agent=%s — slack_bot_user_id not configured",
            agent.id,
        )
        return False
    return joining_user == bot_user_id


async def _handle_channel_join(agent: Agent, event: dict) -> None:
    """3-step on-join flow with LLM soul generation and memory harvesting.

    Budget (enforced in code):
      channels.info / conversations.history   — 1 call each
      search.messages                          — max 2 calls, keywords only
      conversations.replies                    — banned entirely
    """
    channel_id: str = event.get("channel", "").strip()
    if not channel_id:
        return
    if not _is_bot_joining_channel(agent, event):
        logger.debug(
            "_handle_channel_join: ignored — joining user=%s is not bot=%s",
            event.get("user"), agent.slack_bot_user_id,
        )
        return

    xoxb = _read_secret(agent.xoxb_secret_ref)
    if not xoxb:
        logger.error("_handle_channel_join: agent %s has no xoxb token", agent.id)
        return

    loop = asyncio.get_running_loop()

    # Auto-provision the config row immediately so admins see the channel even if we crash.
    upsert_channel_config(
        str(agent.id), channel_id,
        join_status="pending",
    )

    # ── Step 1 — Classify (no tool calls except channels.info) ────────────────
    try:
        from slack_sdk import WebClient
        wc = WebClient(token=xoxb)

        def _fetch_channel_info():
            try:
                r = wc.conversations_info(channel=channel_id)
                return r.get("channel", {})
            except Exception as e:
                logger.warning("_handle_channel_join: channels.info failed: %s", e)
                return {}

        ch_info: dict = await loop.run_in_executor(None, _fetch_channel_info)
        channel_name: str = ch_info.get("name", channel_id)
        channel_topic: str = (ch_info.get("topic") or {}).get("value", "") or ""
        member_count: int = ch_info.get("num_members", 0)
        is_private: bool = bool(ch_info.get("is_private", False))
        channel_type = _classify_channel(channel_name, channel_topic, member_count, is_private)

        channel_metadata: dict = {
            "num_members": member_count,
            "is_private": is_private,
            "is_channel": ch_info.get("is_channel", True),
        }
        join_skill_ids: list[str] | None = None
        if not is_private:
            try:
                agent_slugs = _json.loads(agent.skill_ids or "[]")
            except Exception:
                agent_slugs = []
            from skills_acl import get_skill_acl

            excluded_critical: list[str] = []
            for slug in agent_slugs:
                acl = get_skill_acl(slug)
                if acl and acl.risk_level == "critical":
                    excluded_critical.append(slug)
            if excluded_critical:
                channel_metadata["auto_excluded_critical_skills"] = excluded_critical
                join_skill_ids = [s for s in agent_slugs if s not in excluded_critical]

        upsert_channel_config(
            str(agent.id), channel_id,
            channel_name=channel_name,
            channel_topic=channel_topic,
            channel_type=channel_type,
            channel_metadata=channel_metadata,
            join_status="pending",
            skill_ids=join_skill_ids,
        )

    except Exception:
        logger.exception("_handle_channel_join: Step 1 failed for channel %s", channel_id)
        channel_name = channel_id
        channel_topic = ""
        channel_type = "TEAM"

    display_name = agent.name or APP_NAME

    # ── Step 2 — Post immediate welcome ───────────────────────────────────────
    try:
        from slack_sdk import WebClient
        wc = WebClient(token=xoxb)

        if channel_type == "BROADCAST":
            welcome_text = (
                f"Hi :wave: I'm *{display_name}*, your AI assistant. "
                f"I'll be here if anyone needs help — feel free to mention me anytime."
            )
            def _post_broadcast():
                wc.chat_postMessage(channel=channel_id, text=welcome_text)
            await loop.run_in_executor(None, _post_broadcast)
            # BROADCAST: no further research, just update soul and exit
            upsert_channel_config(str(agent.id), channel_id, join_status="done")
            return
        else:
            topic_part = f"*{channel_topic[:80]}*" if channel_topic else f"*#{channel_name}*"
            welcome_text = f":wave: Just joined. Reading up on {topic_part} now."
            def _post_welcome():
                return wc.chat_postMessage(channel=channel_id, text=welcome_text)
            welcome_resp = await loop.run_in_executor(None, _post_welcome)
            welcome_ts = (welcome_resp or {}).get("ts", "")
    except Exception:
        logger.exception("_handle_channel_join: Step 2 failed for channel %s", channel_id)
        welcome_ts = ""

    # ── Step 3 — Research (strict budget) ─────────────────────────────────────
    messages: list[dict] = []
    try:
        from slack_sdk import WebClient
        wc = WebClient(token=xoxb)

        # Budget: exactly 1 history call
        def _fetch_history():
            try:
                r = wc.conversations_history(channel=channel_id, limit=20)
                return r.get("messages", [])
            except Exception as e:
                logger.warning("_handle_channel_join: conversations.history failed: %s", e)
                return []

        messages = await loop.run_in_executor(None, _fetch_history)

        # Budget: up to 2 search calls using keywords from channel name/topic only
        keywords = [w for w in (channel_name + " " + channel_topic).split() if len(w) > 3][:4]
        search_results: list[dict] = []
        _search_calls = 0
        for kw in keywords[:2]:
            if _search_calls >= 2:
                break
            def _search(q=kw):
                try:
                    r = wc.search_messages(query=q, count=5)
                    return (r.get("messages") or {}).get("matches", [])
                except Exception:
                    return []
            results = await loop.run_in_executor(None, _search)
            search_results.extend(results)
            _search_calls += 1

    except Exception:
        logger.exception("_handle_channel_join: Step 3 research failed for channel %s", channel_id)

    # Build message sample for LLM passes
    msg_texts = []
    for m in messages[:15]:
        text = str(m.get("text", "")).strip()
        if text and not m.get("bot_id"):
            msg_texts.append(text[:200])
    for m in search_results[:5]:
        text = str(m.get("text", "")).strip()
        if text:
            msg_texts.append(text[:200])
    message_sample = "\n".join(f"- {t}" for t in msg_texts[:20]) or "(no messages yet)"

    # Extract open questions, promises, chores from message history
    open_items: list[str] = []
    for m in messages[:20]:
        text = str(m.get("text", "")).strip()
        if not text or m.get("bot_id"):
            continue
        if text.endswith("?") or any(kw in text.lower() for kw in ("will ", "todo", "action item", "can someone", "need to")):
            open_items.append(text[:120])

    # Post pickups reply (threaded)
    try:
        from slack_sdk import WebClient
        wc = WebClient(token=xoxb)

        pickups = open_items[:3]
        if pickups:
            pickup_lines = "\n".join(f"• {p}" for p in pickups)
            pickups_text = (
                f"*Here's what I noticed so far:*\n{pickup_lines}\n\n"
                f"I can help work through any of these — just mention me."
            )
        else:
            pickups_text = (
                f"I've had a look at recent activity. Mention me anytime with a question "
                f"or task and I'll get to work."
            )

        def _post_pickups():
            wc.chat_postMessage(
                channel=channel_id,
                thread_ts=welcome_ts or None,
                text=pickups_text,
            )
        await loop.run_in_executor(None, _post_pickups)

        # Post proactivity reply
        proactivity_text = (
            f"*How I work in this channel:*\n"
            f"• Mention *@{display_name}* with any request — data lookups, writeups, analysis\n"
            f"• I remember context across our conversations in this channel\n"
            f"• Try: _@{display_name} summarise what we've agreed on [topic]_"
        )
        def _post_proactivity():
            wc.chat_postMessage(
                channel=channel_id,
                thread_ts=welcome_ts or None,
                text=proactivity_text,
            )
        await loop.run_in_executor(None, _post_proactivity)

    except Exception:
        logger.exception("_handle_channel_join: pickup/proactivity posts failed for channel %s", channel_id)

    # ── LLM Pass 0 — Channel activity summary (posted + stored as memory) ─────
    if _MODEL_GATEWAY_API_KEY and msg_texts:
        try:
            client = anthropic.AsyncAnthropic(
                api_key=_MODEL_GATEWAY_API_KEY,
                base_url=_MODEL_GATEWAY_URL,
            )
            summary_prompt = _CHANNEL_SUMMARY_PROMPT.format(
                channel_name=channel_name,
                channel_topic=channel_topic or "(no topic set)",
                message_sample=message_sample,
            )
            summary_response = await client.messages.create(
                model=_MODEL,
                max_tokens=500,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            generated_summary = "".join(
                b.text for b in summary_response.content if hasattr(b, "text") and b.text
            ).strip()
            if generated_summary:
                # Post the summary as a threaded reply to the welcome message
                from slack_sdk import WebClient as _WC
                _wc_s = _WC(token=xoxb)
                summary_slack_text = f"*Here's what I found in this channel:*\n{generated_summary}"
                def _post_summary():
                    _wc_s.chat_postMessage(
                        channel=channel_id,
                        thread_ts=welcome_ts or None,
                        text=summary_slack_text,
                    )
                await loop.run_in_executor(None, _post_summary)
                # Store as a durable channel memory entry
                upsert_agent_memory(
                    agent_id=str(agent.id),
                    scope="channel",
                    key="channel_summary_on_join",
                    value=generated_summary,
                    channel_id=channel_id,
                    source="auto",
                )
                logger.info(
                    "_handle_channel_join: channel summary generated+posted for agent=%s channel=%s",
                    agent.id, channel_id,
                )
        except Exception:
            logger.exception("_handle_channel_join: channel summary generation failed for channel %s", channel_id)

    # ── LLM Pass A — Soul generation ──────────────────────────────────────────
    if _MODEL_GATEWAY_API_KEY:
        try:
            client = anthropic.AsyncAnthropic(
                api_key=_MODEL_GATEWAY_API_KEY,
                base_url=_MODEL_GATEWAY_URL,
            )
            soul_prompt = _SOUL_GEN_PROMPT.format(
                channel_name=channel_name,
                channel_topic=channel_topic or "(no topic set)",
                message_sample=message_sample,
            )
            soul_response = await client.messages.create(
                model=_MODEL,
                max_tokens=600,
                messages=[{"role": "user", "content": soul_prompt}],
            )
            generated_soul = "".join(
                b.text for b in soul_response.content if hasattr(b, "text") and b.text
            ).strip()
            if generated_soul:
                from datetime import timezone as _tz
                upsert_channel_config(
                    str(agent.id), channel_id,
                    soul_text=generated_soul,
                    soul_generated_at=datetime.now(_tz.utc).replace(tzinfo=None),
                    join_status="pending",
                )
                logger.info(
                    "_handle_channel_join: soul generated for agent=%s channel=%s (%d chars)",
                    agent.id, channel_id, len(generated_soul),
                )
        except Exception:
            logger.exception("_handle_channel_join: soul generation failed for channel %s", channel_id)

    # ── LLM Pass B — Memory harvesting ────────────────────────────────────────
    if _MODEL_GATEWAY_API_KEY and msg_texts:
        try:
            client = anthropic.AsyncAnthropic(
                api_key=_MODEL_GATEWAY_API_KEY,
                base_url=_MODEL_GATEWAY_URL,
            )
            harvest_prompt = _MEMORY_HARVEST_PROMPT.format(message_sample=message_sample)
            harvest_response = await client.messages.create(
                model=_MODEL,
                max_tokens=800,
                system="You are a helpful assistant. Output only valid JSON.",
                messages=[{"role": "user", "content": harvest_prompt}],
            )
            raw_harvest = "".join(
                b.text for b in harvest_response.content if hasattr(b, "text") and b.text
            ).strip()
            if raw_harvest.startswith("```"):
                raw_harvest = raw_harvest.split("```")[1]
                if raw_harvest.startswith("json"):
                    raw_harvest = raw_harvest[4:]
                raw_harvest = raw_harvest.strip()
            facts: list[dict] = _json.loads(raw_harvest)
            if isinstance(facts, list):
                for fact in facts[:15]:
                    if not isinstance(fact, dict):
                        continue
                    key = str(fact.get("key", "")).strip()
                    value = str(fact.get("value", "")).strip()
                    if key and value:
                        upsert_agent_memory(
                            agent_id=str(agent.id),
                            scope="channel",
                            key=key,
                            value=value,
                            channel_id=channel_id,
                            source="auto",
                        )
                logger.info(
                    "_handle_channel_join: harvested %d memories for agent=%s channel=%s",
                    len(facts), agent.id, channel_id,
                )
        except Exception:
            logger.exception("_handle_channel_join: memory harvest failed for channel %s", channel_id)

    # Mark join flow complete
    upsert_channel_config(str(agent.id), channel_id, join_status="done")
    logger.info("_handle_channel_join: complete agent=%s channel=%s type=%s", agent.id, channel_id, channel_type)


# ── Event dispatcher (background task) ───────────────────────────────────────

async def _dispatch_event(
    agent: Agent,
    event: dict,
    *,
    event_id: str | None = None,
) -> None:
    """Run the agent LLM and post the reply to Slack.

    Runs as a fire-and-forget background task so the HTTP handler can return
    200 immediately.  All failures are logged; nothing propagates to the caller.

    When *event_id* is set, the dedup lease is marked ``done`` immediately after
    a successful user-visible reply so post-reply housekeeping failures (or a
    worker crash) cannot release the claim and re-run the turn.
    """
    event_type = event.get("type", "")
    channel = event.get("channel", "")
    thread_ts = event.get("thread_ts") or event.get("ts", "")
    # Prefer text + flattened Block Kit body so agents see full bot reports, not
    # only the short chat.postMessage text= fallback (shared by all agents).
    user_text: str = _plain_text_from_slack_message(event)
    slack_files: list = event.get("files") or []

    # Strip the bot mention prefix (<@UXXXXX> ...) from app_mention events
    if event_type == "app_mention" and user_text.startswith("<@"):
        user_text = user_text.split(">", 1)[-1].strip()

    if not channel or (not user_text and not slack_files):
        return

    # ── Channel config resolution ──────────────────────────────────────────────
    # For platform agents, if there is no channel config, drop the event silently.
    # For regular agents with channel_fallback_behavior="silent", same behaviour.
    channel_cfg: AgentChannelConfig | None = _resolve_channel_config(agent, channel)
    # A plain (non-mention, non-DM) "message" event only ever reaches this
    # function because the webhook-level gate already confirmed the channel
    # is in "ambient" mode for this agent — see _is_ambient_channel().
    is_ambient: bool = event_type == "message" and event.get("channel_type") != "im"
    fallback = getattr(agent, "channel_fallback_behavior", "default") or "default"
    if channel_cfg is None and (getattr(agent, "is_platform_agent", False) or fallback == "silent"):
        logger.debug(
            "_dispatch_event: no channel config for agent=%s channel=%s — dropping (silent fallback)",
            agent.id, channel,
        )
        return

    # Determine effective config (channel override > global fallback)
    effective_soul: str | None = (channel_cfg.soul_text if channel_cfg else None) or agent.soul_text
    effective_display_name: str = (
        (channel_cfg.display_name if channel_cfg and channel_cfg.display_name else None)
        or agent.name
    )
    effective_guardrails = (
        channel_cfg.guardrails if (channel_cfg and channel_cfg.guardrails is not None)
        else (agent.guardrails or [])
    )
    # Effective skill list: channel override (if set) else global
    if channel_cfg and channel_cfg.skill_ids:
        try:
            import json as _stdlib_json
            effective_skill_ids: list[str] = _stdlib_json.loads(channel_cfg.skill_ids)
        except Exception:
            effective_skill_ids = _json.loads(agent.skill_ids or "[]")
    else:
        effective_skill_ids = _json.loads(agent.skill_ids or "[]")

    # Memory scope
    effective_memory_scope: str = (channel_cfg.memory_scope if channel_cfg else None) or "global"
    # For platform agents always scope memory to the channel — each channel is its own context.
    # For regular agents, respect the configured memory_scope.
    _is_platform_for_memory = bool(getattr(agent, "is_platform_agent", False))
    effective_channel_id_for_memory: str | None = (
        channel if (_is_platform_for_memory or effective_memory_scope == "channel") else None
    )

    # Retrieve bot token
    xoxb = _read_secret(agent.xoxb_secret_ref)
    if not xoxb:
        logger.error("Agent %s has no readable xoxb token — cannot reply", agent.id)
        return

    slack_user_id: str = _resolve_sender_slack_user_id(event, xoxb=xoxb)
    event_bot_id: str = str(event.get("bot_id") or "").strip()

    # Give the user immediate, visible feedback that the agent has seen the
    # message and is working on it — an 👀 reaction on the trigger message,
    # swapped to ✅/⚠️ once we know the outcome (see below).
    trigger_ts: str = event.get("ts", "") or thread_ts

    invocation_id = str(uuid.uuid4())
    workspace_prefix: str | None = (
        f"workspaces/{invocation_id}" if _workspace_storage_available() else None
    )
    workspace_manifest: dict[str, str] = {}
    workspace_outputs: set[str] = set()
    workspace_total_bytes: list[int] = [0]

    def _fetch_attachment_blocks() -> list[dict]:
        return _build_slack_attachment_blocks(
            xoxb,
            slack_files,
            workspace_prefix=workspace_prefix,
            workspace_manifest=workspace_manifest,
            workspace_total_bytes=workspace_total_bytes,
        )

    # Fetch thread history and sender profile concurrently.
    # Skill secrets are intentionally loaded AFTER the skill filtering step so
    # that only secrets for skills the user can access are injected into the
    # sandbox environment.  Loading them here would expose restricted API keys
    # even when the corresponding skill is filtered out.
    loop = asyncio.get_running_loop()
    # For ambient (non-mention) candidates, defer the 👀 ack reaction until
    # after should_handle_ambient_message approves — otherwise the bot would
    # visibly react to channel messages it never ends up replying to.
    _ack_reaction_task = (
        loop.run_in_executor(None, lambda: None)
        if is_ambient
        else loop.run_in_executor(None, _slack_react, xoxb, channel, trigger_ts, _ACK_REACTION)
    )
    history, sender_profile, attachment_blocks, _ = await asyncio.gather(
        loop.run_in_executor(None, _fetch_thread_history, xoxb, channel, thread_ts),
        loop.run_in_executor(None, _fetch_sender_profile, xoxb, slack_user_id),
        loop.run_in_executor(None, _fetch_attachment_blocks),
        _ack_reaction_task,
    )

    # Thread file fallback: if trigger has no files, find them in the thread.
    # Walk newest-first so the most recent attachment wins.
    if not slack_files and not attachment_blocks and workspace_prefix:
        for msg in reversed(history):
            thread_files = msg.get("files")
            if thread_files:
                slack_files = thread_files
                attachment_blocks = await loop.run_in_executor(
                    None,
                    lambda: _build_slack_attachment_blocks(
                        xoxb,
                        thread_files,
                        workspace_prefix=workspace_prefix,
                        workspace_manifest=workspace_manifest,
                        workspace_total_bytes=workspace_total_bytes,
                    ),
                )
                break

    user_content = _compose_user_message_content(user_text, attachment_blocks)
    user_message_audit = _audit_user_message(user_text, slack_files)

    if slack_files and not attachment_blocks and not user_text:
        logger.warning(
            "Slack files present but none could be processed agent=%s channel=%s",
            agent.id, channel,
        )
        def _post_attachment_failure() -> None:
            from slack_sdk import WebClient
            try:
                WebClient(token=xoxb).chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts or None,
                    text=(
                        "I received your file(s) but couldn't process them. "
                        "Please share a JPEG, PNG, GIF, WebP, PDF, Word (.docx), "
                        "Excel (.xlsx), or ZIP file "
                        f"(up to {_SLACK_FILE_MAX_BYTES // (1024 * 1024)} MB each, "
                        f"{_SLACK_FILE_MAX_COUNT} files per message)."
                    ),
                )
            except Exception:
                logger.exception("Failed to post attachment-failure message")
        await loop.run_in_executor(None, _post_attachment_failure)
        await loop.run_in_executor(
            None, _slack_swap_reaction, xoxb, channel, trigger_ts, _ACK_REACTION, _ERROR_REACTION
        )
        await _maybe_cleanup_workspace(loop, workspace_prefix)
        return
    skill_secrets: dict[str, str] = {}

    sender_email: str = sender_profile.get("email", "")
    sender_name: str = sender_profile.get("real_name", "") or sender_profile.get("display_name", "")
    is_bot_sender: bool = bool(sender_profile.get("is_bot", False))
    # Bot app_mention events may omit ``user``; once we resolve a member ID from
    # ``bot_id``, treat the sender as a bot even if users.info returned empty.
    if not is_bot_sender and _event_is_bot_authored(event) and slack_user_id:
        is_bot_sender = True
    sender_slack_user_id: str = sender_profile.get("slack_user_id", slack_user_id) or slack_user_id

    # Ambient gate — for channels where this agent watches ALL messages, run
    # the message through the cheap/fast router before doing anything else
    # (no ack reaction, no history/skill/LLM work beyond what was already
    # fetched above). Fails closed: any router error means "don't process".
    if is_ambient:
        should_handle, ambient_reason = await loop.run_in_executor(
            None,
            should_handle_ambient_message,
            effective_display_name,
            (channel_cfg.channel_name if channel_cfg else "") or "",
            user_text,
            history,
        )
        if not should_handle:
            logger.info(
                "ambient gate: skipping agent=%s channel=%s reason=%s",
                agent.id, channel, ambient_reason,
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return
        logger.info(
            "ambient gate: handling agent=%s channel=%s reason=%s",
            agent.id, channel, ambient_reason,
        )
        await loop.run_in_executor(None, _slack_react, xoxb, channel, trigger_ts, _ACK_REACTION)

    # Gate 0 — lifecycle status. Pause/activate only flip DB status today; this
    # is where that status is enforced for Slack. Re-fetch from DB so a pause
    # that lands after the worker loaded the agent row still takes effect.
    runtime_status = await loop.run_in_executor(None, get_agent_status, str(agent.id))
    if not agent_accepts_runtime_requests(runtime_status):
        logger.info(
            "Agent not accepting Slack events agent=%s status=%s sender_bot=%s — dropping",
            agent.id, runtime_status, is_bot_sender,
        )
        if not is_bot_sender:
            await loop.run_in_executor(
                None,
                _post_agent_unavailable_sync,
                xoxb,
                channel,
                thread_ts,
                effective_display_name,
                runtime_status or getattr(agent, "status", "unknown"),
            )
        await _maybe_cleanup_workspace(loop, workspace_prefix)
        return

    # Gate 1 — identity must be resolvable before the LLM is ever called.
    # If we cannot map the Slack user ID to an email we cannot enforce skill ACL,
    # so we deny the request outright rather than letting it through unchecked.
    # Exception: Slack app (bot) senders are routed through a separate grant check
    # and do not require an email.
    if not sender_email and not is_bot_sender:
        if _event_is_bot_authored(event):
            logger.info(
                "Bot-authored event with unresolvable sender agent=%s bot_id=%s — dropping silently",
                agent.id,
                event_bot_id or "(none)",
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return
        logger.warning(
            "Could not resolve email for Slack user_id=%s agent=%s — denying request",
            slack_user_id, agent.id,
        )
        def _post_identity_failure() -> None:
            from slack_sdk import WebClient
            text = (
                "I wasn't able to verify your identity automatically.\n\n"
                "Please make sure your Slack profile has a work email set, "
                f"or contact your {APP_NAME} admin for help."
            )
            try:
                WebClient(token=xoxb).chat_postMessage(
                    channel=channel, text=text, thread_ts=thread_ts or None
                )
            except Exception:
                logger.exception("Failed to post identity-failure message")
        await loop.run_in_executor(None, _post_identity_failure)
        await _maybe_cleanup_workspace(loop, workspace_prefix)
        return

    # ── Flood / loop guard (all senders) ────────────────────────────────────────
    # Rate-limit invocations per (agent, sender) regardless of bot/human. Each
    # accepted invocation claims a sandbox, so an unbounded sender — an
    # agent<->agent loop OR a human/script re-mentioning an agent every few
    # seconds (e.g. a misfiring watchdog) — can exhaust the warm pool. Bursts
    # beyond the window cap are dropped. Fails open on DB error; authorisation
    # (grant / skill ACL) remains the primary gate.
    invoke_ok = await loop.run_in_executor(
        None, check_slack_invocation_allowed, str(agent.id), sender_slack_user_id
    )
    if not invoke_ok:
        logger.warning(
            "Slack invocation flood guard tripped: slack_user=%s agent=%s is_bot=%s — dropping to protect pool",
            sender_slack_user_id, agent.id, is_bot_sender,
        )
        await _maybe_cleanup_workspace(loop, workspace_prefix)
        return

    # ── Slack App (bot) path ───────────────────────────────────────────────────
    # Bots don't have work emails; we authorise them via an explicit per-agent
    # AgentSlackAppGrant row keyed on their Slack user ID.  Granted apps receive
    # full skill access (Gate 2 is skipped) so they can invoke any skill the
    # agent is configured with.
    if is_bot_sender:
        # Self-loop guard — never respond to our own bot's messages, regardless
        # of any grant. An agent authorised to invoke itself would otherwise
        # reply to its own posts forever.
        own_bot_user_id: str = (getattr(agent, "slack_bot_user_id", "") or "").strip()
        if own_bot_user_id and sender_slack_user_id == own_bot_user_id:
            logger.info(
                "Ignoring self-authored Slack message agent=%s bot=%s — self-loop guard",
                agent.id, own_bot_user_id,
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return

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
                "Slack app not authorised: slack_user=%s agent=%s workflow_candidates=%s — dropping silently",
                sender_slack_user_id,
                agent.id,
                sorted(workflow_candidates),
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return

        # (Flood/loop rate-limit already applied above for all senders.)
        # Use a synthetic email for audit logging / memory keying
        sender_email = f"bot:{sender_slack_user_id}"
        sender_name = sender_name or f"Slack App ({sender_slack_user_id})"
        # Full skill access — bypass Gate 2 entirely; use effective skill ids
        accessible_skill_slugs: list[str] = effective_skill_ids
        restricted_skill_slugs: list[str] = []
        if workflow_grant_ok and not grant_ok:
            logger.info(
                "Slack workflow authorised: slack_user=%s agent=%s candidates=%s full_skills=%s",
                sender_slack_user_id,
                agent.id,
                sorted(workflow_candidates),
                accessible_skill_slugs,
            )
        else:
            logger.info(
                "Slack app authorised: slack_user=%s agent=%s full_skills=%s",
                sender_slack_user_id, agent.id, accessible_skill_slugs,
            )

        # ── Gate 2.5 — reply-necessity & loop breakers (bot senders only) ─────
        # A bot mention is a trigger, not a mandate to reply. Two independent
        # gates stop agent<->agent loops before any expensive LLM/sandbox work:
        #   1. Deterministic same-pair ping-pong cap — only trips when the same
        #      two bots alternate repeatedly, not multi-bot orchestration chains.
        #      A human message resets the trailing window.
        #   2. A cheap "should I actually reply?" classifier that stays silent on
        #      acknowledgements, status echoes, FYI/handoff mentions, etc.
        # Both are skipped for human senders (handled in the else-branch), so
        # real users always get a reply.
        if bot_turn_limit_reached(
            history,
            current_sender_id=sender_slack_user_id,
            agent_bot_user_id=own_bot_user_id,
        ):
            logger.warning(
                "Slack same-pair ping-pong limit reached agent=%s thread=%s sender=%s — breaking loop, staying silent",
                agent.id, thread_ts, sender_slack_user_id,
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return
        _respond, _gate_reason = await loop.run_in_executor(
            None, should_respond_to_bot, effective_display_name, sender_name, user_text, history
        )
        if not _respond:
            logger.info(
                "Slack reply gate: no reply needed agent=%s sender=%s reason=%s — staying silent",
                agent.id, sender_slack_user_id, _gate_reason,
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return
        # Skip Gate 1.5 and Gate 2, fall through to Gate 3 and LLM call below
    else:
        # ── Human sender path (existing gates) ────────────────────────────────

        # Gate 1.5 — agent-level access check: user must have an explicit grant (or be the creator).
        # Platform agents are open to all workspace members; access is governed per-channel via
        # channel_cfg.access_policy instead (Gate 1.6 below handles the allowlist mode).
        _is_platform = bool(getattr(agent, "is_platform_agent", False))
        _channel_access_mode = (
            (channel_cfg.access_policy or {}).get("mode", "open") if channel_cfg else "open"
        )
        if _is_platform and _channel_access_mode != "allowlist":
            agent_allowed = True
        else:
            agent_allowed = await _check_agent_access(sender_email, agent)
        if not agent_allowed:
            logger.info(
                "Agent access denied for email=%s agent=%s", sender_email, agent.id
            )
            await loop.run_in_executor(
                None, _post_agent_access_denied_sync, xoxb, channel, thread_ts, agent.name, str(agent.id)
            )
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return

        # Gate 1.6 — channel-level allowlist (for channel configs with mode="allowlist")
        if channel_cfg:
            access_policy = channel_cfg.access_policy or {}
            if access_policy.get("mode") == "allowlist":
                allowed_emails = [e.strip().lower() for e in (access_policy.get("emails") or [])]
                if sender_email.lower() not in allowed_emails:
                    logger.info(
                        "Channel allowlist denied email=%s agent=%s channel=%s",
                        sender_email, agent.id, channel,
                    )
                    def _post_channel_denied() -> None:
                        from slack_sdk import WebClient
                        WebClient(token=xoxb).chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts or None,
                            text=(
                                "You don't have access to this agent in this channel. "
                                f"Visit *<{_APP_BASE_URL}|the platform>* to request access."
                            ),
                        )
                    try:
                        await loop.run_in_executor(None, _post_channel_denied)
                    except Exception:
                        pass
                    await _maybe_cleanup_workspace(loop, workspace_prefix)
                    return

        # Gate 2 (skill filtering) — compute the subset of skills the user can access.
        # Use effective_skill_ids (per-channel override or global) as the pool.
        accessible_skill_slugs = await _filter_accessible_skills(
            sender_email, agent, skill_override=effective_skill_ids
        )
        all_skill_slugs: list[str] = effective_skill_ids
        restricted_skill_slugs = [s for s in all_skill_slugs if s not in accessible_skill_slugs]
        logger.debug(
            "Skill filter: email=%s agent=%s all=%s accessible=%s restricted=%s",
            sender_email, agent.id, all_skill_slugs, accessible_skill_slugs, restricted_skill_slugs,
        )

    # Gate 3 — owner self-configuration (runs in-process, no sandbox).
    # Only the agent creator can issue config commands. Bypasses the normal LLM path.
    if _is_owner_or_collaborator(agent, sender_email) and _looks_like_config_command(user_text):
        logger.info(
            "Config command detected from owner email=%s agent=%s", sender_email, agent.id
        )
        await _handle_owner_config_command(
            agent=agent,
            user_text=user_text,
            xoxb=xoxb,
            channel=channel,
            thread_ts=thread_ts,
            sender_email=sender_email,
        )
        await _maybe_cleanup_workspace(loop, workspace_prefix)
        return

    # Gate 3.5 — channel instruction (platform agents only).
    # Only the agent owner/collaborator may change channel soul or shared channel memory.
    _is_platform = bool(getattr(agent, "is_platform_agent", False))
    if _is_platform and _is_owner_or_collaborator(agent, sender_email) and _looks_like_channel_instruction(user_text):
        _cfg_channel_name = (channel_cfg.channel_name if channel_cfg else None) or channel
        logger.info(
            "Channel instruction detected from email=%s agent=%s channel=%s",
            sender_email, agent.id, channel,
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
        await _maybe_cleanup_workspace(loop, workspace_prefix)
        return

    selected_skill_slugs, skill_routing_meta = await _resolve_slack_skill_selection(
        agent=agent,
        accessible_skill_slugs=accessible_skill_slugs,
        user_text=user_text,
        history=history,
        channel=channel,
        thread_ts=thread_ts,
    )
    if skill_routing_meta.get("enabled"):
        logger.info(
            "Slack skill routing agent=%s load=%s predicted=%s fallback=%s mode=%s",
            agent.id,
            selected_skill_slugs,
            skill_routing_meta.get("selected_slugs"),
            skill_routing_meta.get("fallback"),
            skill_routing_meta.get("mode"),
        )

    is_public_channel = _channel_is_public_for_ephemeral(event, channel_cfg)
    sensitive_slugs, critical_slugs = _classify_skill_risk_slugs(accessible_skill_slugs)
    force_ephemeral = is_public_channel and bool(critical_slugs)
    skip_thinking_placeholder = is_public_channel and bool(sensitive_slugs)

    stored_private_reply = await loop.run_in_executor(
        None,
        get_latest_ephemeral_reply,
        str(agent.id),
        channel,
        thread_ts,
        sender_slack_user_id,
    )

    # Build enriched system prompt: identity + sender context + soul + skills
    identity = (
        f"You are {effective_display_name}, a Slack bot assistant running inside this Slack workspace. "
        "You have full access to your assigned tools and skills — never claim you lack access. "
        "Do NOT say you are an AI or disclaim Slack integration. Just answer helpfully.\n\n"
        "SLACK REPLY RULES — follow these exactly:\n"
        "- The platform posts your final reply to the current conversation automatically. "
        "Put everything the user (or another agent) should see in your final reply text.\n"
        "- Do NOT call chat.postMessage, chat.update, or otherwise post to "
        "`SLACK_CHANNEL_ID` / `SLACK_THREAD_TS` from `run_python`. "
        "That duplicates the platform reply.\n"
        "- To @mention another agent in this thread, include the mention in your final reply only.\n"
        "- Do NOT @mention another agent or bot unless you are making a specific, actionable request "
        "that genuinely requires their response. Acknowledgements, status echoes, FYI notes, and "
        "handoffs to a human team must NOT tag another agent — tagging re-triggers them and causes "
        "endless reply loops. Refer to people or teams in plain text (e.g. 'the Compliance team') instead.\n"
        "- You may still use `run_python` + Slack APIs to *read* workspace data, or to post to a "
        "*different* channel when a skill explicitly requires routing/forwarding elsewhere.\n\n"
        "FORMATTING RULES — follow these exactly, every reply:\n"
        "- Your reply is always posted as Slack Block Kit. Default: write standard "
        "markdown (**bold**, _italic_, # headings, tables, ```code``` fences) — it "
        "renders natively, no need for Slack's legacy single-asterisk mrkdwn.\n"
        "- For structured layouts (side-by-side fields, real data tables, charts), "
        "emit exactly one fenced ```blockkit code block containing a JSON array of "
        "Slack Block Kit block objects (header/section/markdown/rich_text/"
        "data_table/data_visualization). Prefer ≤50 blocks; if you emit more, the "
        "platform splits them across multiple threaded messages automatically. "
        "If present, the blockkit fence replaces the plain-text rendering for that reply.\n"
        "- Do NOT narrate formatting choices, rules, or reasoning before the blockkit "
        "payload — output only user-facing prose (if any) plus the ```blockkit fence "
        "or JSON array, with no APP/thinking preamble.\n"
        "- DO NOT wrap plain prose in a code block unless showing actual code.\n"
        "- Keep replies concise. Avoid disclaimers, warnings, and filler phrases.\n\n"
    )

    # Inject sender identity so skills can scope responses (e.g. role-based filtering)
    if sender_email:
        name_part = f"{sender_name} " if sender_name else ""
        identity += f"The person asking is: {name_part}<{sender_email}>\n\n"

    if stored_private_reply and not is_bot_sender:
        preview = stored_private_reply
        if len(preview) > 4000:
            preview = preview[:4000] + "…"
        identity += (
            "## Earlier private reply in this thread\n"
            f"You previously sent this answer only to the asker (not visible to the channel):\n\n"
            f"{preview}\n\n"
            "If they are now asking you to share or post that answer with everyone in the "
            f"channel, repeat it as your reply and do NOT prefix it with {EPHEMERAL_SENTINEL}. "
            "Otherwise ignore this note.\n\n"
        )

    soul = (effective_soul or "").strip()
    skill_context = _load_agent_skills(agent, skill_slugs=selected_skill_slugs)

    # Load secrets ONLY for the selected skill subset so that restricted API
    # keys (e.g. integration tokens for a filtered-out skill) are never injected
    # into the sandbox environment.
    skill_secrets = await loop.run_in_executor(
        None, _load_skill_secrets_for_agent, agent, selected_skill_slugs
    )

    system_prompt = identity
    if soul:
        system_prompt += soul + "\n\n"

    index_text = (getattr(agent, "skill_index_text", None) or "").strip()
    if getattr(agent, "skill_routing_enabled", False) and index_text:
        system_prompt += "## Skill Index\n" + index_text + "\n\n"

    # Inject active guardrails right after soul / before skills so they take
    # precedence over any skill-level instructions.
    active_guardrails = [g for g in (effective_guardrails or []) if g.get("enabled")]
    if active_guardrails:
        from api_agents import _resolve_guardrail_text
        rules = "\n".join(
            f"- {_resolve_guardrail_text(g)}"
            for g in active_guardrails
            if _resolve_guardrail_text(g)
        )
        if rules:
            system_prompt += "## Guardrails\nYou must follow these rules at all times:\n" + rules + "\n\n"

    if skill_context:
        system_prompt += skill_context

    # For skills this user cannot access, inject description-only context so the
    # LLM knows what the skill does and can explain the access requirement.
    # No implementation details, API keys, or code patterns are included —
    # only the human-readable description from the skill's ACL manifest entry.
    if restricted_skill_slugs:
        restricted_lines: list[str] = []
        from skills_acl import get_skill_acl
        for slug in restricted_skill_slugs:
            acl = get_skill_acl(slug)
            desc = acl.description if acl else slug
            restricted_lines.append(f"- **{slug}**: {desc}")
        system_prompt += (
            "\n\n## Skills Required But Not Accessible\n\n"
            "The following skills are attached to this agent but **this user does not have access** to them. "
            "You have been given their descriptions only — no implementation details or API keys. "
            "Do NOT attempt to call run_python or make any API calls for these skills.\n\n"
            + "\n".join(restricted_lines)
            + "\n\nIf the user's request requires any of these skills, tell them clearly which skill "
            f"they need and instruct them to visit {_APP_BASE_URL} to request access. "
            "Be specific about what the skill does and why it is needed for their query."
        )
        logger.info(
            "Injected restricted skill descriptions for email=%s agent=%s slugs=%s",
            sender_email, agent.id, restricted_skill_slugs,
        )

    # Inject persisted memories (agent-scope + user-scope) into the system prompt.
    mem_ctx = _build_memory_context(str(agent.id), sender_email, channel_id=effective_channel_id_for_memory)
    if mem_ctx:
        system_prompt += "\n\n" + mem_ctx

    # Merge sender context into the env that skill subprocesses can read.
    # Skills should use SENDER_EMAIL / SENDER_NAME directly rather than
    # re-calling the Slack API — the resolution has already been done here.
    merged_secrets = dict(skill_secrets or {})
    if sender_email:
        merged_secrets["SENDER_EMAIL"] = sender_email
    if sender_name:
        merged_secrets["SENDER_NAME"] = sender_name
    if slack_user_id:
        merged_secrets["SLACK_USER_ID"] = slack_user_id

    # Inject the bot token so run_python subprocesses can query the Slack API.
    # SLACK_CHANNEL_ID / SLACK_THREAD_TS let the agent read its own conversation.
    merged_secrets["SLACK_BOT_TOKEN"] = xoxb
    if channel:
        merged_secrets["SLACK_CHANNEL_ID"] = channel
    if thread_ts:
        merged_secrets["SLACK_THREAD_TS"] = thread_ts

    # Append Slack query capability to the system prompt so the LLM knows how to use it.
    system_prompt += _SLACK_QUERY_SKILL

    # Inject A2A platform env vars so skill subprocesses can call other agents.
    # AGENT_ID and PLATFORM_INTERNAL_BASE_URL are always injected.
    # DERIV_ONE_CTX_TOKEN is a short-lived signed JWT encoding sender identity —
    # it is NOT the signing secret itself (which is never shared with subprocesses).
    merged_secrets["AGENT_ID"] = str(agent.id)
    merged_secrets["PLATFORM_INTERNAL_BASE_URL"] = _PLATFORM_INTERNAL_BASE_URL
    merged_secrets["DERIV_ONE_BASE_URL"] = _PLATFORM_INTERNAL_BASE_URL
    if _INTERNAL_SIGNING_SECRET and sender_email:
        try:
            merged_secrets["DERIV_ONE_CTX_TOKEN"] = _sign_invocation_context(
                sender_email=sender_email,
                source_agent_id=str(agent.id),
            )
        except Exception:
            logger.warning("Could not sign invocation context for agent %s", agent.id)

    # Load agents this agent is permitted to invoke and inject callable-agents
    # context into the system prompt so the LLM knows what peers are available.
    from agents import Agent as _Agent
    can_invoke_ids: list[str] = []
    try:
        import json as _j
        can_invoke_ids = _j.loads(agent.can_invoke or "[]")
    except (ValueError, TypeError):
        pass

    invokable_agents: list = []
    if can_invoke_ids:
        _db = SessionLocal()
        try:
            invokable_agents = _db.query(_Agent).filter(_Agent.id.in_(can_invoke_ids)).all()
        except Exception:
            logger.warning("Could not load invokable agents for agent %s", agent.id)
        finally:
            _db.close()

    if invokable_agents:
        agent_lines = "\n".join(
            f"- *{a.name}* (id: `{a.id}`): {(a.description or 'no description').strip()}"
            for a in invokable_agents
        )
        system_prompt += (
            "\n\n---\n\n"
            "*Agent-to-Agent Calls*\n"
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

    if is_public_channel and sensitive_slugs and not is_bot_sender:
        slug_list = ", ".join(sensitive_slugs)
        system_prompt += (
            "\n\n## Private replies in this public channel\n"
            f"This channel is public. Skills with elevated sensitivity are enabled here: "
            f"{slug_list}.\n"
            "When your answer includes confidential, PII, financial, or otherwise sensitive "
            f"information, prefix your entire reply with {EPHEMERAL_SENTINEL} (nothing before it) "
            "so only the asker sees it.\n"
            "If the user explicitly asks you to share or post the answer for everyone in the "
            f"channel, do NOT use {EPHEMERAL_SENTINEL}.\n"
        )

    # Gate 2.5b (LLM reply-necessity) is disabled via SLACK_REPLY_GATE_ENABLED.
    # Keep bot-turn guidance lightweight: orchestration handoffs with a concrete task
    # (e.g. "continue investigation", status checks, @mentions with new identifiers)
    # must always get a real reply. Only use __NO_REPLY__ for pure acknowledgements
    # or exact status echoes with no new information.
    if is_bot_sender:
        system_prompt += (
            "\n\n## Replying in an agent-to-agent thread\n"
            "This message was sent by another bot/agent, not a human. When another agent "
            "@mentions you with a concrete task, new identifiers, or asks you to continue "
            "work, always write a real reply — do not use "
            f"{NO_REPLY_SENTINEL}.\n"
            f"Use {NO_REPLY_SENTINEL} only for pure acknowledgements (e.g. 'Noted', 'Understood') "
            "or when you would repeat the same status with no new facts or action required.\n"
        )

    placeholder_ts: str | None = None
    if not skip_thinking_placeholder:
        # Drop a "Thinking…" placeholder into the thread now — the LLM call below
        # can take a while, and this is what turns into the final reply (edited in
        # place) so the user sees visible progress instead of dead air.
        placeholder_ts = await loop.run_in_executor(
            None, _post_thinking_placeholder, xoxb, channel, thread_ts
        )

    llm_started = time.time()
    _tool_audit: list[dict] = []
    interaction_id = str(uuid.uuid4())
    try:
        llm_outcome = await _call_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            agent=agent,
            history=history,
            skill_secrets=merged_secrets,
            tool_audit=_tool_audit,
            accessible_skill_slugs=selected_skill_slugs,
            pool_skill_slugs=accessible_skill_slugs,
            channel_id=channel,
            thread_ts=thread_ts,
            workspace_manifest=workspace_manifest if workspace_prefix else None,
            workspace_prefix=workspace_prefix,
            workspace_outputs=workspace_outputs if workspace_prefix else None,
            workspace_total_bytes=workspace_total_bytes if workspace_prefix else None,
        )
        if llm_outcome.failure_kind:
            await notify_agent_failure(
                agent,
                llm_outcome,
                source="slack_event",
                channel_id=channel,
                thread_ts=thread_ts,
                interaction_id=interaction_id,
            )
        reply_text = _sanitize_agent_reply_for_slack(llm_outcome.text)
    except Exception as exc:
        # Surface the failure in-thread instead of leaving "Thinking…" stuck
        # forever, then re-raise so existing dedup-release/logging still runs.
        dispatch_trigger = "dm" if event.get("channel_type") == "im" else "mention"
        dispatch_duration_ms = int((time.time() - llm_started) * 1000)
        await loop.run_in_executor(
            None,
            lambda: record_agent_exchange(
                interaction_id=interaction_id,
                agent_id=agent.id,
                user_message=user_message_audit,
                assistant_message=(
                    "⚠️ Something went wrong while processing your request. Please try again."
                ),
                invoker_email=sender_email,
                invoker_slack_user_id=slack_user_id or None,
                trigger_type=dispatch_trigger,
                channel_id=channel or None,
                thread_ts=thread_ts or None,
                duration_ms=dispatch_duration_ms,
                secrets_loaded=sorted(merged_secrets.keys()),
                tools_called=_tool_audit or None,
                routing_metadata=skill_routing_meta if skill_routing_meta.get("enabled") else None,
                failure_kind="dispatch_error",
                failure_detail=str(exc)[:500],
            ),
        )
        if placeholder_ts:
            await _post_slack_reply(
                xoxb=xoxb,
                channel=channel,
                thread_ts=thread_ts,
                text="⚠️ Something went wrong while processing your request. Please try again.",
                update_ts=placeholder_ts,
            )
        await loop.run_in_executor(
            None, _slack_swap_reaction, xoxb, channel, trigger_ts, _ACK_REACTION, _ERROR_REACTION
        )
        raise

    # ── Reply-necessity post-checks (bot senders only) ───────────────────────
    # Honour a model-chosen abstention (__NO_REPLY__) and suppress near-duplicate
    # replies that would otherwise re-post the same status into a loop. Skipped
    # for human senders so a real user always gets a response.
    reply_text, _abstained = strip_no_reply(reply_text)
    if is_bot_sender:
        if _abstained or not (reply_text or "").strip():
            logger.info(
                "Slack agent chose to stay silent agent=%s thread=%s — nothing posted",
                agent.id, thread_ts,
            )
            if placeholder_ts:
                await loop.run_in_executor(None, _slack_delete_message, xoxb, channel, placeholder_ts)
            await loop.run_in_executor(None, _slack_unreact, xoxb, channel, trigger_ts, _ACK_REACTION)
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return
        if is_near_duplicate_reply(reply_text, history):
            logger.warning(
                "Slack reply suppressed as near-duplicate agent=%s thread=%s sender=%s — breaking loop",
                agent.id, thread_ts, sender_slack_user_id,
            )
            if placeholder_ts:
                await loop.run_in_executor(None, _slack_delete_message, xoxb, channel, placeholder_ts)
            await loop.run_in_executor(None, _slack_unreact, xoxb, channel, trigger_ts, _ACK_REACTION)
            await _maybe_cleanup_workspace(loop, workspace_prefix)
            return

    reply_text, wants_ephemeral = strip_ephemeral_flag(reply_text)
    is_ephemeral = (
        not is_bot_sender
        and is_public_channel
        and (force_ephemeral or wants_ephemeral)
    )

    # Post reply via Slack Web API — edits the "Thinking…" placeholder in place
    # when we have one, else posts fresh. Ephemeral replies use chat.postEphemeral.
    # Commit the dedup lease as soon as Slack accepts the message — before
    # audit/memory — so a later failure cannot re-dispatch this event_id and
    # double-post.
    if is_ephemeral:
        if placeholder_ts:
            await loop.run_in_executor(None, _slack_delete_message, xoxb, channel, placeholder_ts)
        posted = await _post_slack_ephemeral(
            xoxb=xoxb,
            channel=channel,
            thread_ts=thread_ts,
            user=sender_slack_user_id,
            text=reply_text,
        )
        if not posted:
            await loop.run_in_executor(
                None, _slack_swap_reaction, xoxb, channel, trigger_ts, _ACK_REACTION, _ERROR_REACTION
            )
            raise RuntimeError(
                f"Failed to post ephemeral Slack reply agent={agent.id} channel={channel}"
            )
        await loop.run_in_executor(
            None,
            lambda: store_ephemeral_reply(
                agent_id=str(agent.id),
                channel_id=channel,
                thread_ts=thread_ts,
                slack_user_id=sender_slack_user_id,
                reply_text=reply_text,
            ),
        )
        await loop.run_in_executor(
            None, _slack_swap_reaction, xoxb, channel, trigger_ts, _ACK_REACTION, _PRIVATE_REPLY_REACTION
        )
    else:
        posted = await _post_slack_reply(
            xoxb=xoxb, channel=channel, thread_ts=thread_ts, text=reply_text, update_ts=placeholder_ts
        )
        if not posted:
            await loop.run_in_executor(
                None, _slack_swap_reaction, xoxb, channel, trigger_ts, _ACK_REACTION, _ERROR_REACTION
            )
            raise RuntimeError(
                f"Failed to post Slack reply agent={agent.id} channel={channel}"
            )
        await loop.run_in_executor(
            None, _slack_swap_reaction, xoxb, channel, trigger_ts, _ACK_REACTION, _DONE_REACTION
        )
    if (
        workspace_prefix
        and workspace_outputs
        and not is_ephemeral
    ):
        await loop.run_in_executor(
            None,
            lambda: _deliver_workspace_outputs_to_slack(
                xoxb, channel, thread_ts, workspace_prefix, workspace_outputs
            ),
        )
    if event_id:
        await loop.run_in_executor(None, mark_slack_event_done, event_id)

    # Housekeeping is best-effort: the user-visible reply is already committed.
    llm_duration_ms = int((time.time() - llm_started) * 1000)
    trigger_type = "dm" if event.get("channel_type") == "im" else "mention"
    _secrets_loaded = sorted(merged_secrets.keys())
    try:
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: record_agent_exchange(
                interaction_id=interaction_id,
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
                routing_metadata=skill_routing_meta if skill_routing_meta.get("enabled") else None,
                failure_kind=llm_outcome.failure_kind,
                failure_detail=llm_outcome.failure_detail,
                resolved_model=llm_outcome.resolved_model,
            ),
        )

        # Memory extraction runs after the Slack reply is posted.  Must be awaited
        # (not fire-and-forget) because agent_worker runs _dispatch_event inside
        # asyncio.run(), which cancels pending tasks when the coroutine returns.
        await _extract_and_store_memories(
            agent_id=str(agent.id),
            user_email=sender_email,
            user_msg=user_text,
            agent_msg=reply_text,
            channel_id=effective_channel_id_for_memory,
        )
    except Exception:
        logger.exception(
            "Post-reply housekeeping failed agent=%s event_id=%s — reply already committed",
            agent.id,
            event_id or "(none)",
        )
    finally:
        await _maybe_cleanup_workspace(loop, workspace_prefix)


def _serialize_assistant_block(block) -> dict:
    """Serialize an Anthropic content block without SDK-only fields.

    ``model_dump()`` includes extras such as ``toolset_name`` and
    ``cache_control=None`` that the model gateway/Anthropic reject on the next turn
    with "Extra inputs are not permitted".
    """
    block_type = getattr(block, "type", "")
    if block_type == "text":
        return {"type": "text", "text": str(getattr(block, "text", "") or "")}
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": str(block.id),
            "name": str(block.name),
            "input": block.input if isinstance(block.input, dict) else {},
        }
    if block_type == "thinking":
        serialized = {
            "type": "thinking",
            "thinking": str(getattr(block, "thinking", "") or ""),
        }
        signature = getattr(block, "signature", None)
        if signature:
            serialized["signature"] = str(signature)
        return serialized
    if block_type == "server_tool_use":
        return {
            "type": "server_tool_use",
            "id": str(getattr(block, "id", "") or ""),
            "name": str(getattr(block, "name", "") or ""),
            "input": block.input if isinstance(getattr(block, "input", None), dict) else {},
        }
    return {"type": "text", "text": str(block)}


def _response_has_tool_use(response) -> bool:
    """Return True when the model emitted at least one tool_use content block."""
    return any(getattr(b, "type", None) == "tool_use" for b in response.content)


def _response_text(response) -> str:
    """Extract user-visible text without exposing hidden reasoning blocks."""
    return "\n".join(
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ).strip()


def _latest_partial_assistant_text(messages: list[dict]) -> str:
    """Return the newest user-visible assistant text retained in tool history."""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        for block in reversed(msg.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                partial = str(block.get("text") or "").strip()
                if partial:
                    return partial
    return ""


_NO_TOOLS_SYNTHESIS_NUDGES: tuple[str, ...] = (
    "Please summarize your findings so far and give me your final answer now, "
    "in plain text.",
    "You did not return any text last time. Write at least one paragraph now "
    "summarizing what you found and any recommended next steps, even if the "
    "investigation is incomplete. Do not return an empty response.",
)


def _mechanical_fallback_summary(tool_audit: list | None) -> str:
    """Deterministic, non-LLM summary of what was attempted.

    Used only as a last resort when the model returns no text on both the
    normal turn and the no-tools synthesis retries. Telling the user what was
    actually run is strictly better than the generic "try again" message,
    since real investigation work often did happen before the budget ran out.
    """
    if not tool_audit:
        return ""

    skill_activations = [e for e in tool_audit if e.get("tool") == "activate_skill"]
    run_python_calls = [e for e in tool_audit if e.get("tool") == "run_python"]
    artifact_calls = [e for e in tool_audit if e.get("tool") == "call_artifact"]
    if not (skill_activations or run_python_calls or artifact_calls):
        return ""

    lines = [
        "I ran out of time before I could write up a summary, but here's what "
        "I attempted:"
    ]
    for entry in skill_activations:
        status = "loaded" if entry.get("ok") else "failed to load"
        lines.append(f"• Skill `{entry.get('slug', '?')}` — {status}")
    if run_python_calls:
        ok_count = sum(1 for e in run_python_calls if e.get("ok"))
        lines.append(
            f"• Ran {len(run_python_calls)} code step(s) — {ok_count} succeeded, "
            f"{len(run_python_calls) - ok_count} failed"
        )
    for entry in artifact_calls:
        status = "succeeded" if entry.get("ok") else "failed"
        lines.append(f"• Called artifact `{entry.get('artifact', '?')}` — {status}")
    lines.append(
        "\nAsk me to continue, or narrow the request, and I'll pick up from here."
    )
    return "\n".join(lines)


async def _request_no_tools_final(
    *,
    client,
    model: str,
    system_prompt: str,
    messages: list[dict],
    usage_acc,
    agent_id: str,
    reason: str,
) -> str:
    from agent_llm import cached_system, with_trailing_cache_breakpoint
    """Ask for a final answer with tools removed, retrying once with a stronger nudge.

    A system-only instruction layered on top of a long tool-call transcript is
    often not enough to make the model actually emit text: Claude can come back
    with stop_reason=end_turn and zero content blocks, especially after many
    tool_use/tool_result turns. Appending a genuine user-turn request — and
    retrying once with an even more direct ask if that still comes back empty —
    meaningfully reduces how often callers fall through to a generic failure
    message despite having real tool output to summarize.
    """
    logger.warning(
        "Agent %s forcing no-tools final synthesis reason=%s",
        agent_id,
        reason,
    )
    # Kept as a separate, uncached trailing block (via volatile_suffix) rather
    # than concatenated into system_prompt — this recovery path shares the
    # same base prefix as the main tool loop, so leaving that prefix intact
    # lets this call reuse the cache the main loop already wrote.
    recovery_suffix = (
        "\n\n[FINAL RESPONSE REQUIRED: Do not call tools. Using only information "
        "already available in the conversation, write the user-facing answer now. "
        "Return only the final answer, with no internal reasoning. Be concise "
        "(at most 600 words) and prioritise the actionable conclusion. "
        "Never include <function_calls>, <function_results>, <invoke>, <parameter>, "
        "or raw Python/run_python code — write plain Slack markdown only.]"
    )
    recovery_system = cached_system(system_prompt, model=model, volatile_suffix=recovery_suffix)

    for attempt, nudge in enumerate(_NO_TOOLS_SYNTHESIS_NUDGES, start=1):
        attempt_messages = messages + [{"role": "user", "content": nudge}]
        response = await client.messages.create(
            model=model,
            max_tokens=_AGENT_LLM_MAX_TOKENS,
            system=recovery_system,
            messages=with_trailing_cache_breakpoint(attempt_messages, model=model),
        )
        usage_acc.add(response.usage)
        text = _response_text(response)
        if text:
            return text
        logger.error(
            "Agent %s no-tools synthesis returned no text stop_reason=%s reason=%s attempt=%d/%d",
            agent_id,
            getattr(response, "stop_reason", None),
            reason,
            attempt,
            len(_NO_TOOLS_SYNTHESIS_NUDGES),
        )
    return ""


async def _call_llm(
    system_prompt: str,
    user_content: str | list,
    agent: Agent,
    history: list[dict] | None = None,
    skill_secrets: dict[str, str] | None = None,
    tool_audit: list | None = None,
    accessible_skill_slugs: list[str] | None = None,
    pool_skill_slugs: list[str] | None = None,
    channel_id: str | None = None,
    thread_ts: str | None = None,
    workspace_manifest: dict[str, str] | None = None,
    workspace_prefix: str | None = None,
    workspace_outputs: set[str] | None = None,
    workspace_total_bytes: list[int] | None = None,
) -> AgentLlmOutcome:
    """Call the LLM with a multi-turn tool-use loop and return the final reply outcome.

    Passes the run_python tool so the agent can execute code to fetch live data.
    Up to the per-agent tool turn budget (see ``_max_tool_turns_for_agent``) is
    allowed before forcing a final answer.
    Returns a generic error string on failure so the bot always replies.

    Model resolution:
      - agent.llm_model set to a canonical slug → use that model
      - null → platform default (env MODEL)
      - gemini-3.5-flash-lite | grok-4.5 | deepseek-v4-flash → OpenAI chat/completions
      - canonical → Anthropic messages transport

    if ``tool_audit`` is a list, each run_python invocation appends a record:
    ``{"tool": "run_python", "code_len": N, "output_len": N, "ok": bool}``.
    user_content may be a plain string or a list of Anthropic content blocks
    (text + image/document) to support multimodal messages.
    """
    from agent_llm import (
        agent_model_transport,
        uses_openai_agent_transport,
        resolve_agent_gateway_api_key,
        resolve_agent_model,
        run_agent_openai_tool_loop,
        cached_system,
        cached_tools,
        with_trailing_cache_breakpoint,
    )
    from token_tracking import TokenAccumulator, record_token_usage as _record_usage

    api_key, key_source = resolve_agent_gateway_api_key(
        agent,
        platform_key=_MODEL_GATEWAY_API_KEY,
        read_secret=_read_secret,
    )
    if not api_key:
        logger.error(
            "No gateway API key (BYOK or platform) — cannot call LLM for agent %s",
            agent.id,
        )
        return AgentLlmOutcome(
            text="I'm unable to process your request right now. Please try again later.",
        )

    requested = getattr(agent, "llm_model", None)
    model = resolve_agent_model(agent)
    transport = agent_model_transport(model)
    logger.info(
        "agent_llm_dispatch agent_id=%s requested_llm_model=%r resolved_model=%s "
        "transport=%s gateway_key_source=%s",
        agent.id,
        requested,
        model,
        transport,
        key_source,
    )

    messages: list[dict] = _slack_history_for_llm(history)
    messages.append({"role": "user", "content": user_content})
    max_tool_turns = _max_tool_turns_for_agent(agent)
    _usage_acc = TokenAccumulator()

    active_skill_slugs = list(accessible_skill_slugs or [])
    secrets_state = dict(skill_secrets or {})
    system_prompt_ref = [system_prompt]
    activation_count = [0]
    offer_activate_skill = _should_offer_activate_skill(agent, pool_skill_slugs)
    if offer_activate_skill and pool_skill_slugs:
        guidance = _activate_skill_system_guidance(active_skill_slugs, pool_skill_slugs)
        if guidance:
            system_prompt_ref[0] += guidance

    anthropic_tools: list[dict] = [_RUN_PYTHON_TOOL]
    offer_call_artifact_ref = [False]
    if active_skill_slugs:
        from artifact_runtime import artifacts_enabled_for_agent

        _use_artifacts = artifacts_enabled_for_agent(agent)
    else:
        _use_artifacts = False
    if _use_artifacts and active_skill_slugs:
        def _load_artifact_section() -> str:
            db = SessionLocal()
            try:
                from artifact_runtime import (
                    build_artifact_prompt_section,
                    load_approved_artifacts,
                )
                return build_artifact_prompt_section(
                    load_approved_artifacts(db, active_skill_slugs)
                )
            finally:
                db.close()

        section = await asyncio.get_event_loop().run_in_executor(
            None, _load_artifact_section
        )
        if section.strip():
            system_prompt_ref[0] += "\n\n" + section
            anthropic_tools.append(_CALL_ARTIFACT_TOOL)
            offer_call_artifact_ref[0] = True

    if offer_activate_skill:
        anthropic_tools.append(_ACTIVATE_SKILL_TOOL)

    # Knowledge base: offered only when the agent's owner enabled it and the
    # bucket + data store are provisioned. Independent of skills — retrieval is
    # per-agent data, not a shared capability.
    from agent_knowledge_base_tool import (
        SEARCH_KB_TOOL,
        SEARCH_KB_TOOL_OPENAI,
        append_kb_audit,
        execute_search_knowledge_base,
        knowledge_base_enabled_for_agent,
    )

    _offer_kb_search = await asyncio.get_event_loop().run_in_executor(
        None, knowledge_base_enabled_for_agent, agent
    )
    if _offer_kb_search:
        anthropic_tools.append(SEARCH_KB_TOOL)

    async def _execute_search_kb(query: str) -> str:
        # Blocking the retrieval provider call — keep it off the event loop.
        output = await asyncio.get_event_loop().run_in_executor(
            None, execute_search_knowledge_base, str(agent.id), query, agent.name
        )
        append_kb_audit(tool_audit, query, output)
        return output

    async def _execute_activate_skill(slug: str) -> str:
        return await _activate_skill_mid_run(
            slug=slug,
            agent=agent,
            pool_skill_slugs=list(pool_skill_slugs or []),
            active_skill_slugs=active_skill_slugs,
            skill_secrets=secrets_state,
            system_prompt_ref=system_prompt_ref,
            activation_count=activation_count,
            tool_audit=tool_audit,
            channel_id=channel_id,
            thread_ts=thread_ts,
            anthropic_tools=anthropic_tools,
            offer_call_artifact_ref=offer_call_artifact_ref,
            use_artifacts=_use_artifacts,
        )

    async def _execute_artifact_openai(name: str, params: dict) -> str:
        return await _execute_call_artifact(
            name,
            params if isinstance(params, dict) else {},
            accessible_skill_slugs=active_skill_slugs,
            skill_secrets=secrets_state,
            agent_id=str(agent.id),
            tool_audit=tool_audit,
            workspace_manifest=workspace_manifest,
            workspace_prefix=workspace_prefix,
            workspace_outputs=workspace_outputs,
            workspace_total_bytes=workspace_total_bytes,
        )

    async def _run_python_with_workspace(code: str, env: dict[str, str] | None = None) -> str:
        return await _execute_code(
            code,
            env,
            workspace_manifest=workspace_manifest,
            workspace_prefix=workspace_prefix,
            workspace_outputs=workspace_outputs,
            workspace_total_bytes=workspace_total_bytes,
        )

    # ── OpenAI chat/completions (Auto + direct the model gateway slugs) ───────────────
    if uses_openai_agent_transport(model):
        try:
            text = await run_agent_openai_tool_loop(
                model=model,
                system_prompt=system_prompt_ref[0],
                system_prompt_ref=system_prompt_ref,
                messages=messages,
                api_key=api_key,
                base_url_raw=_MODEL_GATEWAY_URL_RAW,
                max_tokens=_AGENT_LLM_MAX_TOKENS if max_tool_turns > 0 else 2048,
                max_tool_turns=max_tool_turns,
                skill_secrets=secrets_state,
                execute_code=_run_python_with_workspace,
                usage_acc=_usage_acc,
                tool_audit=tool_audit,
                agent_id=str(agent.id),
                run_python_tool_openai=_RUN_PYTHON_TOOL_OPENAI,
                tool_time_budget_secs=_AGENT_LLM_TOOL_TIME_BUDGET_SECS,
                call_artifact_tool_openai=_CALL_ARTIFACT_TOOL_OPENAI
                if _use_artifacts
                else None,
                execute_artifact=_execute_artifact_openai if _use_artifacts else None,
                offer_call_artifact_ref=offer_call_artifact_ref,
                activate_skill_tool_openai=_ACTIVATE_SKILL_TOOL_OPENAI
                if offer_activate_skill
                else None,
                execute_activate_skill=_execute_activate_skill if offer_activate_skill else None,
                search_kb_tool_openai=SEARCH_KB_TOOL_OPENAI if _offer_kb_search else None,
                execute_search_kb=_execute_search_kb if _offer_kb_search else None,
            )
            _record_usage("slack", model, _usage_acc, agent_id=agent.id)
            return AgentLlmOutcome(
                text=text or "I wasn't able to generate a response. Please try again.",
                resolved_model=model,
            )
        except Exception as exc:
            logger.exception("OpenAI Auto LLM call failed for agent %s", agent.id)
            _record_usage("slack", model, _usage_acc, agent_id=agent.id)
            reply, detail = _llm_failure_reply(exc)
            return AgentLlmOutcome(
                text=reply,
                failure_detail=detail or None,
                resolved_model=model,
            )

    # ── Non-Auto → Anthropic messages ───────────────────────────────────────
    # Agents with max_tool_turns == 0 are pure text-reply agents.
    # Do not offer the run_python tool at all — the LLM cannot call what it cannot see.
    if max_tool_turns == 0:
        try:
            client = anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url=_MODEL_GATEWAY_URL,
            )
            response = await client.messages.create(
                model=model,
                max_tokens=2048,
                system=cached_system(system_prompt_ref[0], model=model),
                messages=with_trailing_cache_breakpoint(messages, model=model),
            )
            _usage_acc.add(response.usage)
            result = _response_text(response)
            if not result:
                result = await _request_no_tools_final(
                    client=client,
                    model=model,
                    system_prompt=system_prompt_ref[0],
                    messages=messages,
                    usage_acc=_usage_acc,
                    agent_id=str(agent.id),
                    reason=f"empty pure-text response stop_reason={response.stop_reason}",
                )
            _record_usage("slack", model, _usage_acc, agent_id=agent.id)
            return AgentLlmOutcome(
                text=result or "I was unable to complete a response. Please try again.",
                resolved_model=model,
            )
        except Exception as exc:
            logger.exception("LLM call failed for agent %s", agent.id)
            _record_usage("slack", model, _usage_acc, agent_id=agent.id)
            reply, detail = _llm_failure_reply(exc)
            return AgentLlmOutcome(
                text=reply,
                failure_detail=detail or None,
                resolved_model=model,
            )

    try:
        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=_MODEL_GATEWAY_URL,
        )

        # Turns before the hard limit at which we inject a wind-down hint into the
        # system prompt so the model wraps up proactively instead of hitting the wall.
        _WIND_DOWN_TURNS_BEFORE = 2
        # Fraction of the elapsed-time budget after which we also start nudging,
        # regardless of turns remaining. Turn-based wind-down alone never fires on
        # long-running tool loops that get cut off by the time budget well before
        # max_tool_turns (e.g. hitting the 540s wall at turn 24 of 40) — this makes
        # a proactive, in-band wrap-up far more likely than the emergency no-tools
        # synthesis path below.
        _WIND_DOWN_ELAPSED_FRACTION = 0.75
        tool_loop_started = time.monotonic()
        synthesis_reason = f"tool budget exhausted ({max_tool_turns} turns)"

        # Turns lost to sandbox infrastructure faults are refunded: they advance
        # neither the turn counter nor the elapsed-time budget, up to a cap.
        _turn = 0
        infra_retries_used = 0
        infra_lost_secs = 0.0

        while _turn < max_tool_turns:
            turn_started = time.monotonic()
            elapsed = turn_started - tool_loop_started - infra_lost_secs
            if _turn > 0 and elapsed >= _AGENT_LLM_TOOL_TIME_BUDGET_SECS:
                synthesis_reason = (
                    f"tool time budget exhausted "
                    f"({elapsed:.1f}s/{_AGENT_LLM_TOOL_TIME_BUDGET_SECS:.1f}s)"
                )
                break

            turns_remaining = max_tool_turns - _turn
            time_budget_nearly_spent = (
                _turn > 0
                and elapsed >= _AGENT_LLM_TOOL_TIME_BUDGET_SECS * _WIND_DOWN_ELAPSED_FRACTION
            )

            # Augment system prompt for the final stretch so the model knows to
            # stop fetching and synthesise. Passed as a separate, UNCACHED
            # trailing block (via cached_system's volatile_suffix) rather than
            # concatenated into system_prompt_ref[0] — toggling this on/off in
            # the last few turns would otherwise invalidate the cached prefix
            # on every turn it appears.
            wind_down_suffix: str | None = None
            if 0 < turns_remaining <= _WIND_DOWN_TURNS_BEFORE:
                wind_down_suffix = (
                    f"\n\n[INSTRUCTION: You have {turns_remaining} tool call(s) remaining. "
                    "Unless a tool call is absolutely essential, write your final reply now "
                    "based on the information already gathered. Do not make unnecessary calls.]"
                )
            elif time_budget_nearly_spent:
                remaining_secs = max(0.0, _AGENT_LLM_TOOL_TIME_BUDGET_SECS - elapsed)
                wind_down_suffix = (
                    f"\n\n[INSTRUCTION: You have used {elapsed:.0f}s of your "
                    f"{_AGENT_LLM_TOOL_TIME_BUDGET_SECS:.0f}s tool-time budget "
                    f"(~{remaining_secs:.0f}s left before you are cut off). Unless a tool "
                    "call is absolutely essential, write your final reply now based on the "
                    "information already gathered. Do not make unnecessary calls.]"
                )

            response = await client.messages.create(
                model=model,
                max_tokens=_AGENT_LLM_MAX_TOKENS,
                system=cached_system(system_prompt_ref[0], model=model, volatile_suffix=wind_down_suffix),
                messages=with_trailing_cache_breakpoint(messages, model=model),
                tools=cached_tools(anthropic_tools, model=model),
            )
            _usage_acc.add(response.usage)

            # Execute tools when the model requested them.  stop_reason may be
            # "tool_use" OR "max_tokens" when the run_python payload is large —
            # the latter was previously mis-handled as an empty text reply.
            if _response_has_tool_use(response):
                if response.stop_reason == "max_tokens":
                    logger.warning(
                        "Agent %s hit max_tokens while emitting tool_use (turn %d) — executing anyway",
                        agent.id, _turn + 1,
                    )
                messages.append({
                    "role": "assistant",
                    "content": [_serialize_assistant_block(b) for b in response.content],
                })

                tool_results = []
                turn_had_infra = False
                turn_had_progress = False

                def _note(out: str) -> None:
                    nonlocal turn_had_infra, turn_had_progress
                    if _is_infra_failure_output(out):
                        turn_had_infra = True
                    else:
                        turn_had_progress = True

                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    if block.name == "run_python":
                        code = (block.input or {}).get("code", "") if isinstance(block.input, dict) else ""
                        logger.info(
                            "Agent %s running tool code (turn %d)", agent.id, _turn + 1
                        )
                        output = await _run_python_with_workspace(code, secrets_state)
                        _note(output)
                        _append_run_python_audit(tool_audit, code, output)
                        from tool_code_retention import maybe_record_run_python_sample

                        maybe_record_run_python_sample(
                            agent_id=str(agent.id),
                            skill_slugs=active_skill_slugs,
                            code=code,
                            ok=not output.startswith("[error]"),
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output or "(no output)",
                        })
                    elif block.name == "call_artifact":
                        raw = block.input if isinstance(block.input, dict) else {}
                        art_name = (raw.get("name") or "").strip()
                        art_params = raw.get("params") or {}
                        output = await _execute_call_artifact(
                            art_name,
                            art_params if isinstance(art_params, dict) else {},
                            accessible_skill_slugs=active_skill_slugs,
                            skill_secrets=secrets_state,
                            agent_id=str(agent.id),
                            tool_audit=tool_audit,
                            workspace_manifest=workspace_manifest,
                            workspace_prefix=workspace_prefix,
                            workspace_outputs=workspace_outputs,
                            workspace_total_bytes=workspace_total_bytes,
                        )
                        _note(output)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output or "(no output)",
                        })
                    elif block.name == "activate_skill":
                        raw = block.input if isinstance(block.input, dict) else {}
                        act_slug = (raw.get("slug") or "").strip()
                        output = await _execute_activate_skill(act_slug)
                        _note(output)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output or "(no output)",
                        })
                    elif block.name == "search_knowledge_base":
                        raw = block.input if isinstance(block.input, dict) else {}
                        kb_query = (raw.get("query") or "").strip()
                        output = await _execute_search_kb(kb_query)
                        _note(output)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output or "(no output)",
                        })
                    else:
                        logger.warning(
                            "Agent %s unknown tool %r (turn %d)", agent.id, block.name, _turn + 1
                        )
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Unknown tool: {block.name}",
                            "is_error": True,
                        })

                if not tool_results:
                    logger.warning(
                        "Agent %s had tool_use blocks but produced no tool_results (turn %d)",
                        agent.id, _turn + 1,
                    )
                    _turn += 1
                    continue

                messages.append({"role": "user", "content": tool_results})

                if (
                    turn_had_infra
                    and not turn_had_progress
                    and infra_retries_used < _MAX_FREE_INFRA_RETRIES
                ):
                    infra_retries_used += 1
                    infra_lost_secs += time.monotonic() - turn_started
                    logger.warning(
                        "Agent %s turn %d lost to sandbox infrastructure — refunding "
                        "(free retry %d/%d, %.1fs excluded from time budget)",
                        agent.id, _turn + 1, infra_retries_used,
                        _MAX_FREE_INFRA_RETRIES, infra_lost_secs,
                    )
                    continue

                _turn += 1
                continue

            # Final answer (stop_reason == "end_turn" or max turns reached)
            result = _response_text(response)
            if not result:
                logger.warning(
                    "Agent %s returned no text blocks (stop_reason=%s turn=%d)",
                    agent.id, response.stop_reason, _turn + 1,
                )
                result = await _request_no_tools_final(
                    client=client,
                    model=model,
                    system_prompt=system_prompt_ref[0],
                    messages=messages,
                    usage_acc=_usage_acc,
                    agent_id=str(agent.id),
                    reason=f"empty response stop_reason={response.stop_reason} turn={_turn + 1}",
                )
            _record_usage("slack", model, _usage_acc, agent_id=agent.id)
            if result:
                return AgentLlmOutcome(text=result, resolved_model=model)
            partial = _latest_partial_assistant_text(messages)
            if partial:
                return AgentLlmOutcome(text=partial, resolved_model=model)
            empty_reason = f"empty response stop_reason={response.stop_reason} turn={_turn + 1}"
            mechanical = _mechanical_fallback_summary(tool_audit)
            if mechanical:
                logger.warning(
                    "Agent %s: no-tools synthesis returned empty, falling back to "
                    "mechanical tool_audit summary",
                    agent.id,
                )
                return AgentLlmOutcome(
                    text=mechanical,
                    failure_kind=FAILURE_KIND_FORMAT,
                    failure_detail=empty_reason,
                    resolved_model=model,
                )
            return AgentLlmOutcome(
                text=AGENT_FORMAT_FAILURE_MESSAGE,
                failure_kind=FAILURE_KIND_FORMAT,
                failure_detail=empty_reason,
                resolved_model=model,
            )

        # The elapsed-time or tool-turn budget was reached. Reserve the remaining
        # request time for one concise, no-tools user-facing synthesis.
        logger.warning(
            "Agent %s forcing final answer after %s",
            agent.id,
            synthesis_reason,
        )
        result = await _request_no_tools_final(
            client=client,
            model=model,
            system_prompt=system_prompt_ref[0],
            messages=messages,
            usage_acc=_usage_acc,
            agent_id=str(agent.id),
            reason=synthesis_reason,
        )
        _record_usage("slack", model, _usage_acc, agent_id=agent.id)
        if result:
            return AgentLlmOutcome(text=result, resolved_model=model)

        # Last resort: salvage the most recent partial assistant text already in
        # the conversation rather than returning a generic error string.
        partial = _latest_partial_assistant_text(messages)
        if partial:
            logger.warning(
                "Agent %s: forced synthesis returned empty, falling back to partial answer",
                agent.id,
            )
            return AgentLlmOutcome(text=partial, resolved_model=model)
        mechanical = _mechanical_fallback_summary(tool_audit)
        if mechanical:
            logger.warning(
                "Agent %s: forced synthesis returned empty, falling back to mechanical "
                "tool_audit summary",
                agent.id,
            )
            return AgentLlmOutcome(
                text=mechanical,
                failure_kind=FAILURE_KIND_TIMEOUT,
                failure_detail=synthesis_reason,
                resolved_model=model,
            )
        return AgentLlmOutcome(
            text=AGENT_TIMEOUT_FAILURE_MESSAGE,
            failure_kind=FAILURE_KIND_TIMEOUT,
            failure_detail=synthesis_reason,
            resolved_model=model,
        )

    except Exception:
        logger.exception("LLM call failed for agent %s", agent.id)
        _record_usage("slack", model, _usage_acc, agent_id=agent.id)
        return AgentLlmOutcome(
            text="I encountered an error processing your request. Please try again later.",
            resolved_model=model,
        )


_FUNCTION_CALLS_BLOCK_RE = re.compile(
    r"<function_calls\b[^>]*>.*?</function_calls>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_RESULTS_BLOCK_RE = re.compile(
    r"<function_results\b[^>]*>(.*?)</function_results>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_XML_TAIL_RE = re.compile(
    r"<function_(?:calls|results)\b[^>]*>[\s\S]*$",
    re.IGNORECASE,
)
_STRAY_TOOL_TAG_RE = re.compile(
    r"</?(?:invoke|parameter|function_calls|function_results)\b[^>]*>",
    re.IGNORECASE,
)
_CODE_PARAMETER_RE = re.compile(
    r'<parameter\s+name=["\']code["\'][^>]*>[\s\S]*?</parameter>',
    re.IGNORECASE,
)
_FENCED_PYTHON_RE = re.compile(
    r"```(?:python)?\s*\n[\s\S]*?```",
    re.IGNORECASE,
)
_LEAKED_TOOL_MARKUP_RE = re.compile(
    r"<(?:function_(?:calls|results)|invoke|parameter)\b",
    re.IGNORECASE,
)
_PYTHON_LEAK_LINE_RE = re.compile(
    r"^(?:import\s+(?:os|requests|json)\b|from\s+\w+\s+import\b|BASE_URL\s*=|"
    r"os\.environ\.get\(|headers_(?:read|write)\s*=|resp(?:_\w+)?\s*=\s*requests\.)",
    re.IGNORECASE,
)

_SLACK_REPLY_SANITIZE_FALLBACK = (
    "I've completed the internal steps for this case but couldn't format a clean summary. "
    "Please tag me again if you need a status update."
)


def _extract_prose_before_code_markup(fragment: str) -> str:
    """Keep user-facing lines from a function_results body before code leaks in."""
    body = fragment or ""
    for marker in ("<parameter", "<invoke", "import os", "import requests"):
        idx = body.find(marker)
        if idx >= 0:
            body = body[:idx]
    lines: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                lines.append("")
            continue
        if _PYTHON_LEAK_LINE_RE.match(stripped):
            break
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _strip_tool_call_blocks(text: str) -> str:
    """Remove raw tool-invocation XML and leaked run_python code from agent replies.

    Models sometimes emit ``<function_calls>`` / ``<function_results>`` markup (or
    truncated variants) in the text they return instead of using the tool API.
    Salvage any short user-facing prose from ``function_results`` blocks, then
    strip the rest.
    """
    if not text:
        return ""

    salvaged: list[str] = []

    def _results_repl(match: re.Match[str]) -> str:
        prose = _extract_prose_before_code_markup(match.group(1) or "")
        if prose:
            salvaged.append(prose)
        return ""

    cleaned = _FUNCTION_RESULTS_BLOCK_RE.sub(_results_repl, text)
    cleaned = _FUNCTION_CALLS_BLOCK_RE.sub("", cleaned)

    unclosed = re.search(
        r"<function_results\b[^>]*>([\s\S]*)$", cleaned, re.IGNORECASE
    )
    if unclosed:
        prose = _extract_prose_before_code_markup(unclosed.group(1))
        if prose:
            salvaged.append(prose)

    cleaned = _FUNCTION_XML_TAIL_RE.sub("", cleaned)
    cleaned = _CODE_PARAMETER_RE.sub("", cleaned)
    cleaned = _STRAY_TOOL_TAG_RE.sub("", cleaned)
    cleaned = _FENCED_PYTHON_RE.sub("", cleaned)

    if salvaged:
        prefix = "\n\n".join(salvaged)
        cleaned = f"{prefix}\n\n{cleaned}" if cleaned.strip() else prefix

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _reply_looks_like_leaked_tool_code(text: str) -> bool:
    """True when the reply is still mostly internal tool/code markup after stripping."""
    if _LEAKED_TOOL_MARKUP_RE.search(text):
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    code_lines = sum(1 for ln in lines if _PYTHON_LEAK_LINE_RE.match(ln))
    return code_lines >= 3 and (code_lines / len(lines)) >= 0.4


def _sanitize_agent_reply_for_slack(text: str) -> str:
    """Strip leaked tool XML/code and fall back to a safe message when nothing remains."""
    cleaned = _strip_tool_call_blocks(text or "")
    if not cleaned.strip():
        logger.warning("Slack reply empty after tool-markup strip — using fallback message")
        return _SLACK_REPLY_SANITIZE_FALLBACK
    if _reply_looks_like_leaked_tool_code(cleaned):
        logger.warning("Slack reply still looks like leaked tool/code markup — using fallback message")
        return _SLACK_REPLY_SANITIZE_FALLBACK
    return cleaned


_SLACK_MAX_BLOCKS = 50
_SLACK_BLOCK_TEXT_CHUNK = 2900  # conservative margin under Slack's ~3000 char block limit
_SLACK_TEXT_FALLBACK_MAX = 3000  # chat.postMessage text= notification / a11y fallback


def _plain_text_to_markdown_blocks(text: str) -> list[dict]:
    """Wrap plain text as markdown blocks, chunked under Slack's per-block limit."""
    safe_text = text or "(empty reply)"
    chunks = [
        safe_text[i : i + _SLACK_BLOCK_TEXT_CHUNK]
        for i in range(0, len(safe_text), _SLACK_BLOCK_TEXT_CHUNK)
    ] or ["(empty reply)"]
    return [{"type": "markdown", "text": c} for c in chunks]


_BLOCKKIT_FENCE_CLOSED_RE = re.compile(
    r"```blockkit[^\n`]*\s*\n([\s\S]*?)\s*```",
    re.IGNORECASE,
)
_BLOCKKIT_FENCE_OPEN_RE = re.compile(
    r"```blockkit[^\n`]*\s*\n([\s\S]+)$",
    re.IGNORECASE,
)
_BLOCKKIT_FENCE_OPEN_MARKER_RE = re.compile(
    r"```blockkit[^\n`]*\s*\n",
    re.IGNORECASE,
)
_BLOCKKIT_LOOSE_MARKER_RE = re.compile(
    r"(?:```\s*)?blockkit\b",
    re.IGNORECASE,
)
_BLOCKKIT_LEAD_NOISE_RE = re.compile(
    r"(?i)(let'?s formulate|according to the rules|format the response|fenced\s*blockkit|"
    r"native markdown|structured layout|block kit json)",
)
_KNOWN_SLACK_BLOCK_TYPES = frozenset(
    {
        "actions",
        "context",
        "divider",
        "file",
        "header",
        "image",
        "input",
        "markdown",
        "rich_text",
        "section",
        "video",
        "data_table",
        "data_visualization",
    }
)


def _coerce_slack_block_list(parsed: object) -> list[dict] | None:
    """Normalize parsed JSON into a list of Slack Block Kit block dicts."""
    if isinstance(parsed, dict):
        if "blocks" in parsed:
            candidate = parsed.get("blocks")
        elif "type" in parsed:
            candidate = [parsed]
        else:
            return None
    elif isinstance(parsed, list):
        candidate = parsed
    else:
        return None
    if not isinstance(candidate, list) or not candidate:
        return None
    if not all(isinstance(b, dict) and isinstance(b.get("type"), str) and b["type"] for b in candidate):
        return None
    return candidate


def _is_probable_slack_blocks(blocks: list[dict]) -> bool:
    """Heuristic: JSON array is likely an agent-emitted Block Kit payload."""
    if not blocks or len(blocks) > 200:
        return False
    for block in blocks:
        block_type = block.get("type")
        if not isinstance(block_type, str) or block_type not in _KNOWN_SLACK_BLOCK_TYPES:
            return False
    return True


def _parse_blockkit_json_payload(raw: str) -> list[dict] | None:
    """Parse JSON from a ```blockkit fence body (tolerates minor model formatting quirks)."""
    body = (raw or "").strip().lstrip("\ufeff")
    if not body:
        return None
    try:
        blocks = _coerce_slack_block_list(_json.loads(body))
        if blocks and _is_probable_slack_blocks(blocks):
            return blocks
    except _json.JSONDecodeError:
        pass

    decoder = _json.JSONDecoder()
    starts = [i for i in (body.find("["), body.find("{")) if i >= 0]
    for start in sorted(set(starts)):
        try:
            parsed, _end = decoder.raw_decode(body, start)
        except _json.JSONDecodeError:
            continue
        blocks = _coerce_slack_block_list(parsed)
        if blocks and _is_probable_slack_blocks(blocks):
            return blocks
    return None


def _find_loose_blockkit_span(cleaned: str) -> tuple[list[dict], int, int] | None:
    """Find an unfenced Block Kit JSON array (models often skip the ``` fence)."""
    best: tuple[list[dict], int, int] | None = None

    for marker in _BLOCKKIT_LOOSE_MARKER_RE.finditer(cleaned):
        blocks = _parse_blockkit_json_payload(cleaned[marker.end() :])
        if not blocks:
            continue
        array_start = cleaned.find("[", marker.end())
        if array_start < 0:
            continue
        decoder = _json.JSONDecoder()
        try:
            _parsed, array_end = decoder.raw_decode(cleaned, array_start)
        except _json.JSONDecodeError:
            continue
        candidate = (blocks, array_start, array_end)
        if best is None or len(blocks) > len(best[0]):
            best = candidate

    decoder = _json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "[":
            continue
        try:
            parsed, end = decoder.raw_decode(cleaned, index)
        except _json.JSONDecodeError:
            continue
        blocks = _coerce_slack_block_list(parsed)
        if blocks and _is_probable_slack_blocks(blocks):
            candidate = (blocks, index, end)
            if best is None or len(blocks) > len(best[0]):
                best = candidate
    return best


def _blockkit_lead_text(cleaned: str, payload_start: int) -> str:
    """Prose before a blockkit payload; omit model meta-reasoning when detected."""
    lead = cleaned[:payload_start].strip()
    if not lead:
        return ""
    lead = re.sub(r"^APP(?=[A-Za-z])", "", lead).strip()
    if _BLOCKKIT_LEAD_NOISE_RE.search(lead) or len(lead) > 400:
        return ""
    return lead[:_SLACK_BLOCK_TEXT_CHUNK]


def _strip_blockkit_payload_regions(text: str) -> str:
    """Remove fenced or loose blockkit JSON from text shown as plain markdown."""
    stripped = text or ""
    while True:
        span = _locate_blockkit_fence(stripped)
        if span is None:
            break
        stripped = (stripped[: span.fence_start] + stripped[span.fence_end :]).strip()
    loose_span = _find_loose_blockkit_span(stripped)
    if loose_span:
        _blocks, start, end = loose_span
        stripped = (stripped[:start] + stripped[end:]).strip()
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


class _BlockkitFenceSpan:
    """Span of a ```blockkit fence (JSON body may contain inner ``` code fences)."""

    __slots__ = ("fence_start", "body", "fence_end")

    def __init__(self, fence_start: int, body: str, fence_end: int):
        self.fence_start = fence_start
        self.body = body
        self.fence_end = fence_end

    def start(self, group: int = 0) -> int:
        if group == 0:
            return self.fence_start
        raise IndexError("no such group")

    def group(self, n: int) -> str:
        if n == 1:
            return self.body
        raise IndexError("no such group")


def _locate_blockkit_fence(cleaned: str) -> _BlockkitFenceSpan | None:
    """Locate a ```blockkit fence using JSON-aware end detection.

    Mrkdwn sections often embed ``` code tables inside the JSON payload. A naive
    non-greedy regex stops at the first inner ``` and truncates the payload.
    """
    for marker in _BLOCKKIT_FENCE_OPEN_MARKER_RE.finditer(cleaned):
        fence_start = marker.start()
        body_start = marker.end()
        rest = cleaned[body_start:]
        json_offsets = sorted({i for i in (rest.find("["), rest.find("{")) if i >= 0})
        decoder = _json.JSONDecoder()
        for offset in json_offsets:
            try:
                _parsed, rel_end = decoder.raw_decode(rest, offset)
            except _json.JSONDecodeError:
                continue
            body_end = body_start + rel_end
            body = cleaned[body_start + offset : body_end]
            trailing = cleaned[body_end:]
            stripped_trail = trailing.lstrip()
            if not stripped_trail:
                return _BlockkitFenceSpan(fence_start, body, body_end)
            close = re.match(r"```[^\n`]*(?:\n|$)", stripped_trail)
            if close:
                fence_end = body_end + len(trailing) - len(stripped_trail) + close.end()
                return _BlockkitFenceSpan(fence_start, body, fence_end)

    open_match = _BLOCKKIT_FENCE_OPEN_RE.search(cleaned)
    if open_match:
        return _BlockkitFenceSpan(open_match.start(), open_match.group(1), len(cleaned))
    return None


def _find_blockkit_fence(cleaned: str) -> _BlockkitFenceSpan | None:
    """Locate a ```blockkit fenced region (closed or truncated at end of reply)."""
    located = _locate_blockkit_fence(cleaned)
    if located is not None:
        return located
    # Legacy fallback for odd payloads without a parseable JSON prefix.
    closed = _BLOCKKIT_FENCE_CLOSED_RE.search(cleaned)
    if closed:
        return _BlockkitFenceSpan(closed.start(), closed.group(1), closed.end())
    return None


def _strip_blockkit_fences(text: str) -> str:
    """Remove blockkit fence bodies so fallbacks never show raw JSON in Slack."""
    return _strip_blockkit_payload_regions(text)


def _slack_reply_plain_fallback(
    cleaned: str,
    fence_match: _BlockkitFenceSpan | None,
    blocks: list[dict],
    *,
    payload_start: int | None = None,
) -> str:
    """Short plain-text fallback for notifications / a11y (no raw blockkit JSON)."""
    has_blockkit = fence_match is not None or payload_start is not None
    if has_blockkit and blocks:
        if fence_match:
            lead = _blockkit_lead_text(cleaned, fence_match.start())
        elif payload_start is not None:
            lead = _blockkit_lead_text(cleaned, payload_start)
        else:
            lead = ""
        if lead:
            return lead[:_SLACK_TEXT_FALLBACK_MAX]
        extracted = _blocks_to_plain_markdown(blocks)
        if extracted and extracted != "(empty reply)":
            return extracted[:_SLACK_TEXT_FALLBACK_MAX]
    display = _strip_blockkit_payload_regions(cleaned) if has_blockkit else (cleaned or "").strip()
    return (display or "(empty reply)")[:_SLACK_TEXT_FALLBACK_MAX]


def _build_slack_blocks(text: str) -> tuple[list[dict], str]:
    """Convert an agent's raw reply text into a Slack Block Kit blocks array.

    Looks for a single ```blockkit fenced JSON block (an array of block
    objects, or {"blocks": [...]}) anywhere in the text. If present and
    valid, those blocks are used (with any leading prose before the fence
    kept as a `markdown` block). Otherwise the whole cleaned reply is
    wrapped as one or more `markdown` blocks, chunked to stay under Slack's
    per-block text limit.

    Returns the *full* block list (may exceed ``_SLACK_MAX_BLOCKS``). Callers
    must split via ``_chunk_slack_blocks`` before posting — Slack rejects
    messages with more than 50 top-level blocks.

    Never raises — any parsing problem falls back to plain markdown blocks
    so a Slack reply is never dropped over a formatting error.
    """
    cleaned = _sanitize_agent_reply_for_slack(text)
    blocks: list[dict] = []
    fence_match = _find_blockkit_fence(cleaned)
    loose_span: tuple[list[dict], int, int] | None = None
    payload_start: int | None = None

    if fence_match:
        candidate_blocks = _parse_blockkit_json_payload(fence_match.group(1))
        if candidate_blocks:
            lead_text = _blockkit_lead_text(cleaned, fence_match.start())
            if lead_text:
                blocks.append({"type": "markdown", "text": lead_text})
            blocks.extend(candidate_blocks)
            payload_start = fence_match.start()
        else:
            logger.warning(
                "Failed to parse ```blockkit fence in agent reply (len=%d); falling back to plain text",
                len(fence_match.group(1) or ""),
            )

    if not blocks:
        loose_span = _find_loose_blockkit_span(cleaned)
        if loose_span:
            candidate_blocks, array_start, _array_end = loose_span
            lead_text = _blockkit_lead_text(cleaned, array_start)
            if lead_text:
                blocks.append({"type": "markdown", "text": lead_text})
            blocks.extend(candidate_blocks)
            payload_start = array_start

    if not blocks:
        display = _strip_blockkit_payload_regions(cleaned) if (fence_match or loose_span) else cleaned
        blocks = _plain_text_to_markdown_blocks(display or "(empty reply)")

    fallback = _slack_reply_plain_fallback(
        cleaned, fence_match, blocks, payload_start=payload_start
    )
    return blocks, fallback


def _chunk_slack_blocks(
    blocks: list[dict],
    *,
    max_blocks: int = _SLACK_MAX_BLOCKS,
) -> list[list[dict]]:
    """Split a block list into batches of at most ``max_blocks`` (Slack limit)."""
    limit = max(1, int(max_blocks))
    if not blocks:
        return [[{"type": "markdown", "text": "(empty reply)"}]]
    return [blocks[i : i + limit] for i in range(0, len(blocks), limit)]


def _extract_block_plain_text(block: dict) -> str:
    """Best-effort plain text from a single Block Kit block (for text= fallback)."""
    if not isinstance(block, dict):
        return ""
    raw = block.get("text")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        inner = raw.get("text")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    fields = block.get("fields")
    if isinstance(fields, list):
        parts = []
        for field in fields:
            if isinstance(field, dict) and isinstance(field.get("text"), str):
                parts.append(field["text"].strip())
        if parts:
            return "\n".join(parts)
    return ""


def _batch_fallback_text(
    batch: list[dict],
    *,
    part: int,
    total: int,
    full_fallback: str,
) -> str:
    """Build a short ``text=`` fallback for one message in a multi-part reply."""
    extracted = ""
    for block in batch:
        extracted = _extract_block_plain_text(block)
        if extracted:
            break
    base = extracted or (full_fallback or "").strip() or "(empty reply)"
    if total > 1:
        prefix = f"(part {part}/{total}) "
        budget = max(0, _SLACK_TEXT_FALLBACK_MAX - len(prefix))
        return prefix + base[:budget]
    return base[:_SLACK_TEXT_FALLBACK_MAX]


def _blocks_to_plain_markdown(blocks: list[dict]) -> str:
    """Flatten Block Kit blocks to plain markdown when Slack rejects the structure."""
    parts: list[str] = []
    for block in blocks:
        piece = _extract_block_plain_text(block)
        if piece:
            parts.append(piece)
    return "\n\n".join(parts).strip() or "(empty reply)"


# Cap flattened inbound Block Kit text so a huge message cannot blow the LLM context.
_SLACK_INBOUND_BLOCKS_MAX_CHARS = max(
    4_000, int(os.environ.get("SLACK_INBOUND_BLOCKS_MAX_CHARS", "100000"))
)


def _plain_text_from_slack_message(msg: dict | None, *, max_chars: int | None = None) -> str:
    """Build LLM-visible text from a Slack message/event ``text`` + ``blocks``.

    Outbound Block Kit replies often put the real body in ``blocks`` and only a
    short teaser in ``text`` (notification / a11y fallback). Inbound
    ``app_mention`` / thread history must flatten ``blocks`` or agents only see
    the teaser (e.g. Hermes "compiling…" while the EDD report is in blocks).
    """
    if not isinstance(msg, dict):
        return ""
    limit = _SLACK_INBOUND_BLOCKS_MAX_CHARS if max_chars is None else max(1, int(max_chars))
    text = (msg.get("text") or "").strip()
    raw_blocks = msg.get("blocks")
    blocks = [b for b in raw_blocks if isinstance(b, dict)] if isinstance(raw_blocks, list) else []
    if not blocks:
        return text[:limit] if len(text) > limit else text

    from_blocks = _blocks_to_plain_markdown(blocks)
    if not from_blocks or from_blocks == "(empty reply)":
        return text[:limit] if len(text) > limit else text

    if not text:
        merged = from_blocks
    elif from_blocks in text:
        merged = text
    elif text in from_blocks:
        # Fallback text is already covered by flattened blocks (common for @mentions).
        merged = from_blocks
    else:
        merged = f"{text}\n\n{from_blocks}"

    if len(merged) > limit:
        return merged[:limit] + f"\n\n…[truncated {len(merged) - limit} chars]"
    return merged


def _is_slack_invalid_blocks_error(exc: BaseException) -> bool:
    """True when Slack rejected the payload for Block Kit shape/size reasons."""
    if "invalid_blocks" in str(exc):
        return True
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    if isinstance(resp, dict):
        return resp.get("error") == "invalid_blocks"
    try:
        if resp.get("error") == "invalid_blocks":  # type: ignore[union-attr]
            return True
    except Exception:
        pass
    data = getattr(resp, "data", None)
    return isinstance(data, dict) and data.get("error") == "invalid_blocks"


def _post_block_batches_sync(
    *,
    client: Any,
    channel: str,
    thread_ts: str | None,
    batches: list[list[dict]],
    full_fallback: str,
    update_ts: str | None = None,
    ephemeral_user: str | None = None,
) -> None:
    """Post each block batch as its own Slack message (sync; runs in a worker thread).

    The first batch may ``chat.update`` an in-progress placeholder. Later batches
    are always new messages in the same thread. If Slack rejects a batch with
    ``invalid_blocks``, that batch is flattened to plain markdown and retried
    (still split to ≤50 blocks) so a long dossier cannot wedge the retry loop.
    """
    total = len(batches)
    for index, batch in enumerate(batches):
        part = index + 1
        text_fb = _batch_fallback_text(
            batch, part=part, total=total, full_fallback=full_fallback
        )
        posted = False

        def _try_post(blocks_payload: list[dict], text_payload: str) -> None:
            nonlocal posted
            if ephemeral_user:
                client.chat_postEphemeral(
                    channel=channel,
                    user=ephemeral_user,
                    text=text_payload,
                    blocks=blocks_payload,
                    thread_ts=thread_ts or None,
                )
            elif index == 0 and update_ts and not posted:
                try:
                    client.chat_update(
                        channel=channel,
                        ts=update_ts,
                        text=text_payload,
                        blocks=blocks_payload,
                    )
                except Exception as upd_exc:
                    # invalid_blocks must bubble up so the caller can flatten to
                    # plain markdown; other update failures (deleted placeholder)
                    # fall back to a fresh post with the same payload.
                    if _is_slack_invalid_blocks_error(upd_exc):
                        raise
                    logger.warning(
                        "chat_update failed for channel=%s ts=%s — posting new message instead",
                        channel,
                        update_ts,
                        exc_info=True,
                    )
                    client.chat_postMessage(
                        channel=channel,
                        text=text_payload,
                        blocks=blocks_payload,
                        thread_ts=thread_ts or None,
                    )
            else:
                client.chat_postMessage(
                    channel=channel,
                    text=text_payload,
                    blocks=blocks_payload,
                    thread_ts=thread_ts or None,
                )
            posted = True

        try:
            _try_post(batch, text_fb)
        except Exception as exc:
            if not _is_slack_invalid_blocks_error(exc):
                raise
            logger.warning(
                "Slack invalid_blocks on part %d/%d channel=%s — falling back to plain markdown",
                part,
                total,
                channel,
                exc_info=True,
            )
            plain = _blocks_to_plain_markdown(batch)
            plain_batches = _chunk_slack_blocks(_plain_text_to_markdown_blocks(plain))
            for plain_batch in plain_batches:
                plain_fb = _batch_fallback_text(
                    plain_batch, part=part, total=total, full_fallback=plain
                )
                _try_post(plain_batch, plain_fb)

        if total > 1:
            logger.info(
                "Posted Slack reply part %d/%d channel=%s blocks=%d",
                part,
                total,
                channel,
                len(batch),
            )


async def _post_slack_reply(
    xoxb: str,
    channel: str,
    thread_ts: str,
    text: str,
    *,
    update_ts: str | None = None,
) -> bool:
    """Post (or, if *update_ts* is given, edit in place) a Slack message.

    Uses the sync WebClient run in a thread executor — aiohttp is not installed
    in this environment, so the async Slack client is unavailable.

    Always sends Block Kit ``blocks=``, with a short plain ``text=`` fallback
    (notifications, screen readers, search).

    Replies that exceed Slack's 50-block limit are split across multiple
    threaded messages (part 1/N, 2/N, …). When *update_ts* is set, part 1 edits
    the in-progress "Thinking…" placeholder; later parts are new posts.

    Returns True if Slack accepted every part, False on failure.
    """
    from slack_sdk import WebClient

    blocks, fallback_text = _build_slack_blocks(text)
    batches = _chunk_slack_blocks(blocks)
    if len(batches) > 1:
        logger.info(
            "Splitting Slack reply into %d messages (%d blocks) channel=%s",
            len(batches),
            len(blocks),
            channel,
        )

    def _send() -> None:
        client = WebClient(token=xoxb)
        _post_block_batches_sync(
            client=client,
            channel=channel,
            thread_ts=thread_ts or None,
            batches=batches,
            full_fallback=fallback_text or "(empty reply)",
            update_ts=update_ts,
        )

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send)
        return True
    except Exception:
        logger.exception("Failed to post Slack reply to channel=%s", channel)
        return False


async def _post_slack_ephemeral(
    xoxb: str,
    channel: str,
    thread_ts: str,
    user: str,
    text: str,
) -> bool:
    """Post an only-visible-to-user reply via ``chat.postEphemeral``.

    Oversized Block Kit replies are split across multiple ephemeral messages
    (same 50-block cap as public replies).
    """
    from slack_sdk import WebClient

    if not user:
        return False

    blocks, fallback_text = _build_slack_blocks(text)
    batches = _chunk_slack_blocks(blocks)

    def _send() -> None:
        client = WebClient(token=xoxb)
        _post_block_batches_sync(
            client=client,
            channel=channel,
            thread_ts=thread_ts or None,
            batches=batches,
            full_fallback=fallback_text or "(empty reply)",
            ephemeral_user=user,
        )

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _send)
        return True
    except Exception:
        logger.exception(
            "Failed to post ephemeral Slack reply to channel=%s user=%s", channel, user
        )
        return False


# Reactions used to give the user immediate, at-a-glance feedback on progress:
#   👀 seen/working  →  ✅ replied / 🔒 private  /  ⚠️ error
_ACK_REACTION = "eyes"
_DONE_REACTION = "white_check_mark"
_ERROR_REACTION = "warning"
_PRIVATE_REPLY_REACTION = "lock"

# Same playful spinner verbs used for the "…" loading state in the workspace
# chat UI (see SPINNER_VERBS in src/pages/WorkspacePage.tsx) — kept in sync
# manually since one side is TS and the other Python.
_SPINNER_VERBS = [
    "Accomplishing", "Actioning", "Actualizing", "Architecting", "Baking", "Beaming",
    "Beboppin'", "Befuddling", "Billowing", "Blanching", "Bloviating", "Boogieing",
    "Boondoggling", "Booping", "Bootstrapping", "Brewing", "Bunning", "Burrowing",
    "Calculating", "Canoodling", "Caramelizing", "Cascading", "Catapulting", "Cerebrating",
    "Channeling", "Channelling", "Choreographing", "Churning", "Clauding", "Coalescing",
    "Cogitating", "Combobulating", "Composing", "Computing", "Concocting", "Considering",
    "Contemplating", "Cooking", "Crafting", "Creating", "Crunching", "Crystallizing",
    "Cultivating", "Deciphering", "Deliberating", "Determining", "Dilly-dallying",
    "Discombobulating", "Doing", "Doodling", "Drizzling", "Ebbing", "Effecting",
    "Elucidating", "Embellishing", "Enchanting", "Envisioning", "Evaporating", "Fermenting",
    "Fiddle-faddling", "Finagling", "Flambéing", "Flibbertigibbeting", "Flowing",
    "Flummoxing", "Fluttering", "Forging", "Forming", "Frolicking", "Frosting",
    "Gallivanting", "Galloping", "Garnishing", "Generating", "Gesticulating", "Germinating",
    "Gitifying", "Grooving", "Gusting", "Harmonizing", "Hashing", "Hatching", "Herding",
    "Honking", "Hullaballooing", "Hyperspacing", "Ideating", "Imagining", "Improvising",
    "Incubating", "Inferring", "Infusing", "Ionizing", "Jitterbugging", "Julienning",
    "Kneading", "Leavening", "Levitating", "Lollygagging", "Manifesting", "Marinating",
    "Meandering", "Metamorphosing", "Misting", "Moonwalking", "Moseying", "Mulling",
    "Mustering", "Musing", "Nebulizing", "Nesting", "Newspapering", "Noodling", "Nucleating",
    "Orbiting", "Orchestrating", "Osmosing", "Perambulating", "Percolating", "Perusing",
    "Philosophising", "Photosynthesizing", "Pollinating", "Pondering", "Pontificating",
    "Pouncing", "Precipitating", "Prestidigitating", "Processing", "Proofing", "Propagating",
    "Puttering", "Puzzling", "Quantumizing", "Razzle-dazzling", "Razzmatazzing",
    "Recombobulating", "Reticulating", "Roosting", "Ruminating", "Sautéing", "Scampering",
    "Schlepping", "Scurrying", "Seasoning", "Shenaniganing", "Shimmying", "Simmering",
    "Skedaddling", "Sketching", "Slithering", "Smooshing", "Sock-hopping", "Spelunking",
    "Spinning", "Sprouting", "Stewing", "Sublimating", "Swirling", "Swooping", "Symbioting",
    "Synthesizing", "Tempering", "Thinking", "Thundering", "Tinkering", "Tomfoolering",
    "Topsy-turvying", "Transfiguring", "Transmuting", "Twisting", "Undulating", "Unfurling",
    "Unravelling", "Vibing", "Waddling", "Wandering", "Warping", "Whatchamacalliting",
    "Whirlpooling", "Whirring", "Whisking", "Wibbling", "Working", "Wrangling", "Zesting",
    "Zigzagging",
]


def _random_spinner_verb() -> str:
    return random.choice(_SPINNER_VERBS)


def _slack_react(xoxb: str, channel: str, ts: str, emoji: str) -> None:
    """Best-effort: add a reaction to a message. Failures are non-fatal."""
    if not ts:
        return
    from slack_sdk import WebClient
    try:
        WebClient(token=xoxb).reactions_add(channel=channel, timestamp=ts, name=emoji)
    except Exception:
        logger.debug("Failed to add :%s: reaction channel=%s ts=%s", emoji, channel, ts, exc_info=True)


def _slack_unreact(xoxb: str, channel: str, ts: str, emoji: str) -> None:
    """Best-effort: remove a reaction from a message. Failures are non-fatal."""
    if not ts:
        return
    from slack_sdk import WebClient
    try:
        WebClient(token=xoxb).reactions_remove(channel=channel, timestamp=ts, name=emoji)
    except Exception:
        logger.debug("Failed to remove :%s: reaction channel=%s ts=%s", emoji, channel, ts, exc_info=True)


def _slack_swap_reaction(xoxb: str, channel: str, ts: str, old: str, new: str) -> None:
    """Best-effort: remove *old* reaction and add *new* on the same message."""
    _slack_unreact(xoxb, channel, ts, old)
    _slack_react(xoxb, channel, ts, new)


def _post_thinking_placeholder(xoxb: str, channel: str, thread_ts: str) -> str | None:
    """Post a lightweight "<Verb>…" placeholder (using the same random spinner
    verbs as the workspace chat UI) so the user knows work has started, before
    the (possibly slow) LLM call. Returns the message ``ts`` so it can later be
    edited in place with the real reply, or ``None`` on failure (in which case
    the final reply just posts as a new message)."""
    from slack_sdk import WebClient
    verb = _random_spinner_verb()
    placeholder_text = f"_{verb}…_ :hourglass_flowing_sand:"
    try:
        resp = WebClient(token=xoxb).chat_postMessage(
            channel=channel,
            thread_ts=thread_ts or None,
            text=f"{verb}…",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": placeholder_text}}],
        )
        return resp.get("ts")
    except Exception:
        logger.debug("Failed to post thinking placeholder channel=%s", channel, exc_info=True)
        return None


def _slack_delete_message(xoxb: str, channel: str, ts: str) -> None:
    """Best-effort: delete a message (used to clean up an unused placeholder)."""
    if not ts:
        return
    from slack_sdk import WebClient
    try:
        WebClient(token=xoxb).chat_delete(channel=channel, ts=ts)
    except Exception:
        logger.debug("Failed to delete message channel=%s ts=%s", channel, ts, exc_info=True)


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/api/slack/events")
async def slack_events(request: Request) -> Response:
    """Single inbound endpoint for all Slack Event API payloads.

    Slack sends a POST for every subscribed event.  The ``api_app_id`` field
    in the payload identifies which registered agent should handle it.
    """
    raw_body = await request.body()

    # Parse JSON — fail fast if malformed
    try:
        import json
        payload = json.loads(raw_body)
    except (ValueError, TypeError):
        return JSONResponse(status_code=400, content={"error": "invalid_json"})

    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "invalid_payload"})

    # url_verification challenge (no signature header yet when Slack tests the URL)
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge", "")
        return JSONResponse(content={"challenge": challenge})

    # Identify the app
    slack_app_id: str = payload.get("api_app_id", "").strip()
    if not slack_app_id:
        logger.warning("Slack event received with no api_app_id")
        return JSONResponse(status_code=400, content={"error": "missing_api_app_id"})

    # Look up agent
    agent = _get_agent_by_app_id(slack_app_id)
    if not agent:
        logger.warning("No agent registered for slack_app_id=%s", slack_app_id)
        return JSONResponse(status_code=200, content={"ok": True})  # 200 to stop Slack retries

    # Retrieve signing secret and verify request signature
    signing_secret = _read_secret(getattr(agent, "signing_secret_ref", None))
    if not signing_secret:
        logger.error(
            "Agent %s (app=%s) has no signing_secret stored — rejecting event",
            agent.id, slack_app_id,
        )
        return Response(status_code=403)

    timestamp = request.headers.get("x-slack-request-timestamp", "")
    provided_sig = request.headers.get("x-slack-signature", "")

    if not _verify_slack_signature(signing_secret, timestamp, raw_body, provided_sig):
        logger.warning(
            "Invalid Slack signature for app=%s — possible replay or spoofed request",
            slack_app_id,
        )
        return Response(status_code=403)

    # Dispatch the inner event
    event_wrapper = payload.get("event", {})
    event_type = event_wrapper.get("type", "")
    channel_type = event_wrapper.get("channel_type", "")
    event_id: str = (payload.get("event_id") or "").strip()
    retry_num: str = request.headers.get("x-slack-retry-num", "").strip()

    logger.info(
        "Slack event app=%s agent=%s event_id=%s event_type=%s channel_type=%s retry_num=%s",
        slack_app_id,
        agent.id,
        event_id or "(none)",
        event_type or "(none)",
        channel_type or "(none)",
        retry_num or "(none)",
    )

    # Ignore bot messages to prevent reply loops.
    # Exception: app_mention events from bots are allowed through so that
    # authorised Slack apps (AgentSlackAppGrant) can invoke this agent.
    # The grant check happens inside _dispatch_event; unauthorised bots are
    # dropped there.  Non-mention bot events (DMs, channel broadcasts, etc.)
    # are still filtered here to avoid loops.
    if event_wrapper.get("subtype") == "bot_message" or event_wrapper.get("bot_id"):
        if event_type != "app_mention":
            return JSONResponse(content={"ok": True})

    # Handle member_joined_channel — only when the bot itself is joining
    if event_type == "member_joined_channel":
        if _is_bot_joining_channel(agent, event_wrapper):
            asyncio.create_task(_handle_channel_join(agent, event_wrapper))
        return JSONResponse(content={"ok": True})

    # Only dispatch app_mention (channels), message in DMs (channel_type == "im"),
    # or a plain channel message when the agent has opted into "ambient" mode
    # for this specific channel (AgentChannelConfig.join_behavior.proactivity).
    # This prevents double-replies: channel mentions fire both app_mention AND message
    # for the SAME text — so a mention of this bot must not also qualify as an
    # ambient plain-channel message, or both events would dispatch independently.
    own_bot_user_id = str(getattr(agent, "slack_bot_user_id", "") or "").strip()
    mentions_this_bot = bool(
        own_bot_user_id and f"<@{own_bot_user_id}>" in (event_wrapper.get("text") or "")
    )
    is_plain_channel_message = (
        event_type == "message"
        and channel_type != "im"
        and not event_wrapper.get("subtype")
        and not mentions_this_bot
    )
    should_dispatch = (
        event_type == "app_mention"
        or (event_type == "message" and channel_type == "im")
        or (is_plain_channel_message and _is_ambient_channel(agent, event_wrapper.get("channel", "")))
    )
    if not should_dispatch:
        return JSONResponse(content={"ok": True})

    if not agent_accepts_runtime_requests(agent.status):
        logger.info(
            "Agent not active — ignoring Slack event app=%s agent=%s status=%s",
            slack_app_id, agent.id, agent.status,
        )
        is_bot = bool(
            event_wrapper.get("bot_id") or event_wrapper.get("subtype") == "bot_message"
        )
        if not is_bot:
            try:
                _notify_agent_unavailable_from_event(agent, event_wrapper)
            except Exception:
                logger.exception(
                    "Failed to notify unavailable agent app=%s agent=%s",
                    slack_app_id, agent.id,
                )
        return JSONResponse(content={"ok": True})

    # Deduplicate on Slack's event_id — at-least-once delivery from Slack/relay
    # retries would otherwise spawn multiple LLM calls for the same user message.
    # A leased claim (see claim_slack_event) also lets a Slack retry reprocess an
    # event whose dispatch died mid-flight instead of being ack-dropped forever.
    claimed_event = False
    if event_id:
        if not validate_slack_event_id(event_id):
            logger.warning(
                "Slack event_id has unexpected shape app=%s event_id=%s — skipping dedup",
                slack_app_id,
                event_id,
            )
        else:
            outcome = claim_slack_event(event_id)
            if outcome == CLAIM_DONE:
                logger.info(
                    "Duplicate Slack event ignored (already completed) app=%s event_id=%s "
                    "event_type=%s retry_num=%s",
                    slack_app_id, event_id, event_type, retry_num or "(none)",
                )
                return JSONResponse(content={"ok": True})
            if outcome == CLAIM_IN_PROGRESS:
                logger.info(
                    "Slack event already in progress app=%s event_id=%s retry_num=%s — skipping",
                    slack_app_id, event_id, retry_num or "(none)",
                )
                return JSONResponse(content={"ok": True})
            claimed_event = True
    else:
        logger.warning(
            "Slack event missing event_id app=%s event_type=%s — cannot deduplicate",
            slack_app_id,
            event_type,
        )

    if random.random() < 0.01:
        loop_purge = asyncio.get_running_loop()
        await loop_purge.run_in_executor(None, purge_stale_slack_event_dedup)
        await loop_purge.run_in_executor(None, purge_stale_ephemeral_replies)

    # Fire-and-forget dispatch. The lease is marked done inside _dispatch_event
    # as soon as the Slack reply posts; settle here is a safety net for early
    # returns (no reply) and for failures before the reply.
    async def _dispatch_and_settle() -> None:
        loop = asyncio.get_running_loop()
        try:
            await _dispatch_event(
                agent,
                event_wrapper,
                event_id=event_id if claimed_event else None,
            )
        except Exception:
            logger.exception(
                "Slack dispatch failed app=%s event_id=%s — releasing dedup claim "
                "(no-op if reply already committed)",
                slack_app_id, event_id,
            )
            if claimed_event and event_id:
                await loop.run_in_executor(None, release_slack_event_id, event_id)
            return
        # Early returns (access denied, silent drop, etc.) never marked done
        # inside dispatch — settle the lease now so retries do not re-run.
        if claimed_event and event_id:
            await loop.run_in_executor(None, mark_slack_event_done, event_id)

    asyncio.create_task(_dispatch_and_settle())

    return JSONResponse(content={"ok": True})
