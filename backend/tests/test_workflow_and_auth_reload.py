"""Regression tests for the workflow collaborator check and the auth reload."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def db():
    from agents import Agent, AgentCollaborator
    from database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[Agent.__table__, AgentCollaborator.__table__]
    )
    session = sessionmaker(bind=engine)()
    session.add(Agent(id="agent-1", name="Agent", owner_sub="owner-sub", status="active"))
    session.add(AgentCollaborator(agent_id="agent-1", user_email="collab@example.com"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def test_workflow_collaborator_is_allowed(db):
    from api_workflows import _ensure_agent_access

    agent = _ensure_agent_access(db, "agent-1", "other-sub", "Collab@Example.com")
    assert agent.id == "agent-1"


def test_workflow_stranger_is_denied(db):
    from api_workflows import _ensure_agent_access

    with pytest.raises(HTTPException) as exc:
        _ensure_agent_access(db, "agent-1", "other-sub", "stranger@example.com")
    assert exc.value.status_code == 403


def test_workflow_request_email_comes_from_auth_context():
    from api_workflows import _request_email

    request = SimpleNamespace(state=SimpleNamespace(auth=SimpleNamespace(email="a@example.com")))
    assert _request_email(request) == "a@example.com"
    assert _request_email(SimpleNamespace(state=SimpleNamespace())) is None


@pytest.fixture
def restore_auth_env(monkeypatch):
    import cf_access

    yield monkeypatch
    monkeypatch.undo()
    cf_access.reload_auth_env_flags()


def test_reload_auth_env_picks_up_oidc_settings(restore_auth_env):
    import cf_access

    restore_auth_env.setenv("ENABLE_MOCK_AUTH", "false")
    restore_auth_env.setenv("OIDC_ISSUER", "https://issuer.example.com/")
    restore_auth_env.setenv("POLICY_AUD", "deriv-one")
    restore_auth_env.setenv("OIDC_JWKS_URL", "https://issuer.example.com/jwks")
    cf_access._resolved_jwks_uri = "https://stale.example.com/jwks"
    cf_access._jwks = object()

    cf_access.reload_auth_env_flags()

    assert cf_access.ENABLE_MOCK_AUTH is False
    assert cf_access.OIDC_ISSUER == "https://issuer.example.com"
    assert cf_access.POLICY_AUD == "deriv-one"
    assert cf_access._jwks is None
    assert cf_access.resolve_jwks_uri() == "https://issuer.example.com/jwks"
