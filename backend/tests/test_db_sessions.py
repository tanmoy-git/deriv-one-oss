"""get_db and session_scope commit, roll back and close as documented."""

from __future__ import annotations

import pytest


@pytest.fixture
def sessions(monkeypatch, sqlite_session_factory):
    """Point database.SessionLocal at an in-memory DB and record each session."""
    import database
    from database import Feedback

    seed = sqlite_session_factory(Feedback)
    bind = seed.get_bind()
    made = []

    def _factory():
        from sqlalchemy.orm import Session

        s = Session(bind=bind)
        made.append(s)
        return s

    monkeypatch.setattr(database, "SessionLocal", _factory)
    return seed, made


def _row(id_):
    from database import Feedback

    return Feedback(
        id=id_, owner_sub="u", email="u@example.com", route="/", element_context="{}", user_message="m", category="bug"
    )


def test_session_scope_commits_on_success(sessions):
    from database import Feedback, session_scope

    seed, _ = sessions
    with session_scope() as db:
        db.add(_row("f1"))
    assert seed.get(Feedback, "f1") is not None


def test_session_scope_rolls_back_on_error(sessions):
    from database import Feedback, session_scope

    seed, _ = sessions
    with pytest.raises(RuntimeError):
        with session_scope() as db:
            db.add(_row("f2"))
            db.flush()
            raise RuntimeError("boom")
    assert seed.get(Feedback, "f2") is None


def test_get_db_rolls_back_uncommitted_work_on_error(sessions):
    from database import Feedback, get_db

    seed, _ = sessions
    gen = get_db()
    db = next(gen)
    db.add(_row("f3"))
    db.flush()
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("route failed"))
    assert seed.get(Feedback, "f3") is None
