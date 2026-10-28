"""Shared pytest setup for the backend.

Puts backend/ on sys.path and sets the environment the app needs to import
under test (mock auth, a development APP_ENV, a placeholder model gateway), so
`python -m pytest tests/` works without exporting anything first. Values that
are already set in the environment win.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("ENABLE_MOCK_AUTH", "true")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("MODEL_GATEWAY_URL", "http://127.0.0.1:4000")


@pytest.fixture
def secret_encryption_key(monkeypatch) -> str:
    """A fresh SECRET_ENCRYPTION_KEY for the duration of one test."""
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", key)
    return key


@pytest.fixture
def local_secret_backend(tmp_path, secret_encryption_key):
    from providers.secrets import LocalSecretBackend

    return LocalSecretBackend(root=tmp_path / "secrets")


@pytest.fixture
def local_blob_store(tmp_path, monkeypatch):
    from providers.blobs import LocalBlobStore

    monkeypatch.setenv("BLOB_URL_SECRET", "test-blob-url-secret")
    return LocalBlobStore(root=tmp_path / "blobs")


@pytest.fixture
def sqlite_session_factory():
    """Build an in-memory SQLite session holding only the given tables.

    Usage: ``session = sqlite_session_factory(Agent, AgentCollaborator)``.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database import Base

    sessions = []

    def _make(*models):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[m.__table__ for m in models])
        session = sessionmaker(bind=engine)()
        sessions.append(session)
        return session

    yield _make
    for session in sessions:
        session.close()
