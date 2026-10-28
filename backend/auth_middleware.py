"""The authentication middleware and the auth dependencies routes use.

identity_gate authenticates every /api request from the verified identity
token (or a Lab capability header) and sets ``request.state.auth``.
get_current_user and deps_owner_sub read it back for route handlers.

Moved out of main.py so the app factory and routers can import them without
importing main.
"""

from __future__ import annotations

import logging

import jwt as pyjwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from auth_policy import AuthContext
from cf_access import (
    AuthConfigurationError,
    build_auth_context,
    is_exempt_path,
    rbac_violation,
)
from tenancy import request_owner_sub

logger = logging.getLogger(__name__)


async def identity_gate(request: Request, call_next):
    """Authenticate every /api route from the verified identity token."""
    path = request.url.path
    if request.method == "OPTIONS":
        from lab_app_api_guard import lab_cors_headers, lab_runtime_app_id

        if (
            request.headers.get("origin") == "null"
            and lab_runtime_app_id(path)
        ):
            return Response(status_code=204, headers=lab_cors_headers())
        return await call_next(request)
    if is_exempt_path(path):
        return await call_next(request)

    from lab_app_api_guard import lab_cors_headers, lab_runtime_app_id
    from lab_capability import lab_capability_header_name, verify_lab_capability

    _cap_header = request.headers.get(lab_capability_header_name())
    _runtime_app = lab_runtime_app_id(path) if path.startswith("/api/") else None
    if _cap_header:
        cap_auth = verify_lab_capability(_cap_header, _runtime_app or "")
        if cap_auth is None or not _runtime_app:
            return JSONResponse(
                status_code=403,
                content={"detail": "Lab capability is not valid for this API."},
            )
        request.state.auth = cap_auth
        response = await call_next(request)
        if request.headers.get("origin") == "null":
            for key, value in lab_cors_headers().items():
                response.headers[key] = value
        return response
    if request.headers.get("origin") == "null" and path.startswith("/api/"):
        return JSONResponse(
            status_code=403,
            content={"detail": "Lab apps cannot use the platform session."},
        )

    try:
        auth = build_auth_context(request.headers)
    except PermissionError as exc:
        logger.warning(
            "Access JWT rejected groups/policy (403): %s "
            "(if Cf-Access-Jwt-Assertion is missing you would get 401 instead)",
            str(exc),
        )
        return JSONResponse(status_code=403, content={"detail": str(exc)})
    except ValueError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    except AuthConfigurationError:
        logger.exception("JWT auth misconfiguration")
        return JSONResponse(
            status_code=500,
            content={"detail": "Authentication misconfigured"},
        )
    except pyjwt.PyJWTError as exc:
        logger.warning("JWT verification failed: %s", exc)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or expired session token"},
        )

    forbid = rbac_violation(auth, request.method, path)
    if forbid:
        return JSONResponse(status_code=403, content={"detail": forbid})

    from lab_app_api_guard import (
        is_sensitive_platform_api,
        lab_app_api_path_allowed,
        request_from_lab_app_sandbox,
        untrusted_same_origin_api_call,
    )
    from platform_client_guard import (
        attestation_enforced,
        is_valid_platform_client,
        platform_client_header_name,
    )

    _pc_header = request.headers.get(platform_client_header_name())
    if path.startswith("/api/"):
        if request_from_lab_app_sandbox(
            request.headers.get("origin"),
            request.headers.get("referer"),
        ) and not lab_app_api_path_allowed(path):
            return JSONResponse(
                status_code=403,
                content={"detail": "Lab apps may only call /api/apps endpoints."},
            )
        if untrusted_same_origin_api_call(
            path=path,
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            platform_client_header=_pc_header,
            sec_fetch_site=request.headers.get("sec-fetch-site"),
            sec_fetch_mode=request.headers.get("sec-fetch-mode"),
            method=request.method,
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "This API is not available from Lab app contexts."},
            )
        if (
            attestation_enforced()
            and is_sensitive_platform_api(path)
            and not is_valid_platform_client(_pc_header)
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Admin APIs require a platform UI attestation header."},
            )

    request.state.auth = auth
    return await call_next(request)


def get_current_user(request: Request) -> AuthContext:
    user = getattr(request.state, "auth", None)
    if not isinstance(user, AuthContext):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def deps_owner_sub(request: Request) -> str:
    """The token's `sub` claim, used for row-level tenancy."""
    return request_owner_sub(request)
