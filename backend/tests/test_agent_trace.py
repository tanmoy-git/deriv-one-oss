"""Agent traces: collection during a run, storage, the timeline API and replay."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _usage(inp, out, cr=0, cc=0):
    return SimpleNamespace(
        input_tokens=inp, output_tokens=out, cache_read_input_tokens=cr, cache_creation_input_tokens=cc
    )


# ── collection ────────────────────────────────────────────────────────────────

def test_model_calls_are_collected_only_inside_a_trace():
    import agent_trace
    from token_tracking import TokenAccumulator

    acc = TokenAccumulator()
    acc.add(_usage(5, 5))  # outside any trace: nothing to collect into
    with agent_trace.collecting() as trace:
        acc.add(_usage(100, 20, cr=50))
        acc.add(_usage(30, 10))
    acc.add(_usage(1, 1))

    assert [(c["input_tokens"], c["output_tokens"], c["cache_read_tokens"]) for c in trace.model_calls] == [
        (100, 20, 50),
        (30, 10, 0),
    ]
    assert acc.input_tokens == 136  # accounting itself is unchanged


def test_tool_audit_entries_are_stamped_and_stay_json():
    import agent_trace

    trace = agent_trace.AgentTrace()
    trace.tool_audit.append({"tool": "run_python", "ok": True})
    trace.tool_audit.append({"tool": "search_knowledge_base", "t_ms": 7})

    assert isinstance(trace.tool_audit[0]["t_ms"], int)
    assert trace.tool_audit[1]["t_ms"] == 7  # an existing stamp is kept
    assert json.loads(json.dumps(trace.tool_audit))[0]["tool"] == "run_python"


def test_trace_caps_model_calls_and_prompt_size(monkeypatch):
    import agent_trace

    monkeypatch.setattr(agent_trace, "MAX_MODEL_CALLS", 2)
    monkeypatch.setattr(agent_trace, "MAX_SYSTEM_PROMPT_CHARS", 5)
    with agent_trace.collecting() as trace:
        for _ in range(4):
            agent_trace.note_model_call(_usage(1, 1))
        agent_trace.note_system_prompt("abcdefgh")
        agent_trace.note_system_prompt("ignored: the first prompt wins")
    record = trace.to_record()
    assert len(record["model_calls"]) == 2
    assert record["system_prompt"] == "abcde"


def test_call_llm_records_its_system_prompt(monkeypatch):
    import agent_llm
    import agent_trace
    import api_slack_events

    monkeypatch.setattr(agent_llm, "resolve_agent_gateway_api_key", lambda *a, **k: ("", "none"))
    with agent_trace.collecting() as trace:
        asyncio.run(api_slack_events._call_llm("You are the support bot.", "hi", SimpleNamespace(id="a1", llm_model=None)))
    assert trace.system_prompt == "You are the support bot."


# ── storage and API ───────────────────────────────────────────────────────────

@pytest.fixture
def trace_db(monkeypatch, sqlite_session_factory):
    import agents
    from agents import Agent, AgentInteractionLog, AgentInteractionTrace

    session = sqlite_session_factory(Agent, AgentInteractionLog, AgentInteractionTrace)
    bind = session.get_bind()

    def _factory():
        from sqlalchemy.orm import Session

        return Session(bind=bind)

    monkeypatch.setattr(agents, "SessionLocal", _factory)
    session.add(Agent(id="a1", name="Support bot", owner_sub="owner-sub"))
    session.commit()
    return session


def _exchange(trace=None, tools=None, routing=None):
    from agents import record_agent_exchange

    return record_agent_exchange(
        interaction_id="i1",
        agent_id="a1",
        user_message="How many open tickets?",
        assistant_message="There are 4 open tickets.",
        invoker_email="Dev@Example.com",
        trigger_type="mention",
        duration_ms=2500,
        tools_called=tools,
        routing_metadata=routing,
        resolved_model="claude-sonnet-4-6",
        trace=trace,
    )


def test_record_agent_exchange_stores_the_trace(trace_db):
    from agents import AgentInteractionTrace

    _exchange(trace={"model_calls": [{"t_ms": 10, "input_tokens": 1, "output_tokens": 2}], "system_prompt": "sys"})
    stored = trace_db.get(AgentInteractionTrace, "i1")
    assert stored.agent_id == "a1"
    assert json.loads(stored.model_calls)[0]["output_tokens"] == 2
    assert stored.system_prompt == "sys"


def test_build_timeline_orders_steps_and_totals_cost(trace_db, monkeypatch):
    import api_agent_traces
    from agents import AgentInteractionLog, AgentInteractionTrace

    monkeypatch.setattr(api_agent_traces, "cost_for_model", lambda m, i, o, cr, cc: (i + o) / 1000)
    _exchange(
        trace={
            "model_calls": [
                {"t_ms": 100, "input_tokens": 1000, "output_tokens": 0},
                {"t_ms": 900, "input_tokens": 500, "output_tokens": 500},
            ],
            "system_prompt": "sys",
        },
        tools=[{"tool": "run_python", "ok": False, "code_len": 40, "output_len": 12, "t_ms": 600}],
        routing={"enabled": True, "selected_slugs": ["tickets"], "mode": "auto"},
    )
    row = trace_db.get(AgentInteractionLog, "i1")
    timeline = api_agent_traces.build_timeline(row, trace_db.get(AgentInteractionTrace, "i1"))

    assert [s["kind"] for s in timeline["steps"]] == ["routing", "model", "tool", "model"]
    assert timeline["steps"][0]["selected"] == ["tickets"]
    assert timeline["timed"] is True
    assert timeline["totals"] == {
        "duration_ms": 2500,
        "model_calls": 2,
        "input_tokens": 1500,
        "output_tokens": 500,
        "cost_usd": 2.0,
        "tool_calls": 1,
        "tool_errors": 1,
    }


def test_build_timeline_works_for_interactions_without_a_trace(trace_db):
    import api_agent_traces
    from agents import AgentInteractionLog

    _exchange(tools=[{"tool": "run_python", "ok": True}, {"tool": "search_knowledge_base", "ok": True}])
    timeline = api_agent_traces.build_timeline(trace_db.get(AgentInteractionLog, "i1"), None)
    assert [s["tool"] for s in timeline["steps"]] == ["run_python", "search_knowledge_base"]
    assert timeline["timed"] is False
    assert timeline["totals"]["model_calls"] == 0


@pytest.fixture
def client(trace_db):
    from app_factory import create_app
    from database import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: trace_db
    with TestClient(app) as c:
        yield c


def test_trace_endpoint_returns_the_timeline(client):
    _exchange(trace={"model_calls": [{"t_ms": 5, "input_tokens": 10, "output_tokens": 3}], "system_prompt": "sys"})
    body = client.get("/api/agents/a1/interactions/i1/trace").json()
    assert body["replayable"] is True
    assert body["user_message"] == "How many open tickets?"
    assert body["totals"]["model_calls"] == 1


def test_trace_endpoint_hides_other_peoples_agents(client, monkeypatch):
    import api_agent_traces
    import api_agents
    from auth_policy import AuthContext

    _exchange()
    member = AuthContext(sub="someone-else", email="m@example.com", groups=("platform-users",))
    monkeypatch.setattr(api_agent_traces, "_auth_context", lambda _r: member)
    monkeypatch.setattr(api_agents, "_is_owner_or_collaborator", lambda *a, **k: False)
    assert client.get("/api/agents/a1/interactions/i1/trace").status_code == 404


@pytest.fixture
def replay_ready(monkeypatch):
    import agent_llm
    import api_agent_traces
    import model_catalog
    import token_tracking

    calls = {}

    async def complete(model, system, user, api_key):
        calls.update(model=model, system=system, user=user)
        return "Replayed answer", 40, 8

    async def catalog(**_kwargs):
        return {}

    monkeypatch.setattr(api_agent_traces, "_complete", complete)
    monkeypatch.setattr(model_catalog, "get_model_catalog", catalog)
    monkeypatch.setattr(model_catalog, "is_chat_model_allowed", lambda m, c: m != "unknown-model")
    monkeypatch.setattr(agent_llm, "resolve_agent_gateway_api_key", lambda *a, **k: ("key", "platform"))
    monkeypatch.setattr(token_tracking, "record_token_usage", lambda *a, **k: calls.setdefault("recorded", a[0]))
    return calls


def test_replay_runs_the_stored_prompt_on_another_model(client, replay_ready):
    _exchange(trace={"model_calls": [], "system_prompt": "You are the support bot."})
    res = client.post("/api/agents/a1/interactions/i1/replay", json={"model": "gpt-5.4-mini"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["output"] == "Replayed answer"
    assert body["dry_run"] is True
    assert body["original"] == {"model": "claude-sonnet-4-6", "output": "There are 4 open tickets."}
    assert replay_ready == {
        "model": "gpt-5.4-mini",
        "system": "You are the support bot.",
        "user": "How many open tickets?",
        "recorded": "trace_replay",
    }


def test_replay_needs_a_trace_and_a_known_model(client, replay_ready):
    _exchange()
    assert client.post("/api/agents/a1/interactions/i1/replay", json={"model": "gpt-5.4-mini"}).status_code == 409


def test_replay_rejects_models_outside_the_catalog(client, replay_ready):
    _exchange(trace={"model_calls": [], "system_prompt": "sys"})
    assert client.post("/api/agents/a1/interactions/i1/replay", json={"model": "unknown-model"}).status_code == 400
