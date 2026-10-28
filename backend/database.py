import logging
import os
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import Depends

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text as sa_text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

_DATABASE_URL = os.environ.get("DATABASE_URL", "")

if _DATABASE_URL:
    # PostgreSQL via Cloud SQL Auth Proxy (asyncpg driver → use psycopg2 for sync SQLAlchemy)
    _sync_url = _DATABASE_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    engine = create_engine(
        _sync_url,
        pool_pre_ping=True,
        # Each API replica, worker and the scheduler gets its own pool, so the
        # total against Postgres is replicas x (pool + overflow).
        pool_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", "20")),
        pool_timeout=int(os.environ.get("DB_POOL_TIMEOUT", "30")),
    )
else:
    # Local development fallback — SQLite
    _default_db = Path(__file__).resolve().parent / "hub.db"
    DB_PATH = Path(os.environ.get("HUB_DATABASE_PATH", str(_default_db))).resolve()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    The route (or the service it calls) commits; this only rolls back on an
    error and closes. Use it as ``db: DB`` instead of calling SessionLocal()
    in a route, so tests can swap the database with
    ``app.dependency_overrides[get_db]``.
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session for code outside a request: workers, scheduler jobs, SSE writes.

    Commits when the block exits normally and rolls back if it raises.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

OWNER_LEGACY_SENTINEL = "__LEGACY_UNASSIGNED__"


def _uuid() -> str:
    return str(uuid.uuid4())


logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id = Column(String, primary_key=True, default=_uuid)
    title = Column(String, default="New Chat", nullable=False)
    active_skill_id = Column(String, nullable=True)
    owner_sub = Column(String, nullable=False, index=True, default=lambda: OWNER_LEGACY_SENTINEL)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    messages = relationship(
        "Message",
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=_uuid)
    chat_id = Column(String, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    role = Column(String, nullable=False)  # user | assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chat = relationship("Chat", back_populates="messages")


class Automation(Base):
    __tablename__ = "automations"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    instructions = Column(Text, nullable=False)
    skill_id = Column(String, nullable=True)
    schedule = Column(String, nullable=False)  # 5-field cron: "0 9 * * *"
    is_active = Column(Boolean, default=True, nullable=False)
    slack_channel = Column(String, nullable=True)
    owner_sub = Column(String, nullable=False, index=True, default=lambda: OWNER_LEGACY_SENTINEL)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    runs = relationship(
        "AutomationRun",
        back_populates="automation",
        cascade="all, delete-orphan",
        order_by="AutomationRun.started_at.desc()",
    )


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id = Column(String, primary_key=True, default=_uuid)
    automation_id = Column(
        String, ForeignKey("automations.id", ondelete="CASCADE"), nullable=False
    )
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running", nullable=False)
    output = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    duration_secs = Column(Float, nullable=True)

    automation = relationship("Automation", back_populates="runs")


class ResourceProposal(Base):
    """A draft Skill/Automation/Lab proposed by the agent, awaiting user approval.

    Created synchronously by the propose_* tool handlers in main.py once the
    agent has gathered enough info from the user in-chat. `content` holds a
    kind-specific JSON payload (e.g. {"skill_md": ..., "api_ref_url": ...}
    for skills, or the automation/app creation fields for the other kinds).
    """

    __tablename__ = "resource_proposals"

    id = Column(String, primary_key=True, default=_uuid)
    chat_id = Column(String, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)  # "skill" | "automation" | "lab"
    name = Column(String(300), nullable=False)
    content = Column(Text, nullable=False)  # JSON string, kind-specific payload
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending|approved|discarded
    created_resource_id = Column(String, nullable=True)  # automation/app id after approval; null for skills
    owner_sub = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserEvent(Base):
    """Append-only behavioral event log — one row per user action."""
    __tablename__ = "user_events"

    id         = Column(String, primary_key=True, default=_uuid)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    owner_sub  = Column(String, nullable=False, index=True)
    email      = Column(String(320), nullable=False, default="", index=True)
    event      = Column(String(80), nullable=False, index=True)   # e.g. "lab.app_created"
    properties = Column(Text, nullable=False, default="{}")       # JSON string


class TokenUsageLog(Base):
    """One row per LLM call (or accumulated multi-turn interaction)."""
    __tablename__ = "token_usage_logs"

    id            = Column(String, primary_key=True, default=_uuid)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    # Source: "chat" | "slack" | "automation" | "lab" | "a2a" | "skill_create" | "skill_publish" | "memory"
    source        = Column(String(40), nullable=False, index=True)
    model         = Column(String(80), nullable=False)
    user_email    = Column(String(320), nullable=True, index=True)
    agent_id      = Column(String, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
    input_tokens          = Column(Integer, nullable=False, default=0)
    output_tokens         = Column(Integer, nullable=False, default=0)
    cache_read_tokens     = Column(Integer, nullable=False, default=0)
    cache_creation_tokens = Column(Integer, nullable=False, default=0)


class Memory(Base):
    __tablename__ = "memory"
    __table_args__ = (
        UniqueConstraint("owner_sub", "key", name="uq_memory_owner_key"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    owner_sub = Column(String, nullable=False, index=True)
    key = Column(String, nullable=False)
    title = Column(String, nullable=False)
    category = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    source = Column(String, nullable=True)
    status = Column(String, default="pending", nullable=False)
    last_confirmed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MemorySyncLog(Base):
    __tablename__ = "memory_sync_log"

    id = Column(String, primary_key=True, default=_uuid)
    owner_sub = Column(String, nullable=True, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running", nullable=False)
    entries_created = Column(Integer, default=0, nullable=False)
    entries_updated = Column(Integer, default=0, nullable=False)
    entries_pruned = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)


class StoredPdf(Base):
    """Maps filenames under generated/ to tenant for `/api/files/...`."""

    __tablename__ = "stored_pdfs"

    id = Column(String, primary_key=True, default=_uuid)
    stored_name = Column(String, unique=True, nullable=False)
    owner_sub = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OpsAlertState(Base):
    """Persistent debounce / notification state for infrastructure ops alerts."""

    __tablename__ = "ops_alert_state"
    __table_args__ = (UniqueConstraint("alert_key", name="uq_ops_alert_state_key"),)

    id = Column(String, primary_key=True, default=_uuid)
    alert_key = Column(String(120), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="ok")  # ok | breaching
    breach_streak = Column(Integer, nullable=False, default=0)
    first_breach_at = Column(DateTime, nullable=True)
    last_notified_at = Column(DateTime, nullable=True)
    slack_thread_ts = Column(String(32), nullable=True)
    last_value = Column(String(500), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(String, primary_key=True, default=_uuid)
    owner_sub = Column(String, nullable=False, index=True)
    email = Column(String, nullable=False)
    route = Column(String, nullable=False)
    element_context = Column(Text, nullable=False)  # JSON string
    user_message = Column(Text, nullable=False)
    category = Column(String, nullable=False)  # bug | feature | ux | other
    status = Column(String, nullable=False, default="open")  # open | triaged | in_progress | done | wont_do
    assignee = Column(String, nullable=True)  # email or name of triage owner
    priority = Column(String, nullable=True)  # low | medium | high | urgent
    resolution_note = Column(Text, nullable=True)
    triaged_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SlackUserOAuthState(Base):
    """Short-lived CSRF state token for the Slack user-integration OAuth flow.

    One row is created by POST /api/slack-user-oauth/start and consumed (then
    deleted) by GET /api/slack-user-oauth/callback.  Rows expire after 15 min.
    """

    __tablename__ = "slack_user_oauth_states"

    id = Column(String, primary_key=True, default=_uuid)   # UUID used as OAuth state param
    owner_sub = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)


class GoogleUserOAuthState(Base):
    """Short-lived CSRF state token for the Google Workspace user-integration OAuth flow.

    One row is created by POST /api/google-user-oauth/start and consumed (then
    deleted) by GET /api/google-user-oauth/callback.  Rows expire after 15 min.
    """

    __tablename__ = "google_user_oauth_states"

    id = Column(String, primary_key=True, default=_uuid)   # UUID used as OAuth state param
    owner_sub = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)


class UserProfile(Base):
    """Per-user app preferences keyed by JWT sub (login + product tour state)."""

    __tablename__ = "user_profiles"

    owner_sub = Column(String, primary_key=True)
    last_login_at = Column(DateTime, nullable=True)
    last_seen_tour_version = Column(String(32), nullable=True)
    enabled_models_json = Column(Text, nullable=True)
    preferred_model = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class UserSecret(Base):
    """A user-owned personal secret stored as vault referenceerence.

    One row per (owner_sub, slug) pair.  The actual secret value lives in the
    the vault; only the opaque version ref is persisted here so raw token
    material never touches the database.

    secret name: {RESOURCE_PREFIX}-user-{sha256(owner_sub)[:32]}-{slug_lowercase}
    """

    __tablename__ = "user_secrets"
    __table_args__ = (
        UniqueConstraint("owner_sub", "slug", name="uq_user_secrets_owner_slug"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    owner_sub = Column(String, nullable=False, index=True)
    slug = Column(String(120), nullable=False)          # env var name, e.g. MY_SLACK_TOKEN
    display_name = Column(String(200), nullable=True)   # human-readable label
    description = Column(String(500), nullable=True)
    secret_ref = Column(String(512), nullable=False)    # version resource name
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AgentSession(Base):
    """Durable per-agent-per-thread session state for the warm async fleet.

    session_id:     application-assigned, e.g. "{agent_id}:{channel_id}:{thread_ts}"
    version:        OCC counter — every turn does UPDATE … WHERE version = :orig
    state:          full compacted agent context (JSONB on Postgres)
    summary:        compacted oldest-turns summary; NULL until first compaction fires
    history_cursor: marks verbatim-tail start in Slack history; NULL until first compaction
    working_state:  durable scratch state carried across turns (JSONB on Postgres)

    fillfactor=70: every turn bumps version + rewrites state; leaving 30% headroom per
    page enables Postgres HOT (Heap-Only Tuple) updates and avoids page splits under
    ~2000 concurrent hot sessions.
    """

    __tablename__ = "agent_sessions"

    session_id     = Column(Text, primary_key=True)
    agent_id       = Column(Text, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    state          = Column(JSON, nullable=False, default=dict)
    version        = Column(Integer, nullable=False, default=0)
    summary        = Column(Text, nullable=True)
    history_cursor = Column(Text, nullable=True)
    working_state  = Column(JSON, nullable=False, default=dict)
    created_at     = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at     = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)


class ReplyOutbox(Base):
    """Transactional outbox for Slack replies — co-committed with the OCC state update.

    event_id:    unique queue message-id / Slack event-id — the idempotency key.
    payload:     Slack reply content to post via chat.update.
    status:      'pending' (committed, not yet delivered) | 'delivered' (sent to Slack).
    slack_ts:    message timestamp returned by chat.update; stored after delivery.
    delivered_at: timestamp of delivery — supports TTL cleanup of old delivered rows.

    Partial index on (created_at) WHERE status = 'pending': the outbox sweeper query
    only ever scans the small pending tail; at scale the vast majority of rows are
    'delivered' and are invisible to the index entirely.
    """

    __tablename__ = "reply_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_reply_outbox_event_id"),
        CheckConstraint("status IN ('pending', 'delivered')", name="ck_reply_outbox_status"),
    )

    id           = Column(Text, primary_key=True, default=_uuid)
    event_id     = Column(Text, nullable=False)
    session_id   = Column(Text, ForeignKey("agent_sessions.session_id", ondelete="CASCADE"), nullable=False)
    payload      = Column(Text, nullable=False)
    status       = Column(Text, nullable=False, default="pending")
    slack_ts     = Column(Text, nullable=True)
    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)


class TurnIdempotency(Base):
    """Per-event idempotency + audit record — co-committed with the OCC write.

    event_id:     PRIMARY KEY — crash-recovery lookup is always a single PK scan.
    committed_at: index supports TTL cleanup (delete rows older than ack-deadline window).

    Intentionally no FK to agent_sessions — the idempotency record must survive
    session deletion so a redelivered event after cascade-delete is correctly
    recognised as already-processed and skipped rather than reprocessed.
    """

    __tablename__ = "turn_idempotency"

    event_id     = Column(Text, primary_key=True)
    session_id   = Column(Text, nullable=False)
    committed_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)


def _table_cols(conn, table: str) -> set[str]:
    """Return the set of column names for *table*. Works on SQLite and PostgreSQL."""
    if engine.dialect.name == "sqlite":
        rows = conn.execute(sa_text(f"PRAGMA table_info({table})")).fetchall()
        return {r[1] for r in rows}
    rows = conn.execute(
        sa_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = 'public'"
        ),
        {"t": table},
    ).fetchall()
    return {r[0] for r in rows}


def table_cols(conn, table: str) -> set[str]:
    """Public alias — importable by agents.py, access_models.py, etc."""
    return _table_cols(conn, table)


def _rebuild_memory_table_add_owner_sub(conn) -> None:
    """Add owner_sub + compound uniqueness by rebuilding; SQLite cannot DROP INDEX on UNIQUE autoindexes."""
    conn.execute(
        sa_text("""
            CREATE TABLE memory_new (
                id TEXT PRIMARY KEY NOT NULL,
                owner_sub TEXT NOT NULL,
                key TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                status TEXT NOT NULL,
                last_confirmed_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_memory_owner_key UNIQUE (owner_sub, key)
            )
        """)
    )
    conn.execute(
        sa_text("""
            INSERT INTO memory_new
                (id, owner_sub, key, title, category, content, source, status,
                 last_confirmed_at, created_at, updated_at)
            SELECT
                id, :sent, key, title, category, content, source, status,
                last_confirmed_at, created_at, updated_at
            FROM memory
        """),
        {"sent": OWNER_LEGACY_SENTINEL},
    )
    conn.execute(sa_text("DROP TABLE memory"))
    conn.execute(sa_text("ALTER TABLE memory_new RENAME TO memory"))
    conn.execute(
        sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_owner_key ON memory(owner_sub, key)"
        )
    )


def _rebuild_memory_table_compound_owner(conn) -> None:
    conn.execute(
        sa_text("""
            CREATE TABLE memory_new (
                id TEXT PRIMARY KEY NOT NULL,
                owner_sub TEXT NOT NULL,
                key TEXT NOT NULL,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                status TEXT NOT NULL,
                last_confirmed_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                CONSTRAINT uq_memory_owner_key UNIQUE (owner_sub, key)
            )
        """)
    )
    conn.execute(
        sa_text("""
            INSERT INTO memory_new
            SELECT id, owner_sub, key, title, category, content, source, status,
                   last_confirmed_at, created_at, updated_at
            FROM memory
        """)
    )
    conn.execute(sa_text("DROP TABLE memory"))
    conn.execute(sa_text("ALTER TABLE memory_new RENAME TO memory"))
    conn.execute(
        sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_owner_key ON memory(owner_sub, key)"
        )
    )


def _is_sqlite() -> bool:
    return engine.dialect.name == "sqlite"


def _table_cols_pg(conn, table: str) -> set[str]:
    rows = conn.execute(
        sa_text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    ).fetchall()
    return {r[0] for r in rows}


def init_db() -> None:
    from agents import init_agent_tables  # noqa: F401
    from access_models import init_access_tables  # noqa: F401

    Base.metadata.create_all(engine)
    OpsAlertState.__table__.create(engine, checkfirst=True)
    SlackUserOAuthState.__table__.create(engine, checkfirst=True)
    GoogleUserOAuthState.__table__.create(engine, checkfirst=True)

    if _is_sqlite():
        # SQLite-only incremental migration helpers (column additions, table rebuilds).
        # On PostgreSQL, create_all() handles the schema; these patches are not needed.
        with engine.begin() as conn:
            cols_auto = _table_cols(conn, "automations")
            if "slack_channel" not in cols_auto:
                conn.execute(sa_text("ALTER TABLE automations ADD COLUMN slack_channel TEXT"))

            if "owner_sub" not in _table_cols(conn, "chats"):
                conn.execute(sa_text("ALTER TABLE chats ADD COLUMN owner_sub TEXT"))
                conn.execute(
                    sa_text("UPDATE chats SET owner_sub = :s WHERE owner_sub IS NULL"),
                    {"s": OWNER_LEGACY_SENTINEL},
                )

            if "owner_sub" not in _table_cols(conn, "automations"):
                conn.execute(sa_text("ALTER TABLE automations ADD COLUMN owner_sub TEXT"))
                conn.execute(
                    sa_text("UPDATE automations SET owner_sub = :s WHERE owner_sub IS NULL"),
                    {"s": OWNER_LEGACY_SENTINEL},
                )

            mem_cols_before = _table_cols(conn, "memory")
            if mem_cols_before:
                if "owner_sub" not in mem_cols_before:
                    logger.info("Migrating memory: rebuild table to add owner_sub")
                    _rebuild_memory_table_add_owner_sub(conn)

                ix_check = conn.execute(
                    sa_text(
                        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_memory_owner_key'"
                    )
                ).fetchone()
                if ix_check is None:
                    try:
                        conn.execute(
                            sa_text(
                                "CREATE UNIQUE INDEX uq_memory_owner_key ON memory(owner_sub, key)"
                            )
                        )
                    except Exception:
                        logger.warning("Rebuilding memory table for multitenant UNIQUE(owner_sub,key)")
                        _rebuild_memory_table_compound_owner(conn)

            slog = _table_cols(conn, "memory_sync_log")
            if slog and "owner_sub" not in slog:
                conn.execute(sa_text("ALTER TABLE memory_sync_log ADD COLUMN owner_sub TEXT"))

            fb_cols = _table_cols(conn, "feedback")
            if fb_cols:
                if "assignee" not in fb_cols:
                    conn.execute(sa_text("ALTER TABLE feedback ADD COLUMN assignee TEXT"))
                if "priority" not in fb_cols:
                    conn.execute(sa_text("ALTER TABLE feedback ADD COLUMN priority TEXT"))

            wf_defn_cols = _table_cols(conn, "workflow_definitions")
            if wf_defn_cols:
                if "owner_sub" not in wf_defn_cols:
                    conn.execute(sa_text("ALTER TABLE workflow_definitions ADD COLUMN owner_sub TEXT"))
                    conn.execute(
                        sa_text("UPDATE workflow_definitions SET owner_sub = :s WHERE owner_sub IS NULL"),
                        {"s": OWNER_LEGACY_SENTINEL},
                    )
                if "kind" not in wf_defn_cols:
                    conn.execute(
                        sa_text("ALTER TABLE workflow_definitions ADD COLUMN kind TEXT")
                    )
                    conn.execute(
                        sa_text("UPDATE workflow_definitions SET kind = 'playbook' WHERE kind IS NULL")
                    )

            wf_run_cols = _table_cols(conn, "workflow_runs")
            if wf_run_cols and "owner_sub" not in wf_run_cols:
                conn.execute(sa_text("ALTER TABLE workflow_runs ADD COLUMN owner_sub TEXT"))
                conn.execute(
                    sa_text("UPDATE workflow_runs SET owner_sub = :s WHERE owner_sub IS NULL"),
                    {"s": OWNER_LEGACY_SENTINEL},
                )

            profile_cols = _table_cols(conn, "user_profiles")
            if profile_cols:
                if "enabled_models_json" not in profile_cols:
                    conn.execute(
                        sa_text("ALTER TABLE user_profiles ADD COLUMN enabled_models_json TEXT")
                    )
                if "preferred_model" not in profile_cols:
                    conn.execute(
                        sa_text("ALTER TABLE user_profiles ADD COLUMN preferred_model TEXT")
                    )

    Base.metadata.create_all(engine)
    init_agent_tables(engine)
    from workflow_models import init_workflow_tables  # noqa: F401

    init_workflow_tables(engine)
    init_access_tables(engine)
    from setup_models import init_setup_tables

    init_setup_tables(engine)
