"""Slack App Configuration Token + App Manifest API helpers for per-agent Slack app auto-creation.

App creation flow
─────────────────
The platform holds a platform-level App Configuration Token (``xoxe.xoxp-``),
generated once from https://api.slack.com/apps → "Your App Configuration
Tokens" → "Generate Token".  This token is used to call
``apps.manifest.create`` on behalf of the platform — no per-user OAuth
consent screen is required for app creation.

The access token expires after 12 hours.  A single-use, non-expiring refresh
token is stored alongside it and rotated automatically via
``tooling.tokens.rotate`` before any ``apps.manifest.create`` call.  The two
tokens live in their own vault entries —
two standalone config-token vault entries — the
same secrets the staging/prod ``ExternalSecret`` manifests mirror into the
``SLACK_APP_CONFIGURATION_ACCESS_TOKEN``/``_REFRESH_TOKEN`` env vars, so the
live-read path here and the env-var fallback path always point at the same
source of truth. On rotation the refresh token is written first (it's the
single-use value — once it's saved, a reader whose access token is still
stale just re-rotates against the now-current refresh token); the access
token write follows.  Tokens are always read live (never cached in
``os.environ`` beyond the local-dev fallback below) since the refresh token
is shared across every deployment that can call auto-create — caching it
per-process for a pod's lifetime is what causes stale, already-invalidated
tokens after any other process rotates.  Falls back to plain env vars only
when ``PROJECT_ID`` is unset (local dev without the platform's creds):
  - SLACK_APP_CONFIGURATION_ACCESS_TOKEN
  - SLACK_APP_CONFIGURATION_REFRESH_TOKEN

Concurrent rotation across pods is serialized via a cross-pod Redis lock
(``_rotate_lock``) with a double-checked-read pattern — see
``rotate_and_get_config_token`` — so two pods hitting an expired token at
once don't both call ``tooling.tokens.rotate`` on the same single-use
refresh token.

Workspace installation (Leg 2)
───────────────────────────────
After ``apps.manifest.create`` returns the new app's credentials and
``oauth_authorize_url``, the user is redirected to that URL to install the
bot into their Slack workspace.  After install (or a pending-admin-approval
redirect) we exchange the code for the bot token (``xoxb-``) via
``oauth.v2.access``.

Security posture
────────────────
- No token value ever appears in a log line.  Only app IDs, error codes, and
  truncated ref paths are logged.
- All outbound calls go to canonical Slack endpoints over TLS.
- The ``state`` parameter in the install OAuth leg equals the session UUID,
  validated on the callback to prevent CSRF and cross-user injection.
- Config tokens are rotated proactively before expiry; expired tokens trigger
  an immediate rotation attempt before failing.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from agents import (
    SLACK_AVATAR_MAX_BYTES,
    SLACK_MAX_LONG_DESCRIPTION,
    normalize_slack_manifest_name,
)
from app_config import APP_NAME, RESOURCE_PREFIX

logger = logging.getLogger(__name__)

# ── Slack endpoint constants ──────────────────────────────────────────────────

SLACK_OAUTH_ACCESS_URL = "https://slack.com/api/oauth.v2.access"
SLACK_MANIFEST_CREATE_URL = "https://slack.com/api/apps.manifest.create"
SLACK_MANIFEST_UPDATE_URL = "https://slack.com/api/apps.manifest.update"
SLACK_MANIFEST_EXPORT_URL = "https://slack.com/api/apps.manifest.export"
SLACK_APPS_ICON_SET_URL = "https://slack.com/api/apps.icon.set"
SLACK_OAUTH_AUTHORIZE_BASE = "https://slack.com/oauth/v2/authorize"
TOOLING_TOKENS_ROTATE_URL = "https://slack.com/api/tooling.tokens.rotate"

_HTTP_TIMEOUT_SECS = 15.0
_SLACK_ICON_TARGET = 512
_MANUAL_APP_ICON_MSG = (
    f"This Slack app was created outside {APP_NAME}. "
    "Update the icon at api.slack.com/apps → your app → Basic Information."
)

# Bot scopes required by Phase-B runtime (kept minimal for admin approval UX).
# This is the LEGACY default used by the manual/guided path and as a fallback
# when the auto-create flow has no AI-approved scope set. The auto-create flow
# now passes an explicit, AI-reviewed ``bot_scopes`` list per agent; see
# ``scope_router.infer_scopes_for_agent`` and ``slack_scope_catalog``.
_DEFAULT_BOT_SCOPES = [
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "users:read",
    "users:read.email",
]

# Back-compat: some callers/tests read the comma-joined string.
_BOT_SCOPES = ",".join(_DEFAULT_BOT_SCOPES)

# Slack Restrict API Token Usage — optional egress IPs appended when an app
# already has an IP allowlist. Comma-separated CIDRs in SLACK_MANIFEST_IP_ALLOWLIST.
# When unset, no IPs are injected (the restriction stays off unless the app owner
# configured it themselves).
SLACK_CONFIG_COLLABORATOR = os.environ.get("SLACK_CONFIG_COLLABORATOR", "").strip()


def slack_config_collaborator() -> str:
    """Slack user that must be added as app collaborator for manifest sync."""
    return SLACK_CONFIG_COLLABORATOR


def _manifest_ip_allowlist() -> list[str]:
    raw = os.environ.get("SLACK_MANIFEST_IP_ALLOWLIST", "").strip()
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def slack_platform_tokens_configured() -> bool:
    """True when Slack App Configuration tokens are available for auto-create/sync."""
    access = os.environ.get("SLACK_APP_CONFIGURATION_ACCESS_TOKEN", "").strip()
    refresh = os.environ.get("SLACK_APP_CONFIGURATION_REFRESH_TOKEN", "").strip()
    if access and refresh:
        return True
    try:
        from secret_vault import get_vault

        vault = get_vault()
        access_name = f"{RESOURCE_PREFIX}-config-access-token"
        refresh_name = f"{RESOURCE_PREFIX}-config-refresh-token"
        access_ref = vault.latest_version_ref(access_name)
        refresh_ref = vault.latest_version_ref(refresh_name)
        return bool(access_ref and refresh_ref)
    except Exception:
        return False


# ── Error type ────────────────────────────────────────────────────────────────

class SlackOAuthError(RuntimeError):
    """Raised when a Slack API call fails.  Message never includes raw tokens."""

    def __init__(self, message: str, slack_error: Optional[str] = None):
        super().__init__(message)
        self.slack_error = slack_error  # Slack's machine-readable error code


# User-facing explanations for Slack error codes seen during auto-create.
_SLACK_OAUTH_USER_MESSAGES: dict[str, str] = {
    "missing_config_tokens": (
        "Slack auto-create is not configured on this server. "
        "The platform Slack configuration tokens are missing — contact support."
    ),
    "invalid_auth": (
        "The platform Slack configuration token is invalid or expired. "
        "Try again in a minute; if it persists, contact support."
    ),
    "token_expired": (
        "The platform Slack configuration token expired and could not be refreshed. "
        "Try again in a minute; if it persists, contact support."
    ),
    "account_inactive": "The Slack workspace tied to the platform configuration token is inactive.",
    "token_revoked": "The platform Slack configuration token was revoked. Contact support.",
    "rotation_busy": (
        "Slack token rotation is in progress on another server — wait a few seconds and try again."
    ),
    "network_error": (
        "Could not reach Slack's servers (network timeout or connection error). Try again in a moment."
    ),
    "name_taken": (
        "Slack rejected this app name — it may already be in use. Try a more distinctive agent name."
    ),
    "invalid_manifest": (
        "Slack rejected the app configuration. This may be a platform issue — contact support."
    ),
    "invalid_scopes": (
        "One or more requested Slack scopes are invalid. Remove any custom scope requests and try again."
    ),
    "app_limit_reached": (
        "Your Slack workspace has reached its app creation limit. "
        "Remove unused apps or ask a Slack admin for help."
    ),
    "missing_scope": (
        "The platform Slack configuration token lacks permission to create apps. Contact support."
    ),
    "not_allowed": (
        "Slack does not allow creating this app. Check workspace policies or contact your Slack admin."
    ),
}


def _format_manifest_validation_errors(errors: object) -> str:
    """Turn Slack manifest validation errors into a short, user-safe suffix."""
    if not isinstance(errors, list) or not errors:
        return ""
    parts: list[str] = []
    for item in errors[:3]:
        if not isinstance(item, dict):
            continue
        msg = str(item.get("msg") or item.get("message") or "").strip()
        pointer = str(item.get("pointer") or item.get("field") or "").strip()
        if msg and pointer:
            parts.append(f"{pointer}: {msg}")
        elif msg:
            parts.append(msg)
    if not parts:
        return ""
    suffix = "; ".join(parts)
    if len(errors) > 3:
        suffix += f" (+{len(errors) - 3} more)"
    return f" Details: {suffix}"


def user_message_for_manifest_create_failure(
    slack_error: str,
    manifest_errors: object = None,
) -> str:
    """Build a user-facing message for apps.manifest.create failures."""
    code = (slack_error or "unknown").strip().lower()
    base = _SLACK_OAUTH_USER_MESSAGES.get(
        code,
        f"Slack could not create the app (error: {code or 'unknown'}).",
    )
    return base + _format_manifest_validation_errors(manifest_errors)


def user_message_for_slack_oauth_error(
    exc: SlackOAuthError,
    *,
    context: str = "create_app",
) -> str:
    """Map a SlackOAuthError to text safe to return in an HTTP ``detail`` field."""
    code = (exc.slack_error or "").strip().lower()
    mapped = _SLACK_OAUTH_USER_MESSAGES.get(code)
    if mapped:
        return mapped
    msg = str(exc).strip()
    if msg:
        if context == "config_token" and code == "missing_config_tokens":
            return msg
        return msg
    if context == "config_token":
        return (
            "Slack auto-create is not configured on this deployment. "
            "Set SLACK_APP_CONFIGURATION_ACCESS_TOKEN and "
            "SLACK_APP_CONFIGURATION_REFRESH_TOKEN in the environment."
        )
    return "Failed to create the Slack app. Try again or contact support if this persists."


# ── Manifest generation ───────────────────────────────────────────────────────

def _events_request_url(app_id: str = "") -> str:
    """Return the Slack event subscription request_url for the given app_id.

    When SLACK_EVENTS_GATEWAY_URL is set (production), uses the per-app
    gateway route: {gateway_base}/slack/agent/{app_id}.
    Falls back to the monolith endpoint on the app base URL for local dev
    or when app_id is not yet known.
    """
    gateway_base = os.environ.get("SLACK_EVENTS_GATEWAY_URL", "").strip().rstrip("/")
    if gateway_base and app_id:
        return f"{gateway_base}/slack/agent/{app_id}"
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    return f"{base_url}/api/slack/events" if base_url else ""


def _build_event_subscriptions(bot_scopes: list[str], app_id: str) -> dict:
    """Build ``settings.event_subscriptions`` (bot_events + request_url) for ``bot_scopes``.

    Bot events must be consistent with the granted scopes — Slack rejects a
    manifest whose events require a scope the app doesn't have (error:
    invalid_manifest / *_event_missing_scope). app_mention only needs
    app_mentions:read (always in base); message.im needs im:history (now
    conditional, granted only when the agent handles DMs); message.channels /
    message.groups need channels:history / groups:history (both in base).
    """
    scope_set = set(bot_scopes)
    bot_events = ["app_mention", "member_joined_channel"]
    if "im:history" in scope_set:
        bot_events.append("message.im")
    if "channels:history" in scope_set:
        bot_events.append("message.channels")
    if "groups:history" in scope_set:
        bot_events.append("message.groups")
    event_subscriptions: dict = {"bot_events": bot_events}
    request_url = _events_request_url(app_id)
    if request_url:
        event_subscriptions["request_url"] = request_url
    return event_subscriptions


def ensure_allowed_ip_ranges(manifest: dict) -> dict:
    """Return a copy of an exported manifest with the platform's egress IP appended
    to ``settings.allowed_ip_address_ranges`` — but only if that allowlist is
    already present and non-empty.

    Slack's "Restrict API Token Usage" only applies when an allowlist exists.
    If one does, the platform's egress IP must be in it or every Web API call the
    platform makes with the app's bot token is refused at runtime — the agent
    registers cleanly and then silently fails to post. If no allowlist exists
    the restriction is off and nothing needs adding; writing one here would
    *enable* the restriction and lock out the app owner's own infrastructure,
    so an absent or empty list is deliberately left alone.

    Existing entries are always preserved — this only ever appends.
    """
    patched = copy.deepcopy(manifest)
    settings = patched.get("settings")
    if not isinstance(settings, dict):
        return patched
    existing = settings.get("allowed_ip_address_ranges")
    if not isinstance(existing, list) or not existing:
        return patched
    allowlist = _manifest_ip_allowlist()
    if not allowlist:
        return patched
    merged = list(existing)
    for cidr in allowlist:
        if cidr not in merged:
            merged.append(cidr)
    settings["allowed_ip_address_ranges"] = merged
    return patched


def add_event_subscriptions(manifest: dict, app_id: str, bot_scopes: list[str]) -> dict:
    """Return a copy of an *existing/exported* manifest with the platform's event
    subscriptions merged in, leaving every other field — most importantly
    ``oauth_config.scopes.bot`` — completely untouched.

    Additive by design: the app may have been configured well beyond the platform's
    canonical template (extra granted scopes, custom ``bot_events``,
    ``app_home``, its own IP allowlist). Regenerating the template from scratch
    would strip all of that, and Slack rejects a manifest that removes
    already-granted scopes (``invalid_manifest``). So bot_events are unioned
    with whatever is already there rather than replaced, and ``request_url`` is
    only written when it's missing or already points at our own events gateway
    — a URL belonging to the app's owner is never redirected.
    """
    patched = copy.deepcopy(manifest)
    event_subscriptions = patched.setdefault("settings", {}).setdefault("event_subscriptions", {})

    ours = _build_event_subscriptions(bot_scopes, app_id)
    existing_events = list(event_subscriptions.get("bot_events", []))
    for event in ours["bot_events"]:
        if event not in existing_events:
            existing_events.append(event)
    event_subscriptions["bot_events"] = existing_events

    our_url = ours.get("request_url", "")
    if our_url:
        current_url = event_subscriptions.get("request_url", "")
        gateway_base = os.environ.get("SLACK_EVENTS_GATEWAY_URL", "").strip().rstrip("/")
        app_base = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
        ours_to_claim = [b for b in (gateway_base, app_base) if b]
        if not current_url or any(current_url.startswith(b) for b in ours_to_claim):
            event_subscriptions["request_url"] = our_url
    return patched


def generate_manifest(
    agent_name: str,
    description: str = "",
    app_id: str = "",
    bot_scopes: Optional[list[str]] = None,
    include_events: bool = True,
) -> dict:
    """Build a Slack app manifest dict pre-configured for the platform agents.

    Uses HTTP Events API mode (Socket Mode disabled).  Pass ``app_id`` when
    the app already exists so the gateway-routed request_url is used; omit it
    during initial creation (the caller must call ``apps.manifest.update``
    afterwards with the real app_id to set the correct URL).

    ``bot_scopes`` is the per-agent, AI-reviewed scope list. When omitted, the
    legacy default ``_DEFAULT_BOT_SCOPES`` is used (manual/guided path & tests).

    ``include_events`` controls whether ``settings.event_subscriptions`` (and
    its ``request_url``) is included at all. The gateway-routed request_url
    can only be verified by Slack once the Agent row exists in our DB (the
    relay looks up the signing secret by app_id to answer the verification
    challenge) — so callers pushing a manifest *before* that row exists
    (guided registration's "Done" button, ahead of the final Register
    submit) must pass ``include_events=False`` to avoid an unverifiable
    request_url causing Slack to reject the whole manifest with
    ``invalid_manifest``. A follow-up push with ``include_events=True`` (once
    the Agent row is committed) adds the event subscription back.
    """
    safe_name = normalize_slack_manifest_name(agent_name)
    safe_long_desc = (description or "").strip()[:SLACK_MAX_LONG_DESCRIPTION]
    base_url = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
    scopes = list(bot_scopes) if bot_scopes else list(_DEFAULT_BOT_SCOPES)

    event_subscriptions: dict = _build_event_subscriptions(scopes, app_id) if include_events else {}
    settings: dict = {
        "interactivity": {
            "is_enabled": False,
        },
        "org_deploy_enabled": False,
        "socket_mode_enabled": False,
        "token_rotation_enabled": False,
    }
    # Restrict API Token Usage only when the operator configured CIDRs; an
    # empty list is left out so the restriction stays off.
    allowlist = _manifest_ip_allowlist()
    if allowlist:
        settings["allowed_ip_address_ranges"] = allowlist
    if event_subscriptions:
        settings["event_subscriptions"] = event_subscriptions
    return {
        "display_information": {
            "name": safe_name,
            "long_description": safe_long_desc,
        },
        "features": {
            "bot_user": {
                "display_name": safe_name,
                "always_online": False,
            },
        },
        "oauth_config": {
            "redirect_urls": (
                [f"{base_url}/api/slack-oauth/install-callback"] if base_url else []
            ),
            "scopes": {
                "bot": scopes,
            },
        },
        "settings": settings,
    }


def current_manifest_bot_scopes(manifest: dict) -> Optional[list[str]]:
    """Read the currently-granted bot scope list out of an exported manifest, if any."""
    scopes = (manifest.get("oauth_config") or {}).get("scopes", {}).get("bot")
    return list(scopes) if scopes else None


def patch_manifest_display_info(
    manifest: dict,
    agent_name: str,
    description: str = "",
    bot_scopes: Optional[list[str]] = None,
) -> dict:
    """Return a copy of an *existing, exported* Slack manifest with only the
    fields the platform is allowed to manage updated: name, description, and
    (when provided) bot scopes.

    Unlike ``generate_manifest`` (which synthesizes a manifest from scratch
    and is only faithful for apps the platform created end-to-end via
    ``auto_oauth``), this is for self-created apps whose redirect URLs, IP
    allowlist, and other event/interactivity settings were configured by the
    app's own owner directly in Slack — the platform never owned those and must
    not overwrite them just because a display field or the admin-managed
    scope list changed. Everything outside
    ``display_information``/``features.bot_user``/``oauth_config.scopes.bot``
    (plus the scope-gated entries in ``bot_events``, see below) is left
    byte-for-byte as exported.

    When ``bot_scopes`` is given, only the three scope-gated bot_events
    (``message.im``/``message.channels``/``message.groups``, gated on
    ``im:history``/``channels:history``/``groups:history`` respectively —
    Slack rejects a manifest whose events require a scope the app doesn't
    have) are added or removed to stay consistent with the new scope list.
    Any other existing bot_events entries are left untouched.
    """
    safe_name = normalize_slack_manifest_name(agent_name)
    safe_long_desc = (description or "").strip()[:SLACK_MAX_LONG_DESCRIPTION]
    patched = copy.deepcopy(manifest)
    patched.setdefault("display_information", {})["name"] = safe_name
    patched["display_information"]["long_description"] = safe_long_desc
    bot_user = patched.setdefault("features", {}).setdefault("bot_user", {})
    bot_user["display_name"] = safe_name

    if bot_scopes is not None:
        scopes = list(bot_scopes)
        scope_set = set(scopes)
        oauth_config = patched.setdefault("oauth_config", {})
        oauth_config.setdefault("scopes", {})["bot"] = scopes

        event_subscriptions = patched.setdefault("settings", {}).setdefault("event_subscriptions", {})
        bot_events = list(event_subscriptions.get("bot_events", []))
        gated_events = {
            "im:history": "message.im",
            "channels:history": "message.channels",
            "groups:history": "message.groups",
        }
        for scope, event in gated_events.items():
            has_event = event in bot_events
            needs_event = scope in scope_set
            if needs_event and not has_event:
                bot_events.append(event)
            elif not needs_event and has_event:
                bot_events.remove(event)
        event_subscriptions["bot_events"] = bot_events

    return patched


async def _update_slack_app_manifest(app_id: str, config_token: str, manifest: dict) -> None:
    """Call apps.manifest.update to patch the manifest of an existing app.

    Used immediately after creation to set the gateway-routed request_url.
    Errors are logged but not re-raised — a wrong URL is recoverable via
    a follow-up update; failing here should not abort agent registration.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
        resp = await client.post(
            SLACK_MANIFEST_UPDATE_URL,
            headers={"Authorization": f"Bearer {config_token}"},
            json={"app_id": app_id, "manifest": manifest},
        )
    try:
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            logger.error(
                "apps.manifest.update failed for app_id=%s: error=%s errors=%s",
                app_id, data.get("error", "unknown"), data.get("errors"),
            )
        else:
            logger.info("apps.manifest.update succeeded for app_id=%s", app_id)
    except Exception:
        logger.exception("apps.manifest.update request error for app_id=%s", app_id)


async def update_slack_app_manifest_strict(app_id: str, config_token: str, manifest: dict) -> None:
    """Like ``_update_slack_app_manifest`` but raises ``SlackOAuthError`` on failure."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
        resp = await client.post(
            SLACK_MANIFEST_UPDATE_URL,
            headers={"Authorization": f"Bearer {config_token}"},
            json={"app_id": app_id, "manifest": manifest},
        )
    try:
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            err = data.get("error", "unknown")
            logger.error(
                "apps.manifest.update failed for app_id=%s: error=%s errors=%s",
                app_id, err, data.get("errors"),
            )
            raise SlackOAuthError("Could not update Slack app name.", slack_error=err)
        logger.info("apps.manifest.update succeeded for app_id=%s", app_id)
    except SlackOAuthError:
        raise
    except Exception as exc:
        logger.exception("apps.manifest.update request error for app_id=%s", app_id)
        raise SlackOAuthError("Could not update Slack app name.") from exc


_CONFIG_TOKEN_EXPIRY_ERRORS = frozenset({"token_expired", "invalid_auth", "account_inactive", "token_revoked"})


async def export_slack_app_manifest(app_id: str, config_token: str) -> dict:
    """Call apps.manifest.export and return the exported manifest dict.

    Requires the config token's owner to be an admin/collaborator on the
    target app — used both for the bot-scopes backfill and to verify a
    self-created app has granted the platform's service account access.
    Retries once with a rotated token if the passed-in ``config_token`` has
    expired, mirroring ``set_slack_app_icon`` — otherwise a stale platform
    token surfaces as a false "not verified yet" to the end user regardless
    of whether they actually added the collaborator.
    Raises ``SlackOAuthError`` on failure (never logs ``config_token``).
    """
    token = config_token
    for attempt in range(2):
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
            resp = await client.post(
                SLACK_MANIFEST_EXPORT_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"app_id": app_id},
            )
        try:
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.exception("apps.manifest.export request error for app_id=%s", app_id)
            raise SlackOAuthError("Could not export Slack app manifest.") from exc

        if data.get("ok"):
            logger.info("apps.manifest.export succeeded for app_id=%s", app_id)
            return data.get("manifest", {})

        err = data.get("error", "unknown")
        if err in _CONFIG_TOKEN_EXPIRY_ERRORS and attempt == 0:
            logger.warning("apps.manifest.export got %s for app_id=%s; rotating token and retrying", err, app_id)
            token = await rotate_and_get_config_token(known_bad_access=token)
            continue

        logger.error("apps.manifest.export failed for app_id=%s: error=%s", app_id, err)
        raise SlackOAuthError("Could not export Slack app manifest.", slack_error=err)


async def verify_config_token_app_access(app_id: str) -> tuple[bool, Optional[str], Optional[list[str]]]:
    """Check whether the platform's App Configuration Token can manage ``app_id``.

    For self-created apps this is only true once the org's service account
    (serviceaccount) has been added as a collaborator on the app in
    Slack. Any failure is treated uniformly as "not verified yet" — Slack's
    exact error code for "not a collaborator" isn't distinguished from other
    failures (e.g. a mistyped app_id), so the raw error is logged for
    debugging but not used to vary the user-facing outcome.

    Returns ``(verified, slack_error, current_bot_scopes)``. The third value
    is read straight from the export this call already has to make — it's
    the app's *real* currently-granted scope list, used by callers to record
    an accurate ``agent.bot_scopes`` for a self-created app the first time
    it's verified, instead of leaving that column NULL (which the scopes UI
    silently displays as a generic default that has nothing to do with what
    the app actually has).
    """
    try:
        token = await load_and_rotate_config_token()
        manifest = await export_slack_app_manifest(app_id, token)
        return True, None, current_manifest_bot_scopes(manifest)
    except SlackOAuthError as exc:
        logger.info("verify_config_token_app_access: not yet verified for app_id=%s (error=%s)", app_id, exc.slack_error)
        return False, exc.slack_error, None


_AGENT_FRAME_PATH = Path(__file__).resolve().parent / "agent_frame.png"


def _load_agent_frame(size: int):
    """Load the platform agent frame, resized to ``size``, using its native alpha channel."""
    from PIL import Image

    frame = Image.open(_AGENT_FRAME_PATH).convert("RGBA")
    if frame.size != (size, size):
        frame = frame.resize((size, size), Image.Resampling.LANCZOS)
    return frame


def resize_avatar_for_slack(data: bytes) -> tuple[bytes, str, str]:
    """Letterbox/resize an image to 512×512 PNG for Slack ``apps.icon.set``.

    If ``agent_frame.png`` exists in the backend directory the platform frame is
    composited over the user photo before upload.
    """
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    w, h = img.size
    side = max(w, h, 1)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    if side != _SLACK_ICON_TARGET:
        canvas = canvas.resize((_SLACK_ICON_TARGET, _SLACK_ICON_TARGET), Image.Resampling.LANCZOS)

    # Composite the platform frame over the user image.
    if _AGENT_FRAME_PATH.exists():
        try:
            frame = _load_agent_frame(_SLACK_ICON_TARGET)
            canvas = Image.alpha_composite(canvas, frame)
            logger.info("Applied platform frame to Slack app icon")
        except Exception:
            logger.warning("agent_frame.png could not be applied; uploading without frame")
    else:
        logger.warning("agent_frame.png missing at %s; uploading without frame", _AGENT_FRAME_PATH)

    rgb = Image.new("RGB", canvas.size, (255, 255, 255))
    rgb.paste(canvas, mask=canvas.split()[3] if canvas.mode == "RGBA" else None)
    out = io.BytesIO()
    rgb.save(out, format="PNG")
    return out.getvalue(), "image/png", "avatar.png"


_ICON_SET_USER_ERRORS: dict[str, str] = {
    "invalid_icon_size": "Icon must be between 512×512 and 2000×2000 pixels.",
    "error_bad_format": "Icon must be a valid image file (JPEG, PNG, GIF, or WebP).",
    "icon_not_accessible": "The icon URL is not accessible.",
    "unable_to_open_file": "Could not read the uploaded image file.",
}
_AVATAR_MAX_BYTES = SLACK_AVATAR_MAX_BYTES
_AVATAR_MAGIC: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
]
_ICON_TRANSIENT_ERRORS = frozenset({
    "invalid_app",
    "app_not_found",
    "internal_error",
    "service_unavailable",
    "fatal_error",
    "ratelimited",
})
_ICON_RETRY_DELAYS_SECS = (0.8, 1.6, 3.2)


def _detect_avatar_bytes(data: bytes) -> Optional[str]:
    for magic, mime in _AVATAR_MAGIC:
        if data.startswith(magic):
            if mime == "image/webp" and (len(data) < 12 or data[8:12] != b"WEBP"):
                continue
            return mime
    return None


def decode_agent_avatar_base64(avatar_base64: str) -> bytes:
    """Decode a user-supplied avatar (raw base64 or data-URL) and validate type/size."""
    raw_b64 = avatar_base64.strip()
    if raw_b64.lower().startswith("data:") and "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        data = base64.b64decode(raw_b64, validate=False)
    except Exception as exc:
        raise SlackOAuthError("Could not read the uploaded image file.") from exc
    if not data:
        raise SlackOAuthError("Could not read the uploaded image file.")
    if len(data) > _AVATAR_MAX_BYTES:
        size_mb = len(data) / (1024 * 1024)
        raise SlackOAuthError(
            f"This image is {size_mb:.1f} MB. Slack app icons must be 2 MB or smaller. "
            "Compress the image or choose a smaller file, then upload again."
        )
    detected = _detect_avatar_bytes(data)
    if not detected:
        raise SlackOAuthError(
            "Unsupported image type. Upload a JPEG, PNG, GIF, or WebP file up to 2 MB."
        )
    return data


async def apply_framed_icon_to_slack_app(app_id: str, image_bytes: bytes) -> None:
    """Letterbox, apply the platform frame, and upload the icon to Slack."""
    png_bytes, content_type, filename = resize_avatar_for_slack(image_bytes)
    await set_slack_app_icon(app_id, png_bytes, filename, content_type)


async def set_slack_app_icon(app_id: str, image_bytes: bytes, filename: str, content_type: str) -> None:
    """Upload an app icon to Slack via ``apps.icon.set``.

    Slack is often not ready for icon updates in the first second after
    ``apps.manifest.create``, so transient errors are retried with backoff.
    """
    _EXPIRY_ERRORS = frozenset({"token_expired", "invalid_auth", "account_inactive", "token_revoked"})
    _MANUAL_ERRORS = frozenset({
        "not_allowed",
        "missing_scope",
        "invalid_app_id",
        "cannot_update_app",
    })

    async def _call(token: str, file_field: str) -> dict:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
            resp = await client.post(
                SLACK_APPS_ICON_SET_URL,
                headers={"Authorization": f"Bearer {token}"},
                data={"app_id": app_id},
                files={file_field: (filename, image_bytes, content_type)},
            )
        resp.raise_for_status()
        return resp.json()

    token = await load_and_rotate_config_token()
    file_field = "file"
    data: dict = {}
    last_err = "unknown"

    for attempt, delay in enumerate((0.0, *_ICON_RETRY_DELAYS_SECS)):
        if delay:
            await asyncio.sleep(delay)
        try:
            data = await _call(token, file_field)
        except Exception as exc:
            logger.exception("apps.icon.set request error for app_id=%s", app_id)
            raise SlackOAuthError("Could not upload icon to Slack.") from exc

        if data.get("ok"):
            logger.info("apps.icon.set succeeded for app_id=%s", app_id)
            return

        last_err = data.get("error", "unknown")
        if last_err in _EXPIRY_ERRORS:
            try:
                token = await rotate_and_get_config_token(known_bad_access=token)
            except SlackOAuthError:
                raise
            except Exception as exc:
                logger.exception("apps.icon.set retry-after-rotate failed for app_id=%s", app_id)
                raise SlackOAuthError("Could not upload icon to Slack.") from exc
            continue
        if last_err == "missing_arguments" and file_field == "file":
            file_field = "image"
            continue
        if last_err in _ICON_TRANSIENT_ERRORS and attempt < len(_ICON_RETRY_DELAYS_SECS):
            logger.warning(
                "apps.icon.set transient error=%s for app_id=%s; retrying",
                last_err, app_id,
            )
            continue
        break

    if last_err in _MANUAL_ERRORS:
        raise SlackOAuthError(_MANUAL_APP_ICON_MSG, slack_error=last_err)
    msg = _ICON_SET_USER_ERRORS.get(last_err, "Could not upload icon to Slack.")
    raise SlackOAuthError(msg, slack_error=last_err)


# ── Platform App Configuration Token helpers ──────────────────────────────────

_CONFIG_ACCESS_TOKEN_SECRET_NAME = f"{RESOURCE_PREFIX}-config-access-token"
_CONFIG_REFRESH_TOKEN_SECRET_NAME = f"{RESOURCE_PREFIX}-config-refresh-token"

# Cross-pod lock serializing the actual tooling.tokens.rotate call (the
# refresh token is single-use, so two pods must never race to rotate it).
_ROTATE_LOCK_KEY = "slack:config-token-rotate"
_ROTATE_LOCK_TTL_SECONDS = 8
_ROTATE_LOCK_WAIT_ATTEMPTS = 20
_ROTATE_LOCK_WAIT_INTERVAL_SECONDS = 0.25

_REDIS_HOST = os.environ.get("REDIS_HOST", "").strip()
_REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379") or 6379)
_REDIS_DB = int(os.environ.get("REDIS_DB", "0") or 0)

# Process-local fallback lock, used when Redis is unconfigured or unreachable.
_LOCAL_ROTATE_LOCK = asyncio.Lock()

# Atomic compare-and-delete: only remove the lock if it still holds the value
# this process set when acquiring it — prevents a slow rotation (running past
# the lock TTL) from deleting a different process's newer lock.
_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class _RotateLockUnavailable(Exception):
    """Raised internally when the rotation lock could not be acquired in time."""


def _load_raw_config_tokens_from_env() -> tuple[str, str]:
    access = os.environ.get("SLACK_APP_CONFIGURATION_ACCESS_TOKEN", "").strip()
    refresh = os.environ.get("SLACK_APP_CONFIGURATION_REFRESH_TOKEN", "").strip()
    if not access or not refresh:
        raise SlackOAuthError(
            "SLACK_APP_CONFIGURATION_ACCESS_TOKEN and SLACK_APP_CONFIGURATION_REFRESH_TOKEN "
            "must both be set to use auto-create.",
            slack_error="missing_config_tokens",
        )
    return access, refresh


async def _load_raw_config_tokens() -> tuple[str, str]:
    """Return ``(access_token, refresh_token)``, always read live.

    Read from the vault on every call rather than cached at startup, so this
    process always sees whatever another replica — or a manual rotation — wrote
    most recently, instead of a copy baked in when it booted.

    Falls back to environment variables when the vault has no entry or cannot be
    read, which is what keeps local development working without any credential
    store configured. Never logs token values.

    Raises ``SlackOAuthError`` if neither source yields both tokens.
    """
    try:
        from secret_vault import get_vault

        loop = asyncio.get_running_loop()
        vault = get_vault()

        def _read(name: str) -> str:
            return str(vault.read(vault.latest_version_ref(name))).strip()

        access = await loop.run_in_executor(None, _read, _CONFIG_ACCESS_TOKEN_SECRET_NAME)
        refresh = await loop.run_in_executor(None, _read, _CONFIG_REFRESH_TOKEN_SECRET_NAME)
        if not access or not refresh:
            raise SlackOAuthError(
                f"{_CONFIG_ACCESS_TOKEN_SECRET_NAME}/{_CONFIG_REFRESH_TOKEN_SECRET_NAME} "
                "must both resolve to a non-empty value",
                slack_error="missing_config_tokens",
            )
        return access, refresh
    except SlackOAuthError:
        raise
    except Exception:
        logger.warning(
            "Could not read %s/%s from the vault — falling back to environment variables",
            _CONFIG_ACCESS_TOKEN_SECRET_NAME,
            _CONFIG_REFRESH_TOKEN_SECRET_NAME,
            exc_info=True,
        )
        return _load_raw_config_tokens_from_env()


def _get_platform_bot_token() -> Optional[str]:
    """Best-effort platform bot token lookup for critical-alert Slack posts.

    Mirrors ops_alerts.py::_get_platform_bot_token — kept as its own small
    copy here rather than a shared import, consistent with how each
    alerting call site in this codebase already keeps its own minimal
    helper (see also agent_failure_alerts.py).
    """
    try:
        from agent_channel_configs import get_platform_agent
        from secret_vault import SecretVaultError, get_vault

        agent = get_platform_agent()
        if not agent or not agent.xoxb_secret_ref:
            return None
        try:
            return get_vault().read(agent.xoxb_secret_ref)
        except SecretVaultError:
            return None
    except Exception:
        logger.warning("slack_oauth: could not resolve platform bot token for alert", exc_info=True)
        return None


def _alert_persist_failure(exc: Exception) -> None:
    """Best-effort ops alert when a post-rotation vault write fails.

    The Slack refresh token is single-use — if tooling.tokens.rotate already
    succeeded but this write fails, the new tokens exist only in this
    process's memory and the old refresh token is already burned at Slack's
    end. This is a "wake a human up" situation, not just a log line.
    """
    channel = os.environ.get("OPS_ALERTS_SLACK_CHANNEL_ID", "").strip()
    if not channel:
        return
    token = _get_platform_bot_token()
    if not token:
        return
    try:
        from slack_sdk import WebClient

        text = (
            ":rotating_light: *CRITICAL*: Slack App Configuration token rotation "
            "succeeded at Slack but writing the new tokens to the vault "
            f"(`{_CONFIG_REFRESH_TOKEN_SECRET_NAME}`) failed: `{exc}`. The old refresh "
            "token is already burned — the new tokens exist only in this "
            "process's memory until this is resolved."
        )
        WebClient(token=token).chat_postMessage(channel=channel, text=text)
    except Exception:
        logger.warning("slack_oauth: failed to post critical persistence-failure alert", exc_info=True)


async def _persist_config_tokens(new_access: str, new_refresh: str) -> None:
    """Write rotated tokens to the two standalone vault entries.

    Writes the refresh token first: it's the single-use value,
    so persisting it is the critical step — once it's saved, a subsequent
    process can always recover (its stale access token will just fail with
    ``token_expired`` and trigger another rotation against the now-current
    refresh token). The access token write follows and is comparatively low
    stakes since Slack's next 12-hour-expiry cycle would replace it anyway.

    Falls back to a warning if the vault is unavailable (e.g. local dev), so
    the in-process update still takes effect for the current run. A failed
    refresh-token write is a critical failure — the refresh token is single-use
    and already burned at Slack — so it logs at CRITICAL and fires an alert.
    """
    try:
        from secret_vault import get_vault

        loop = asyncio.get_running_loop()
        vault = get_vault()
        await loop.run_in_executor(None, vault.write, _CONFIG_REFRESH_TOKEN_SECRET_NAME, new_refresh)
    except Exception as exc:
        logger.critical(
            "Failed to persist rotated Slack refresh token to the vault after a "
            "successful Slack-side rotation — the old refresh token is already burned "
            "and the new one exists only in this process's memory",
            exc_info=True,
        )
        _alert_persist_failure(exc)
        return

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, vault.write, _CONFIG_ACCESS_TOKEN_SECRET_NAME, new_access)
        logger.info("Persisted rotated Slack config tokens to the vault")
    except Exception:
        logger.error(
            "Persisted the rotated refresh token but failed to persist the new access "
            "token — next call will see token_expired and re-rotate, which is safe "
            "since the refresh token is already up to date",
            exc_info=True,
        )


async def _rotate_config_token(refresh_token: str) -> tuple[str, str]:
    """Call tooling.tokens.rotate and return (new_access_token, new_refresh_token).

    Updates the in-process environment variables (needed for the local-dev
    fallback path where there is no vault to read live), and
    persists both tokens to their respective vault entries
    (refresh token first — see ``_persist_config_tokens``) so they survive
    pod restarts and are immediately visible to every other pod. Never logs
    values. Callers MUST hold the cross-pod rotation lock before calling
    this — see rotate_and_get_config_token.
    """
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
            resp = await client.post(
                TOOLING_TOKENS_ROTATE_URL,
                data={"refresh_token": refresh_token},
            )
        resp.raise_for_status()
        data = resp.json()
    except SlackOAuthError:
        raise
    except Exception as exc:
        logger.exception("tooling.tokens.rotate request error")
        raise SlackOAuthError("Config token rotation failed") from exc
    if not data.get("ok"):
        err = data.get("error", "unknown")
        logger.error("tooling.tokens.rotate failed: error=%s", err)
        raise SlackOAuthError("Config token rotation failed", slack_error=err)

    new_access = data.get("token", "")
    new_refresh = data.get("refresh_token", "")
    if not new_access or not new_refresh:
        raise SlackOAuthError("tooling.tokens.rotate returned incomplete token data")

    # Update in-process env so the local-dev (no vault) fallback path
    # sees the new tokens too.
    os.environ["SLACK_APP_CONFIGURATION_ACCESS_TOKEN"] = new_access
    os.environ["SLACK_APP_CONFIGURATION_REFRESH_TOKEN"] = new_refresh

    # Persist to the vault so tokens survive pod restarts and are
    # immediately visible to every other pod's next live read.
    await _persist_config_tokens(new_access, new_refresh)

    logger.info("Rotated Slack app configuration token successfully")
    return new_access, new_refresh


@asynccontextmanager
async def _rotate_lock():
    """Cross-pod lock serializing config-token rotation.

    Uses a Redis ``SET key uuid4 NX EX ttl`` acquire (busy-waited with
    asyncio.sleep) and releases via an atomic Lua compare-and-delete, so a
    process can only ever remove the lock it itself holds — a rotation that
    runs past the TTL can never delete a different process's newer lock
    (unlike a plain get-then-delete, which has a TOCTOU gap between the two
    round trips).

    Falls back to a process-local asyncio.Lock when REDIS_HOST is unset or
    Redis is unreachable, logging a warning. Raises _RotateLockUnavailable if
    the busy-wait is exhausted without acquiring either lock.
    """
    token = str(uuid.uuid4())
    redis_client = None
    acquired_redis = False

    if _REDIS_HOST:
        try:
            import redis.asyncio as aioredis
            from redis.exceptions import RedisError

            redis_client = aioredis.Redis(
                host=_REDIS_HOST,
                port=_REDIS_PORT,
                db=_REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            for _ in range(_ROTATE_LOCK_WAIT_ATTEMPTS):
                if await redis_client.set(_ROTATE_LOCK_KEY, token, nx=True, ex=_ROTATE_LOCK_TTL_SECONDS):
                    acquired_redis = True
                    break
                await asyncio.sleep(_ROTATE_LOCK_WAIT_INTERVAL_SECONDS)
        except (RedisError, OSError):
            logger.warning(
                "slack_oauth: Redis rotation lock unavailable — falling back to local lock",
                exc_info=True,
            )
            redis_client = None
            acquired_redis = False

    if redis_client is not None and acquired_redis:
        try:
            yield
        finally:
            try:
                await redis_client.eval(_RELEASE_LOCK_LUA, 1, _ROTATE_LOCK_KEY, token)
            except Exception:
                logger.warning("slack_oauth: failed to release Redis rotation lock cleanly", exc_info=True)
            try:
                await redis_client.aclose()
            except Exception:
                pass
        return

    if redis_client is not None and not acquired_redis:
        try:
            await redis_client.aclose()
        except Exception:
            pass
        raise _RotateLockUnavailable("Timed out waiting for the Slack config-token rotation lock")

    # Redis unconfigured or unreachable — process-local fallback.
    try:
        await asyncio.wait_for(_LOCAL_ROTATE_LOCK.acquire(), timeout=_ROTATE_LOCK_TTL_SECONDS)
    except asyncio.TimeoutError:
        raise _RotateLockUnavailable("Timed out waiting for the local Slack config-token rotation lock")
    try:
        yield
    finally:
        _LOCAL_ROTATE_LOCK.release()


async def load_and_rotate_config_token() -> str:
    """Return a valid App Configuration access token, rotating if necessary.

    Flow:
    1. Load current access + refresh tokens live from the vault (or env
       in local dev).
    2. Attempt apps.manifest.validate (lightweight ping) — omitted for
       simplicity; instead we attempt the real call and rotate on
       token_expired / invalid_auth errors in create_slack_app.
    3. Return the current access token; create_slack_app will call
       rotate_and_get_config_token if the token is expired.
    """
    access, _refresh = await _load_raw_config_tokens()
    return access


async def rotate_and_get_config_token(known_bad_access: Optional[str] = None) -> str:
    """Return a valid App Configuration access token, rotating only if needed.

    ``known_bad_access`` should be the access token the caller just tried and
    got an expiry-class error for. This implements a double-checked-locking
    pattern so concurrent pods never race to rotate the same single-use
    refresh token:

    1. Check 1 (cheap, no lock): re-read live. If another pod already
       rotated since the caller's failed attempt, the live access token will
       differ from ``known_bad_access`` — use it and skip Slack entirely.
    2. Otherwise acquire the cross-pod rotation lock.
    3. Check 2 (inside the lock — the actual double-check): re-read live
       again. Another pod may have rotated while this one waited for the
       lock. If so, use the fresh value, release the lock, and skip the
       Slack rotate call.
    4. Only if the token is *still* stale does this process call
       tooling.tokens.rotate and persist the result.

    If the lock can't be acquired within the busy-wait window, does one
    final live re-read; if the token is still stale, raises a retryable
    SlackOAuthError (slack_error="rotation_busy") rather than proceeding to
    rotate un-serialized.
    """

    def _still_bad(access: str) -> bool:
        return known_bad_access is None or access == known_bad_access

    access, refresh = await _load_raw_config_tokens()
    if not _still_bad(access):
        return access

    try:
        async with _rotate_lock():
            access, refresh = await _load_raw_config_tokens()
            if not _still_bad(access):
                return access
            new_access, _ = await _rotate_config_token(refresh)
            return new_access
    except _RotateLockUnavailable:
        access, _refresh = await _load_raw_config_tokens()
        if not _still_bad(access):
            return access
        raise SlackOAuthError(
            "Slack config token rotation is busy — try again shortly.",
            slack_error="rotation_busy",
        )


# ── App Manifest API ──────────────────────────────────────────────────────────

async def create_slack_app(config_token: str, manifest: dict) -> dict:
    """Create a new Slack app from a manifest using the platform App Configuration Token.

    Returns a dict with keys:
        app_id, client_id, client_secret, signing_secret,
        verification_token, oauth_authorize_url.

    The ``oauth_authorize_url`` is Slack's pre-built URL for installing the new
    app into a workspace — use it directly for the workspace install step.

    If the token is expired, rotates automatically and retries once.
    Never logs ``config_token`` or returned secret values.
    """
    _EXPIRY_ERRORS = frozenset({"token_expired", "invalid_auth", "account_inactive", "token_revoked"})

    async def _call(token: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
                resp = await client.post(
                    SLACK_MANIFEST_CREATE_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    json={"manifest": manifest},
                )
            resp.raise_for_status()
            return resp.json()
        except SlackOAuthError:
            raise
        except Exception as exc:
            logger.exception("Slack apps.manifest.create request error")
            raise SlackOAuthError(
                "Could not reach Slack to create the app — the request timed out or the network failed.",
                slack_error="network_error",
            ) from exc

    try:
        data = await _call(config_token)
    except SlackOAuthError:
        raise

    if not data.get("ok"):
        err = data.get("error", "unknown")
        if err in _EXPIRY_ERRORS:
            # Token expired — rotate and retry once.
            logger.info("Config token expired (error=%s); rotating and retrying", err)
            new_token = await rotate_and_get_config_token(known_bad_access=config_token)
            data = await _call(new_token)

    if not data.get("ok"):
        err = data.get("error", "unknown")
        logger.error(
            "Slack apps.manifest.create failed: error=%s errors=%s",
            err,
            data.get("errors"),
        )
        raise SlackOAuthError(
            user_message_for_manifest_create_failure(err, data.get("errors")),
            slack_error=err,
        )

    creds = data.get("credentials") or {}
    app_id = data.get("app_id", "")

    # Immediately update the manifest with the correct per-app gateway URL.
    # The initial create used an empty app_id so the gateway route wasn't set.
    gateway_url = _events_request_url(app_id)
    initial_url = _events_request_url("")
    if app_id and gateway_url and gateway_url != initial_url:
        import copy as _copy
        updated_manifest = _copy.deepcopy(manifest)
        updated_manifest.setdefault("settings", {}).setdefault(
            "event_subscriptions", {}
        )["request_url"] = gateway_url
        await _update_slack_app_manifest(app_id, config_token, updated_manifest)

    return {
        "app_id": app_id,
        "client_id": creds.get("client_id", ""),
        "client_secret": creds.get("client_secret", ""),
        "signing_secret": creds.get("signing_secret", ""),
        "verification_token": creds.get("verification_token", ""),
        "oauth_authorize_url": data.get("oauth_authorize_url", ""),
    }


# ── User Integration OAuth helpers ───────────────────────────────────────────

# User token scopes needed for personal Slack context (search, channels, DMs).
_USER_INTEGRATION_SCOPES = " ".join([
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "im:read",
    "im:history",
    "mpim:read",
    "mpim:history",
    "search:read",
    "users:read",
    "files:read",
    "team:read",
])


def build_user_integration_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Return the Slack OAuth URL that grants user token scopes for personal context.

    The resulting ``xoxp-`` token is stored as SLACK_USER_TOKEN and injected into
    run_python sandboxes so agents can read the user's channels, DMs, and messages.
    """
    params = {
        "client_id": client_id,
        "user_scope": _USER_INTEGRATION_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{SLACK_OAUTH_AUTHORIZE_BASE}?{urlencode(params)}"


async def exchange_user_integration_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> str:
    """Exchange a user-integration auth code for a personal user token (``xoxp-``).

    Calls ``oauth.v2.access`` and extracts ``authed_user.access_token``.
    Never logs the returned token.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
        resp = await client.post(
            SLACK_OAUTH_ACCESS_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        err = data.get("error", "unknown")
        logger.error("Slack oauth.v2.access (user integration) failed: error=%s", err)
        raise SlackOAuthError("User integration token exchange failed", slack_error=err)

    token = (data.get("authed_user") or {}).get("access_token", "")
    if not token:
        logger.error("Slack user integration OAuth response missing authed_user.access_token")
        raise SlackOAuthError("User integration OAuth response contained no user token")
    return token


# ── Leg 2 — Install OAuth helpers ────────────────────────────────────────────

def build_install_auth_url(
    new_app_client_id: str,
    redirect_uri: str,
    state: str,
    bot_scopes: Optional[list[str]] = None,
) -> str:
    """Construct the OAuth install URL for the newly created app.

    For workspace admins this completes immediately; for non-admins Slack shows
    a "Request to install" UI and redirects back with ``error=access_denied``.

    ``bot_scopes`` must match the scopes embedded in the app manifest; pass the
    same AI-reviewed list used by ``generate_manifest``. Defaults to the legacy
    ``_DEFAULT_BOT_SCOPES`` when omitted.
    """
    scope_csv = ",".join(bot_scopes) if bot_scopes else _BOT_SCOPES
    params = {
        "client_id": new_app_client_id,
        "scope": scope_csv,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{SLACK_OAUTH_AUTHORIZE_BASE}?{urlencode(params)}"


async def exchange_install_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    """Exchange Leg-2 auth code for a bot token and workspace info.

    Returns ``{"xoxb": ..., "team_id": ..., "bot_user_id": ...}``.
    Never logs ``xoxb`` or ``client_secret``.
    """
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
        resp = await client.post(
            SLACK_OAUTH_ACCESS_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        err = data.get("error", "unknown")
        logger.error("Slack oauth.v2.access (install leg) failed: error=%s", err)
        raise SlackOAuthError("Install token exchange failed", slack_error=err)

    access_token = data.get("access_token", "")
    team = data.get("team") or {}
    return {
        "xoxb": access_token,
        "team_id": team.get("id", ""),
        "bot_user_id": data.get("bot_user_id", ""),
    }
