"""Slack event consumer — takes events off the queue and dispatches them to agents.

This process replaces the direct HTTP inbound path for Slack events. The flow is:

  Slack → external DMZ relay (your own gateway service)
        → message queue topic: slack.events
        → this worker (pulls slack-events-sub subscription)
        → agent_worker.spawn_agent_request() — isolated subprocess per agent

Each agent event is dispatched to a dedicated OS subprocess (via agent_worker)
so that no two agents' secrets ever share a process address space.  The parent
worker only handles routing and deduplication; it never loads agent secrets.

The worker runs as a separate process and can be scaled horizontally — the
dedup lease is what keeps two replicas off the same event. It shares the same
image as the backend.

Run:
    python -m slack_consumer_worker

Environment (the same values the API process uses):
    DATABASE_URL               postgresql+asyncpg://...  (required)
    MESSAGE_QUEUE              which queue provider to use
    SLACK_EVENTS_TOPIC         topic to consume (default: slack.events)
    MODEL_GATEWAY_URL / API_KEY model gateway credentials
    INTERNAL_SIGNING_SECRET    agent-to-agent invocation signing secret

Deduplication:
    Slack guarantees at-least-once delivery. Dedup is done in the DB via
    claim_slack_event(), a *leased* claim on event_id: a claim is held as
    in_progress with a time-bounded lease (refreshed while the dispatch runs),
    marked done as soon as the Slack reply posts (not after audit/memory), and
    only deleted on failure *before* a reply. A lease orphaned by an abrupt
    worker death (OOM / SIGKILL / eviction) before the reply expires and is
    reclaimed by the next delivery. A failure after the reply keeps status=done
    so redelivery is ack-dropped — no double post. No Redis required for dedup
    at current scale — Redis is used only for IAM cache and APScheduler locks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from logging_setup import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger("slack_consumer_worker")

# ── Config ────────────────────────────────────────────────────────────────────

SLACK_EVENTS_TOPIC: str = os.environ.get("SLACK_EVENTS_TOPIC", "slack.events")

# A dispatch can run for many minutes, so the dedup lease is refreshed on this
# cadence while the subprocess is alive. Redelivery timing belongs to the queue
# implementation; this only keeps the application-level claim alive.
LEASE_REFRESH_INTERVAL_S: int = int(os.environ.get("LEASE_REFRESH_INTERVAL", "45"))
MAX_CONCURRENT_DISPATCH: int = int(
    os.environ.get("SLACK_CONSUMER_MAX_CONCURRENT_DISPATCH", "4")
)

# ── Import event dispatcher from api_slack_events ────────────────────────────
# We reuse _dispatch_event, _get_agent_by_app_id, claim_slack_event_id, and
# validate_slack_event_id from the existing HTTP handler module.

from database import init_db, SessionLocal  # noqa: E402
from skill_submission_models import init_skill_submission_tables  # noqa: E402
from agents import (  # noqa: E402
    CLAIM_DONE,
    CLAIM_IN_PROGRESS,
    agent_accepts_runtime_requests,
    claim_slack_event,
    is_slack_event_done,
    mark_slack_event_done,
    purge_stale_slack_event_dedup,
    refresh_slack_event_lease,
    release_slack_event_id,
    validate_slack_event_id,
)
from api_slack_events import (  # noqa: E402
    _get_agent_by_app_id,
    _is_ambient_channel,
    _notify_agent_unavailable_from_event,
)
from agent_worker import spawn_agent_request  # noqa: E402
from providers import get_message_queue

# ── Event envelope schema ───────────────────────────────────────────────────
# Messages published by the DMZ relay follow this structure:
# {
#   "envelope_id": "<uuid>",
#   "slack_event_id": "EvXXXXX",
#   "api_app_id": "AXXXXXX",
#   "team_id": "TXXXXXX",
#   "type": "event_callback",
#   "event": { "type": "app_mention", "user": "...", "text": "...", ... },
#   "received_at_ms": 1715610601234
# }

_shutdown_requested = False
_loop: Optional[asyncio.AbstractEventLoop] = None


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    logger.info("Received signal %d — requesting graceful shutdown", signum)
    _shutdown_requested = True


async def _with_lease_heartbeat(
    loop: asyncio.AbstractEventLoop,
    executor: ThreadPoolExecutor,
    work_coro,
    event_id: str = "",
):
    """Keep the dedup lease fresh while a long dispatch is in flight.

    A dispatch can run for minutes. The lease is what stops a second worker
    picking up the same event, so it has to be refreshed for as long as this
    one is still working. When this worker dies the heartbeat stops, the lease
    lapses, and a redelivery is free to reclaim the event.

    Redelivery timing itself is the message queue's problem, not ours — see
    ``providers.queue``. This only owns the application-level claim.
    """

    async def _heartbeat() -> None:
        while not _shutdown_requested:
            if event_id:
                try:
                    await loop.run_in_executor(
                        executor, refresh_slack_event_lease, event_id, None
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to refresh dedup lease event_id=%s: %s", event_id, exc
                    )
            try:
                await asyncio.sleep(LEASE_REFRESH_INTERVAL_S)
            except asyncio.CancelledError:
                break

    beat = asyncio.create_task(_heartbeat())
    try:
        return await work_coro
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _release_claim(
    loop: asyncio.AbstractEventLoop,
    event_id: str,
) -> None:
    if not event_id:
        return
    await loop.run_in_executor(None, release_slack_event_id, event_id)


async def _process_message(
    data: dict,
    executor: ThreadPoolExecutor,
    dispatch_semaphore: asyncio.Semaphore,
) -> bool:
    """Process one inbound event.

    Returns True when the message is finished with (whether it succeeded or was
    discarded as unprocessable), and False when it should be retried. A message
    we cannot parse is *not* retried — a poison pill would otherwise redeliver
    forever.
    """
    if not isinstance(data, dict):
        logger.error("queue message is not a JSON object — discarding")
        return True

    msg_type = data.get("type", "")

    # Slack url_verification should never reach the queue, but be safe.
    if msg_type == "url_verification":
        logger.info("url_verification challenge received on the queue — ignoring")
        return True

    if msg_type != "event_callback":
        logger.debug("Ignoring non-event_callback message type=%s", msg_type)
        return True

    slack_app_id: str = data.get("api_app_id", "").strip()
    event_id: str = (data.get("event_id") or data.get("slack_event_id") or "").strip()
    event: dict = data.get("event", {})
    event_type: str = event.get("type", "")
    channel_type: str = event.get("channel_type", "")
    channel: str = event.get("channel", "")

    logger.info(
        "Processing event app=%s event_id=%s event_type=%s",
        slack_app_id, event_id or "(none)", event_type,
    )

    # Ignore bot messages to prevent reply loops.
    # Exception: app_mention events from bots are let through so that
    # authorised Slack apps (AgentSlackAppGrant) can invoke agents.
    # The grant check happens inside _dispatch_event; unauthorised bots are
    # dropped there.  Non-mention bot events (broadcasts, DMs, etc.) are
    # still filtered here.
    if event.get("subtype") == "bot_message" or event.get("bot_id"):
        if event_type != "app_mention":
            return True

    # Look up agent
    if not slack_app_id:
        logger.warning("No api_app_id in the event — discarding")
        return True

    agent = _get_agent_by_app_id(slack_app_id)
    if not agent:
        logger.warning("No agent registered for slack_app_id=%s — discarding", slack_app_id)
        return True

    # Only dispatch app_mention, DMs, channel-join events, or — when the agent
    # has opted into "ambient" mode for this specific channel — a plain
    # channel message. A message that also mentions this bot is excluded from
    # the ambient clause: that same text already fires its own app_mention
    # event, which is the one that dispatches (avoids a double-reply).
    own_bot_user_id = str(getattr(agent, "slack_bot_user_id", "") or "").strip()
    mentions_this_bot = bool(
        own_bot_user_id and f"<@{own_bot_user_id}>" in (event.get("text") or "")
    )
    is_plain_channel_message = (
        event_type == "message"
        and channel_type != "im"
        and not event.get("subtype")
        and not mentions_this_bot
    )
    should_dispatch = (
        event_type == "app_mention"
        or (event_type == "message" and channel_type == "im")
        or event_type == "member_joined_channel"
        or (is_plain_channel_message and _is_ambient_channel(agent, channel))
    )
    if not should_dispatch:
        return True

    if not agent_accepts_runtime_requests(agent.status):
        logger.info(
            "Agent not active — discarding event app=%s agent=%s status=%s",
            slack_app_id, agent.id, agent.status,
        )
        is_bot = bool(event.get("bot_id") or event.get("subtype") == "bot_message")
        if not is_bot:
            try:
                _notify_agent_unavailable_from_event(agent, event)
            except Exception:
                logger.exception(
                    "Failed to notify unavailable agent app=%s agent=%s",
                    slack_app_id, agent.id,
                )
        return True

    # member_joined_channel fires for every user who joins — only run on-join flow
    # when this agent's bot is the one being added to the channel.
    if event_type == "member_joined_channel":
        from api_slack_events import _is_bot_joining_channel
        if not _is_bot_joining_channel(agent, event):
            logger.debug(
                "Ignoring member_joined_channel app=%s user=%s bot=%s",
                slack_app_id, event.get("user"), agent.slack_bot_user_id,
            )
            return True

    claimed_event = False
    if event_id:
        if not validate_slack_event_id(event_id):
            logger.warning(
                "Unexpected event_id shape app=%s event_id=%s — skipping dedup",
                slack_app_id, event_id,
            )
        else:
            outcome = claim_slack_event(event_id)
            if outcome == CLAIM_DONE:
                logger.info(
                    "Duplicate event ignored (already completed) app=%s event_id=%s",
                    slack_app_id, event_id,
                )
                return True  # ack — genuine duplicate
            if outcome == CLAIM_IN_PROGRESS:
                logger.info(
                    "Event already in progress on another worker app=%s event_id=%s — nacking",
                    slack_app_id, event_id,
                )
                return False  # nack — a live lease owns it; redeliver after it frees
            claimed_event = True  # CLAIM_NEW — we own the lease
    else:
        logger.warning(
            "Event missing event_id app=%s event_type=%s — cannot deduplicate",
            slack_app_id, event_type,
        )

    agent_id = str(agent.id)
    loop = asyncio.get_running_loop()

    # run_in_executor only forwards *args; bind event_id via lambda.
    async def _spawn() -> bool:
        return await loop.run_in_executor(
            None,
            lambda: spawn_agent_request(
                agent_id,
                event,
                event_id=event_id if claimed_event else "",
            ),
        )

    try:
        async with dispatch_semaphore:
            success = await _with_lease_heartbeat(
                loop,
                executor,
                _spawn(),
                event_id if claimed_event else "",
            )
    except Exception:
        logger.exception(
            "Unhandled exception spawning agent worker app=%s event_id=%s",
            slack_app_id, event_id,
        )
        if claimed_event:
            # No-op if the reply was already committed (status=done).
            await _release_claim(loop, event_id)
            if await loop.run_in_executor(None, is_slack_event_done, event_id):
                logger.info(
                    "Worker crashed but reply already committed app=%s event_id=%s — acking",
                    slack_app_id, event_id,
                )
                return True
        return False

    if not success:
        if claimed_event and await loop.run_in_executor(None, is_slack_event_done, event_id):
            # Reply posted inside the worker; housekeeping/timeout failed afterward.
            # Do not release or redeliver — that would double-post.
            logger.warning(
                "Agent worker failed after reply committed app=%s event_id=%s agent_id=%s — acking",
                slack_app_id, event_id, agent_id,
            )
            return True
        logger.warning(
            "Agent worker reported failure app=%s event_id=%s agent_id=%s — nacking for retry",
            slack_app_id, event_id, agent_id,
        )
        if claimed_event:
            await _release_claim(loop, event_id)
        return False

    # Completed successfully — mark the lease done so redeliveries are ack-dropped
    # (idempotent if the worker already marked done after the Slack post).
    if claimed_event:
        await loop.run_in_executor(None, mark_slack_event_done, event_id)

    return True


async def _run_consumer() -> None:
    """Main async consumer loop. Pulls messages in batches and processes them."""
    logger.info(
        "Starting event consumer topic=%s max_concurrent_dispatch=%d",
        SLACK_EVENTS_TOPIC,
        MAX_CONCURRENT_DISPATCH,
    )

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=4)
    dispatch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DISPATCH)
    queue = get_message_queue()

    # Skill-invalidation events so this worker picks up edits immediately
    # rather than waiting for the periodic sync.
    _skills_subscriber_task: Optional[asyncio.Task] = None
    try:
        from skills_invalidate import run_subscriber as _run_skills_subscriber

        _skills_subscriber_task = asyncio.create_task(_run_skills_subscriber())
        logger.info("Skills invalidation subscriber started")
    except Exception:
        logger.exception(
            "Could not start skills invalidation subscriber — relying on the periodic sync"
        )

    async def _handle(payload: dict) -> None:
        """Queue handler. Raising means "retry"; returning means "done".

        Dispatch is capped by the semaphore rather than by how fast messages
        arrive, so a burst queues up instead of spawning unbounded subprocesses.
        """
        done = await _process_message(payload, executor, dispatch_semaphore)
        if not done:
            raise RuntimeError("message not processed — leaving it for redelivery")

    await queue.subscribe(SLACK_EVENTS_TOPIC, _handle)
    await queue.start()

    purge_counter = 0
    while not _shutdown_requested:
        await asyncio.sleep(1)
        purge_counter += 1
        # Periodically drop stale dedup rows, as the HTTP path does.
        if purge_counter % 100 == 0:
            await loop.run_in_executor(executor, purge_stale_slack_event_dedup)

    logger.info("Consumer loop exited (shutdown requested)")
    if _skills_subscriber_task and not _skills_subscriber_task.done():
        _skills_subscriber_task.cancel()
    await queue.stop()
    executor.shutdown(wait=False)


def main() -> None:
    global _loop

    logger.info("Slack consumer worker starting up")

    # Initialise DB schema (idempotent)
    init_db()
    init_skill_submission_tables()

    # Pull the latest skills-acl.yaml + skill files from the blob store so this worker
    # uses the same ACL as the backend pods (the seed baked into the image may
    # be older than what admins have published via the platform).
    try:
        from skills_store import pull_from_store
        pull_from_store()
        logger.info("Skills pull complete")
    except Exception:
        logger.exception("Skills pull failed — continuing with seed ACL")

    # Register shutdown handlers
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    try:
        _loop.run_until_complete(_run_consumer())
    finally:
        _loop.close()

    logger.info("Slack consumer worker stopped")


if __name__ == "__main__":
    main()
