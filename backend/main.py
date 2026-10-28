import asyncio
import base64
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import partial
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from model_catalog import platform_default_model

# Load backend/.env before any module reads os.environ (database, cf_access, …).
_env_dir = Path(__file__).resolve().parent
load_dotenv(_env_dir / ".env")
# Wizard-written overrides (local backend/platform.env or /data/platform.env in Docker).
from auth_env_guard import load_platform_env_safely, reconcile_auth_environment

_hub_db = os.environ.get("HUB_DATABASE_PATH", "").strip()
if _hub_db:
    _platform_env = Path(_hub_db).resolve().parent / "platform.env"
    load_platform_env_safely(_platform_env)
elif (_env_dir / "platform.env").is_file():
    load_platform_env_safely(_env_dir / "platform.env")
reconcile_auth_environment()

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from auth_policy import AuthContext
from app_config import APP_NAME, ORG_NOUN, http_user_agent
from cf_access import (
    ENABLE_MOCK_AUTH,
    OIDC_ISSUER,
    POLICY_AUD,
    TEAM_DOMAIN,
    assert_mock_auth_not_in_production,
    build_auth_context,
)
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from database import (
    Automation,
    AutomationRun,
    Chat,
    Memory,
    MemorySyncLog,
    Message,
    SessionLocal,
    StoredPdf,
    UserSecret,
    init_db,
)

from user_profile import (
    get_user_profile,
    profile_payload,
    touch_user_session,
    update_tour_version,
)
from tenancy import request_owner_sub, workspace_subdirectory_for_owner, get_tenant_or_404
from model_catalog import (
    CHAT_FALLBACK_MODEL,
    WORKSPACE_CHAT_DEFAULT_MODEL,
    get_model_catalog,
    is_auto_workspace_model,
    is_canonical_ui_model,
    is_chat_model_allowed,
    normalize_workspace_model,
)
from skill_router import recent_user_messages, resolve_user_skill_access, route_skill_for_chat
from chat_session_control import (
    clear_session,
    get_session,
    register_session,
    request_cancel,
)
from workspace_openai_chat import (
    ChatToolRuntime,
    OpenAIChatState,
    stream_auto_answer_now_synthesis,
    stream_workspace_auto_chat,
)

logger = logging.getLogger(__name__)

# Per-request secrets context variable — scopes merged_secrets to the current
# asyncio Task so concurrent web-chat requests cannot share secret state.
# Set by the /api/chat handler before the streaming generator runs; cleared in
# the generator's finally block.  Callers that need to pass secrets explicitly
# (e.g. execute_in_sandbox) continue doing so; this ContextVar provides a
# second, defence-in-depth isolation layer.
_request_secrets: ContextVar[dict] = ContextVar("_request_secrets", default={})


def _normalize_gateway_base_url_for_anthropic_sdk(url: str) -> tuple[str, str]:
    """Return (normalized_url, original_for_logging).

    The Anthropic Python client posts to {base_url}/v1/messages. If callers set
    MODEL_GATEWAY_URL with a trailing /v1 (common when copying OpenAI-style URLs),
    requests hit .../v1/v1/messages and gateways return HTTP 404.
    """
    raw = url.strip()
    u = raw.rstrip("/")
    lowered = u.lower()
    if lowered.endswith("/v1"):
        u = u[: -len("/v1")].rstrip("/")
    return u or raw.rstrip("/"), raw


# ── Config ────────────────────────────────────────────────────────────────────

_MODEL_GATEWAY_URL_RAW: str = os.environ.get(
    "MODEL_GATEWAY_URL", "http://localhost:4000/v1"
)
MODEL_GATEWAY_URL, _MODEL_GATEWAY_URL_PRE_NORMALIZE = (
    _normalize_gateway_base_url_for_anthropic_sdk(_MODEL_GATEWAY_URL_RAW)
)
if MODEL_GATEWAY_URL.rstrip("/") != _MODEL_GATEWAY_URL_PRE_NORMALIZE.rstrip("/"):
    logger.warning(
        "MODEL_GATEWAY_URL had a trailing /v1; using %r for Anthropic SDK "
        "(the client appends /v1/messages). Remove /v1 from .env if you prefer.",
        MODEL_GATEWAY_URL,
    )
MODEL_GATEWAY_API_KEY: Optional[str] = os.environ.get("MODEL_GATEWAY_API_KEY")
MODEL: str = platform_default_model()


def _reload_model_gateway_from_env() -> None:
    """Refresh gateway module vars after platform.env is loaded."""
    global MODEL_GATEWAY_API_KEY, MODEL_GATEWAY_URL, _MODEL_GATEWAY_URL_RAW, _MODEL_GATEWAY_URL_PRE_NORMALIZE

    from model_gateway import apply_gateway_env_defaults, resolved_gateway_api_key

    apply_gateway_env_defaults()
    _MODEL_GATEWAY_URL_RAW = os.environ.get("MODEL_GATEWAY_URL", "http://localhost:4000/v1")
    MODEL_GATEWAY_URL, _MODEL_GATEWAY_URL_PRE_NORMALIZE = (
        _normalize_gateway_base_url_for_anthropic_sdk(_MODEL_GATEWAY_URL_RAW)
    )
    MODEL_GATEWAY_API_KEY = resolved_gateway_api_key(
        os.environ.get("MODEL_GATEWAY_API_KEY"),
        os.environ.get("MODEL_GATEWAY_URL"),
    )


def _non_empty_slack_channel(ch: Optional[str]) -> Optional[str]:
    """Normalized channel id/name or None if unset."""
    if ch is None:
        return None
    s = str(ch).strip()
    return s if s else None


def slack_user_token_integration_allowed(owner_sub: str) -> bool:
    """Return True if this user is permitted to use the Slack user-token integration.

    Controlled by the SLACK_USER_TOKEN_OWNER_SUBS env var: a comma-separated
    list of JWT subject IDs.  If the variable is unset or empty, the feature
    is open to all authenticated users.
    """
    raw = os.environ.get("SLACK_USER_TOKEN_OWNER_SUBS", "").strip()
    if not raw:
        return True
    allowed = {s.strip() for s in raw.split(",") if s.strip()}
    return owner_sub in allowed

SKILLS_DIR = Path(__file__).parent.parent / "src" / "data" / "skills"
GENERATED_DIR = Path(__file__).parent / "generated"
GENERATED_DIR.mkdir(exist_ok=True)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# blob store used for skills and chat attachments (same bucket, different prefixes)


def _user_workspace_dir(owner_sub: str) -> Path:
    """Per-tenant subdirectory under generated/."""
    return workspace_subdirectory_for_owner(GENERATED_DIR, owner_sub)


# ── URL fetcher for the fetch_url tool ────────────────────────────────────────

async def _fetch_url_content(url: str) -> str:
    """Fetch a public URL and return cleaned plain text.

    Security measures:
    - Validates scheme (http/https only)
    - SSRF protection: rejects private/loopback/link-local IP ranges
    - Re-validates each redirect target
    - 15-second timeout
    - 500 KB response body cap
    - Strips HTML tags via BeautifulSoup before returning
    """
    from url_fetch_guard import async_fetch_response, validate_fetch_url

    ok, err = validate_fetch_url(url)
    if not ok:
        return f"Error: {err}"

    try:
        from bs4 import BeautifulSoup

        headers = {
            "User-Agent": http_user_agent("Fetch"),
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        }
        resp = await async_fetch_response(url, headers=headers, timeout=15.0, max_redirects=5)
        resp.raise_for_status()

        # Cap raw body at 500 KB before parsing
        raw = resp.text[:512_000]
        content_type = resp.headers.get("content-type", "")

        if "html" in content_type:
            soup = BeautifulSoup(raw, "lxml")
            # Remove non-content elements
            for tag in soup(["script", "style", "nav", "footer", "header",
                              "aside", "noscript", "iframe", "form"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
        else:
            text = raw

        # Collapse runs of blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        # Cap at ~30 000 chars (~7 500 tokens) to keep context manageable
        if len(text) > 30_000:
            text = text[:30_000] + "\n\n[Content truncated at 30 000 characters]"

        return text or "(page returned no readable text)"

    except httpx.HTTPStatusError as exc:
        return f"Error: HTTP {exc.response.status_code} — {exc.response.reason_phrase}"
    except httpx.TimeoutException:
        return "Error: Request timed out after 15 seconds."
    except Exception as exc:
        logger.warning("fetch_url failed for %s: %s", url, exc)
        return f"Error fetching URL: {exc}"


DEFAULT_SYSTEM = (
    f"You are an AI assistant integrated into {APP_NAME} — "
    f"an enterprise AI productivity platform for {ORG_NOUN}s. Be concise, direct, and "
    "technically precise. Use markdown tables for data comparisons and "
    "code blocks for SQL, Python, or config snippets."
)

# ── Slack query skill — injected when user has SLACK_USER_TOKEN ───────────────
#
# The user token (xoxp-...) supports search:read, channels:history,
# channels:read, users:read, etc., so the full search API is available.
# Bot tokens (xoxb-...) do not support search:read.
#
_WORKSPACE_SLACK_SKILL = """

---

## Slack Workspace Queries

The user has connected their Slack account. `SLACK_USER_TOKEN` is available in
every `run_python` call. Use `slack_sdk` to answer questions about Slack messages,
channels, people, and conversations.

```python
import os, json
from slack_sdk import WebClient
slack = WebClient(token=os.environ["SLACK_USER_TOKEN"])

# ── Who am I? ─────────────────────────────────────────────────────────────────
me = slack.auth_test()
print("Logged in as:", me["user"], "in workspace:", me["team"])

# ── Search messages across the whole workspace ────────────────────────────────
results = slack.search_messages(query="budget Q2", count=20, sort="timestamp", sort_dir="desc")
for m in results["messages"]["matches"]:
    print(f"[#{m['channel']['name']}] {m['username']}: {m['text'][:200]}")

# ── Search messages sent by a specific person ─────────────────────────────────
results = slack.search_messages(query="from:@ansh deployment", count=10)
for m in results["messages"]["matches"]:
    print(m["text"][:300])

# ── List channels ─────────────────────────────────────────────────────────────
result = slack.conversations_list(types="public_channel,private_channel", limit=200)
for c in result["channels"]:
    print(c["id"], "#" + c["name"], "members:", c.get("num_members", "?"))

# ── Read recent messages from a channel ──────────────────────────────────────
result = slack.conversations_history(channel="CXXXXXXXX", limit=30)
for msg in reversed(result["messages"]):
    user_id = msg.get("user", msg.get("bot_id", "?"))
    print(f"{user_id}: {msg.get('text','')[:200]}")

# ── Read a thread ─────────────────────────────────────────────────────────────
result = slack.conversations_replies(channel="CXXXXXXXX", ts="1234567890.123456")
for msg in result["messages"]:
    print(msg.get("text", "")[:300])

# ── Find a user by email ──────────────────────────────────────────────────────
result = slack.users_lookupByEmail(email="name@company.com")
u = result["user"]
print(u["id"], u["real_name"], u["profile"].get("title",""))

# ── Get user profile ──────────────────────────────────────────────────────────
result = slack.users_info(user="UXXXXXXXX")
print(result["user"]["profile"]["real_name"], result["user"]["profile"].get("email",""))

# ── Resolve <@UXXXXXXXX> mentions in messages ─────────────────────────────────
import re
def resolve_mentions(text, slack_client):
    for uid in re.findall(r"<@(U[A-Z0-9]+)>", text):
        try:
            name = slack_client.users_info(user=uid)["user"]["real_name"]
            text = text.replace(f"<@{uid}>", f"@{name}")
        except Exception:
            pass
    return text
```


# ── Read latest DMs (direct messages) ────────────────────────────────────────
# ALWAYS use types="im" — never call conversations_list() without types.
# Calling without types lists ALL channels and instantly hits rate limits.
result = slack.conversations_list(types="im", limit=50)
dm_channels = result["channels"]  # each has "id" and "user" (other person's UID)

# Resolve user IDs → names
def get_name(uid):
    try: return slack.users_info(user=uid)["user"]["real_name"]
    except: return uid

for dm in dm_channels[:10]:  # limit to 10 most recent DMs
    other = get_name(dm["user"])
    history = slack.conversations_history(channel=dm["id"], limit=5)
    for msg in history["messages"]:
        sender = "me" if msg.get("user") == me["user_id"] else other
        print(f"[DM with {other}] {sender}: {msg.get('text','')[:200]}")

# ── Rate limit handling ───────────────────────────────────────────────────────
# Slack rate limits: Tier 1 = 1/min, Tier 2 = 20/min, Tier 3 = 50/min.
# If you hit ratelimited, sleep and retry — do NOT retry immediately.
import time
for attempt in range(3):
    try:
        result = slack.conversations_list(types="im", limit=50)
        break
    except Exception as e:
        if "ratelimited" in str(e).lower():
            time.sleep(30)  # Slack resets most limits within 60s
        else:
            raise
```

Tips:
- **For "check my messages" or "latest DMs"**: use `conversations_list(types="im")` then `conversations_history` per DM.
- **For "find messages about X"**: use `search_messages(query="X", sort="timestamp", sort_dir="desc", count=20)` — fastest and most powerful.
- **NEVER call `conversations_list()` without `types=`** — it lists all channels and rate limits immediately.
- Paginate with `cursor=result["response_metadata"]["next_cursor"]` for large results.
- User token scopes include `search:read`, `channels:history`, `im:history`,
  `groups:history`, `channels:read`, `users:read`, `users:read.email`.
- If rate limited: sleep 30s and retry. The Slack window is 60s for most methods.
"""

# ── Google Workspace skill — injected when user has GOOGLE_OAUTH_CREDENTIALS ──
#
# GOOGLE_OAUTH_CREDENTIALS is a JSON blob with client_id, client_secret,
# refresh_token, token_uri, scopes.
# Scopes: gmail.readonly, calendar.readonly, drive.readonly.
#
# The sandbox uses plain `requests` — no vendor auth libraries needed.
#
_WORKSPACE_GOOGLE_SKILL = """

---

## Google Workspace Queries

The user has connected their Google account. `GOOGLE_OAUTH_CREDENTIALS` is
available in every `run_python` call as a JSON string.

**IMPORTANT**: Do NOT import or pip-install cloud-vendor SDKs,
or `google-api-python-client`. They are NOT available in the sandbox.
Use `requests` directly — it is always available.

```python
import os, json, requests, base64
from datetime import datetime, timezone

# ── Step 1: always refresh the access token first ─────────────────────────────
creds = json.loads(os.environ["GOOGLE_OAUTH_CREDENTIALS"])
tok = requests.post(creds["token_uri"], data={
    "client_id":     creds["client_id"],
    "client_secret": creds["client_secret"],
    "refresh_token": creds["refresh_token"],
    "grant_type":    "refresh_token",
}).json()
access_token = tok["access_token"]
H = {"Authorization": f"Bearer {access_token}"}   # reuse H in every request below

# ── Gmail: list inbox messages ────────────────────────────────────────────────
r = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                 headers=H, params={"maxResults": 10, "labelIds": "INBOX"})
msgs = r.json().get("messages", [])
for m in msgs:
    detail = requests.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
        headers=H, params={"format": "metadata", "metadataHeaders": ["Subject","From","Date"]}
    ).json()
    hdrs = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
    print(hdrs.get("Date",""), hdrs.get("From",""), hdrs.get("Subject",""))

# ── Gmail: search messages ────────────────────────────────────────────────────
r = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/messages",
                 headers=H, params={"q": "is:unread", "maxResults": 20})
for m in r.json().get("messages", []):
    detail = requests.get(
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
        headers=H, params={"format": "full"}
    ).json()
    # body is base64url encoded in payload parts
    for part in detail["payload"].get("parts", [detail["payload"]]):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            body = base64.urlsafe_b64decode(part["body"]["data"] + "==").decode("utf-8", errors="ignore")
            print(body[:500])

# ── Gmail: list all labels ────────────────────────────────────────────────────
labels = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/labels", headers=H).json()
for lbl in labels.get("labels", []):
    print(lbl["id"], lbl["name"])

# ── Calendar: list upcoming events ────────────────────────────────────────────
now_iso = datetime.now(timezone.utc).isoformat()
r = requests.get(
    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    headers=H,
    params={"timeMin": now_iso, "maxResults": 20, "singleEvents": True, "orderBy": "startTime"}
)
for e in r.json().get("items", []):
    start = e["start"].get("dateTime", e["start"].get("date"))
    print(start, e.get("summary","(no title)"), e.get("location",""))

# ── Calendar: list all calendars ─────────────────────────────────────────────
cal_list = requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", headers=H).json()
for c in cal_list.get("items", []):
    print(c["id"], c["summary"], c.get("accessRole",""))

# ── Calendar: events between two dates ────────────────────────────────────────
r = requests.get(
    "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    headers=H,
    params={
        "timeMin": "2026-06-27T00:00:00Z",
        "timeMax": "2026-07-04T00:00:00Z",
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 50,
    }
)
for e in r.json().get("items", []):
    start = e["start"].get("dateTime", e["start"].get("date"))
    print(start, e.get("summary","(no title)"))

# ── Drive: list files ─────────────────────────────────────────────────────────
r = requests.get("https://www.googleapis.com/drive/v3/files",
                 headers=H, params={"pageSize": 20, "fields": "files(id,name,mimeType,modifiedTime)"})
for f in r.json().get("files", []):
    print(f["name"], f["mimeType"], f.get("modifiedTime",""))

# ── Drive: search files ───────────────────────────────────────────────────────
r = requests.get("https://www.googleapis.com/drive/v3/files",
                 headers=H,
                 params={"q": "name contains 'budget'", "pageSize": 10,
                         "fields": "files(id,name,mimeType,modifiedTime)"})
for f in r.json().get("files", []):
    print(f["id"], f["name"], f["mimeType"])

# ── Drive: export a Google Doc as plain text ──────────────────────────────────
r = requests.get("https://www.googleapis.com/drive/v3/files/<FILE_ID>/export",
                 headers=H, params={"mimeType": "text/plain"})
print(r.text[:2000])
```

Tips:
- **Always refresh first**: call `token_uri` with `refresh_token` to get `access_token` before any API call.
- **Set `H = {"Authorization": f"Bearer {access_token}"}`** and reuse it for every request.
- **Gmail search**: `q` uses Gmail search syntax — `"is:unread"`, `"from:x@y.com"`, `"subject:invoice"`, `"after:2024/01/01"`.
- **Calendar**: use `"primary"` as calendarId for the user's main calendar. Use the `/calendarList` endpoint to discover other calendar IDs.
- **Drive export**: Google Docs/Sheets cannot be downloaded as binary — use `/export?mimeType=text/plain` or `text/csv`.
- **NEVER use `pip install`** inside run_python — it is blocked in the sandbox.
"""

# ── Tool: run_python (used in skill-powered workspace chats) ──────────────────


def _sandbox_python_packages() -> str:
    """What the sandbox image actually has installed, as prose for the model.

    A run has no network, so the model cannot install anything it is missing —
    which makes an inaccurate list here worse than no list: it produces code
    that cannot run. The default describes the stock python:3.12-slim image.
    Set SANDBOX_PYTHON_PACKAGES to match whatever SANDBOX_PYTHON_IMAGE points at.
    """
    configured = (os.environ.get("SANDBOX_PYTHON_PACKAGES") or "").strip()
    if configured:
        return (
            "PRE-INSTALLED PACKAGES (already available — see the note on pip below):\n"
            f"  {configured}\n"
        )
    return (
        "AVAILABLE MODULES: the Python standard library only (json, os, pathlib, "
        "base64, re, csv, io, urllib, sqlite3, …). No third-party packages are "
        "installed. Do not import requests, pandas, numpy or similar — solve it "
        "with the standard library, or say plainly that this needs a package the "
        "sandbox does not have.\n"
    )


RUN_PYTHON_TOOL_MINIMAL = {
    "name": "run_python",
    "description": (
        "Execute Python in an isolated sandbox and return stdout. "
        "Secrets are available via os.environ. Print results to stdout."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute. Print results to stdout.",
            },
            "label": {
                "type": "string",
                "description": "Short UI label for this step (under 60 characters).",
            },
        },
        "required": ["code"],
    },
}

RUN_PYTHON_TOOL = {
    "name": "run_python",
    "description": (
        "Execute Python code in an isolated sandbox and return stdout. "
        "Use this to run queries, perform computations, and generate files. "
        "ALWAYS use real code to produce real output — never invent or fabricate data.\n\n"
        + _sandbox_python_packages()
        + "\n"
        "IMPORTANT: NEVER use pip install inside this tool. Each call runs in a fresh "
        "sandbox with no network access, so an install cannot succeed and will waste "
        "the run.\n\n"
        "NETWORK: outbound access is off unless the skill requested it. Do not assume "
        "an HTTP call will connect.\n\n"
        "Secrets the skill declared are pre-loaded as environment variables.\n\n"
        "To generate a downloadable file (Word doc, Excel, CSV, etc.), write it then pipe it back:\n"
        "  import base64\n"
        "  with open('output.docx','rb') as f: data=base64.b64encode(f.read()).decode()\n"
        "  print(f'<|FILE|>output.docx:{data}<|ENDFILE|>')\n"
        "The backend intercepts the marker, saves the file, and shows a download card to the user.\n\n"
        "Print raw data to stdout. After the tool returns, format results as clean markdown "
        "(tables, headers, bullets). Never wrap your final prose response in a code block."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute. Print results to stdout.",
            },
            "label": {
                "type": "string",
                "description": (
                    "Short description of this step shown in the UI, e.g. "
                    "'Read docx skill', 'Generate Word document', 'Install packages'. "
                    "Keep under 60 characters."
                ),
            },
        },
        "required": ["code"],
    },
}

# ── Built-in server-side tools (executed by Anthropic, not by us) ─────────────
# web_search and web_fetch run on Anthropic's infrastructure; we only pass the
# tool definitions and handle any server_tool_use blocks in serialization.
WEB_TOOLS: list[dict] = [
    {"type": "web_search_20260209"},
    {"type": "web_fetch_20260209"},
]

# ── Client-side URL fetch tool ─────────────────────────────────────────────────
# This is a standard Anthropic custom tool (works with the model gateway too).
# The backend executes the HTTP fetch and returns extracted text to the model.
FETCH_URL_TOOL: dict = {
    "name": "fetch_url",
    "description": (
        "Fetch the readable text content of any public URL and return it for analysis. "
        "Use this whenever the user pastes a link, asks you to read/summarise a webpage, "
        "article, documentation page, or any public internet URL. "
        "Returns cleaned plain text (up to ~30 000 characters). "
        "Never use this for private, internal, or localhost URLs."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full public URL to fetch (must start with https:// or http://).",
            }
        },
        "required": ["url"],
    },
}

PROPOSE_SKILL_TOOL: dict = {
    "name": "propose_skill",
    "description": (
        "Draft a new Skill for the user to review. Call this once you have "
        "authored a complete SKILL.md (including YAML frontmatter if the "
        "user's skills use it) — do not call it with a half-written draft. "
        "This does NOT create the skill; it shows the user a draft card they "
        "can approve (which opens the full publish form) or discard. Never "
        "call this more than once for the same request — wait for the user's "
        "decision on the card shown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Human-readable Skill name."},
            "skill_md": {"type": "string", "description": "Full SKILL.md content, markdown."},
            "api_ref_url": {
                "type": "string",
                "description": "Optional API reference URL used while authoring this skill.",
            },
        },
        "required": ["name", "skill_md"],
    },
}

PROPOSE_AUTOMATION_TOOL: dict = {
    "name": "propose_automation",
    "description": (
        "Draft a new scheduled Automation for the user to review and approve. "
        "Gather the name, what it should do (instructions), and a cron "
        "schedule before calling this — approving actually creates the "
        "automation. Never call this more than once for the same request — "
        "wait for the user's decision on the card shown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "instructions": {
                "type": "string",
                "description": "What the automation should do each run.",
            },
            "schedule": {
                "type": "string",
                "description": "5-field cron expression, e.g. '0 9 * * 1' for every Monday 9am.",
            },
            "is_active": {
                "type": "boolean",
                "description": "Whether it should run on schedule immediately once approved. Default true.",
            },
            "skill_id": {
                "type": "string",
                "description": "Optional skill slug to run this automation with.",
            },
            "slack_channel": {
                "type": "string",
                "description": (
                    "Optional Slack channel to post results to (requires the "
                    "user's Slack account to already be connected)."
                ),
            },
        },
        "required": ["name", "instructions", "schedule"],
    },
}

PROPOSE_LAB_TOOL: dict = {
    "name": "propose_lab",
    "description": (
        "Draft a new Lab app for the user to review and approve. Only name "
        "and description are required — tech stack and build mode are "
        "AI-inferred at creation time. Approving actually creates the app. "
        "Never call this more than once for the same request — wait for the "
        "user's decision on the card shown."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {
                "type": "string",
                "description": "What the app should do — used to infer build settings.",
            },
            "skill_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional skill slugs the app should have access to.",
            },
        },
        "required": ["name", "description"],
    },
}

# Allowed MIME types for chat attachments (validated by magic bytes server-side)
ATTACHMENT_ALLOWED_MIMES: set[str] = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "application/pdf",
}
ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024  # 15 MB per file
ATTACHMENT_MAX_COUNT = 3  # max files per message

# ── Scheduler ─────────────────────────────────────────────────────────────────

from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor

from cron_worker_pool import (
    CRON_EXECUTOR,
    DEFAULT_EXECUTOR,
    cron_max_workers,
    maintenance_max_workers,
)

# Two executor pools:
#   - "cron": heavy subprocess spawns (agent scheduled tasks, automations), bounded
#     concurrency via cron_worker_pool + SCHEDULER_CRON_MAX_WORKERS
#   - "default": light interval jobs (resync, skills pull, sandbox reaper)
scheduler = BackgroundScheduler(
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    },
    executors={
        DEFAULT_EXECUTOR: APSchedulerThreadPoolExecutor(
            max_workers=maintenance_max_workers(),
        ),
        CRON_EXECUTOR: APSchedulerThreadPoolExecutor(
            max_workers=cron_max_workers() * 2,
        ),
    },
)


def _extract_final_output(raw: str) -> str:
    """Mirror of the frontend extractFinalOutput: strip intermediate code/steps,
    return only the prose after the last closing ``` fence."""
    lines = raw.split("\n")
    last_closing = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "```":
            last_closing = i
            break
    if last_closing != -1:
        remaining = "\n".join(lines[last_closing + 1:]).strip()
        if len(remaining) > 20:
            return remaining
    # Fallback: strip all fenced code blocks
    import re
    return re.sub(r"```[\w\s]*\n[\s\S]*?```", "", raw).strip()


def _post_to_slack(
    automation_owner_sub: str,
    channel: str,
    automation_name: str,
    status: str,
    output: Optional[str],
    error: Optional[str],
    duration: float,
    user_slack_token: Optional[str] = None,
) -> None:
    """Fire-and-forget Slack notification for an automation run result.

    Uses the owner's personal SLACK_USER_TOKEN fetched from the vault.
    No server-level token is ever used.
    """
    token = user_slack_token
    if not token:
        return
    try:
        from slack_sdk import WebClient  # type: ignore
        from slack_sdk.errors import SlackApiError  # type: ignore  # noqa: F401

        icon = "✅" if status == "success" else "❌"
        final = _extract_final_output(output) if output else (error or "No output.")
        summary = final[:2800]
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{icon} {automation_name}", "emoji": True},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Status:* {status}"},
                    {"type": "mrkdwn", "text": f"*Duration:* {duration:.1f}s"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{summary}```"},
            },
        ]
        WebClient(token=token).chat_postMessage(
            channel=channel,
            text=f"{icon} *{automation_name}* — {status} ({duration:.1f}s)",
            blocks=blocks,
        )
    except Exception:
        logger.exception("Failed to post automation result to Slack channel %s", channel)


AUTOMATION_CODE_PROMPT = (
    "You are a data automation agent. Write Python code to fetch data and print results.\n\n"
    "━━━ CRITICAL EXECUTION MODEL — READ THIS FIRST ━━━\n"
    "The script runs ONCE and exits. The platform scheduler calls it on the configured interval.\n"
    "NEVER write while True loops. NEVER call time.sleep(). NEVER add a scheduler.\n"
    "If the script does not finish within 90 seconds it is killed. Keep it fast.\n\n"
    "━━━ ENVIRONMENT VARIABLES ━━━\n"
    "- ALWAYS use os.environ.get('KEY', '') inside functions — NEVER at module level.\n"
    "- NEVER use os.environ['KEY'] (square brackets) — it raises KeyError if missing.\n\n"
    "━━━ CODE RULES ━━━\n"
    "- Use requests for HTTP/REST API calls. Always set timeout=15.\n"
    "- Use print() for all output — it is captured and turned into a report.\n"
    "- Handle empty/error results gracefully — print a clear message instead of crashing.\n"
    "- Wrap ALL top-level logic in a main() function and call it at the end: main()\n"
    "- Respond with ONLY ```python ... ``` code block(s) — no prose, no explanation.\n\n"
    "━━━ SINGLE-RUN PATTERN (follow this structure) ━━━\n"
    "```python\n"
    "import os, requests, json\n"
    "from datetime import datetime, timezone, timedelta\n\n"
    "def main():\n"
    "    # read credentials inside the function\n"
    "    api_key = os.environ.get('MY_API_KEY', '')\n"
    "    # fetch data\n"
    "    # print results\n\n"
    "main()\n"
    "```\n"
)

from automation_prompts import DEFAULT_AUTOMATION_REPORT_PROMPT as AUTOMATION_REPORT_PROMPT


MEMORY_SYNTHESIS_PROMPT = (
    f"You are a memory synthesis agent for a personal AI assistant used by an {ORG_NOUN}.\n"
    "Analyze recent workspace conversations and Slack activity to extract durable knowledge.\n\n"
    "Extract entries for these categories:\n"
    "- people: colleagues mentioned, their roles, contact details\n"
    "- active_work: current projects, tasks, deadlines\n"
    "- capabilities: tools, skills, integrations the user has access to\n"
    "- preferences: response format, tone, timezone, working patterns\n"
    "- organization: team structure, key processes, tools stack\n\n"
    "Return ONLY valid JSON — an array of objects, each with:\n"
    '{"key":"snake_case_id","title":"<60 chars","category":"people|active_work|capabilities|preferences|organization",'
    '"content":"1-3 factual sentences","source":"brief source","stale":false}\n\n'
    "Rules:\n"
    "- Only include entries with clear evidence in the provided context\n"
    "- Reuse existing keys when updating existing entries\n"
    "- Set stale=true for entries with no recent evidence\n"
    "- Never invent details not present in the conversations\n"
    "- Focus on durable facts, not ephemeral one-off questions\n"
    "- Output ONLY the raw JSON array starting with [ — no explanation, no code fences, no preamble"
)


def _collect_slack_context(owner_sub: str, slack_token: str, hours: int = 168) -> str:
    """Collect Slack context in two passes:
    1. search.messages(from:<user>) — user's own posts (intent signal + channel frequency)
    2. conversations.history on top-3 most-active channels — surrounding thread context
    Requires: search:read, channels:history, groups:history on the user token.

    The caller must pass the user's personal SLACK_USER_TOKEN from the vault.
    """
    import time as _time
    from collections import Counter
    try:
        from slack_sdk import WebClient  # type: ignore

        client = WebClient(token=slack_token)

        me = client.auth_test()
        user_id = me["user_id"]
        user_name = me.get("user", user_id)

        after_ts = _time.time() - hours * 3600
        after_date = __import__("datetime").datetime.utcfromtimestamp(after_ts).strftime("%Y-%m-%d")
        oldest_ts = str(after_ts)

        # ── Pass 1: user's own messages ───────────────────────────────────────
        results = client.search_messages(
            query=f"from:<@{user_id}> after:{after_date}",
            count=200,
            sort="timestamp",
            sort_dir="desc",
        )
        matches = results.get("messages", {}).get("matches", [])

        user_lines: list[str] = [f"# Messages sent by {user_name} (last {hours // 24}d)\n"]
        channel_freq: Counter = Counter()
        channel_id_map: dict[str, str] = {}  # name -> id

        for m in matches:
            ch = m.get("channel", {})
            ch_name = ch.get("name", "?")
            ch_id = ch.get("id", "")
            text = (m.get("text") or "")[:300]
            user_lines.append(f"[#{ch_name}] {text}")
            if ch_id:
                channel_freq[ch_id] += 1
                channel_id_map[ch_id] = ch_name

        # ── Pass 2: channel history for top-3 channels ────────────────────────
        top_channel_ids = [ch_id for ch_id, _ in channel_freq.most_common(3)]

        channel_sections: list[str] = []
        for ch_id in top_channel_ids:
            ch_name = channel_id_map.get(ch_id, ch_id)
            try:
                hist = client.conversations_history(
                    channel=ch_id,
                    oldest=oldest_ts,
                    limit=100,
                )
                hist_msgs = hist.get("messages", [])
                if not hist_msgs:
                    continue
                section_lines = [f"\n# #{ch_name} — recent activity (last {hours // 24}d)\n"]
                for msg in reversed(hist_msgs):  # chronological order
                    sender = msg.get("user", "?")
                    sender_label = f"<@{sender}>" if sender != user_id else user_name
                    body = (msg.get("text") or "")[:300]
                    section_lines.append(f"{sender_label}: {body}")
                channel_sections.append("\n".join(section_lines))
            except Exception as _ch_err:
                logger.warning("conversations.history failed for %s: %s", ch_name, _ch_err)

        parts = ["\n".join(user_lines)] + channel_sections
        return "\n\n".join(parts)

    except Exception as _e:
        logger.warning("Slack context collection failed: %s", _e)
        return "(Slack unavailable)"


def _build_memory_context(owner_sub: str) -> str:
    """Build a memory context string from approved entries for chat injection (one tenant)."""
    db = SessionLocal()
    try:
        entries = (
            db.query(Memory)
            .filter(Memory.owner_sub == owner_sub, Memory.status == "approved")
            .order_by(Memory.category, Memory.updated_at.desc())
            .all()
        )
        if not entries:
            return ""
        by_cat: dict[str, list] = {}
        for e in entries:
            by_cat.setdefault(e.category, []).append(e)
        cat_labels = {
            "people": "People",
            "active_work": "Active Work",
            "capabilities": "Capabilities",
            "preferences": "Preferences",
            "organization": "Organization",
        }
        parts = ["## User Context (auto-updated daily)\n"]
        for cat, label in cat_labels.items():
            if cat not in by_cat:
                continue
            parts.append(f"### {label}")
            for e in by_cat[cat]:
                parts.append(f"**{e.title}:** {e.content}")
            parts.append("")
        return "\n".join(parts)
    finally:
        db.close()


def _mine_memory_job(for_owner_sub: Optional[str] = None) -> None:
    """Mine recent chats and synthesize memories. One owner, or all distinct chat owners."""
    if for_owner_sub:
        _mine_memory_for_one_owner(for_owner_sub)
        return

    db = SessionLocal()
    try:
        rows = db.query(Chat.owner_sub).distinct().all()
        owners = sorted({r[0] for r in rows if r[0]})
    finally:
        db.close()

    if not owners:
        logger.info("Memory mining (global) skipped — no chats")
        return

    for owner in owners:
        try:
            _mine_memory_for_one_owner(owner)
        except Exception:
            logger.exception("Memory mining failed for tenant %s...", owner[:24])


def _mine_memory_for_one_owner(owner_sub: str) -> None:
    """Mine recent chats for a single JWT subject.

    Slack context is included when MEMORY_SYNTHESIS_INCLUDE_SLACK=1 and a token can be
    resolved for this tenant.  Resolution order:
      1. User's own SLACK_USER_TOKEN from the vault (no allowlist needed).
      2. Server-level SLACK_TOKEN, only when the tenant is in the allowlist (legacy).
    """
    import json as _json
    import re as _re
    from datetime import timedelta, timezone

    logger.info("Memory mining started for tenant %s...", owner_sub[:24])

    log_db = SessionLocal()
    try:
        sync_log = MemorySyncLog(status="running", owner_sub=owner_sub)
        log_db.add(sync_log)
        log_db.commit()
        log_db.refresh(sync_log)
        log_id = sync_log.id
    finally:
        log_db.close()

    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
        db = SessionLocal()
        try:
            msgs = (
                db.query(Message)
                .join(Chat, Message.chat_id == Chat.id)
                .filter(Chat.owner_sub == owner_sub, Message.created_at >= cutoff)
                .order_by(Message.created_at)
                .limit(400)
                .all()
            )
            chat_lines = [f"[{m.role}]: {m.content[:400]}" for m in msgs]
            chat_context = "\n".join(chat_lines) if chat_lines else "(no recent conversations)"

            existing = (
                db.query(Memory)
                .filter(Memory.owner_sub == owner_sub, Memory.status == "approved")
                .all()
            )
            existing_summary = "\n".join(
                f"key={e.key} category={e.category}: {e.title} — {e.content[:120]}"
                for e in existing
            ) or "(none yet)"
        finally:
            db.close()

        # ── Resolve Slack token for this tenant ───────────────────────────────
        # Only the user's own SLACK_USER_TOKEN from the vault is used.
        # No server-level shared token is ever accepted.
        _user_vault = _fetch_user_secrets(owner_sub)
        resolved_slack_token: Optional[str] = _user_vault.get("SLACK_USER_TOKEN")

        slack_context = ""
        if resolved_slack_token:
            logger.info("Memory mining: collecting Slack context for tenant %s...", owner_sub[:24])
            slack_context = _collect_slack_context(owner_sub, slack_token=resolved_slack_token)
        else:
            logger.info("Memory mining: no SLACK_USER_TOKEN in vault for tenant %s — skipping Slack", owner_sub[:24])

        client = anthropic.Anthropic(api_key=MODEL_GATEWAY_API_KEY, base_url=MODEL_GATEWAY_URL)
        slack_section = (
            f"SLACK ACTIVITY (last 7d):\n{slack_context[:4000]}"
            if slack_context
            else "SLACK ACTIVITY:\n(no SLACK_USER_TOKEN configured — connect Slack in the Integrations page)"
        )
        user_msg = (
            f"EXISTING APPROVED MEMORY:\n{existing_summary}\n\n"
            f"RECENT CONVERSATIONS (last 48h):\n{chat_context[:4000]}\n\n"
            f"{slack_section}\n\n"
            "Return a JSON array of memory entries."
        )
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=MEMORY_SYNTHESIS_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        from token_tracking import record_token_usage as _record_usage
        _record_usage("memory", MODEL, response.usage)
        raw = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

        # Strip markdown code fences (```json ... ``` or ``` ... ```)
        raw_stripped = _re.sub(r"```(?:json)?\s*", "", raw).strip()

        match = _re.search(r"\[[\s\S]*\]", raw_stripped)
        if not match:
            logger.error("Memory synthesis raw response (no JSON found): %s", raw[:500])
            raise ValueError("No JSON array in synthesis response")

        entries_parsed = _json.loads(match.group())
        now = datetime.now(timezone.utc)
        created = updated = pruned = 0

        db = SessionLocal()
        try:
            for e in entries_parsed:
                if not isinstance(e, dict):
                    continue
                key = (e.get("key") or "").strip()
                if not key:
                    continue
                existing_row = (
                    db.query(Memory)
                    .filter(Memory.owner_sub == owner_sub, Memory.key == key)
                    .first()
                )
                if e.get("stale"):
                    if existing_row and existing_row.status == "pending":
                        db.delete(existing_row)
                        pruned += 1
                    continue
                if existing_row:
                    existing_row.title = e.get("title", existing_row.title)
                    existing_row.content = e.get("content", existing_row.content)
                    existing_row.source = e.get("source", existing_row.source)
                    existing_row.category = e.get("category", existing_row.category)
                    existing_row.last_confirmed_at = now
                    existing_row.updated_at = now
                    updated += 1
                else:
                    db.add(
                        Memory(
                            owner_sub=owner_sub,
                            key=key,
                            title=e.get("title", key),
                            category=e.get("category", "active_work"),
                            content=e.get("content", ""),
                            source=e.get("source", "Auto-synthesized"),
                            status="pending",
                            last_confirmed_at=now,
                        )
                    )
                    created += 1

            stale_cutoff = now - timedelta(days=30)
            stale_rows = (
                db.query(Memory)
                .filter(
                    Memory.owner_sub == owner_sub,
                    Memory.last_confirmed_at < stale_cutoff,
                    Memory.status == "pending",
                )
                .all()
            )
            for s in stale_rows:
                db.delete(s)
                pruned += 1

            db.commit()
            logger.info(
                "Memory sync owner=%s: created=%d updated=%d pruned=%d",
                owner_sub[:20],
                created,
                updated,
                pruned,
            )
        finally:
            db.close()

        log_db = SessionLocal()
        try:
            log = log_db.get(MemorySyncLog, log_id)
            if log:
                log.status = "success"
                log.finished_at = datetime.now(timezone.utc)
                log.entries_created = created
                log.entries_updated = updated
                log.entries_pruned = pruned
                log_db.commit()
        finally:
            log_db.close()

    except Exception as exc:
        logger.exception("Memory mining job failed for %s", owner_sub[:24])
        log_db = SessionLocal()
        try:
            log = log_db.get(MemorySyncLog, log_id)
            if log:
                log.status = "error"
                log.finished_at = datetime.now(timezone.utc)
                log.error = str(exc)
                log_db.commit()
        finally:
            log_db.close()


def _load_skill_secrets(slug: str) -> dict[str, str]:
    """Resolve all vault references for a published skill into a key→value dict.

    Returns an empty dict if the skill has no secrets or if the vault is unavailable.
    Individual secret read failures are logged and skipped so a single bad ref does
    not prevent the skill from functioning.
    """
    from skill_submission_models import SkillSecret
    from secret_vault import get_vault, SecretVaultError

    db = SessionLocal()
    try:
        rows = db.query(SkillSecret).filter(SkillSecret.slug == slug).all()
        if not rows:
            return {}
        try:
            vault = get_vault()
        except Exception:
            logger.warning("_load_skill_secrets: vault unavailable for skill '%s'", slug)
            return {}
        result: dict[str, str] = {}
        for row in rows:
            try:
                result[row.env_key] = vault.read(row.secret_ref)
            except SecretVaultError:
                logger.warning(
                    "_load_skill_secrets: could not read secret '%s' for slug '%s'",
                    row.env_key, slug,
                )
        return result
    finally:
        db.close()


def _fetch_user_secrets(owner_sub: str) -> dict[str, str]:
    """Resolve all personal vault references for a user into a key→value dict.

    Fetched once per request before the tool-call loop so every run_python turn
    within the same session shares the same snapshot without re-hitting the vault.

    Returns an empty dict (never raises) so a missing vault or individual bad ref
    does not break the session — the user's code will simply not find that var.
    """
    from secret_vault import get_vault, SecretVaultError, SecretVaultMisconfiguredError

    db = SessionLocal()
    try:
        rows = (
            db.query(UserSecret)
            .filter(UserSecret.owner_sub == owner_sub)
            .all()
        )
        if not rows:
            return {}
    finally:
        db.close()

    try:
        vault = get_vault()
    except SecretVaultMisconfiguredError:
        logger.warning("_fetch_user_secrets: vault unavailable for owner (sub omitted)")
        return {}

    result: dict[str, str] = {}
    for row in rows:
        try:
            result[row.slug] = vault.read(row.secret_ref)
        except SecretVaultError:
            logger.warning(
                "_fetch_user_secrets: could not read secret slug='%s' (ref omitted)",
                row.slug,
            )
    return result


def _load_skill(slug: str) -> str:
    path = SKILLS_DIR / slug / "skill.md"
    if not path.exists():
        return DEFAULT_SYSTEM
    content = path.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            content = content[end + 4:].strip()

    # Append all reference files so the agent has full schema context
    refs_dir = SKILLS_DIR / slug / "references"
    if refs_dir.is_dir():
        for ref in sorted(refs_dir.glob("*.md")):
            # OX Agent: Path Traversal prevented by Path.resolve() + relative_to()
            safe = ref.resolve()
            try:
                safe.relative_to(refs_dir.resolve())
            except ValueError:
                continue
            content += f"\n\n---\n\n## {ref.stem}\n\n{safe.read_text(encoding='utf-8')}"

    return content


def _execute_code_sync(
    code: str,
    owner_sub: str,
    env_extras: dict[str, str] | None = None,
) -> str:
    """Run Python code in a the sandbox provider (isolated pod).

    Delegates to executor.execute_in_sandbox_sync which handles the full
    SandboxClaim lifecycle: create → wait-ready → run → terminate.
    PDF registration still happens here so the DB knows about new files.
    """
    work_dir = _user_workspace_dir(owner_sub)
    workspace_str = str(work_dir)
    patched = _rewrite_python_workspace_paths(code, workspace_str)
    before_pdf = {p.name for p in work_dir.glob("*.pdf")}
    output = execute_in_sandbox_sync(patched, workspace_str, env_extras or None)
    _register_new_pdfs_for_owner(work_dir, owner_sub, before_pdf)
    return output


def _run_automation_job(automation_id: str, manual: bool = False) -> None:
    """Two-step automation runner: (1) generate code, (2) execute, (3) report.

    `manual=True` bypasses the is_active check — a manually-triggered run
    (Run Now, or testing a draft automation before it's approved/activated)
    should still execute. Scheduled/cron-triggered calls never pass `manual`,
    and inactive automations are never registered with the cron scheduler in
    the first place (see _schedule_automation), so this only relaxes the
    manual-trigger path.
    """
    import re

    db = SessionLocal()
    try:
        automation = db.get(Automation, automation_id)
        if not automation or (not automation.is_active and not manual):
            return
        automation_owner_sub = automation.owner_sub
        skill_id = automation.skill_id
        instructions = automation.instructions
        automation_name = automation.name
        slack_channel = automation.slack_channel

        from automation_skill_access import user_can_access_skill

        if skill_id and not user_can_access_skill(automation_owner_sub, None, skill_id):
            denied_run = AutomationRun(
                automation_id=automation_id,
                status="error",
                error="Skill access denied",
                finished_at=datetime.now(timezone.utc),
                duration_secs=0.0,
            )
            db.add(denied_run)
            db.commit()
            logger.warning(
                "Automation %s blocked: owner lacks access to skill %s",
                automation_id,
                skill_id,
            )
            return

        run = AutomationRun(automation_id=automation_id, status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
        run_id = run.id
    finally:
        db.close()

    started = datetime.now(timezone.utc)
    try:
        skill_context = _load_skill(skill_id) if skill_id else DEFAULT_SYSTEM
        skill_secrets = _load_skill_secrets(skill_id) if skill_id else {}
        user_secrets = _fetch_user_secrets(automation_owner_sub)
        # Skill secrets (admin-controlled) take precedence over user secrets
        merged_secrets: dict[str, str] = {**user_secrets, **skill_secrets}
        client = anthropic.Anthropic(
            api_key=MODEL_GATEWAY_API_KEY, base_url=MODEL_GATEWAY_URL
        )

        # ── Step 1: Ask Claude to write Python code ──────────────────────────
        code_system = AUTOMATION_CODE_PROMPT + "\n\n" + skill_context
        available_keys = sorted(merged_secrets)
        if available_keys:
            keys_block = "\n".join(f"- `{k}`" for k in available_keys)
            code_system += (
                "\n\n---\n\n"
                "## Available Secrets\n"
                "The following secrets are pre-loaded as environment variables in the "
                "execution environment. Access them with `os.environ['KEY']` or "
                "`os.environ.get('KEY')`:\n"
                + keys_block
            )
        code_response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=code_system,
            messages=[{"role": "user", "content": instructions}],
        )
        from token_tracking import record_token_usage as _record_usage
        _record_usage("automation", MODEL, code_response.usage)
        code_text = "".join(
            b.text for b in code_response.content
            if getattr(b, "type", None) == "text"
        )

        code_blocks = re.findall(r"```python\n([\s\S]*?)```", code_text)

        if not code_blocks:
            # No executable code — use Claude's prose response directly
            output = code_text
        else:
            # ── Step 2: Execute each code block ─────────────────────────────
            exec_results: list[str] = []
            for i, block in enumerate(code_blocks):
                logger.info("Automation %s executing code block %d/%d", automation_id, i + 1, len(code_blocks))
                result = _execute_code_sync(block, automation_owner_sub, merged_secrets or None)
                exec_results.append(f"### Code block {i + 1} output\n```\n{result}\n```")

            combined_results = "\n\n".join(exec_results)

            # ── Step 3: Ask Claude to write a report from the real results ───
            report_prompt = (
                f"Task: {instructions}\n\n"
                f"Query results:\n\n{combined_results}\n\n"
                "Write a concise markdown report summarising these results."
            )
            report_response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=AUTOMATION_REPORT_PROMPT,
                messages=[{"role": "user", "content": report_prompt}],
            )
            _record_usage("automation", MODEL, report_response.usage)
            output = "".join(
                b.text for b in report_response.content
                if getattr(b, "type", None) == "text"
            )

        duration = (datetime.now(timezone.utc) - started).total_seconds()

        db = SessionLocal()
        try:
            run = db.get(AutomationRun, run_id)
            if run:
                run.output = output
                run.status = "success"
                run.finished_at = datetime.now(timezone.utc)
                run.duration_secs = duration
                auto = db.get(Automation, automation_id)
                if auto:
                    auto.updated_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

        if slack_channel:
            _post_to_slack(
                automation_owner_sub, slack_channel, automation_name,
                "success", output, None, duration,
                user_slack_token=user_secrets.get("SLACK_USER_TOKEN"),
            )

    except Exception as e:
        logger.exception("Automation %s run %s failed", automation_id, run_id)
        duration = (datetime.now(timezone.utc) - started).total_seconds()
        db = SessionLocal()
        try:
            run = db.get(AutomationRun, run_id)
            if run:
                run.error = str(e)
                run.status = "error"
                run.finished_at = datetime.now(timezone.utc)
                run.duration_secs = duration
                db.commit()
        finally:
            db.close()

        if slack_channel:
            _post_to_slack(
                automation_owner_sub, slack_channel, automation_name,
                "error", None, str(e), duration,
                user_slack_token=user_secrets.get("SLACK_USER_TOKEN"),
            )


def _schedule_automation(automation: Automation) -> None:
    from cron_scheduler import cron_scheduler_enabled

    if not cron_scheduler_enabled():
        return
    parts = automation.schedule.strip().split()
    if len(parts) != 5:
        logger.warning("Invalid cron '%s' for automation %s", automation.schedule, automation.id)
        return
    try:
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )
        scheduler.add_job(
            _run_automation_job,
            trigger=trigger,
            id=automation.id,
            replace_existing=True,
            args=[automation.id],
            executor=CRON_EXECUTOR,
        )
    except Exception:
        logger.exception("Failed to schedule automation %s", automation.id)


def _unschedule_automation(automation_id: str) -> None:
    try:
        scheduler.remove_job(automation_id)
    except Exception:
        pass


def sync_automations_from_db() -> int:
    """Reload automation cron jobs from DB — schedule active, remove inactive."""
    from cron_scheduler import cron_scheduler_enabled

    if not cron_scheduler_enabled():
        return 0
    db = SessionLocal()
    try:
        all_autos = db.query(Automation).all()
        scheduled = 0
        for auto in all_autos:
            if auto.is_active:
                _schedule_automation(auto)
                scheduled += 1
            else:
                _unschedule_automation(auto.id)
        logger.info(
            "Synced automations from DB: %d active scheduled, %d inactive unscheduled",
            scheduled,
            len(all_autos) - scheduled,
        )
        return scheduled
    finally:
        db.close()


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    assert_mock_auth_not_in_production()
    if ENABLE_MOCK_AUTH:
        project = os.environ.get("PROJECT_ID", "").strip()
        if project and project not in ("", "local", "test"):
            raise RuntimeError(
                f"ENABLE_MOCK_AUTH must not be set when PROJECT_ID={project!r}. "
                "Remove ENABLE_MOCK_AUTH from the production environment."
            )
        logger.warning(
            "ENABLE_MOCK_AUTH is enabled — JWT checks disabled; use mock users only locally."
        )
    elif not ((OIDC_ISSUER or TEAM_DOMAIN) and POLICY_AUD):
        logger.warning(
            "OIDC issuer and/or POLICY_AUD unset — API auth will reject requests until configured."
        )

    from datetime import timedelta, timezone

    init_db()
    from platform_setup_store import load_platform_env_file

    load_platform_env_file()
    _reload_model_gateway_from_env()
    from skill_submission_models import init_skill_submission_tables
    init_skill_submission_tables()
    from skill_system_links_model import init_skill_system_links_table
    init_skill_system_links_table()
    from artifact_models import init_artifact_tables

    init_artifact_tables()

    from agent_channel_configs import ensure_platform_agent
    ensure_platform_agent()

    from skills_store import pull_from_store
    pull_from_store()
    prune_expired_sessions()
    prune_expired_user_oauth_states()
    prune_expired_google_oauth_states()

    from cron_scheduler import cron_scheduler_enabled

    db = SessionLocal()
    try:
        has_memory = False
        if cron_scheduler_enabled():
            sync_automations_from_db()
            has_memory = db.query(Memory).count() > 0

            from agent_scheduled_tasks import load_and_schedule_agent_tasks
            load_and_schedule_agent_tasks()
    finally:
        db.close()

    # Sync skills manifest from the blob store every 60 s so all pods stay in sync
    def _reload_skills_from_gcs():
        try:
            from skills_store import pull_from_store
            pull_from_store()
        except Exception:
            logger.exception("skills the blob store reload failed")

    scheduler.add_job(
        _reload_skills_from_gcs,
        trigger="interval",
        seconds=60,
        id="skills-store-sync",
        replace_existing=True,
        misfire_grace_time=30,
    )

    from tool_code_retention import purge_expired_tool_code_samples

    scheduler.add_job(
        purge_expired_tool_code_samples,
        trigger="interval",
        hours=24,
        id="tool-code-samples-purge",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Schedule memory mining every 24h; run immediately if DB is empty (scheduler pod only)
    if cron_scheduler_enabled():
        scheduler.add_job(
            _mine_memory_job,
            trigger="interval",
            hours=24,
            id="memory-mining",
            replace_existing=True,
            misfire_grace_time=None,
        )
        if not has_memory:
            import threading
            t = threading.Timer(10, _mine_memory_job)
            t.daemon = True
            t.start()

    # Reap orphaned SandboxClaims (crashed pods, failed terminate, cancelled tasks)
    from sandbox_reaper import reap_stale_sandbox_claims_sync

    try:
        startup_max_age = int(os.environ.get("SANDBOX_CLAIM_STARTUP_MAX_AGE_S", "900"))
        n = reap_stale_sandbox_claims_sync(max_age_seconds=startup_max_age)
        if n:
            logger.info("lifespan: reaped %d stale SandboxClaim(s) on startup", n)
    except Exception:
        logger.warning("lifespan: stale SandboxClaim cleanup failed", exc_info=True)

    def _reap_stale_sandbox_claims_job() -> None:
        try:
            n = reap_stale_sandbox_claims_sync()
            if n:
                logger.info("sandbox_reaper: deleted %d stale SandboxClaim(s)", n)
        except Exception:
            logger.exception("sandbox_reaper periodic job failed")

    scheduler.add_job(
        _reap_stale_sandbox_claims_job,
        trigger="interval",
        seconds=int(os.environ.get("SANDBOX_CLAIM_REAP_INTERVAL_S", "300")),
        id="sandbox-claim-reaper",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Start Redis pub/sub subscriber for instant cross-pod ACL invalidation
    _subscriber_task = None
    _redis_host = os.environ.get("REDIS_HOST", "").strip()
    if _redis_host:
        from skills_invalidate import run_subscriber
        _subscriber_task = asyncio.create_task(run_subscriber())
    else:
        logger.warning("REDIS_HOST not set — cross-pod skill invalidation disabled")

    # Start background infrastructure metrics collector (persists to Redis every 30 s)
    from api_infra import start_background_collector
    start_background_collector()

    scheduler.start()
    yield
    scheduler.shutdown()
    if _subscriber_task:
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass


from app_factory import create_app  # noqa: E402

app = create_app(lifespan=lifespan)


# Names main.py itself uses from router modules (scheduler jobs, table setup).
# The routers are included by app_factory.create_app().
from api_slack_oauth import prune_expired_sessions  # noqa: E402
from api_slack_user_oauth import prune_expired_user_oauth_states  # noqa: E402
from api_google_user_oauth import prune_expired_google_oauth_states  # noqa: E402
from apps_models import init_app_tables  # noqa: E402


# Ensure Lab apps table exists
from database import engine as _db_engine  # noqa: E402
init_app_tables(_db_engine)

# Ensure Slack app registry table exists
from slack_app_registry import SlackAppRecord as _SlackAppRecord  # noqa: E402,F401
from database import Base as _Base  # noqa: E402
_Base.metadata.create_all(bind=_db_engine, tables=[_SlackAppRecord.__table__])

# Ensure Slack app agent grants table exists
from access_models import AgentSlackAppGrant as _AgentSlackAppGrant  # noqa: E402,F401
from access_models import SlackAppRegistrationClaim as _SlackAppRegistrationClaim  # noqa: E402,F401
_AgentSlackAppGrant.__table__.create(_db_engine, checkfirst=True)
_SlackAppRegistrationClaim.__table__.create(_db_engine, checkfirst=True)

from auth_middleware import deps_owner_sub, get_current_user, identity_gate  # noqa: E402,F401


# ── Helpers ───────────────────────────────────────────────────────────────────


_FAKE_PYTHON_OUTPUT_PATH_RE = re.compile(
    r'(?:/app/outputs?|/tmp/outputs?|/app/generated|/outputs?|~/outputs?)'
    r'(?=/|["\']|$)'
)

# ── Sandbox execution (the sandbox provider) ────────────────────────────────────
from executor import execute_in_sandbox, execute_in_sandbox_sync
from providers import get_blob_store


def _rewrite_python_workspace_paths(code: str, workspace_str: str) -> str:
    return _FAKE_PYTHON_OUTPUT_PATH_RE.sub(workspace_str, code)


def _register_new_pdfs_for_owner(workspace: Path, owner_sub: str, before_names: set[str]) -> None:
    """Persist any new *.pdf files in workspace as StoredPdf rows for this tenant."""
    after_names = {p.name for p in workspace.glob("*.pdf")}
    new_files = sorted(after_names - before_names)
    if not new_files:
        return
    db = SessionLocal()
    try:
        for name in new_files:
            exists = db.query(StoredPdf).filter(StoredPdf.stored_name == name).first()
            if not exists:
                db.add(StoredPdf(stored_name=name, owner_sub=owner_sub))
        db.commit()
    finally:
        db.close()


_GENERATED_FILE_MIMES: dict[str, str] = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf":  "application/pdf",
    ".csv":  "text/csv",
    ".txt":  "text/plain",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _register_generated_file(filename: str, owner_sub: str) -> None:
    """Register a generated file (any type) so /api/files/ can serve it."""
    db = SessionLocal()
    try:
        exists = db.query(StoredPdf).filter(StoredPdf.stored_name == filename).first()
        if not exists:
            db.add(StoredPdf(stored_name=filename, owner_sub=owner_sub))
            db.commit()
    finally:
        db.close()

def _sse(payload: dict) -> str:
    import json
    return f"data: {json.dumps(payload)}\n\n"


# ── Sync DB helpers called via run_in_executor from async endpoints ───────────
# These run in FastAPI's default thread pool so they never block the event loop.

def _chat_load_history(chat_id: str, owner_sub: str) -> tuple[list, bool]:
    """Load chat history. Returns (history, is_first)."""
    db = SessionLocal()
    try:
        chat_obj = get_tenant_or_404(db, Chat, chat_id, owner_sub, "Chat not found")
        history = [{"role": m.role, "content": m.content} for m in chat_obj.messages]
        return history, len(history) == 0
    finally:
        db.close()


def _chat_save_user_msg(
    chat_id: str,
    owner_sub: str,
    message: str,
    is_first: bool,
    effective_skill_id: "Optional[str]",
    *,
    clear_active_skill: bool = False,
) -> None:
    """Persist the user message and update the chat title / timestamp."""
    db = SessionLocal()
    try:
        chat_obj = get_tenant_or_404(db, Chat, chat_id, owner_sub, "Chat not found")
        db.add(Message(chat_id=chat_id, role="user", content=message))
        if is_first:
            chat_obj.title = message[:60] + ("…" if len(message) > 60 else "")
        chat_obj.updated_at = datetime.now(timezone.utc)
        if clear_active_skill:
            chat_obj.active_skill_id = None
        elif effective_skill_id is not None:
            chat_obj.active_skill_id = effective_skill_id
        db.commit()
    finally:
        db.close()


def _save_assistant_msg(chat_id: str, content: str) -> None:
    """Persist the assistant message after the stream completes."""
    db = SessionLocal()
    try:
        db.add(Message(chat_id=chat_id, role="assistant", content=content))
        db.commit()
    except Exception:
        logger.exception("Failed to persist assistant message")
    finally:
        db.close()


def _register_pdf_row(stored_name: str, owner_sub: str) -> None:
    """Register an uploaded PDF in the DB."""
    db = SessionLocal()
    try:
        db.add(StoredPdf(stored_name=stored_name, owner_sub=owner_sub))
        db.commit()
    finally:
        db.close()


def _check_file_ownership(name_only: str, owner_sub: str) -> bool:
    """Return True if the file exists and belongs to owner_sub."""
    db = SessionLocal()
    try:
        row = (
            db.query(StoredPdf)
            .filter(StoredPdf.stored_name == name_only, StoredPdf.owner_sub == owner_sub)
            .first()
        )
        return row is not None
    finally:
        db.close()


def _automation_to_dict(a: Automation) -> dict:
    runs_sorted = sorted(a.runs, key=lambda r: r.started_at, reverse=True)
    last = runs_sorted[0] if runs_sorted else None
    return {
        "id": a.id,
        "name": a.name,
        "instructions": a.instructions,
        "skill_id": a.skill_id,
        "schedule": a.schedule,
        "is_active": a.is_active,
        "slack_channel": a.slack_channel,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
        "run_count": len(a.runs),
        "last_run": {
            "id": last.id,
            "started_at": last.started_at.isoformat(),
            "status": last.status,
            "duration_secs": last.duration_secs,
        } if last else None,
    }


def _run_to_dict(r: AutomationRun) -> dict:
    return {
        "id": r.id,
        "automation_id": r.automation_id,
        "started_at": r.started_at.isoformat(),
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status,
        "output": r.output,
        "error": r.error,
        "duration_secs": r.duration_secs,
    }


# ── Request models ────────────────────────────────────────────────────────────

class ChatCreate(BaseModel):
    title: str = "New Chat"
    active_skill_id: Optional[str] = None


class ChatUpdate(BaseModel):
    title: Optional[str] = None
    active_skill_id: Optional[str] = None


class AttachmentRef(BaseModel):
    id: str       # stored object name returned by /api/chat/upload
    mime: str     # MIME type validated at upload time


class ChatRequest(BaseModel):
    chat_id: str
    message: str
    skill_id: Optional[str] = None
    active_skill_hint: Optional[str] = None  # prior auto-selected skill for routing context
    no_skill: bool = False  # user chose general assistant (disambiguation or explicit)
    thinking: bool = False          # enable adaptive thinking
    attachments: list[AttachmentRef] = []  # pre-uploaded file references
    model: Optional[str] = None    # override server default; validated against model catalogue
    create_kind: Optional[Literal["skill", "automation", "lab"]] = None  # composer "Creating: X" chip


class ChatCancelRequest(BaseModel):
    chat_id: str
    mode: str = "stop"  # "stop" | "answer_now"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("stop", "answer_now"):
            raise ValueError("mode must be 'stop' or 'answer_now'")
        return v


class AutomationCreate(BaseModel):
    name: str
    instructions: str
    skill_id: Optional[str] = None
    schedule: str
    is_active: bool = True
    slack_channel: Optional[str] = None


class AutomationUpdate(BaseModel):
    name: Optional[str] = None
    instructions: Optional[str] = None
    skill_id: Optional[str] = None
    schedule: Optional[str] = None
    is_active: Optional[bool] = None
    slack_channel: Optional[str] = None


# ── Python code execution (disabled) ─────────────────────────────────────────
# Direct user-triggered execution was removed; code runs only via the agent
# run_python tool inside the workspace chat SSE loop and Slack dispatch paths.

@app.post("/api/run-python")
async def run_python_disabled(owner_sub: str = Depends(deps_owner_sub)):
    raise HTTPException(
        status_code=410,
        detail="This endpoint has been disabled. Code runs only via agent tool calls in the sandbox.",
    )



@app.get("/api/health")
async def health(request: Request):
    # Read through the module: the setup wizard reloads these at runtime.
    import cf_access

    auth_mode = (
        "mock"
        if cf_access.ENABLE_MOCK_AUTH
        else ("jwt" if cf_access.OIDC_ISSUER and cf_access.POLICY_AUD else "unconfigured")
    )
    body: dict = {
        "status": "ok",
        "model": MODEL,
        "base_url": MODEL_GATEWAY_URL,
        "auth_mode": auth_mode,
    }
    # Temporary ops: CF_ACCESS_DEBUG=1 shows whether the app JWT header reaches uvicorn (lengths only).
    if os.environ.get("CF_ACCESS_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        jwt_h = (request.headers.get("cf-access-jwt-assertion") or "").strip()
        bearer = (request.headers.get("authorization") or "").strip()
        body["cf_access_probe"] = {
            "cf_access_jwt_assertion_chars": len(jwt_h),
            "authorization_header_chars": len(bearer),
        }
    return body


@app.get("/api/models")
async def list_models(auth: AuthContext = Depends(get_current_user)):
    """Return Workspace chat models (gateway catalog ∩ platform/user allowlists)."""
    from model_preferences import model_preferences_payload
    from user_profile import get_user_profile

    catalog = await get_model_catalog(
        api_key=MODEL_GATEWAY_API_KEY or "",
        base_url_raw=_MODEL_GATEWAY_URL_RAW,
        default_model=WORKSPACE_CHAT_DEFAULT_MODEL,
    )
    profile = get_user_profile(auth.sub)
    prefs = model_preferences_payload(catalog, profile=profile)
    return prefs["picker"]


@app.get("/api/me")
def api_me(auth: AuthContext = Depends(get_current_user)):
    """Verified identity and its group projection."""
    profile = get_user_profile(auth.sub)
    return {
        "sub": auth.sub,
        "email": auth.email,
        "groups": list(auth.groups),
        "role": auth.primary_role,
        "slack_user_token_integration": slack_user_token_integration_allowed(auth.sub),
        **profile_payload(profile),
    }


class TourVersionUpdate(BaseModel):
    last_seen_tour_version: str

    @field_validator("last_seen_tour_version")
    @classmethod
    def validate_tour_version(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed or len(trimmed) > 32:
            raise ValueError("last_seen_tour_version must be 1–32 characters")
        return trimmed


@app.post("/api/me/session")
def api_me_session(auth: AuthContext = Depends(get_current_user)):
    """Record app session; return previous login/tour fields for tour eligibility."""
    previous = touch_user_session(auth.sub)
    return previous


@app.patch("/api/me/tour")
def api_me_tour(body: TourVersionUpdate, auth: AuthContext = Depends(get_current_user)):
    """Persist product tour completion/skip version for the current user."""
    return update_tour_version(auth.sub, body.last_seen_tour_version)


# ── Chat persistence ──────────────────────────────────────────────────────────

@app.get("/api/chats")
def list_chats(owner_sub: str = Depends(deps_owner_sub), limit: Optional[int] = None):
    db = SessionLocal()
    try:
        query = (
            db.query(Chat)
            .filter(Chat.owner_sub == owner_sub)
            .order_by(Chat.updated_at.desc())
        )
        if limit is not None:
            query = query.limit(limit)
        chats = query.all()
        return [
            {
                "id": c.id,
                "title": c.title,
                "active_skill_id": c.active_skill_id,
                "created_at": c.created_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in chats
        ]
    finally:
        db.close()


@app.post("/api/chats", status_code=201)
def create_chat(body: ChatCreate, request: Request, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        chat = Chat(
            title=body.title,
            active_skill_id=body.active_skill_id,
            owner_sub=owner_sub,
        )
        db.add(chat)
        db.commit()
        db.refresh(chat)
        try:
            from event_log import log_event as _log_event
            _email = getattr(build_auth_context(request.headers), "email", "")
            _log_event(owner_sub, _email, "workspace.chat_started", {"skill_id": body.active_skill_id})
        except Exception:
            pass
        return {
            "id": chat.id,
            "title": chat.title,
            "active_skill_id": chat.active_skill_id,
            "created_at": chat.created_at.isoformat(),
            "updated_at": chat.updated_at.isoformat(),
            "messages": [],
        }
    finally:
        db.close()


# Declared before /api/chats/{chat_id}: FastAPI matches routes in declaration order.
@app.get("/api/chats/search")
def search_chat_history(
    q: str = "",
    limit: int = 25,
    owner_sub: str = Depends(deps_owner_sub),
):
    """Search the caller's own chat titles and message bodies."""
    from chat_search import search_chats

    db = SessionLocal()
    try:
        return {"query": q, "results": search_chats(db, owner_sub, q, limit)}
    finally:
        db.close()


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        chat = get_tenant_or_404(db, Chat, chat_id, owner_sub, "Chat not found")
        return {
            "id": chat.id,
            "title": chat.title,
            "active_skill_id": chat.active_skill_id,
            "created_at": chat.created_at.isoformat(),
            "updated_at": chat.updated_at.isoformat(),
            "streaming": get_session(owner_sub, chat_id) is not None,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                }
                for m in chat.messages
            ],
        }
    finally:
        db.close()


@app.get("/api/chats/{chat_id}/active")
def get_chat_active(chat_id: str, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        get_tenant_or_404(db, Chat, chat_id, owner_sub, "Chat not found")
        return {"active": get_session(owner_sub, chat_id) is not None}
    finally:
        db.close()


@app.put("/api/chats/{chat_id}")
def update_chat(chat_id: str, body: ChatUpdate, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        chat = get_tenant_or_404(db, Chat, chat_id, owner_sub, "Chat not found")
        if body.title is not None:
            chat.title = body.title
        if body.active_skill_id is not None:
            chat.active_skill_id = body.active_skill_id
        chat.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        chat = get_tenant_or_404(db, Chat, chat_id, owner_sub, "Chat not found")
        db.delete(chat)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ── Chat streaming ────────────────────────────────────────────────────────────

def _text_from_anthropic_blocks(content) -> str:
    """Extract visible assistant text from a final Anthropic message."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None) or ""
            if text.strip():
                parts.append(text)
    return "\n".join(parts).strip()


def _system_blocks_for_model(system: str, model_slug: str) -> list[dict] | str:
    """Claude models use cacheable blocks; canonical / other models use a plain string."""
    from agent_llm import cached_system

    return cached_system(system, model=model_slug)


@app.post("/api/chat/cancel")
def cancel_chat(request: Request, body: ChatCancelRequest):
    """Stop an in-flight workspace chat stream or request an immediate answer."""
    owner_sub = request_owner_sub(request)
    accepted = request_cancel(owner_sub, body.chat_id, body.mode)  # type: ignore[arg-type]
    if not accepted:
        raise HTTPException(status_code=404, detail="No active chat stream for this conversation.")
    return {"ok": True, "mode": body.mode}


@app.post("/api/chat")
async def chat(request: Request, req: ChatRequest):
    if not MODEL_GATEWAY_API_KEY:
        async def _missing():
            yield _sse({"type": "error", "content": "MODEL_GATEWAY_URL is not set on the server."})
            yield _sse({"type": "done"})
        return StreamingResponse(_missing(), media_type="text/event-stream")

    model_catalog = await get_model_catalog(
        api_key=MODEL_GATEWAY_API_KEY,
        base_url_raw=_MODEL_GATEWAY_URL_RAW,
        default_model=WORKSPACE_CHAT_DEFAULT_MODEL,
    )
    from model_catalog import pick_workspace_chat_model

    try:
        requested_model = pick_workspace_chat_model(model_catalog, req.model)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not is_chat_model_allowed(requested_model, model_catalog):
        raise HTTPException(
            status_code=400,
            detail=f"Model {requested_model!r} is not available. Refresh the model list and try again.",
        )

    owner_sub = request_owner_sub(request)
    _chat_ctx = build_auth_context(request.headers)
    _chat_email = _chat_ctx.email if _chat_ctx else None

    effective_skill_id = req.skill_id
    auto_skill_slug: str | None = None
    auto_skill_confidence: float | None = None
    auto_skill_reason: str | None = None
    auto_skill_cleared = False
    clear_active_skill = False

    # Step 1: Load history in thread executor — pure sync read, no event-loop blocking.
    _loop = asyncio.get_running_loop()
    history, is_first = await _loop.run_in_executor(
        None, _chat_load_history, req.chat_id, owner_sub
    )

    # Step 2: Resolve skill ACL — async (calls the directory), manages its own DB session.
    _, accessible_catalog, accessible_slugs = await resolve_user_skill_access(
        owner_sub,
        _chat_email,
    )

    if effective_skill_id and effective_skill_id not in accessible_slugs:
        async def _forbidden_skill():
            yield _sse({
                "type": "error",
                "content": "You do not have access to this skill.",
            })
            yield _sse({"type": "done"})

        return StreamingResponse(_forbidden_skill(), media_type="text/event-stream")

    # Step 3: Workspace-only skill routing (Slack agents use suggest_skills_for_message).
    if req.no_skill:
        effective_skill_id = None
        clear_active_skill = True
        auto_skill_cleared = True
    elif not effective_skill_id and accessible_catalog and MODEL_GATEWAY_API_KEY:
        from model_gateway import gateway_use_openai_transport

        if gateway_use_openai_transport(_MODEL_GATEWAY_URL_RAW):
            auto_skill_cleared = True
            routing = None
        else:
            router_client = anthropic.AsyncAnthropic(
                api_key=MODEL_GATEWAY_API_KEY,
                base_url=MODEL_GATEWAY_URL,
            )
            routing = await route_skill_for_chat(
                req.message,
                accessible_catalog,
                router_client,
                router_model=requested_model,
                recent_messages=recent_user_messages(history),
                active_skill_hint=req.active_skill_hint,
                load_skill_content=_load_skill,
            )
        if routing is not None:
            if routing.mode == "auto" and routing.slug:
                effective_skill_id = routing.slug
                auto_skill_slug = routing.slug
                auto_skill_confidence = routing.confidence
                auto_skill_reason = routing.reason
            elif routing.mode == "disambiguation" and routing.candidates:
                async def _disambiguation():
                    yield _sse({
                        "type": "skill_disambiguation",
                        "candidates": [
                            {
                                "slug": c.slug,
                                "confidence": round(c.confidence, 3),
                                "reason": c.reason,
                            }
                            for c in routing.candidates
                        ],
                    })
                    yield _sse({"type": "done"})

                return StreamingResponse(_disambiguation(), media_type="text/event-stream")
            else:
                auto_skill_cleared = True

    # Step 4: Save user message in thread executor — pure sync write.
    _clear_skill_flag = clear_active_skill
    await _loop.run_in_executor(
        None,
        lambda: _chat_save_user_msg(
            req.chat_id,
            owner_sub,
            req.message,
            is_first,
            effective_skill_id,
            clear_active_skill=_clear_skill_flag,
        ),
    )

    history.append({"role": "user", "content": req.message})
    system = _load_skill(effective_skill_id) if effective_skill_id else DEFAULT_SYSTEM
    memory_ctx = _build_memory_context(owner_sub)
    if memory_ctx:
        system = memory_ctx + "\n\n---\n\n" + system

    # Resolve skill secrets and user personal secrets once per request so they
    # are available across all tool-call rounds without re-fetching them.
    # Skill secrets (admin-controlled) override user secrets with the same key.
    skill_secrets = _load_skill_secrets(effective_skill_id) if effective_skill_id else {}
    user_secrets = _fetch_user_secrets(owner_sub)
    # Merge: user secrets provide the base, skill secrets take precedence
    merged_secrets: dict[str, str] = {**user_secrets, **skill_secrets}
    # Scope secrets to this asyncio Task so concurrent requests cannot share state.
    _request_secrets.set(merged_secrets)

    # Resolve the model once here so we can gate Claude-specific features below.
    # Canonical auto aliases are routed by the model gateway (complexity_router on the proxy).
    _requested_model = requested_model
    effective_model = _requested_model
    _is_claude = effective_model.startswith("claude-")

    # The sections below (Slack skill, secrets list, code execution guide) apply
    # to all models — they're relevant whenever tools are available.
    # If the user has connected their Slack account, inject a Slack query skill
    # so the LLM knows exactly how to use SLACK_USER_TOKEN to query the workspace.
    if merged_secrets.get("SLACK_USER_TOKEN"):
        system += _WORKSPACE_SLACK_SKILL

    if merged_secrets.get("GOOGLE_OAUTH_CREDENTIALS"):
        system += _WORKSPACE_GOOGLE_SKILL

    # Tell the agent which env vars are pre-loaded so it knows how to use them
    available_keys: list[str] = sorted(merged_secrets)
    if available_keys:
        keys_block = "\n".join(f"- `{k}`" for k in available_keys)
        system += (
            "\n\n---\n\n"
            "## Available Secrets\n"
            "The following secrets are pre-loaded as environment variables inside every "
            "`run_python` call. Access them with `os.environ['KEY']` or "
            "`os.environ.get('KEY')`:\n"
            + keys_block
        )

    # Injected for all models that have run_python available
    system += (
        "\n\n---\n\n"
        "## Code Execution Environment\n"
        "When writing `run_python` code:\n"
        "- Use `import os; os.environ['KEY']` to read injected secrets.\n"
        "- Import only what the tool description lists as available. The sandbox has "
        "no network, so a missing package cannot be installed.\n"
        "- Do not use `subprocess`, `os.system`, or shell pipes.\n"
        "- Print results clearly; the output is shown directly to the user."
    )

    system += (
        "\n\n---\n\n"
        "## Creating Skills, Automations, and Labs\n"
        "If the user asks you to build/create a new Skill, Automation, or Lab, "
        "gather the minimum details conversationally (don't interrogate them — "
        "2-3 clarifying questions max), then call `propose_skill`, "
        "`propose_automation`, or `propose_lab`. These only show the user a "
        "draft card to approve or discard — they do not create anything by "
        "themselves. Never call the same propose_* tool twice for the same "
        "request; wait for the user's decision on the card shown."
    )
    if req.create_kind:
        system += (
            f"\n\nThe user clicked \"Create new {req.create_kind}\" in the composer "
            f"just now. Prioritize getting to a `propose_{req.create_kind}` call "
            "quickly with sensible defaults for anything not specified, rather than "
            "asking many questions."
        )

    client = anthropic.AsyncAnthropic(
        api_key=MODEL_GATEWAY_API_KEY,
        base_url=MODEL_GATEWAY_URL,
    )

    from model_gateway import (
        compact_for_local_gateway,
        gateway_use_openai_transport,
        local_gateway_context_limit,
        local_gateway_max_output_tokens,
    )

    _use_local_openai = gateway_use_openai_transport(_MODEL_GATEWAY_URL_RAW)
    _local_context_limit = local_gateway_context_limit() if _use_local_openai else None

    # Build tool list:
    # - run_python and fetch_url work for all models via model gateway's function-calling
    #   translation (OpenAI-compatible format).
    # - WEB_TOOLS (web_search + web_fetch) are Anthropic-native server-side tools
    #   only available when calling Anthropic directly (no model gateway).
    _is_direct_anthropic = "anthropic.com" in MODEL_GATEWAY_URL
    if _use_local_openai:
        tools = [RUN_PYTHON_TOOL_MINIMAL]
    else:
        tools = [FETCH_URL_TOOL, RUN_PYTHON_TOOL]
        if _is_claude and _is_direct_anthropic:
            tools = list(WEB_TOOLS) + tools
        tools.extend([PROPOSE_SKILL_TOOL, PROPOSE_AUTOMATION_TOOL, PROPOSE_LAB_TOOL])

    _workspace_skill_slugs: list[str] = [effective_skill_id] if effective_skill_id else []
    _offer_call_artifact = False
    from api_slack_events import (
        _CALL_ARTIFACT_TOOL,
        _SKILL_ARTIFACTS_ENABLED,
        _execute_call_artifact as _workspace_execute_call_artifact,
    )
    if _SKILL_ARTIFACTS_ENABLED and _workspace_skill_slugs and not _use_local_openai:
        def _load_workspace_artifact_section() -> str:
            db = SessionLocal()
            try:
                from artifact_runtime import (
                    build_artifact_prompt_section,
                    load_approved_artifacts,
                )
                return build_artifact_prompt_section(
                    load_approved_artifacts(db, _workspace_skill_slugs)
                )
            finally:
                db.close()

        artifact_section = await _loop.run_in_executor(None, _load_workspace_artifact_section)
        if artifact_section.strip():
            system += "\n\n" + artifact_section
            tools.append(_CALL_ARTIFACT_TOOL)
            _offer_call_artifact = True

    # For Claude: pass system as a block list with cache_control for prompt caching.
    # For other models: pass system as a plain string — the model gateway does not translate
    # the Anthropic block format correctly for non-Claude models (it sends
    # input_json_delta events instead of text_delta and drops the system prompt).
    system_blocks: list[dict] | str = _system_blocks_for_model(system, effective_model)

    # Build the first user message content.  If the request includes attachments
    # (pre-uploaded via /api/chat/upload) we prepend image/document blocks so
    # Claude can reason over them alongside the text.
    first_user_content: list[dict] | str = req.message
    if req.attachments:
        first_user_content = []
        for att in req.attachments[:ATTACHMENT_MAX_COUNT]:
            if att.mime not in ATTACHMENT_ALLOWED_MIMES:
                continue
            try:
                raw_bytes = get_blob_store().get(att.id)
                b64 = base64.b64encode(raw_bytes).decode()
                if att.mime.startswith("image/"):
                    first_user_content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": att.mime, "data": b64},
                    })
                else:  # PDF / document
                    first_user_content.append({
                        "type": "document",
                        "source": {"type": "base64", "media_type": att.mime, "data": b64},
                    })
            except Exception:
                logger.exception("Failed to load attachment %s", att.id)
        first_user_content.append({"type": "text", "text": req.message})

    # Replace the last history entry (plain string) with the enriched content
    # when attachments are present so multi-modal content reaches the model.
    chat_messages = history[:-1] + [{"role": "user", "content": first_user_content}]

    if _use_local_openai:
        system, chat_messages = compact_for_local_gateway(system, chat_messages)
        system_blocks = system

    from token_tracking import TokenAccumulator, record_token_usage as _record_usage

    async def generate():
        accumulated = ""
        thinking_accumulated = ""
        tool_errors = 0  # stop retrying after 5 consecutive tool errors
        tool_call_count = 0  # total client-side tool calls this turn (for progress nudges)
        last_progress_nudge_count = 0
        _usage_acc = TokenAccumulator()
        _chat_tool_audit: list[dict] = []  # populated as run_python calls execute
        _session_ctrl = register_session(owner_sub, req.chat_id)
        _exit_mode: str | None = None
        original_model = effective_model
        active_model = effective_model
        final_model_used = effective_model
        initial_messages = list(chat_messages)
        messages = list(chat_messages)
        import json as _json

        _oai_tools_chars = len(_json.dumps(tools)) if _use_local_openai else 0
        oai_max_tokens = (
            local_gateway_max_output_tokens(
                system,
                initial_messages,
                context_limit=_local_context_limit,
                tools_json_chars=_oai_tools_chars,
            )
            if _use_local_openai
            else 8192
        )
        oai_state: Optional[OpenAIChatState] = None
        model_attempts: list[str] = [effective_model]
        if (
            is_canonical_ui_model(effective_model, model_catalog)
            and effective_model != CHAT_FALLBACK_MODEL
        ):
            model_attempts.append(CHAT_FALLBACK_MODEL)
        active_system_blocks: list[dict] | str = system_blocks
        active_is_claude = _is_claude

        async def _run_answer_now_synthesis():
            nonlocal accumulated
            yield _sse({
                "type": "answer_now",
                "content": "Synthesizing answer from data collected so far…",
            })
            synthesis_messages = list(messages) + [{
                "role": "user",
                "content": (
                    "The user clicked **Answer now**. Do not call any tools. "
                    "Provide the best possible answer using only information already "
                    "available in this conversation (including prior tool outputs). "
                    "If something could not be retrieved, state what is missing."
                ),
            }]
            async with client.messages.stream(
                model=active_model,
                max_tokens=4096,
                system=active_system_blocks,
                messages=synthesis_messages,
            ) as synth_stream:
                async for event in synth_stream:
                    if _session_ctrl.event.is_set() and _session_ctrl.mode == "stop":
                        break
                    event_type = getattr(event, "type", None)
                    if event_type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        if getattr(delta, "type", None) == "text_delta":
                            text = delta.text
                            accumulated += text
                            yield _sse({"type": "text", "content": text})
                synth_final = await synth_stream.get_final_message()
                _usage_acc.add(synth_final.usage)

        try:
            for attempt_idx, attempt_model in enumerate(model_attempts):
                if attempt_idx > 0:
                    if accumulated.strip() or _exit_mode:
                        break
                    logger.warning(
                        "canonical_model_failed model=%s fallback=%s reason=empty_or_failed_response",
                        original_model,
                        attempt_model,
                    )
                    accumulated = ""
                    thinking_accumulated = ""
                    messages = list(initial_messages)
                    tool_errors = 0
                    tool_call_count = 0
                    last_progress_nudge_count = 0
                    active_model = attempt_model
                    active_is_claude = active_model.startswith("claude-")
                    active_system_blocks = _system_blocks_for_model(system, active_model)
                    yield _sse({
                        "type": "model_selected",
                        "model": active_model,
                        "fallback_from": original_model,
                        "reason": "empty_or_failed_response",
                    })
                else:
                    active_model = attempt_model
                    yield _sse({
                        "type": "model_selected",
                        "model": active_model,
                    })
                    if auto_skill_slug:
                        yield _sse({
                            "type": "skill_auto_selected",
                            "slug": auto_skill_slug,
                            "confidence": round(auto_skill_confidence or 0.0, 3),
                            "reason": auto_skill_reason or "",
                        })
                    elif auto_skill_cleared:
                        yield _sse({"type": "skill_auto_cleared"})

                try:
                    from model_gateway import gateway_use_openai_transport

                    if is_auto_workspace_model(active_model) or gateway_use_openai_transport(
                        _MODEL_GATEWAY_URL_RAW
                    ):
                        # Local gateways (LM Studio, Ollama) and Auto use OpenAI
                        # /v1/chat/completions — they do not implement Anthropic /v1/messages.
                        print(
                            f"[CHAT] using OpenAI chat/completions transport for {active_model}",
                            flush=True,
                        )
                        from openai import APIError as OpenAIAPIError
                        from openai import APIStatusError as OpenAIAPIStatusError

                        oai_state = OpenAIChatState()
                        tool_runtime = ChatToolRuntime(
                            owner_sub=owner_sub,
                            chat_id=req.chat_id,
                            merged_secrets=merged_secrets or {},
                            fetch_url=_fetch_url_content,
                            user_workspace_dir=_user_workspace_dir,
                            rewrite_paths=_rewrite_python_workspace_paths,
                            register_new_pdfs=_register_new_pdfs_for_owner,
                            register_generated_file=_register_generated_file,
                            tool_audit=_chat_tool_audit,
                            skill_slugs_for_artifacts=_workspace_skill_slugs
                            if _offer_call_artifact
                            else [],
                        )
                        try:
                            async for ev in stream_workspace_auto_chat(
                                model=active_model,
                                system=system,
                                initial_messages=messages,
                                tools=tools,
                                api_key=MODEL_GATEWAY_API_KEY,
                                base_url_raw=_MODEL_GATEWAY_URL_RAW,
                                runtime=tool_runtime,
                                session_ctrl=_session_ctrl,
                                usage_acc=_usage_acc,
                                state=oai_state,
                                max_tokens=oai_max_tokens,
                                local_context_limit=_local_context_limit,
                            ):
                                if ev.get("type") == "__keepalive__":
                                    yield ": keep-alive\n\n"
                                    continue
                                yield _sse(ev)
                        except (OpenAIAPIStatusError, OpenAIAPIError) as openai_err:
                            err_msg = getattr(openai_err, "message", str(openai_err))
                            err_status = getattr(openai_err, "status_code", None)
                            if (
                                attempt_idx + 1 < len(model_attempts)
                                and not oai_state.accumulated.strip()
                            ):
                                logger.warning(
                                    "canonical_model_api_error model=%s status=%s → trying %s",
                                    active_model,
                                    err_status,
                                    model_attempts[attempt_idx + 1],
                                )
                                accumulated = ""
                                thinking_accumulated = ""
                                messages = list(initial_messages)
                                tool_errors = 0
                                tool_call_count = 0
                                last_progress_nudge_count = 0
                                continue
                            if "context" in err_msg.lower():
                                user_msg = (
                                    "The model ran out of context. Start a new chat, remove the "
                                    "active skill, or increase the context length in LM Studio."
                                )
                            else:
                                user_msg = (
                                    f"API error {err_status}: {err_msg}"
                                    if err_status
                                    else err_msg
                                )
                            yield _sse({"type": "error", "content": user_msg})
                            break

                        accumulated = oai_state.accumulated
                        thinking_accumulated = oai_state.thinking_accumulated
                        tool_errors = oai_state.tool_errors
                        if oai_state.exit_mode:
                            _exit_mode = oai_state.exit_mode
                        final_model_used = oai_state.routed_model or active_model
                    else:
                        # Up to 25 turns: web tools and multi-step skills can add extra
                        # roundtrips on top of run_python calls.
                        for _turn in range(25):
                            if _session_ctrl.event.is_set():
                                _exit_mode = _session_ctrl.mode
                                break
    
                            stream_kwargs: dict = dict(
                                model=active_model,
                                max_tokens=8192,
                                system=active_system_blocks,
                                messages=messages,
                            )
                            if tools:
                                stream_kwargs["tools"] = tools
                            # Adaptive thinking is only supported by Claude models.
                            if req.thinking and active_is_claude:
                                stream_kwargs["thinking"] = {"type": "adaptive"}
    
                            print(f"[CHAT] stream start model={active_model} turn={_turn} msgs={len(messages)}", flush=True)
                            async with client.messages.stream(**stream_kwargs) as stream:
                                async for event in stream:
                                    if _session_ctrl.event.is_set():
                                        _exit_mode = _session_ctrl.mode
                                        break
                                    # Dispatch on event type for fine-grained streaming
                                    event_type = getattr(event, "type", None)
                                    if event_type == "content_block_delta":
                                        delta = getattr(event, "delta", None)
                                        if delta is None:
                                            continue
                                        delta_type = getattr(delta, "type", None)
                                        if delta_type == "text_delta":
                                            text = delta.text
                                            accumulated += text
                                            yield _sse({"type": "text", "content": text})
                                        elif delta_type == "thinking_delta":
                                            thinking_text = delta.thinking
                                            thinking_accumulated += thinking_text
                                            yield _sse({"type": "thinking", "content": thinking_text})
                                final_msg = await stream.get_final_message()
                            if _exit_mode:
                                break
                            print(f"[CHAT] stream end model={active_model} stop={final_msg.stop_reason} accumulated={len(accumulated)} out_tokens={final_msg.usage.output_tokens}", flush=True)
                            _usage_acc.add(final_msg.usage)
    
                            # Recover text when the model gateway streams input_json_delta instead of text_delta.
                            if not accumulated.strip():
                                recovered = _text_from_anthropic_blocks(final_msg.content)
                                if recovered:
                                    accumulated = recovered
                                    yield _sse({"type": "text", "content": recovered})
    
                            # Some models (e.g. Gemini via model gateway) return stop_reason="end_turn"
                            # even when a tool_use block is present.  Check content blocks too.
                            _has_tool_use = any(getattr(b, "type", None) == "tool_use" for b in final_msg.content)
                            if (final_msg.stop_reason not in ("tool_use",) and not _has_tool_use) or not tools:
                                break  # normal end, no tool call, or tools disabled — done
    
                            # ── Tool-call round ───────────────────────────────────────────
                            # Serialize all content blocks back for the next API call.
                            # Thinking blocks MUST be preserved unmodified to maintain
                            # reasoning continuity across turns (Anthropic requirement).
                            # server_tool_use blocks are produced by web_search/web_fetch
                            # and are handled server-side; we pass them back as-is.
                            serialized_content = []
                            for b in final_msg.content:
                                if b.type == "text":
                                    serialized_content.append({"type": "text", "text": b.text})
                                elif b.type == "thinking":
                                    serialized_content.append({
                                        "type": "thinking",
                                        "thinking": b.thinking,
                                    })
                                elif b.type == "tool_use":
                                    serialized_content.append({
                                        "type": "tool_use",
                                        "id": b.id,
                                        "name": b.name,
                                        "input": b.input,
                                    })
                                elif b.type == "server_tool_use":
                                    # Pass back server-side tool invocations unchanged
                                    serialized_content.append({
                                        "type": "server_tool_use",
                                        "id": b.id,
                                        "name": b.name,
                                        "input": getattr(b, "input", {}),
                                    })
                            messages.append({"role": "assistant", "content": serialized_content})
                            tool_results = []
    
                            for block in final_msg.content:
                                if _exit_mode:
                                    break
                                if block.type == "tool_use" and block.name == "run_python":
                                    # ── run_python: execute in the orchestrator sandbox ───────────────
                                    import time as _time
                                    code = block.input.get("code", "")
                                    step_label = block.input.get("label", "Running code…")
                                    step_id = block.id
                                    yield _sse({"type": "step_start", "id": step_id, "label": step_label})
                                    yield _sse({"type": "tool_start", "label": step_label})
                                    t0 = _time.monotonic()
                                    work_dir = _user_workspace_dir(owner_sub)
                                    patched_code = _rewrite_python_workspace_paths(code, str(work_dir))
                                    before_pdf_set = {p.name for p in work_dir.glob("*.pdf")}
    
                                    # Run sandbox in a background task; yield SSE keepalives
                                    # every 5 s so Cloudflare doesn't close the connection.
                                    sandbox_task = asyncio.create_task(
                                        execute_in_sandbox(patched_code, str(work_dir), merged_secrets or None)
                                    )
                                    _sandbox_cancelled = False
                                    while not sandbox_task.done():
                                        if _session_ctrl.event.is_set():
                                            sandbox_task.cancel()
                                            _sandbox_cancelled = True
                                            _exit_mode = _session_ctrl.mode
                                            try:
                                                await sandbox_task
                                            except asyncio.CancelledError:
                                                pass
                                            break
                                        try:
                                            await asyncio.wait_for(asyncio.shield(sandbox_task), timeout=5.0)
                                        except asyncio.TimeoutError:
                                            yield ": keep-alive\n\n"
                                    if _sandbox_cancelled:
                                        yield _sse({
                                            "type": "step_end",
                                            "id": step_id,
                                            "ok": False,
                                            "duration_ms": int((_time.monotonic() - t0) * 1000),
                                        })
                                        yield _sse({"type": "tool_end", "ok": False})
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_use_id": block.id,
                                            "content": "(tool run stopped by user)",
                                        })
                                        break
    
                                    exec_result = sandbox_task.result()
                                    duration_ms = int((_time.monotonic() - t0) * 1000)
                                    _register_new_pdfs_for_owner(work_dir, owner_sub, before_pdf_set)
                                    _chat_tool_audit.append({
                                        "tool": "run_python",
                                        "code_len": len(code),
                                        "duration_ms": duration_ms,
                                        "ok": exec_result.get("exit_code", 1) == 0,
                                    })
                                    result_parts = []
                                    if exec_result["stdout"]:
                                        result_parts.append(exec_result["stdout"].strip())
                                    if exec_result["stderr"]:
                                        result_parts.append(f"[stderr]\n{exec_result['stderr'].strip()}")
                                    result = "\n".join(result_parts) or "(no output)"
                                    ok = "[stderr]" not in result and not result.startswith("(no output)")
                                    yield _sse({"type": "step_end", "id": step_id, "ok": ok, "duration_ms": duration_ms, "output": result})
                                    yield _sse({"type": "tool_end", "ok": ok})

                                    # Tool stdout is returned via tool_result for the model only —
                                    # do not stream it into the assistant message as a CODE dump.
    
                                    # ── Emit file_generated events for any extracted files ──
                                    for f in exec_result.get("files", []):
                                        fname = f["filename"]
                                        fsize = f["size"]
                                        fmime = f["mime"]
                                        _register_generated_file(fname, owner_sub)
                                        file_url = f"/api/files/{fname}"
    
                                        # Upload to the blob store so both backend replicas can serve it.
                                        # Run in a thread executor — upload_from_filename is
                                        # synchronous and would block the event loop otherwise,
                                        # causing Cloudflare to drop the SSE connection.
                                        local_path = work_dir / fname
                                        if local_path.is_file():
                                            yield ": keep-alive\n\n"
                                            try:
                                                _key = f"generated/{owner_sub}/{fname}"
                                                _loop = asyncio.get_running_loop()
                                                await _loop.run_in_executor(
                                                    None,
                                                    lambda: get_blob_store().put(
                                                        _key,
                                                        local_path.read_bytes(),
                                                        content_type=fmime,
                                                    ),
                                                )
                                            except Exception:
                                                logger.exception("upload failed for %s", fname)
    
                                        yield _sse({
                                            "type": "file_generated",
                                            "filename": fname,
                                            "url": file_url,
                                            "mime": fmime,
                                            "size_bytes": fsize,
                                        })
                                        # Persist file metadata as a hidden marker in the message
                                        # text so it survives page reloads (DB only stores content).
                                        import json as _json
                                        marker = (
                                            f"\n<!-- GENFILE:{_json.dumps({'f': fname, 'u': file_url, 'm': fmime, 's': fsize})} -->"
                                        )
                                        accumulated += marker
                                        yield _sse({"type": "text", "content": marker})
    
                                    if not ok:
                                        tool_errors += 1
                                    else:
                                        tool_errors = 0
                                    tool_call_count += 1
    
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": result,
                                    })

                                elif (
                                    block.type == "tool_use"
                                    and block.name == "call_artifact"
                                    and _offer_call_artifact
                                ):
                                    art_name = block.input.get("name", "")
                                    art_params = block.input.get("params")
                                    if not isinstance(art_params, dict):
                                        art_params = {}
                                    step_label = f"Artifact {art_name}"
                                    step_id = block.id
                                    yield _sse({
                                        "type": "step_start",
                                        "id": step_id,
                                        "label": step_label,
                                    })
                                    yield _sse({"type": "tool_start", "label": step_label})
                                    art_output = await _workspace_execute_call_artifact(
                                        str(art_name),
                                        art_params,
                                        accessible_skill_slugs=_workspace_skill_slugs,
                                        skill_secrets=merged_secrets or {},
                                        agent_id=f"workspace:{owner_sub}",
                                        tool_audit=_chat_tool_audit,
                                    )
                                    ok = not art_output.startswith("[error]")
                                    yield _sse({"type": "step_end", "id": step_id, "ok": ok, "duration_ms": 0, "output": art_output})
                                    yield _sse({"type": "tool_end", "ok": ok})
                                    if not ok:
                                        tool_errors += 1
                                    else:
                                        tool_errors = 0
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": art_output,
                                    })

                                elif block.type == "tool_use" and block.name == "fetch_url":
                                    # Client-side URL fetcher: we execute the HTTP request.
                                    url = block.input.get("url", "")
                                    yield _sse({"type": "tool_start", "label": f"Fetching {url[:60]}…"})
                                    fetch_result = await _fetch_url_content(url)
                                    ok = not fetch_result.startswith("Error:")
                                    yield _sse({"type": "tool_end", "ok": ok})
                                    tool_call_count += 1
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": fetch_result,
                                    })

                                elif block.type == "tool_use" and block.name in (
                                    "propose_skill", "propose_automation", "propose_lab",
                                ):
                                    from resource_proposals import (
                                        content_for_kind,
                                        create_resource_proposal,
                                        proposal_marker,
                                    )
                                    kind = block.name.removeprefix("propose_")
                                    proposal_input = block.input or {}
                                    proposal_name = str(proposal_input.get("name", "Untitled")).strip()[:300] or "Untitled"
                                    proposal_content = content_for_kind(kind, proposal_name, proposal_input)

                                    proposal = create_resource_proposal(
                                        chat_id=req.chat_id,
                                        owner_sub=owner_sub,
                                        kind=kind,
                                        name=proposal_name,
                                        content=proposal_content,
                                    )
                                    yield _sse({
                                        "type": "proposal_created",
                                        "proposal_id": proposal.id,
                                        "kind": kind,
                                        "name": proposal_name,
                                    })
                                    proposal_marker_text = proposal_marker(proposal.id, kind, proposal_name)
                                    accumulated += proposal_marker_text
                                    yield _sse({"type": "text", "content": proposal_marker_text})

                                    tool_call_count += 1
                                    tool_errors = 0
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": block.id,
                                        "content": (
                                            f"Draft {kind} \"{proposal_name}\" created and shown to the "
                                            f"user for review (proposal_id={proposal.id}). Do not call "
                                            f"propose_{kind} again for this request — wait for the user "
                                            "to approve or discard it. Briefly tell the user you've "
                                            "drafted it and they can review it above."
                                        ),
                                    })

                                elif block.type == "tool_use" and block.name in ("web_search", "web_fetch"):
                                    # Server-side tools: Anthropic executes these — we just
                                    # signal the UI that a web action is happening.
                                    label = (
                                        "Searching the web…"
                                        if block.name == "web_search"
                                        else "Fetching page…"
                                    )
                                    yield _sse({"type": "tool_start", "label": label})
    
                                elif block.type == "server_tool_use":
                                    # Newer API shape for server-side tools
                                    label = (
                                        "Searching the web…"
                                        if "search" in block.name
                                        else "Fetching page…"
                                    )
                                    yield _sse({"type": "tool_start", "label": label})
    
                            # Every 3 tool calls, nudge the model to post a brief progress
                            # update (what's been tried / what's next) before it keeps going,
                            # so long tool-call chains don't leave the user without feedback.
                            if tool_results and tool_call_count - last_progress_nudge_count >= 3:
                                last_progress_nudge_count = tool_call_count
                                tool_results.append({
                                    "type": "text",
                                    "text": (
                                        "[SYSTEM NOTE — not from the user] You've made several tool "
                                        "calls. Before continuing, briefly update the user (2-4 "
                                        "sentences) on what you've tried so far and what you still "
                                        "plan to try next, then continue the task."
                                    ),
                                })

                            # server_tool_use results are handled server-side; only append
                            # client-side tool_results when we actually executed something.
                            if tool_results:
                                messages.append({"role": "user", "content": tool_results})

                            # Stop retrying after 5 consecutive failed client tool calls
                            if tool_errors >= 5:
                                err_text = (
                                    "\n\n> ⚠️ The query could not be executed — the tool returned errors "
                                    "on multiple attempts. Please check that the required credentials are configured."
                                )
                                accumulated += err_text
                                yield _sse({"type": "text", "content": err_text})
                                break

                except anthropic.APIStatusError as api_err:
                    if attempt_idx + 1 < len(model_attempts) and not accumulated.strip():
                        logger.warning(
                            "canonical_model_api_error model=%s status=%s → trying %s",
                            active_model,
                            api_err.status_code,
                            model_attempts[attempt_idx + 1],
                        )
                        continue
                    raise

                if not is_auto_workspace_model(active_model):
                    final_model_used = active_model
                if accumulated.strip() or _exit_mode:
                    break

            if _exit_mode == "answer_now":
                if is_auto_workspace_model(active_model) and oai_state is not None:
                    # Reuse the oai_state from the chat turn — it already holds
                    # openai_messages (with all tool outputs) written back by
                    # stream_workspace_auto_chat.  Reset only the accumulated text
                    # so the synthesis appends on top of what was already streamed.
                    oai_state.accumulated = accumulated
                    async for ev in stream_auto_answer_now_synthesis(
                        model=active_model,
                        system=system,
                        messages=messages,
                        api_key=MODEL_GATEWAY_API_KEY,
                        base_url_raw=_MODEL_GATEWAY_URL_RAW,
                        usage_acc=_usage_acc,
                        state=oai_state,
                        session_ctrl=_session_ctrl,
                        max_tokens=oai_max_tokens,
                    ):
                        accumulated = oai_state.accumulated
                        yield _sse(ev)
                else:
                    async for chunk in _run_answer_now_synthesis():
                        yield chunk
            elif _exit_mode == "stop":
                yield _sse({"type": "cancelled", "mode": "stop"})

        except anthropic.APIStatusError as e:
            yield _sse({"type": "error", "content": f"API error {e.status_code}: {e.message}"})
        except Exception:
            logger.exception("Chat stream error")
            yield _sse({"type": "error", "content": "An error occurred processing your request."})
        finally:
            clear_session(owner_sub, req.chat_id)
            _record_usage("chat", final_model_used, _usage_acc, user_email=_chat_email)
            if accumulated:
                # Save in executor — the generator runs inside the event loop,
                # so a direct SessionLocal() call here would block it.
                await asyncio.get_running_loop().run_in_executor(
                    None, _save_assistant_msg, req.chat_id, accumulated
                )
            # Explicitly clear request-scoped secrets so this Task's ContextVar
            # holds no credentials after the streaming response completes.
            _request_secrets.set({})
            # Structured audit log for this web-chat invocation — queryable via
            # log aggregation (Datadog, Cloud Logging).  No secret values are logged.
            import json as _json
            logger.info(
                "CHAT_INVOCATION_AUDIT %s",
                _json.dumps({
                    "owner_sub_hash": __import__("hashlib").sha256(
                        owner_sub.encode()
                    ).hexdigest()[:16],
                    "skill_id": req.skill_id,
                    "model": final_model_used,
                    "secrets_loaded": sorted(merged_secrets.keys()),
                    "tools_called": _chat_tool_audit,
                    "response_len": len(accumulated),
                }),
            )
            yield _sse({"type": "done"})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Automations CRUD ──────────────────────────────────────────────────────────

@app.get("/api/automations")
def list_automations(owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        automations = (
            db.query(Automation)
            .filter(Automation.owner_sub == owner_sub)
            .order_by(Automation.created_at.desc())
            .all()
        )
        return [_automation_to_dict(a) for a in automations]
    finally:
        db.close()


@app.post("/api/automations", status_code=201)
def create_automation(body: AutomationCreate, request: Request, owner_sub: str = Depends(deps_owner_sub)):
    from automation_skill_access import ensure_skill_granted

    slack_ch = _non_empty_slack_channel(body.slack_channel)
    if slack_ch and not slack_user_token_integration_allowed(owner_sub):
        raise HTTPException(
            status_code=403,
            detail="Slack posting is not enabled for this account.",
        )
    caller_email = getattr(build_auth_context(request.headers), "email", "") or ""
    ensure_skill_granted(owner_sub, caller_email, body.skill_id)
    db = SessionLocal()
    try:
        auto = Automation(
            name=body.name,
            instructions=body.instructions,
            skill_id=body.skill_id,
            schedule=body.schedule,
            is_active=body.is_active,
            slack_channel=slack_ch,
            owner_sub=owner_sub,
        )
        db.add(auto)
        db.commit()
        db.refresh(auto)
        if auto.is_active:
            _schedule_automation(auto)
        try:
            from event_log import log_event as _log_event
            _email = getattr(build_auth_context(request.headers), "email", "")
            _log_event(owner_sub, _email, "automation.created", {"skill_id": body.skill_id, "has_slack": bool(slack_ch)})
        except Exception:
            pass
        return _automation_to_dict(auto)
    finally:
        db.close()


@app.get("/api/automations/{automation_id}")
def get_automation(automation_id: str, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        auto = get_tenant_or_404(db, Automation, automation_id, owner_sub, "Automation not found")
        return _automation_to_dict(auto)
    finally:
        db.close()


@app.put("/api/automations/{automation_id}")
def update_automation(
    automation_id: str,
    body: AutomationUpdate,
    request: Request,
    owner_sub: str = Depends(deps_owner_sub),
):
    from automation_skill_access import ensure_skill_granted

    db = SessionLocal()
    try:
        auto = get_tenant_or_404(db, Automation, automation_id, owner_sub, "Automation not found")
        if body.name is not None:
            auto.name = body.name
        if body.instructions is not None:
            auto.instructions = body.instructions
        if body.skill_id is not None:
            caller_email = getattr(build_auth_context(request.headers), "email", "") or ""
            ensure_skill_granted(owner_sub, caller_email, body.skill_id)
            auto.skill_id = body.skill_id
        if body.schedule is not None:
            auto.schedule = body.schedule
        if body.is_active is not None:
            auto.is_active = body.is_active
        patch = body.model_dump(exclude_unset=True)
        if "slack_channel" in patch:
            slack_ch = _non_empty_slack_channel(patch["slack_channel"])
            if slack_ch and not slack_user_token_integration_allowed(owner_sub):
                raise HTTPException(
                    status_code=403,
                    detail="Slack posting is not enabled for this account.",
                )
            auto.slack_channel = slack_ch
        auto.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(auto)
        # Reschedule
        _unschedule_automation(automation_id)
        if auto.is_active:
            _schedule_automation(auto)
        return _automation_to_dict(auto)
    finally:
        db.close()


@app.delete("/api/automations/{automation_id}")
def delete_automation(automation_id: str, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        auto = get_tenant_or_404(db, Automation, automation_id, owner_sub, "Automation not found")
        _unschedule_automation(automation_id)
        db.delete(auto)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ── Automation run endpoints ──────────────────────────────────────────────────

@app.post("/api/automations/{automation_id}/run")
def trigger_run(
    automation_id: str,
    request: Request,
    owner_sub: str = Depends(deps_owner_sub),
):
    from automation_skill_access import ensure_skill_granted

    db = SessionLocal()
    try:
        auto = get_tenant_or_404(db, Automation, automation_id, owner_sub, "Automation not found")
        if auto.skill_id:
            caller_email = getattr(build_auth_context(request.headers), "email", "") or ""
            ensure_skill_granted(owner_sub, caller_email, auto.skill_id)
    finally:
        db.close()
    # Schedule an immediate one-off execution
    scheduler.add_job(
        _run_automation_job,
        id=f"{automation_id}-manual-{datetime.now(timezone.utc).timestamp()}",
        args=[automation_id, True],
    )
    return {"ok": True, "message": "Run triggered"}


@app.get("/api/automations/{automation_id}/runs")
def list_runs(automation_id: str, owner_sub: str = Depends(deps_owner_sub), limit: int = 50):
    db = SessionLocal()
    try:
        auto = get_tenant_or_404(db, Automation, automation_id, owner_sub, "Automation not found")
        runs = (
            db.query(AutomationRun)
            .filter(AutomationRun.automation_id == automation_id)
            .order_by(AutomationRun.started_at.desc())
            .limit(limit)
            .all()
        )
        return [_run_to_dict(r) for r in runs]
    finally:
        db.close()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, owner_sub: str = Depends(deps_owner_sub)):
    db = SessionLocal()
    try:
        run = db.get(AutomationRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        get_tenant_or_404(db, Automation, run.automation_id, owner_sub, "Run not found")
        return _run_to_dict(run)
    finally:
        db.close()


# ── Memory ────────────────────────────────────────────────────────────────────
# Listing, editing and deleting entries live in api_memory.py. The sync
# endpoints stay here because they run the mining job defined in this module.

@app.post("/api/memory/sync", status_code=202)
async def trigger_memory_sync(
    background_tasks: BackgroundTasks,
    owner_sub: str = Depends(deps_owner_sub),
):
    background_tasks.add_task(_mine_memory_job, owner_sub)
    return {"ok": True, "message": "Memory sync triggered"}


@app.post("/api/memory/sync-debug")
def trigger_memory_sync_debug(owner_sub: str = Depends(deps_owner_sub)):
    """Run memory sync synchronously and return detailed result (for debugging)."""
    import traceback as _tb
    try:
        _mine_memory_job(for_owner_sub=owner_sub)
        db = SessionLocal()
        try:
            count = db.query(Memory).filter(Memory.owner_sub == owner_sub).count()
            last = (
                db.query(MemorySyncLog)
                .filter(MemorySyncLog.owner_sub == owner_sub)
                .order_by(MemorySyncLog.started_at.desc())
                .first()
            )
            return {
                "ok": True,
                "memory_count": count,
                "log": {
                    "status": last.status if last else None,
                    "error": last.error if last else None,
                    "created": last.entries_created if last else 0,
                    "updated": last.entries_updated if last else 0,
                } if last else None,
            }
        finally:
            db.close()
    except Exception:
        return {"ok": False, "traceback": _tb.format_exc()}


# ── File upload / serve ───────────────────────────────────────────────────────
async def upload_file(
    file: UploadFile = File(...),
    owner_sub: str = Depends(deps_owner_sub),
):
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 20 MB limit.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="File does not appear to be a valid PDF.")
    prefix = secrets.token_hex(8)
    safe_name = Path(file.filename or "upload.pdf").name
    stored_name = f"{prefix}_{safe_name}"
    work_dir = _user_workspace_dir(owner_sub)
    dest = work_dir / stored_name
    try:
        dest.write_bytes(data)
    except Exception:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Could not save file.")

    await asyncio.get_running_loop().run_in_executor(
        None, _register_pdf_row, stored_name, owner_sub
    )

    return {"filename": stored_name, "url": f"/api/files/{stored_name}"}


@app.post("/api/chat/upload")
async def upload_chat_attachment(
    file: UploadFile = File(...),
    owner_sub: str = Depends(deps_owner_sub),
):
    """Upload a file to be attached to the next chat message.

    Validates file type by magic bytes (not just MIME header), stores in the blob store
    under attachments/<owner_sub>/<uuid>, and returns the attachment id + mime.
    The client passes this id in the `attachments` array of /api/chat.
    """
    # ── Magic-byte MIME detection ─────────────────────────────────────────────
    _MAGIC: list[tuple[bytes, str]] = [
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),   # checked further below
        (b"%PDF", "application/pdf"),
    ]

    data = await file.read(ATTACHMENT_MAX_BYTES + 1)
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {ATTACHMENT_MAX_BYTES // (1024*1024)} MB limit.")

    detected_mime: str | None = None
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            # WebP: RIFF....WEBP
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            detected_mime = mime
            break

    if detected_mime not in ATTACHMENT_ALLOWED_MIMES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Allowed: JPEG, PNG, GIF, WebP, PDF.",
        )

    attachment_id = f"attachments/{owner_sub}/{secrets.token_hex(16)}"
    try:
        # Attachments are transient. Deployments should expire the `attachments/`
        # namespace on a schedule; nothing here depends on them living long.
        get_blob_store().put(attachment_id, data, content_type=detected_mime)
    except Exception:
        logger.exception("Failed to upload attachment to the blob store")
        raise HTTPException(status_code=500, detail="Could not store attachment.")

    return {"id": attachment_id, "mime": detected_mime}


@app.get("/api/files/{filename}")
async def serve_file(filename: str, owner_sub: str = Depends(deps_owner_sub)):
    name_only = Path(filename).name

    exists = await asyncio.get_running_loop().run_in_executor(
        None, _check_file_ownership, name_only, owner_sub
    )
    if not exists:
        raise HTTPException(status_code=404, detail="File not found.")

    base = _user_workspace_dir(owner_sub).resolve()
    safe_path = (base / name_only).resolve()
    try:
        safe_path.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    ext = Path(name_only).suffix.lower()
    media_type = _GENERATED_FILE_MIMES.get(ext, "application/octet-stream")

    # Fast path: file is on this pod's local disk.
    if safe_path.is_file():
        return FileResponse(safe_path, media_type=media_type, filename=safe_path.name)

    # Fallback: fetch from the blob store (file was generated by the other backend replica).
    # Store reads are synchronous — run them in a thread executor so the event
    # loop (and other concurrent requests) are not blocked.
    try:
        import asyncio as _asyncio

        _key = f"generated/{owner_sub}/{name_only}"
        _loop = _asyncio.get_running_loop()
        _store = get_blob_store()
        if await _loop.run_in_executor(None, lambda: _store.exists(_key)):
            base.mkdir(parents=True, exist_ok=True)
            data = await _loop.run_in_executor(None, lambda: _store.get(_key))
            safe_path.write_bytes(data)
            return FileResponse(safe_path, media_type=media_type, filename=name_only)
    except Exception:
        logger.exception("blob-store fallback failed for %s", name_only)

    raise HTTPException(status_code=404, detail="File not found.")
