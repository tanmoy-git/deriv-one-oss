"""/metrics wiring, route-template labels and the JSON log format."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def metrics_app(monkeypatch):
    from app_factory import create_app

    monkeypatch.setenv("METRICS_ENABLED", "true")
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    app = create_app()
    app.add_api_route("/api/test-items/{item_id}", lambda item_id: {"id": item_id})
    with TestClient(app) as client:
        yield client, monkeypatch


def test_metrics_off_by_default(monkeypatch):
    from app_factory import create_app

    monkeypatch.delenv("METRICS_ENABLED", raising=False)
    with TestClient(create_app()) as client:
        # Exempt from auth, but the route does not exist.
        assert client.get("/metrics").status_code == 404


def test_requests_are_counted_by_route_template(metrics_app):
    client, _ = metrics_app
    client.get("/api/test-items/abc123")
    client.get("/api/memory/not-a-route/really")
    body = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/api/test-items/{item_id}",status="200"}' in body
    assert 'route="unmatched"' in body
    assert "abc123" not in body
    assert "not-a-route" not in body
    assert 'route="/metrics"' not in body


def test_metrics_token_is_required_when_set(metrics_app):
    client, monkeypatch = metrics_app
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"})
    assert ok.status_code == 200
    assert "scheduler_is_leader" in ok.text


def test_token_usage_feeds_the_token_counter(monkeypatch):
    import metrics
    from types import SimpleNamespace
    from token_tracking import record_token_usage

    before = metrics.LLM_TOKENS.labels("test-source", "test-model", "output")._value.get()
    # The DB write fails without tables; record_token_usage swallows that, but
    # the counter is updated first.
    record_token_usage("test-source", "test-model", SimpleNamespace(input_tokens=3, output_tokens=5))
    after = metrics.LLM_TOKENS.labels("test-source", "test-model", "output")._value.get()
    assert after - before == 5


def test_json_formatter_emits_one_object_with_extras():
    from logging_setup import JsonFormatter

    record = logging.LogRecord("svc", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    record.request_id = "r-1"
    entry = json.loads(JsonFormatter(label="worker-a").format(record))
    assert entry["message"] == "hello world"
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "svc"
    assert entry["worker"] == "worker-a"
    assert entry["request_id"] == "r-1"


def test_configure_logging_json_mode_routes_uvicorn_through_root(monkeypatch):
    from logging_setup import JsonFormatter, configure_logging

    root = logging.getLogger()
    saved = root.handlers[:], root.level
    monkeypatch.setenv("LOG_FORMAT", "json")
    try:
        configure_logging()
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert logging.getLogger("uvicorn.access").propagate is True
        assert logging.getLogger("uvicorn.access").handlers == []
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
