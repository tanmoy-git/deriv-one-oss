"""create_app(), the request-scoped session and the memory router, end to end.

The app is built with create_app() (no main.py import, no lifespan, so no
scheduler) and get_db is pointed at an in-memory SQLite database.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def memory_db(sqlite_session_factory):
    from cf_access import MOCK_USER_SUB
    from database import Memory, MemorySyncLog

    session = sqlite_session_factory(Memory, MemorySyncLog)
    session.add_all(
        [
            Memory(id="mine", owner_sub=MOCK_USER_SUB, key="k1", title="Mine", category="work", content="a"),
            Memory(id="theirs", owner_sub="someone-else", key="k1", title="Theirs", category="work", content="b"),
        ]
    )
    session.commit()
    return session


@pytest.fixture
def client(memory_db):
    from app_factory import create_app
    from database import get_db

    app = create_app()
    app.dependency_overrides[get_db] = lambda: memory_db
    with TestClient(app) as c:
        yield c


def test_list_memory_returns_only_the_callers_entries(client):
    body = client.get("/api/memory").json()
    assert [e["id"] for e in body["entries"]] == ["mine"]
    assert body["last_sync"] == {"started_at": None, "finished_at": None, "status": None}


def test_update_memory_changes_own_entry(client):
    res = client.put("/api/memory/mine", json={"title": "Renamed", "status": "approved"})
    assert res.status_code == 200
    assert res.json()["title"] == "Renamed"
    assert res.json()["status"] == "approved"


def test_another_owners_entry_is_not_found_for_update_or_delete(client, memory_db):
    from database import Memory

    assert client.put("/api/memory/theirs", json={"title": "x"}).status_code == 404
    assert client.delete("/api/memory/theirs").status_code == 404
    assert memory_db.get(Memory, "theirs").title == "Theirs"


def test_delete_memory_removes_own_entry(client, memory_db):
    from database import Memory

    assert client.delete("/api/memory/mine").json() == {"ok": True}
    assert memory_db.get(Memory, "mine") is None


def test_identity_gate_runs_on_factory_built_app(client, monkeypatch):
    import cf_access

    monkeypatch.setattr(cf_access, "ENABLE_MOCK_AUTH", False)
    assert client.get("/api/memory").status_code == 401


def test_auth_failures_return_fixed_messages(client, monkeypatch):
    import auth_middleware
    import cf_access

    monkeypatch.setattr(cf_access, "ENABLE_MOCK_AUTH", False)
    res = client.get("/api/memory")
    assert res.status_code == 401
    assert res.json() == {"detail": auth_middleware.UNAUTHENTICATED_DETAIL}

    def deny(_headers):
        raise PermissionError("JWT has no recognizable groups claim; set GROUPS_JWT_CLAIM_KEYS")

    monkeypatch.setattr(auth_middleware, "build_auth_context", deny)
    res = client.get("/api/memory")
    assert res.status_code == 403
    assert res.json() == {"detail": auth_middleware.FORBIDDEN_DETAIL}
    assert "GROUPS_JWT_CLAIM_KEYS" not in res.text
