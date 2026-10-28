"""Spend budgets: month-to-date spend, forecasts, the admin API and enforcement."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

NOW = datetime(2026, 10, 15, 12, 0, 0)  # halfway through a 31-day month


@pytest.fixture
def budget_db(monkeypatch, sqlite_session_factory):
    """An in-memory DB with budgets, usage and agents, used by every code path."""
    import spend_budgets
    from agents import Agent
    from database import SpendBudget, TokenUsageLog

    session = sqlite_session_factory(Agent, TokenUsageLog, SpendBudget)
    bind = session.get_bind()

    def _factory():
        from sqlalchemy.orm import Session

        return Session(bind=bind)

    monkeypatch.setattr(spend_budgets, "SessionLocal", _factory)
    # One dollar per million input tokens, nothing else, so costs are easy to read.
    monkeypatch.setattr(
        spend_budgets,
        "cost_for_model",
        lambda model, inp, out, cr, cc: inp / 1_000_000,
    )
    return session


def _usage(db, dollars: float, *, when=NOW, agent_id=None, user_email=None):
    from database import TokenUsageLog

    db.add(
        TokenUsageLog(
            source="test",
            model="m",
            agent_id=agent_id,
            user_email=user_email,
            input_tokens=int(dollars * 1_000_000),
            output_tokens=0,
            created_at=when,
        )
    )
    db.commit()


def _agent(db, agent_id="a1", name="Support bot"):
    from agents import Agent

    db.add(Agent(id=agent_id, name=name, owner_sub="owner"))
    db.commit()


def _budget(db, scope, scope_id="", limit=10.0, pct=80, enforce=True):
    from database import SpendBudget

    b = SpendBudget(
        scope=scope,
        scope_id=scope_id,
        monthly_limit_usd=limit,
        soft_limit_pct=pct,
        enforce=enforce,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(b)
    db.commit()
    return b


# ── evaluate (pure) ───────────────────────────────────────────────────────────

def test_evaluate_states():
    from spend_budgets import evaluate

    assert evaluate(1, 10, 80, NOW).state == "ok"
    assert evaluate(8, 10, 80, NOW).state == "soft"
    assert evaluate(10, 10, 80, NOW).state == "exceeded"


def test_evaluate_forecasts_the_month_from_the_daily_rate():
    from spend_budgets import evaluate

    # 14.5 days elapsed at $1/day: about $31 by month end, limit hit on day 21.
    status = evaluate(14.5, 20, 80, NOW)
    assert status.forecast_usd == pytest.approx(31.0)
    assert status.forecast_exceed_at == datetime(2026, 10, 21, 0, 0, 0)


def test_evaluate_has_no_exceed_date_when_the_rate_stays_under_the_limit():
    from spend_budgets import evaluate

    assert evaluate(1, 100, 80, NOW).forecast_exceed_at is None


def test_evaluate_uses_at_least_an_hour_of_history():
    from spend_budgets import evaluate

    first_minute = datetime(2026, 10, 1, 0, 1, 0)
    # $1 in the first minute must not project to ~$44,640 for the month.
    assert evaluate(1, 1000, 80, first_minute).forecast_usd == pytest.approx(744.0)


# ── spend_since ───────────────────────────────────────────────────────────────

def test_spend_counts_only_this_month_and_this_scope(budget_db):
    from spend_budgets import month_start, spend_since

    _usage(budget_db, 3, agent_id="a1")
    _usage(budget_db, 2, agent_id="a2", user_email="Dev@Example.com")
    _usage(budget_db, 50, agent_id="a1", when=datetime(2026, 9, 30, 23, 0))

    start = month_start(NOW)
    assert spend_since(budget_db, start, scope="org") == pytest.approx(5)
    assert spend_since(budget_db, start, scope="agent", scope_id="a1") == pytest.approx(3)
    assert spend_since(budget_db, start, scope="user", scope_id="dev@example.com") == pytest.approx(2)


# ── check_spend_allowed ───────────────────────────────────────────────────────

def test_calls_are_allowed_without_budgets(budget_db):
    from spend_budgets import check_spend_allowed

    assert check_spend_allowed(agent_id="a1", user_email="u@example.com", now=NOW).allowed


def test_exceeded_agent_budget_blocks_that_agent_only(budget_db):
    from spend_budgets import check_spend_allowed

    _budget(budget_db, "agent", "a1", limit=5)
    _usage(budget_db, 6, agent_id="a1")

    blocked = check_spend_allowed(agent_id="a1", now=NOW)
    assert not blocked.allowed
    assert "This agent has reached its monthly spend limit of $5.00" in blocked.message
    assert check_spend_allowed(agent_id="a2", now=NOW).allowed


def test_user_budget_matches_email_case_insensitively(budget_db):
    from spend_budgets import check_spend_allowed

    _budget(budget_db, "user", "dev@example.com", limit=1)
    _usage(budget_db, 2, user_email="Dev@Example.com")

    decision = check_spend_allowed(user_email="DEV@example.com", now=NOW)
    assert not decision.allowed
    assert decision.message.startswith("Your account")


def test_org_budget_blocks_everyone(budget_db):
    from spend_budgets import check_spend_allowed

    _budget(budget_db, "org", limit=1)
    _usage(budget_db, 1, agent_id="a9")

    assert not check_spend_allowed(user_email="anyone@example.com", now=NOW).allowed


def test_alert_only_budget_never_blocks(budget_db):
    from spend_budgets import check_spend_allowed

    _budget(budget_db, "org", limit=1, enforce=False)
    _usage(budget_db, 5)

    assert check_spend_allowed(now=NOW).allowed


def test_check_fails_open_when_the_database_errors(budget_db, monkeypatch):
    import spend_budgets

    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(spend_budgets, "SessionLocal", broken)
    assert spend_budgets.check_spend_allowed(agent_id="a1", now=NOW).allowed


# ── ops alerts ────────────────────────────────────────────────────────────────

def test_budget_alert_breaches_past_the_soft_limit(budget_db, monkeypatch):
    import ops_alerts
    import spend_budgets

    monkeypatch.setattr(ops_alerts, "SessionLocal", spend_budgets.SessionLocal)
    _agent(budget_db)
    quiet = _budget(budget_db, "org", limit=100)
    loud = _budget(budget_db, "agent", "a1", limit=10, pct=50)
    _usage(budget_db, 6, agent_id="a1")

    checks = {c.key: c for c in ops_alerts._collect_budget_checks()}
    assert not checks[f"budget:{quiet.id}"].breaching
    assert checks[f"budget:{loud.id}"].breaching
    assert "`Support bot` has spent $6.00 of its $10.00 monthly budget" in checks[f"budget:{loud.id}"].summary


# ── admin API ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(budget_db):
    from app_factory import create_app
    from database import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: budget_db
    with TestClient(app) as c:
        yield c


def test_admin_can_create_list_update_and_delete_budgets(client, budget_db):
    _agent(budget_db)

    res = client.put("/api/admin/budgets", json={"scope": "agent", "scope_id": "a1", "monthly_limit_usd": 25})
    assert res.status_code == 200, res.text
    created = res.json()
    assert created["label"] == "Support bot"
    assert created["status"]["state"] == "ok"

    # PUT on the same scope updates rather than duplicating.
    again = client.put(
        "/api/admin/budgets",
        json={"scope": "agent", "scope_id": "a1", "monthly_limit_usd": 30, "enforce": False},
    ).json()
    assert again["id"] == created["id"]
    assert again["monthly_limit_usd"] == 30
    assert again["enforce"] is False

    patched = client.patch(f"/api/admin/budgets/{created['id']}", json={"soft_limit_pct": 90}).json()
    assert patched["soft_limit_pct"] == 90
    assert patched["monthly_limit_usd"] == 30

    listed = client.get("/api/admin/budgets").json()
    assert [b["id"] for b in listed["budgets"]] == [created["id"]]

    assert client.delete(f"/api/admin/budgets/{created['id']}").status_code == 204
    assert client.get("/api/admin/budgets").json()["budgets"] == []


@pytest.mark.parametrize(
    "body, status",
    [
        ({"scope": "agent", "scope_id": "missing", "monthly_limit_usd": 5}, 404),
        ({"scope": "agent", "monthly_limit_usd": 5}, 422),
        ({"scope": "user", "scope_id": "not-an-email", "monthly_limit_usd": 5}, 422),
        ({"scope": "org", "monthly_limit_usd": 0}, 422),
        ({"scope": "org", "monthly_limit_usd": 5, "soft_limit_pct": 0}, 422),
        ({"scope": "team", "monthly_limit_usd": 5}, 422),
    ],
)
def test_invalid_budgets_are_rejected(client, body, status):
    assert client.put("/api/admin/budgets", json=body).status_code == status


def test_user_budget_email_is_stored_lowercased(client):
    body = client.put(
        "/api/admin/budgets",
        json={"scope": "user", "scope_id": " Dev@Example.com ", "monthly_limit_usd": 5},
    ).json()
    assert body["scope_id"] == "dev@example.com"


def test_non_admins_cannot_see_or_change_budgets(client, monkeypatch):
    import auth_middleware
    from auth_policy import AuthContext

    member = AuthContext(sub="member", email="m@example.com", groups=("platform-users",))
    monkeypatch.setattr(auth_middleware, "build_auth_context", lambda _headers: member)

    assert client.get("/api/admin/budgets").status_code == 403
    assert client.put("/api/admin/budgets", json={"scope": "org", "monthly_limit_usd": 5}).status_code == 403


# ── enforcement at the two model-call entry points ────────────────────────────

def _blocked(**_kwargs):
    from spend_budgets import SpendDecision

    return SpendDecision(allowed=False, message="Over budget.", budget_id="b1")


def test_slack_agent_call_is_blocked_before_any_model_request(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import agent_llm
    import api_slack_events
    import spend_budgets
    from agent_failure_alerts import FAILURE_KIND_BUDGET

    seen = {}

    def check(**kwargs):
        seen.update(kwargs)
        return _blocked()

    monkeypatch.setattr(spend_budgets, "check_spend_allowed", check)
    monkeypatch.setattr(agent_llm, "resolve_agent_gateway_api_key", lambda *a, **k: ("key", "platform"))

    def no_model(*_a, **_k):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(agent_llm, "resolve_agent_model", no_model)

    agent = SimpleNamespace(id="a1", llm_model=None)
    outcome = asyncio.run(api_slack_events._call_llm("system", "hi", agent))

    assert seen == {"agent_id": "a1"}
    assert outcome.text == "Over budget."
    assert outcome.failure_kind == FAILURE_KIND_BUDGET
    assert not outcome.is_alertable_failure


def test_workspace_chat_streams_the_budget_message(monkeypatch):
    import main
    from cf_access import MOCK_USER_EMAIL
    import model_catalog
    import spend_budgets

    seen = {}

    def check(**kwargs):
        seen.update(kwargs)
        return _blocked()

    async def catalog(**_kwargs):
        return {}

    monkeypatch.setattr(main, "MODEL_GATEWAY_API_KEY", "key")
    monkeypatch.setattr(main, "get_model_catalog", catalog)
    monkeypatch.setattr(model_catalog, "pick_workspace_chat_model", lambda _c, _m: "m")
    monkeypatch.setattr(main, "is_chat_model_allowed", lambda *_a: True)
    monkeypatch.setattr(spend_budgets, "check_spend_allowed", check)

    def no_history(*_a):
        raise AssertionError("chat must stop before loading history")

    monkeypatch.setattr(main, "_chat_load_history", no_history)

    res = TestClient(main.app).post("/api/chat", json={"chat_id": "c1", "message": "hi"})

    assert res.status_code == 200
    assert '"content": "Over budget."' in res.text
    assert seen == {"user_email": MOCK_USER_EMAIL}
