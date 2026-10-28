"""The agent map graph builder and GET /api/admin/agent-map."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_map import build_agent_map, connected_ids, normalize_focus

SECRET_REF = "projects/p/secrets/very-private/versions/7"


def _agent(id, name, skills=(), channels=(), status="active", **creds):
    return SimpleNamespace(
        id=id, name=name, status=status, agent_type="interactive",
        skill_ids=json.dumps(list(skills)), channel_ids=json.dumps(list(channels)),
        **creds,
    )


def _link(slug, system, direction=None, tag=None):
    return SimpleNamespace(skill_slug=slug, system_name=system, direction=direction,
                           integration_tag=tag or slug, risk_level=None)


@pytest.fixture
def graph_inputs():
    return dict(
        agents=[
            _agent("a1", "Sales Bot", skills=["sf-read", "gmail"], channels=["C1"], xoxb_secret_ref=SECRET_REF),
            _agent("a2", "Ops Bot", skills=["jira"]),
            _agent("a3", "Old Bot", skills=["sf-read"], status="decommissioned"),
        ],
        channel_configs=[
            SimpleNamespace(agent_id="a1", channel_id="C1", channel_name="sales", skill_ids=None, is_active=True),
            SimpleNamespace(agent_id="a2", channel_id="C2", channel_name="ops", skill_ids='["sf-write"]', is_active=True),
        ],
        system_links=[
            _link("sf-read", "Salesforce", "read"),
            _link("sf-write", "salesforce", "write"),
            _link("gmail", "Google Workspace", "read"),
            _link("jira", "Jira", "read", tag="jira"),
            _link("jira", "Jira", "write", tag="jira-api"),
            _link("jira", None),  # unmatched tag: no system node
            _link("ghost", "Salesforce", "write"),  # unpublished skill
        ],
        skill_secret_names=[("sf-read", "SALESFORCE_TOKEN"), ("sf-write", "SALESFORCE_TOKEN"), ("ghost", "GHOST_KEY")],
        skill_names={"sf-read": "Salesforce Read", "sf-write": "Salesforce Write", "gmail": "Gmail", "jira": "Jira"},
        skill_risk={"sf-write": "critical"},
        catalog={"salesforce": {"system_name": "Salesforce", "risk_level": "high", "system_status": "Active"}},
    )


def _ids(graph, type_=None):
    return {n["id"] for n in graph["nodes"] if type_ is None or n["type"] == type_}


def test_builds_every_node_type_and_skips_decommissioned_agents(graph_inputs):
    g = build_agent_map(**graph_inputs)
    assert _ids(g, "agent") == {"agent:a1", "agent:a2"}
    assert _ids(g, "skill") == {"skill:sf-read", "skill:sf-write", "skill:gmail", "skill:jira"}
    assert _ids(g, "system") == {"system:salesforce", "system:google workspace", "system:jira"}
    assert _ids(g, "secret") == {"secret:SALESFORCE_TOKEN", "secret:agent:a1:xoxb"}
    assert _ids(g, "channel") == {"channel:C1", "channel:C2"}
    assert g["totals"] == {"agent": 2, "skill": 4, "system": 3, "secret": 2, "channel": 2}

    by_id = {n["id"]: n for n in g["nodes"]}
    assert by_id["system:salesforce"]["risk_level"] == "high"
    assert by_id["system:jira"]["detail"] == "Not in the active system catalogue"
    assert by_id["channel:C1"]["label"] == "#sales"
    assert by_id["skill:sf-write"]["risk_level"] == "critical"


def test_edges_carry_direction_and_merge_multiple_tags(graph_inputs):
    edges = {e["id"]: e for e in build_agent_map(**graph_inputs)["edges"]}
    assert edges["skill:sf-read->system:salesforce"]["direction"] == "read"
    assert edges["skill:sf-write->system:salesforce"]["direction"] == "write"
    assert edges["skill:jira->system:jira"]["direction"] == "read_write"
    # Channel-level skill overrides count as the agent using the skill.
    assert edges["agent:a2->skill:sf-write"]["kind"] == "uses_skill"
    assert edges["agent:a1->secret:agent:a1:xoxb"]["kind"] == "holds_credential"


def test_secrets_are_names_only(graph_inputs):
    g = build_agent_map(**graph_inputs)
    assert SECRET_REF not in json.dumps(g)


def test_focus_on_system_answers_what_can_touch_it(graph_inputs):
    g = build_agent_map(**graph_inputs, focus="system:Salesforce")
    assert g["focus"] == "system:salesforce"
    assert _ids(g) == {
        "system:salesforce", "skill:sf-read", "skill:sf-write", "agent:a1", "agent:a2",
    }
    # Only edges between kept nodes survive.
    assert all(e["source"] in _ids(g) and e["target"] in _ids(g) for e in g["edges"])


def test_focus_on_secret_answers_what_breaks_on_rotation(graph_inputs):
    g = build_agent_map(**graph_inputs, focus="secret:SALESFORCE_TOKEN")
    assert _ids(g) == {"secret:SALESFORCE_TOKEN", "skill:sf-read", "skill:sf-write", "agent:a1", "agent:a2"}


def test_focus_on_agent_returns_its_reach(graph_inputs):
    g = build_agent_map(**graph_inputs, focus="agent:a2")
    assert _ids(g) == {
        "agent:a2", "skill:jira", "skill:sf-write", "system:jira", "system:salesforce",
        "secret:SALESFORCE_TOKEN", "channel:C2",
    }


def test_unknown_focus_raises_key_error(graph_inputs):
    with pytest.raises(KeyError):
        build_agent_map(**graph_inputs, focus="system:nope")


def test_connected_ids_does_not_cross_directions():
    edges = [{"source": "a", "target": "s1"}, {"source": "a", "target": "s2"}]
    assert connected_ids("s1", edges) == {"s1", "a"}
    assert normalize_focus(" system:Google Workspace ") == "system:google workspace"


# ── Endpoint ─────────────────────────────────────────────────────────────────


@pytest.fixture
def map_db(sqlite_session_factory):
    from agent_channel_configs import AgentChannelConfig
    from agents import Agent
    from skill_submission_models import SkillSecret
    from skill_system_links_model import SkillSystemLink

    db = sqlite_session_factory(Agent, AgentChannelConfig, SkillSystemLink, SkillSecret)
    db.add(Agent(id="a1", name="Sales Bot", status="active", skill_ids='["sf-read"]',
                 channel_ids='["C1"]', xoxb_secret_ref=SECRET_REF))
    db.add(Agent(id="a2", name="Ops Bot", status="active", skill_ids='["jira"]'))
    db.add(SkillSystemLink(skill_slug="sf-read", integration_tag="salesforce",
                           system_name="Salesforce", direction="read", match_confidence="exact"))
    db.add(SkillSystemLink(skill_slug="jira", integration_tag="jira",
                           system_name="Jira", direction="write", match_confidence="exact"))
    db.add(SkillSecret(slug="sf-read", env_key="SALESFORCE_TOKEN", secret_ref=SECRET_REF))
    db.commit()
    return db


@pytest.fixture
def client(map_db, monkeypatch):
    import system_catalog
    from app_factory import create_app
    from database import get_db

    monkeypatch.setattr(system_catalog, "get_system_map", lambda **_: {})
    app = create_app()
    app.dependency_overrides[get_db] = lambda: map_db
    return TestClient(app)


def test_endpoint_returns_graph_without_secret_values(client):
    res = client.get("/api/admin/agent-map")
    assert res.status_code == 200
    body = res.json()
    ids = {n["id"] for n in body["nodes"]}
    assert {"agent:a1", "agent:a2", "skill:sf-read", "system:salesforce",
            "secret:SALESFORCE_TOKEN", "secret:agent:a1:xoxb", "channel:C1"} <= ids
    assert SECRET_REF not in res.text


def test_endpoint_focus_filter(client):
    res = client.get("/api/admin/agent-map", params={"focus": "system:Salesforce"})
    assert res.status_code == 200
    assert {n["id"] for n in res.json()["nodes"]} == {"system:salesforce", "skill:sf-read", "agent:a1"}

    assert client.get("/api/admin/agent-map", params={"focus": "system:nope"}).status_code == 404


def test_endpoint_is_admin_only(client, monkeypatch):
    import auth_middleware
    import auth_policy

    monkeypatch.setattr(
        auth_middleware, "build_auth_context",
        lambda _headers: auth_policy.AuthContext(sub="u1", email="u1@example.com", groups=("platform-users",)),
    )
    assert client.get("/api/admin/agent-map").status_code == 403
