"""LeaderLease: only one holder, and a dead holder's lock is taken over.

The Postgres cases need a real server: set TEST_POSTGRES_URL (CI's migrations
job does). They are skipped otherwise.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from scheduler_leader import LeaderLease

PG_URL = os.environ.get("TEST_POSTGRES_URL", "")
needs_postgres = pytest.mark.skipif(not PG_URL, reason="TEST_POSTGRES_URL not set")

# A key no real scheduler uses, so a test run next to one cannot interfere.
TEST_KEY = 0x7E57_0001


@pytest.fixture
def pg_engine():
    engine = create_engine(PG_URL, pool_pre_ping=True)
    yield engine
    engine.dispose()


def test_sqlite_is_always_leader():
    lease = LeaderLease(create_engine("sqlite://"))
    assert lease.try_acquire()
    assert lease.still_held()
    lease.wait_until_leader(sleep=lambda _: pytest.fail("must not wait on SQLite"))


@needs_postgres
def test_only_one_replica_acquires(pg_engine):
    first = LeaderLease(pg_engine, lock_key=TEST_KEY)
    second = LeaderLease(pg_engine, lock_key=TEST_KEY)
    try:
        assert first.try_acquire()
        assert not second.try_acquire()
        assert first.still_held()
    finally:
        first.release()
        second.release()


@needs_postgres
def test_release_hands_over(pg_engine):
    first = LeaderLease(pg_engine, lock_key=TEST_KEY)
    second = LeaderLease(pg_engine, lock_key=TEST_KEY)
    try:
        assert first.try_acquire()
        first.release()
        assert second.try_acquire()
    finally:
        second.release()


@needs_postgres
def test_killed_leader_connection_frees_the_lock(pg_engine):
    leader = LeaderLease(pg_engine, lock_key=TEST_KEY)
    standby = LeaderLease(pg_engine, lock_key=TEST_KEY)
    try:
        assert leader.try_acquire()
        pid = leader._conn.execute(text("SELECT pg_backend_pid()")).scalar()
        leader._conn.commit()
        with pg_engine.connect() as admin:
            admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
            admin.commit()

        assert not leader.still_held()
        assert standby.try_acquire()
    finally:
        leader.release()
        standby.release()


@needs_postgres
def test_standby_waits_then_takes_over(pg_engine):
    leader = LeaderLease(pg_engine, lock_key=TEST_KEY)
    standby = LeaderLease(pg_engine, lock_key=TEST_KEY)
    assert leader.try_acquire()
    waits = []

    def fake_sleep(seconds):
        waits.append(seconds)
        leader.release()  # the leader goes away while the standby waits

    try:
        standby.wait_until_leader(check_every=0.5, sleep=fake_sleep)
        assert waits == [0.5]
        assert standby.held
    finally:
        standby.release()
