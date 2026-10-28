"""Isolated agent request runner — spawned as a subprocess per agent invocation.

Each Slack event or automation job runs in a dedicated OS process that loads only
that agent's secrets.  When the process exits the OS reclaims all memory, so no
secrets persist between requests and no agent's credentials ever share a process
address space with another agent's.

Entry points
────────────
run_agent_request(agent_id, event)    — Slack event dispatch
run_automation_isolated(automation_id) — automation cron job
run_agent_scheduled_task(agent_id, task_id) — agent scheduled-task cron job

Spawn helpers (for use by the parent worker processes)
──────────────────────────────────────────────────────
spawn_agent_request(agent_id, event, timeout)
spawn_automation(automation_id, timeout)
spawn_agent_scheduled_task(agent_id, task_id, timeout)

Both helpers use multiprocessing.get_context("spawn") so the child process
starts completely fresh — it does not inherit the parent's in-memory state,
open file descriptors, or any other agent's loaded secrets.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys

logger = logging.getLogger(__name__)

# ── Timeout constants ─────────────────────────────────────────────────────────
# Agent requests can take many tool turns (sandbox + LLM per turn); default 600s.
_AGENT_REQUEST_TIMEOUT_S: int = int(os.environ.get("AGENT_WORKER_TIMEOUT", "600"))
# Automation jobs run LLM code-gen + sandbox execution + report generation.
_AUTOMATION_TIMEOUT_S: int = int(os.environ.get("AUTOMATION_WORKER_TIMEOUT", "300"))
# Agent scheduled tasks use the same multi-turn tool loop as Slack dispatch.
_AGENT_SCHEDULED_TASK_TIMEOUT_S: int = int(
    os.environ.get("AGENT_SCHEDULED_TASK_TIMEOUT", os.environ.get("AGENT_WORKER_TIMEOUT", "600"))
)

# Platform-level keys that no agent subprocess ever needs.  Removed from the
# child's os.environ after load_dotenv() so that a bug in the dispatch path
# cannot expose them even if the subprocess is somehow compromised.
# Names are matched exactly. Extend with DENIED_SUBPROCESS_ENV (comma-separated)
# rather than editing this list, so a deployment's own platform credentials can
# be stripped without carrying a patch.
_SUBPROCESS_ENV_DENY_LIST: frozenset[str] = frozenset({
    "SECRET_ENCRYPTION_KEY",
    "BLOB_URL_SECRET",
    "GITHUB_PAT_TOKEN",
    "GRAPHQL_API_KEY",
}) | frozenset(
    name.strip()
    for name in (os.environ.get("DENIED_SUBPROCESS_ENV") or "").split(",")
    if name.strip()
)


def _pull_skills_from_store() -> None:
    """Ensure skill files are on disk before agent dispatch (scheduler lacks API lifespan pull)."""
    try:
        from skills_store import pull_from_store
        pull_from_store()
    except Exception:
        logger.exception("Failed to pull skills from the blob store in agent subprocess")


# ── Subprocess entry points ───────────────────────────────────────────────────

def run_agent_request(agent_id: str, event: dict, event_id: str = "") -> None:
    """Run one Slack agent request in this isolated process.

    Loads the agent from DB by ID, then calls _dispatch_event which handles all
    secret loading internally (xoxb token, skill secrets, A2A context JWT).
    This function is the ``target`` of a spawned multiprocessing.Process — it
    must not be called directly from the parent worker.

    *event_id* is the Slack Events API envelope id used for leased dedup. When
    set, ``_dispatch_event`` marks the lease done as soon as the reply posts.
    """
    _setup_child_logging("agent-worker")

    from dotenv import load_dotenv
    load_dotenv()
    _scrub_subprocess_env()
    _pull_skills_from_store()

    import asyncio
    from database import SessionLocal, init_db
    from agents import Agent

    init_db()

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
    finally:
        db.close()

    if not agent:
        logger.error("run_agent_request: no agent found for id=%s", agent_id)
        sys.exit(1)

    from api_slack_events import _dispatch_event, _handle_channel_join, _is_bot_joining_channel
    try:
        event_type = event.get("type", "")
        if event_type == "member_joined_channel":
            if not _is_bot_joining_channel(agent, event):
                logger.debug(
                    "run_agent_request: ignoring member_joined_channel — user=%s bot=%s",
                    event.get("user"), agent.slack_bot_user_id,
                )
                return
            asyncio.run(_handle_channel_join(agent, event))
        else:
            asyncio.run(
                _dispatch_event(
                    agent,
                    event,
                    event_id=(event_id or None),
                )
            )
    except Exception:
        logger.exception("run_agent_request: unhandled exception agent_id=%s", agent_id)
        sys.exit(1)


def run_agent_scheduled_task(agent_id: str, task_id: str, manual: bool = False) -> None:
    """Run one agent scheduled task in this isolated process."""
    _setup_child_logging("agent-scheduled-task")

    from dotenv import load_dotenv
    load_dotenv()
    _scrub_subprocess_env()
    _pull_skills_from_store()

    # Schema is initialised by scheduler_worker / API lifespan — avoid init_db()
    # here so concurrent subprocess spawns do not deadlock on migrations.

    try:
        from agent_scheduled_tasks import dispatch_scheduled_task
        import asyncio
        asyncio.run(dispatch_scheduled_task(agent_id, task_id, manual=manual))
    except Exception:
        logger.exception(
            "run_agent_scheduled_task: unhandled exception agent_id=%s task_id=%s",
            agent_id, task_id,
        )
        sys.exit(1)


def run_automation_isolated(automation_id: str) -> None:
    """Run one automation job in this isolated process.

    Delegates to _run_automation_job from main.py which loads its own
    skill secrets and user secrets.  This function is the ``target`` of a
    spawned multiprocessing.Process.
    """
    _setup_child_logging("auto-worker")

    from dotenv import load_dotenv
    load_dotenv()
    _scrub_subprocess_env()
    _pull_skills_from_store()

    from database import init_db
    init_db()

    try:
        from main import _run_automation_job
        _run_automation_job(automation_id)
    except Exception:
        logger.exception("run_automation_isolated: unhandled exception automation_id=%s", automation_id)
        sys.exit(1)


# ── Spawn helpers (called from parent worker processes) ───────────────────────

def spawn_agent_request(
    agent_id: str,
    event: dict,
    timeout: int = _AGENT_REQUEST_TIMEOUT_S,
    event_id: str = "",
) -> bool:
    """Spawn an isolated process for a Slack agent request and block until done.

    Uses the ``spawn`` start method so the child process starts completely fresh
    with no inherited parent state.  Safe to call from a thread (e.g. via
    asyncio run_in_executor) since it does not touch the event loop.

    Returns True if the process exited with code 0, False otherwise.
    """
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=run_agent_request,
        args=(agent_id, event, event_id or ""),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        logger.warning(
            "spawn_agent_request: worker timed out after %ds (agent_id=%s) — terminating",
            timeout, agent_id,
        )
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
        return False

    if proc.exitcode != 0:
        logger.warning(
            "spawn_agent_request: worker exited with code %d (agent_id=%s)",
            proc.exitcode, agent_id,
        )
        return False

    return True


def spawn_agent_scheduled_task(
    agent_id: str,
    task_id: str,
    timeout: int = _AGENT_SCHEDULED_TASK_TIMEOUT_S,
    manual: bool = False,
) -> None:
    """Spawn an isolated process for an agent scheduled task and block until done."""
    from agent_scheduled_tasks import _mark_task_run
    from cron_worker_pool import get_cron_worker_pool

    get_cron_worker_pool().run(
        target=run_agent_scheduled_task,
        args=(agent_id, task_id, manual),
        label=f"agent-task:{task_id}",
        timeout=timeout,
        on_timeout=lambda: _mark_task_run(task_id, "timeout"),
        on_error_exit=lambda _code: _mark_task_run(task_id, "error"),
    )


def spawn_automation(automation_id: str, timeout: int = _AUTOMATION_TIMEOUT_S) -> None:
    """Spawn an isolated process for an automation run and block until done.

    Signature matches _run_automation_job(automation_id) so it can be
    registered directly with APScheduler as a drop-in replacement.
    """
    from cron_worker_pool import get_cron_worker_pool

    get_cron_worker_pool().run(
        target=run_automation_isolated,
        args=(automation_id,),
        label=f"automation:{automation_id}",
        timeout=timeout,
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _setup_child_logging(label: str) -> None:
    """Configure stdout logging for a freshly spawned child process."""
    from logging_setup import configure_logging

    configure_logging(text_format=f"%(asctime)s %(levelname)s %(name)s [{label}] %(message)s", label=label)


def _scrub_subprocess_env() -> None:
    """Remove platform admin keys from the subprocess environment.

    Called in every subprocess entry point after load_dotenv().  Reduces
    blast radius if a bug in the dispatch path ever exposed os.environ —
    agents have no legitimate need for these platform-level credentials.
    """
    for key in _SUBPROCESS_ENV_DENY_LIST:
        removed = os.environ.pop(key, None)
        if removed is not None:
            logger.debug("Scrubbed %s from subprocess env", key)
