"""Scheduler worker — standalone process for automation crons and memory mining.

Extracted from main.py lifespan so the FastAPI API pods can scale horizontally
without firing duplicate cron jobs.

Run:
    python -m scheduler_worker

Deployment:
    - Run two replicas on PostgreSQL. They elect a leader through a Postgres
      advisory lock (scheduler_leader.py); only the leader registers and fires
      jobs, and the standby takes over within SCHEDULER_LEASE_CHECK_S seconds
      if the leader dies. A leader that loses its lock exits so it cannot fire
      jobs a new leader is also firing.
    - On SQLite there is no lock to share, so run exactly one replica.
    - Future: replace per-automation APScheduler jobs with native K8s CronJob objects.

Environment (same as backend pods — injected via ConfigMap + ExternalSecret):
    DATABASE_URL          postgresql+asyncpg://...  (required)
    MODEL_GATEWAY_URL      model gateway base URL
    MODEL_GATEWAY_API_KEY       gateway API key
    MODEL                 Claude model slug
    SCHEDULER_LEASE_CHECK_S  seconds between leader checks (default 10)
    SCHEDULER_METRICS_PORT   serve Prometheus metrics on this port (unset: off)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from dotenv import load_dotenv

load_dotenv()

# This process owns all recurring cron jobs — must be set before importing main.
os.environ["ENABLE_CRON_SCHEDULER"] = "true"

from logging_setup import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger("scheduler_worker")

# ── Import shared logic from main.py ──────────────────────────────────────────
# scheduler_worker re-uses all helper functions defined in main.py.
# main.py is safe to import without starting the FastAPI HTTP server because
# the lifespan context manager only runs when uvicorn starts the app.

from main import (  # noqa: E402
    _mine_memory_job,
    scheduler,
    sync_automations_from_db,
)
import main as _main  # noqa: E402
import agent_scheduled_tasks as _agent_tasks  # noqa: E402
from agent_worker import spawn_automation, spawn_agent_scheduled_task  # noqa: E402
from database import Memory, SessionLocal, engine, init_db  # noqa: E402
from scheduler_leader import LeaderLease  # noqa: E402
import metrics as _metrics  # noqa: E402
from skill_submission_models import init_skill_submission_tables  # noqa: E402
from agent_scheduled_tasks import load_and_schedule_agent_tasks  # noqa: E402

# Replace the in-process automation runner with the isolated subprocess version.
# _schedule_automation() calls scheduler.add_job(_run_automation_job, ...) by
# reading the name from main's global namespace, so patching it here ensures
# every job registered after this point runs in its own isolated process.
_main._run_automation_job = spawn_automation  # type: ignore[attr-defined]
_agent_tasks._run_agent_scheduled_task = spawn_agent_scheduled_task  # type: ignore[attr-defined]


def _load_and_schedule_automations() -> int:
    """Load all active automations from DB and register them with APScheduler."""
    return sync_automations_from_db()


def _resync_automations() -> None:
    """Re-load automations from DB (picks up API-side CRUD without scheduler restart)."""
    try:
        sync_automations_from_db()
    except Exception:
        logger.exception("Automation resync failed")


def _schedule_memory_mining() -> bool:
    """Schedule the 24h memory mining job. Returns True if an immediate run is needed."""
    db = SessionLocal()
    try:
        has_memory = db.query(Memory).count() > 0
    finally:
        db.close()

    scheduler.add_job(
        _mine_memory_job,
        trigger="interval",
        hours=24,
        id="memory-mining",
        replace_existing=True,
        misfire_grace_time=None,
    )
    logger.info("Scheduled 24h memory mining job")
    return not has_memory


def _resync_agent_scheduled_tasks() -> None:
    """Re-load agent scheduled tasks from DB (picks up API-side CRUD changes)."""
    try:
        load_and_schedule_agent_tasks()
    except Exception:
        logger.exception("Agent scheduled-task resync failed")


_lease = LeaderLease(engine)


def _handle_shutdown(signum, frame):
    logger.info("Received signal %d — shutting down scheduler", signum)
    if scheduler.running:
        scheduler.shutdown(wait=False)
    _lease.release()
    sys.exit(0)


def main() -> None:
    logger.info("Scheduler worker starting up")

    # Initialise DB schema (idempotent — safe to call even if tables already exist)
    init_db()
    init_skill_submission_tables()

    # Pull skills from the blob store — scheduler pods do not run the FastAPI lifespan hook.
    try:
        from skills_store import pull_from_store
        pull_from_store()
    except Exception:
        logger.exception("Initial skills pull failed in scheduler worker")

    metrics_port = os.environ.get("SCHEDULER_METRICS_PORT", "").strip()
    if metrics_port:
        _metrics.serve_on_port(int(metrics_port))
        logger.info("Serving Prometheus metrics on port %s", metrics_port)

    # Standbys stop here until the leader goes away. SIGTERM must still work
    # while waiting, so install the handlers first.
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)
    check_every = float(os.environ.get("SCHEDULER_LEASE_CHECK_S", "10"))
    _metrics.SCHEDULER_IS_LEADER.set(0)
    _lease.wait_until_leader(check_every=check_every)
    _metrics.SCHEDULER_IS_LEADER.set(1)

    # Register automations
    count = _load_and_schedule_automations()

    # Bounded pool for heavy cron subprocesses (validates env / logs capacity).
    from cron_worker_pool import get_cron_worker_pool

    pool = get_cron_worker_pool()
    logger.info(
        "Cron worker pool ready — max_concurrent_subprocesses=%s",
        pool.max_workers,
    )

    # Register agent scheduled tasks
    agent_task_count = load_and_schedule_agent_tasks()

    # Register memory mining and check whether an immediate run is needed
    run_immediately = _schedule_memory_mining()

    # Re-sync agent tasks periodically so API-side CRUD is picked up without restart
    scheduler.add_job(
        _resync_agent_scheduled_tasks,
        trigger="interval",
        seconds=60,
        id="agent-tasks-resync",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Re-sync automations periodically (API pods have ENABLE_CRON_SCHEDULER=false)
    scheduler.add_job(
        _resync_automations,
        trigger="interval",
        seconds=60,
        id="automations-resync",
        replace_existing=True,
        misfire_grace_time=30,
    )

    def _periodic_skills_sync() -> None:
        try:
            from skills_store import pull_from_store
            pull_from_store()
        except Exception:
            logger.exception("Periodic skills sync failed in scheduler worker")

    scheduler.add_job(
        _periodic_skills_sync,
        trigger="interval",
        seconds=60,
        id="skills-store-sync",
        replace_existing=True,
        misfire_grace_time=30,
    )

    from sandbox_reaper import reap_stale_sandbox_claims_sync

    def _reap_stale_sandbox_claims_job() -> None:
        try:
            n = reap_stale_sandbox_claims_sync()
            if n:
                logger.info("sandbox_reaper: deleted %d stale SandboxClaim(s)", n)
        except Exception:
            logger.exception("sandbox_reaper periodic job failed")

    try:
        startup_max_age = int(os.environ.get("SANDBOX_CLAIM_STARTUP_MAX_AGE_S", "900"))
        n = reap_stale_sandbox_claims_sync(max_age_seconds=startup_max_age)
        if n:
            logger.info("scheduler: reaped %d stale SandboxClaim(s) on startup", n)
    except Exception:
        logger.exception("scheduler: stale SandboxClaim cleanup failed")

    scheduler.add_job(
        _reap_stale_sandbox_claims_job,
        trigger="interval",
        seconds=int(os.environ.get("SANDBOX_CLAIM_REAP_INTERVAL_S", "300")),
        id="sandbox-claim-reaper",
        replace_existing=True,
        misfire_grace_time=60,
    )

    from agent_memory_jobs import run_agent_memory_maintenance_job

    scheduler.add_job(
        run_agent_memory_maintenance_job,
        trigger="interval",
        hours=int(os.environ.get("AGENT_MEMORY_MAINTENANCE_INTERVAL_HOURS", "6")),
        id="agent-memory-maintenance",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    from ops_alerts import run_ops_alert_checks

    def _ops_alerts_job() -> None:
        try:
            run_ops_alert_checks()
        except Exception:
            logger.exception("ops_alerts periodic job failed")

    scheduler.add_job(
        _ops_alerts_job,
        trigger="interval",
        seconds=int(os.environ.get("OPS_ALERT_POLL_INTERVAL_S", "60")),
        id="ops-alerts",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Start the scheduler
    scheduler.start()
    logger.info(
        "APScheduler started with %d automation job(s) and %d agent task job(s)",
        count,
        agent_task_count,
    )

    # Run memory mining immediately if the DB has no entries yet (first boot)
    if run_immediately:
        import threading
        logger.info("No memory entries found — scheduling immediate memory mining in 10s")
        t = threading.Timer(10, _mine_memory_job)
        t.daemon = True
        t.start()

    logger.info("Scheduler worker running — press Ctrl+C to stop")
    try:
        while True:
            time.sleep(check_every)
            if not _lease.still_held():
                # Another replica may already be leader. Stop firing jobs at
                # once and exit so the orchestrator restarts this pod as a
                # standby.
                logger.error("Lost the scheduler lease — stopping to avoid running jobs twice")
                _metrics.SCHEDULER_IS_LEADER.set(0)
                scheduler.shutdown(wait=False)
                os._exit(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=True)
        _lease.release()
        logger.info("Scheduler worker stopped")


if __name__ == "__main__":
    main()
