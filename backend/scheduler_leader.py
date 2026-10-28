"""Leader election for the scheduler worker, on a Postgres advisory lock.

Only the replica holding the lock registers and fires jobs; the others wait.
The lock is session-level: Postgres holds it for as long as the leader's
connection lives and drops it the moment that connection dies, so a crashed or
partitioned leader is replaced within one check interval and no extra service
(Redis, etcd) is needed.

On SQLite there is nothing to coordinate through, so every process is the
leader. That is only safe with a single scheduler, which is the only way a
SQLite deployment can run anyway.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

# Arbitrary but fixed: every scheduler replica must use the same key.
LOCK_KEY = 0x44315343  # "D1SC"


class LeaderLease:
    def __init__(self, engine: Engine, *, lock_key: int = LOCK_KEY) -> None:
        self.engine = engine
        self.lock_key = lock_key
        self._conn: Optional[Connection] = None
        self._single_node = engine.dialect.name != "postgresql"

    @property
    def held(self) -> bool:
        return self._single_node or self._conn is not None

    def try_acquire(self) -> bool:
        """Take the lock if nobody holds it. Never blocks."""
        if self._single_node:
            return True
        if self._conn is not None:
            return True
        conn = self.engine.connect()
        try:
            got = conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": self.lock_key}
            ).scalar()
            # Leave no transaction open: the lock is session-level, not
            # transactional, and an idle-in-transaction connection would pin
            # a snapshot for as long as this replica leads.
            conn.commit()
        except Exception:
            conn.close()
            raise
        if got:
            self._conn = conn
            return True
        conn.close()
        return False

    def still_held(self) -> bool:
        """Whether the connection that holds the lock is still alive."""
        if self._single_node:
            return True
        if self._conn is None:
            return False
        try:
            self._conn.execute(text("SELECT 1"))
            self._conn.commit()
            return True
        except Exception:
            logger.exception("scheduler lease: lock connection is gone")
            self._drop()
            return False

    def release(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.lock_key})
            self._conn.commit()
        except Exception:
            logger.warning("scheduler lease: unlock failed; closing the connection drops it", exc_info=True)
        finally:
            self._drop()

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def wait_until_leader(
        self,
        *,
        check_every: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        on_standby: Optional[Callable[[], None]] = None,
    ) -> None:
        """Block until this process holds the lock."""
        if self._single_node:
            logger.warning(
                "scheduler lease: %s has no advisory locks; acting as leader. "
                "Run exactly one scheduler replica.",
                self.engine.dialect.name,
            )
            return
        logged = False
        while not self.try_acquire():
            if not logged:
                logger.info("scheduler lease: another replica is leader; standing by")
                logged = True
            if on_standby is not None:
                on_standby()
            sleep(check_every)
        logger.info("scheduler lease: became leader")
