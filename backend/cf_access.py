"""Identity-token verification.

Verifies a signed JWT put in front of this application by an identity provider,
and derives the caller's identity and groups from it. Any OIDC provider works:
the issuer, audience, JWKS URL and groups-claim location are all configuration.
The header name a particular proxy uses is one more configurable detail, not a
dependency on that proxy.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Optional

import jwt
from jwt import PyJWKClient
from starlette.datastructures import Headers

from app_config import PRODUCTION_ENV, current_env
from auth_policy import (
    AuthContext,
    canonical_app_groups,
    default_member_group,
    has_app_access,
)

logger = logging.getLogger(__name__)

JWT_HEADER_PRIMARY = "cf-access-jwt-assertion"


def _env_bool(key: str) -> bool:
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes", "on")


TEAM_DOMAIN = os.environ.get("TEAM_DOMAIN", "").strip().rstrip("/")
OIDC_ISSUER = (os.environ.get("OIDC_ISSUER", "").strip().rstrip("/") or TEAM_DOMAIN)
OIDC_JWKS_URL = os.environ.get("OIDC_JWKS_URL", "").strip()
POLICY_AUD = os.environ.get("POLICY_AUD", "").strip()

ENABLE_MOCK_AUTH = _env_bool("ENABLE_MOCK_AUTH")


def reload_auth_env_flags() -> None:
    """Re-read auth settings from os.environ after platform.env is applied.

    The setup wizard can switch from mock auth to OIDC at runtime, so the
    issuer, audience and JWKS settings must be re-read along with the mock
    flag, and the cached JWKS client dropped. Otherwise JWT validation keeps
    the import-time (empty) values and every request fails until a restart.
    """
    global ENABLE_MOCK_AUTH, TEAM_DOMAIN, OIDC_ISSUER, OIDC_JWKS_URL, POLICY_AUD
    global _resolved_jwks_uri, _jwks
    ENABLE_MOCK_AUTH = _env_bool("ENABLE_MOCK_AUTH")
    TEAM_DOMAIN = os.environ.get("TEAM_DOMAIN", "").strip().rstrip("/")
    OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/") or TEAM_DOMAIN
    OIDC_JWKS_URL = os.environ.get("OIDC_JWKS_URL", "").strip()
    POLICY_AUD = os.environ.get("POLICY_AUD", "").strip()
    _resolved_jwks_uri = None
    _jwks = None

# When true, any valid JWT (regardless of groups claim) receives member access.
# Admins still need their group in the token.
AUTH_OPEN_MEMBERSHIP = _env_bool("AUTH_OPEN_MEMBERSHIP") or _env_bool("CF_ACCESS_OPEN")

MOCK_USER_EMAIL = os.environ.get("MOCK_USER_EMAIL", "dev@localhost.local")
MOCK_USER_SUB = os.environ.get("MOCK_USER_SUB", "mock-user-sub")
_mock_groups_raw = os.environ.get(
    "MOCK_USER_GROUPS", "platform-admins,platform-users"
)
MOCK_USER_GROUPS_TUPLE = canonical_app_groups(
    [x.strip() for x in _mock_groups_raw.split(",") if x.strip()]
)

# When MOCK_USER_GROUPS is empty or typo'd, recover so local/docker dev does not brick the API.
_FALLBACK_DEV_MOCK_GROUPS: tuple[str, ...] = canonical_app_groups(
    ["platform-admins", "platform-users"]
)

PUBLIC_OPENAPI = _env_bool("PUBLIC_OPENAPI")

_jwks: Optional[PyJWKClient] = None
_resolved_jwks_uri: Optional[str] = None


class AuthConfigurationError(RuntimeError):
    pass


def assert_mock_auth_not_in_production() -> None:
    """Refuse mock authentication in production deployments."""
    if ENABLE_MOCK_AUTH and current_env() == PRODUCTION_ENV:
        raise RuntimeError(
            "ENABLE_MOCK_AUTH must not be enabled when APP_ENV=production. "
            "Remove ENABLE_MOCK_AUTH or set APP_ENV to a non-production value."
        )


def _looks_like_cloudflare_access_issuer(issuer: str) -> bool:
    low = issuer.lower()
    return "cloudflareaccess.com" in low or low.endswith("/cdn-cgi/access")


def _jwks_uri_from_oidc_discovery(issuer: str) -> Optional[str]:
    discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        from url_fetch_guard import sync_fetch_response, validate_fetch_url

        ok, err = validate_fetch_url(discovery_url)
        if not ok:
            logger.warning("OIDC discovery blocked for %s: %s", discovery_url, err)
            return None
        resp = sync_fetch_response(discovery_url, headers={}, timeout=10.0)
        resp.raise_for_status()
        body = resp.json()
        jwks_uri = body.get("jwks_uri")
        if isinstance(jwks_uri, str) and jwks_uri.strip():
            logger.info("Resolved JWKS URI from OIDC discovery at %s", discovery_url)
            return jwks_uri.strip()
    except Exception as exc:
        logger.warning("OIDC discovery failed for %s: %s", discovery_url, exc)
    return None


def resolve_jwks_uri() -> str:
    """Return the JWKS endpoint used to verify application JWTs."""
    global _resolved_jwks_uri
    if _resolved_jwks_uri:
        return _resolved_jwks_uri

    explicit_jwks = os.environ.get("OIDC_JWKS_URL", "").strip() or OIDC_JWKS_URL
    if explicit_jwks:
        _resolved_jwks_uri = explicit_jwks
        return _resolved_jwks_uri

    issuer = (
        os.environ.get("OIDC_ISSUER", "").strip().rstrip("/")
        or os.environ.get("TEAM_DOMAIN", "").strip().rstrip("/")
        or OIDC_ISSUER
        or TEAM_DOMAIN
    )
    if not issuer:
        raise AuthConfigurationError(
            "OIDC_ISSUER or TEAM_DOMAIN is required when mock auth is off"
        )

    discovered = _jwks_uri_from_oidc_discovery(issuer)
    if discovered:
        _resolved_jwks_uri = discovered
        return _resolved_jwks_uri

    if _looks_like_cloudflare_access_issuer(issuer):
        _resolved_jwks_uri = f"{issuer.rstrip('/')}/cdn-cgi/access/certs"
        return _resolved_jwks_uri

    raise AuthConfigurationError(
        "Could not resolve a JWKS URL. Set OIDC_JWKS_URL explicitly, or use an "
        "issuer that publishes /.well-known/openid-configuration."
    )


def _jwks_client() -> PyJWKClient:
    global _jwks
    if _jwks is None:
        jwks_uri = resolve_jwks_uri()
        _jwks = PyJWKClient(jwks_uri, cache_keys=True)
    return _jwks


def _extract_bearer_token(headers: Headers) -> Optional[str]:
    """Alternate carrier for the same application JWT (validated identically via JWKS)."""
    auth = headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    tok = parts[1].strip()
    return tok or None


def _find_access_jwt(headers: Headers) -> Optional[str]:
    """Cf-Access-Jwt-Assertion (edge → origin), or Authorization: Bearer (API / dev / tunnel overlays)."""
    h = headers.get(JWT_HEADER_PRIMARY)
    if isinstance(h, str) and h.strip():
        return h.strip()
    bearer = _extract_bearer_token(headers)
    return bearer


def find_access_jwt(headers: Headers) -> Optional[str]:
    return _find_access_jwt(headers)


def decode_cf_access(headers: Headers) -> dict:
    token = _find_access_jwt(headers)
    if not token:
        raise ValueError(
            "Missing identity JWT — expected header Cf-Access-Jwt-Assertion "
            "(when an edge proxy injects it) or Authorization: Bearer <JWT>. "
            "Direct http://host:port requests without a token will be rejected."
        )

    if not POLICY_AUD:
        raise AuthConfigurationError("POLICY_AUD is required when validating JWT")

    issuer = OIDC_ISSUER or TEAM_DOMAIN
    if not issuer:
        raise AuthConfigurationError("OIDC_ISSUER or TEAM_DOMAIN is required when validating JWT")

    client = _jwks_client()
    signing_key = client.get_signing_key_from_jwt(token)

    decoded = jwt.decode(
        token,
        signing_key.key,
        algorithms=("RS256",),
        audience=POLICY_AUD,
        issuer=issuer,
        leeway=60,
        options={
            "require": ["exp", "iat"],
        },
    )
    return dict(decoded)


_GROUP_NAME_HINT = re.compile(
    r"^(" + "|".join(
        re.escape(p.strip())
        for p in (os.environ.get("GROUP_NAME_PREFIXES") or "platform-").split(",")
        if p.strip()
    ) + r")",
    re.IGNORECASE,
)

_DEFAULT_GROUPS_CLAIM_KEYS = (
    "groups,roles,role,identity_groups,custom_groups,cognito:groups,"
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role,"
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/group"
)


def _configured_groups_claim_keys() -> list[str]:
    raw = os.environ.get("GROUPS_JWT_CLAIM_KEYS", _DEFAULT_GROUPS_CLAIM_KEYS).strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _groups_from_claim(payload: dict) -> tuple[str, ...]:
    collected: list[str] = []

    def _consume(item: object) -> None:
        if isinstance(item, str):
            parts = [p.strip() for p in re.split(r"[,;|]", item.strip()) if p.strip()]
            if parts:
                collected.extend(parts)
        elif isinstance(item, (list, tuple)):
            for el in item:
                _consume(el)

    for key in _configured_groups_claim_keys():
        _consume(payload.get(key))

    # Cloudflare Access often puts SAML/OIDC attributes under `custom` (object, not a string).
    custom_obj = payload.get("custom")
    if isinstance(custom_obj, dict):
        _consume(custom_obj.get("groups"))
        for key in _configured_groups_claim_keys():
            _consume(custom_obj.get(key))

    gid = payload.get("group_ids")
    if isinstance(gid, str):
        _consume(gid)
    elif isinstance(gid, (list, tuple)):
        for el in gid:
            if isinstance(el, str):
                collected.append(el.strip())

    cc = payload.get("custom_claims")
    if isinstance(cc, str) and cc.strip().startswith("{"):
        try:
            obj = json.loads(cc)
            if isinstance(obj, dict):
                for key in _configured_groups_claim_keys():
                    if key in obj:
                        _consume(obj.get(key))
        except (json.JSONDecodeError, TypeError):
            pass

    # Lists of strings whose entries look like group names used by this app
    seen: set[int] = set()
    if not collected:
        for _k, v in payload.items():
            if isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) for x in v):
                id_list = id(v)
                if id_list in seen:
                    continue
                seen.add(id_list)
                if any(_GROUP_NAME_HINT.search(x.strip() or "") for x in v):
                    for x in v:
                        if isinstance(x, str):
                            cleaned = x.strip()
                            if cleaned:
                                collected.append(cleaned)

    return canonical_app_groups(collected)


def build_auth_context(headers: Headers) -> AuthContext:
    if ENABLE_MOCK_AUTH:
        if logger.isEnabledFor(logging.WARNING):
            logger.warning(
                "ENABLE_MOCK_AUTH is on — JWT verification skipped (development only)."
            )
        groups_tuple = MOCK_USER_GROUPS_TUPLE
        if has_app_access(groups_tuple):
            use_groups = groups_tuple
        elif has_app_access(_FALLBACK_DEV_MOCK_GROUPS):
            logger.warning(
                "MOCK_USER_GROUPS is empty or names no recognised group; "
                "using the default mock groups."
            )
            use_groups = _FALLBACK_DEV_MOCK_GROUPS
        else:
            raise PermissionError(
                "Mock user's MOCK_USER_GROUPS must include at least one allowed app group"
            )
        return AuthContext(
            sub=MOCK_USER_SUB,
            email=MOCK_USER_EMAIL,
            groups=use_groups,
        )

    payload = decode_cf_access(headers)
    groups = _groups_from_claim(payload)

    sub = str(payload.get("sub") or "")
    email = str(
        payload.get("email")
        or payload.get("common_name")
        or payload.get("preferred_username")
        or ""
    )

    if not groups:
        if AUTH_OPEN_MEMBERSHIP:
            # No groups in JWT but open access is enabled — grant base user role.
            groups = canonical_app_groups([default_member_group()])
        else:
            claim_keys = ",".join(sorted(str(k) for k in payload.keys())[:48])
            logger.warning(
                "Access JWT validated but extracted zero app groups; claim keys=%s "
                "(set GROUPS_JWT_CLAIM_KEYS to match your IdP)",
                claim_keys or "(none)",
            )
            raise PermissionError(
                "JWT has no recognizable groups claim — map app groups onto the JWT or set "
                "GROUPS_JWT_CLAIM_KEYS to the claim keys your IdP uses (see backend/.env.example)."
            )
    elif not has_app_access(groups) and AUTH_OPEN_MEMBERSHIP:
        # Has groups but none are recognized — still grant base access in open mode.
        groups = canonical_app_groups([default_member_group()])
    elif not has_app_access(groups):
        raise PermissionError("User lacks a recognized application group assignment")

    return AuthContext(sub=sub or email or "unknown-sub", email=email or "", groups=groups)


def exempt_path_predicate() -> Callable[[str], bool]:
    exact = frozenset({"/api/health", "/api/slack/events", "/api/platform/config"})
    openapi = frozenset({"/openapi.json", "/docs", "/redoc"})

    def is_exempt(path: str) -> bool:
        if path in exact:
            return True
        # Internal service-to-service endpoints (e.g. Cloud Run relay)
        if path.startswith("/internal/"):
            return True
        # /apps/ routes go through normal auth middleware so request.state.auth
        # is populated; the serve_app_file handler does its own visibility check.
        if not PUBLIC_OPENAPI:
            return False
        if path in openapi:
            return True
        # OAuth redirect & static bundles under Swagger UI
        if path.startswith("/docs"):
            return True
        return False

    return is_exempt


is_exempt_path = exempt_path_predicate()


DELETE_AUTOMATION_RE = re.compile(r"^/api/automations/[^/]+$")


def rbac_violation(ctx: AuthContext, method: str, path: str) -> Optional[str]:
    """Return a human-readable rejection reason if this call should fail."""
    if method == "OPTIONS" or is_exempt_path(path):
        return None
    if method in ("HEAD", "GET"):
        return None

    if DELETE_AUTOMATION_RE.match(path) and method == "DELETE":
        if not ctx.is_admin:
            return "Deleting automations requires an administrator group membership."
        return None

    if path == "/api/memory/sync-debug" and method == "POST":
        if not ctx.is_admin:
            return "Debug memory sync requires an administrator group membership."
        return None

    if not ctx.may_mutate_http:
        return "Read-only principals cannot POST, PUT, DELETE, PATCH, etc."

    return None
