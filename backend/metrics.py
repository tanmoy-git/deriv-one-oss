"""Prometheus metrics for the API and the workers.

The API serves them at /metrics when METRICS_ENABLED=true; set METRICS_TOKEN
to require ``Authorization: Bearer <token>``. /metrics is not under /api, so
the bundled nginx does not proxy it and only in-cluster scrapers reach it.
Workers without an HTTP server call serve_on_port() instead.

Label values are bounded on purpose: requests are labelled by route template
(``/api/agents/{agent_id}``), never by raw path, and unmatched paths share one
label.
"""

from __future__ import annotations

import hmac
import os
import time

from fastapi import Request
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by route template and status code.",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Time to produce the response headers, by route template.",
    ["method", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
SCHEDULER_IS_LEADER = Gauge(
    "scheduler_is_leader",
    "1 while this scheduler replica holds the leader lease. Alert if the sum is not 1.",
)
LLM_TOKENS = Counter(
    "llm_tokens_total",
    "Model tokens recorded by token_tracking, by source, model and direction.",
    ["source", "model", "direction"],
)

_UNMATCHED = "unmatched"


def enabled() -> bool:
    return os.environ.get("METRICS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _route_label(request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or _UNMATCHED


async def http_metrics_middleware(request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = _route_label(request)
        if route != "/metrics":
            HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
            HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - start)


def metrics_endpoint(request: Request) -> Response:
    token = os.environ.get("METRICS_TOKEN", "").strip()
    if token:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {token}"):
            return Response(status_code=401)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def record_tokens(source: str, model: str, input_tokens: int, output_tokens: int) -> None:
    LLM_TOKENS.labels(source or "unknown", model or "unknown", "input").inc(max(0, input_tokens))
    LLM_TOKENS.labels(source or "unknown", model or "unknown", "output").inc(max(0, output_tokens))


def serve_on_port(port: int) -> None:
    """Serve /metrics on its own port, for processes with no HTTP server."""
    start_http_server(port)
