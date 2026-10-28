"""Build the FastAPI application.

create_app() wires the parts every deployment shares: the validation error
format, the routers, CORS and the identity middleware. main.py calls it and
then adds the routes it still defines inline (chat, automations, files), which
move into routers one domain at a time.

Tests can build an app without importing main:

    app = create_app()
    app.dependency_overrides[get_db] = lambda: test_session
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app_config import APP_NAME

_DEV_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default 422 body is {"detail": [{"loc": [...], "msg": ..., "type": ...}, ...]} —
    # a list of dicts. Frontend code does `new Error(body.detail)`, which stringifies a list of
    # objects to "[object Object]". Flatten to a single human-readable string here instead.
    messages = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        msg = err.get("msg", "Invalid value")
        messages.append(f"{loc}: {msg}" if loc else msg)
    return JSONResponse(status_code=422, content={"detail": "; ".join(messages) or "Invalid request."})


def _routers() -> list:
    """Every router, in the order they are included.

    Imported here rather than at module level so importing app_factory stays
    cheap and does not pull in every domain.
    """
    from api_agents import router as agents_router
    from api_slack_oauth import router as slack_oauth_router
    from api_slack_user_oauth import router as slack_user_oauth_router
    from api_google_user_oauth import router as google_user_oauth_router
    from api_slack_events import router as slack_events_router
    from api_internal import router as internal_router
    from api_access import router as access_router
    from api_agent_access import router as agent_access_router
    from api_agent_skill_access import router as agent_skill_access_router
    from api_agent_roles import router as agent_roles_router
    from api_skill_publish import router as skill_publish_router
    from api_skill_artifacts import router as skill_artifacts_router
    from api_skill_artifacts_suggest import router as skill_artifacts_suggest_router
    from api_skill_artifact_plan import router as skill_artifact_plan_router
    from api_skill_ai_edit import router as skill_ai_edit_router
    from api_skill_create import router as skill_create_router
    from api_user_secrets import router as user_secrets_router
    from api_user_models import router as user_models_router
    from api_apps import router as apps_router
    from api_infra import router as infra_router
    from api_systems import router as systems_router
    from api_usage import router as usage_router
    from api_analytics import router as analytics_router
    from event_log import router as events_router
    from api_slack_apps import router as slack_apps_router
    from api_channel_configs import router as channel_configs_router
    from api_home import router as home_router
    from api_workflows import router as workflows_router
    from api_proposals import router as proposals_router
    from api_agent_knowledge_base import router as agent_kb_router
    from api_flows import router as flows_router, flow_runs_router
    from api_platform import router as platform_router
    from api_setup import router as setup_router
    from api_memory import router as memory_router
    from api_feedback import router as feedback_router
    from api_agent_map import router as agent_map_router

    return [
        platform_router,
        setup_router,
        agents_router,
        slack_oauth_router,
        slack_user_oauth_router,
        google_user_oauth_router,
        slack_events_router,
        internal_router,
        access_router,
        agent_access_router,
        agent_skill_access_router,
        agent_roles_router,
        infra_router,
        systems_router,
        usage_router,
        skill_publish_router,
        skill_artifacts_router,
        skill_artifacts_suggest_router,
        skill_artifact_plan_router,
        skill_ai_edit_router,
        skill_create_router,
        user_secrets_router,
        user_models_router,
        apps_router,
        slack_apps_router,
        channel_configs_router,
        home_router,
        analytics_router,
        events_router,
        workflows_router,
        proposals_router,
        agent_kb_router,
        flows_router,
        flow_runs_router,
        memory_router,
        feedback_router,
        agent_map_router,
    ]


def create_app(*, lifespan: Optional[Callable[[FastAPI], Any]] = None) -> FastAPI:
    """A configured application with every router and middleware attached.

    ``lifespan`` is main.py's startup/shutdown (scheduler, workers). Tests
    usually leave it out so building the app starts nothing.
    """
    from auth_middleware import identity_gate

    app = FastAPI(title=f"{APP_NAME} API", lifespan=lifespan)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    for router in _routers():
        app.include_router(router)

    # Middleware added last runs first: identity_gate wraps CORS, as before.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(identity_gate)

    import metrics

    if metrics.enabled():
        app.add_api_route("/metrics", metrics.metrics_endpoint, include_in_schema=False)
        # Outermost, so requests identity_gate rejects are counted too.
        app.middleware("http")(metrics.http_metrics_middleware)
    return app
