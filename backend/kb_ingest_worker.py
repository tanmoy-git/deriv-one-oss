"""Background worker that imports uploaded documents into the retrieval provider.

Follows the lease-poll pattern of ``overnight_worker.py``: claims work with a
compare-and-set on lease columns so multiple replicas can run safely, and
recovers automatically from a crashed replica once its lease expires.

Why a worker rather than doing this inline in the upload request: an
``import_documents`` LRO plus indexing takes 30 seconds to 15 minutes, far too
long to hold an HTTP request open.

Per batch:
  1. Claim all pending documents for one knowledge base.
  2. One ``import_documents`` call for the whole batch (not one per file — that
     would multiply the indexing wait by the file count).
  3. Reconcile per-document outcomes via paginated ``list_documents``, because
     the LRO only reports aggregate success/failure counts.

See AGENT_KNOWLEDGE_BASE_SPEC.md §4.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta

import agent_knowledge_base_index as kb_index
import agent_knowledge_base_store as kb_store
from agents import (
    KB_DOC_FAILED,
    KB_DOC_IMPORTING,
    KB_DOC_INDEXED,
    KB_DOC_PENDING,
    KB_STATUS_READY,
    Agent,
    AgentKnowledgeBase,
    AgentKnowledgeBaseDocument,
)
from database import SessionLocal, init_db
from sqlalchemy import text as sa_text

logger = logging.getLogger("kb_ingest_worker")

_WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
_POLL_INTERVAL_S = int(os.environ.get("KB_INGEST_POLL_INTERVAL_S", "15"))
# Lease must outlive a worst-case import LRO (20 min) plus reconciliation, or a
# second replica would claim the same documents mid-import and double-import them.
_LEASE_TTL_S = int(os.environ.get("KB_INGEST_LEASE_TTL_S", "1800"))
_BATCH_LIMIT = int(os.environ.get("KB_INGEST_BATCH_LIMIT", "20"))

# After the import LRO reports success, indexing continues asynchronously for up
# to ~15 minutes. Rather than spending the retrieval provider queries probing for
# searchability (which would pollute the per-agent cost counter, spec §4), the
# document is stamped indexed at LRO-success plus this buffer.
_INDEX_BUFFER_S = int(os.environ.get("KB_INDEX_BUFFER_SECS", "120"))


def _now() -> datetime:
    return datetime.utcnow()


def _claim_batch(only_kb_id: str | None = None) -> tuple[str, list[str]] | None:
    """Claim pending documents for one knowledge base.

    Returns ``(knowledge_base_id, [document_id, ...])`` or None. Documents are
    grouped by knowledge base because an import call targets a single data store.

    ``only_kb_id`` restricts the scan to a single knowledge base, used by the
    upload path to ingest immediately without picking up unrelated work.
    """
    now = _now()
    db = SessionLocal()
    try:
        candidate_q = (
            db.query(AgentKnowledgeBaseDocument)
            .filter(
                # `importing` is claimable too, but only with a dead lease: that
                # is the crash-recovery path. A process that claimed a document
                # and then died (pod recycled, OOM) leaves it at `importing`; if
                # only `pending` were claimable it would be stranded forever.
                AgentKnowledgeBaseDocument.index_status.in_(
                    (KB_DOC_PENDING, KB_DOC_IMPORTING)
                ),
                # Unleased (fresh upload), or a previous holder's lease expired.
                (
                    (AgentKnowledgeBaseDocument.lease_owner.is_(None))
                    | (AgentKnowledgeBaseDocument.lease_expires_at.is_(None))
                    | (AgentKnowledgeBaseDocument.lease_expires_at < now)
                ),
            )
            .order_by(AgentKnowledgeBaseDocument.created_at.asc())
        )
        if only_kb_id:
            candidate_q = candidate_q.filter(
                AgentKnowledgeBaseDocument.knowledge_base_id == only_kb_id
            )
        candidate = candidate_q.first()
        if not candidate:
            return None

        kb_id = candidate.knowledge_base_id
        siblings = (
            db.query(AgentKnowledgeBaseDocument.id)
            .filter(
                AgentKnowledgeBaseDocument.knowledge_base_id == kb_id,
                AgentKnowledgeBaseDocument.index_status.in_(
                    (KB_DOC_PENDING, KB_DOC_IMPORTING)
                ),
                (
                    (AgentKnowledgeBaseDocument.lease_owner.is_(None))
                    | (AgentKnowledgeBaseDocument.lease_expires_at.is_(None))
                    | (AgentKnowledgeBaseDocument.lease_expires_at < now)
                ),
            )
            .order_by(AgentKnowledgeBaseDocument.created_at.asc())
            .limit(_BATCH_LIMIT)
            .all()
        )

        claimed: list[str] = []
        expires = now + timedelta(seconds=_LEASE_TTL_S)
        for (doc_id,) in siblings:
            res = db.execute(
                sa_text(
                    "UPDATE agent_knowledge_base_documents "
                    "SET lease_owner=:w, lease_expires_at=:exp, index_status=:importing "
                    "WHERE id=:id AND index_status IN (:pending, :importing) "
                    "AND (lease_owner IS NULL OR lease_expires_at IS NULL "
                    "     OR lease_expires_at < :now)"
                ),
                {
                    "w": _WORKER_ID,
                    "exp": expires,
                    "now": now,
                    "id": doc_id,
                    "pending": KB_DOC_PENDING,
                    "importing": KB_DOC_IMPORTING,
                },
            )
            if res.rowcount == 1:
                claimed.append(doc_id)
        db.commit()

        if not claimed:
            return None
        logger.info("Claimed %d document(s) for knowledge base %s", len(claimed), kb_id)
        return (kb_id, claimed)
    finally:
        db.close()


def _fail_batch(doc_ids: list[str], message: str) -> None:
    db = SessionLocal()
    try:
        for doc_id in doc_ids:
            doc = db.get(AgentKnowledgeBaseDocument, doc_id)
            if doc and doc.index_status == KB_DOC_IMPORTING:
                doc.index_status = KB_DOC_FAILED
                doc.index_error = message[:2000]
                doc.lease_owner = None
                doc.lease_expires_at = None
        db.commit()
    finally:
        db.close()


def _process_batch(kb_id: str, doc_ids: list[str]) -> None:
    db = SessionLocal()
    try:
        kb = db.get(AgentKnowledgeBase, kb_id)
        if not kb or kb.status != KB_STATUS_READY or not kb.data_store_id:
            _fail_batch(doc_ids, "Knowledge base is not ready.")
            return
        agent = db.get(Agent, kb.agent_id)
        agent_id, agent_name = kb.agent_id, (agent.name if agent else None)
        store_id, location = kb.data_store_id, kb.data_store_location
        bucket = kb.storage_namespace

        docs = [db.get(AgentKnowledgeBaseDocument, d) for d in doc_ids]
        uri_by_doc = {
            d.id: kb_store.storage_uri(bucket, d.object_path) for d in docs if d
        }
    finally:
        db.close()

    if not uri_by_doc:
        return

    try:
        success, failure = kb_index.import_documents(
            store_id,
            list(uri_by_doc.values()),
            agent_id=agent_id,
            agent_name=agent_name,
            location=location,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Import failed for knowledge base %s", kb_id)
        _fail_batch(doc_ids, f"Import failed: {exc}")
        return

    # Per-document reconciliation: the LRO reports only aggregate counts, so map
    # each storage key back to its server-generated document ID.
    try:
        found = kb_index.map_uris_to_document_ids(
            store_id, set(uri_by_doc.values()), location=location
        )
    except Exception:  # noqa: BLE001
        logger.exception("Reconciliation failed for knowledge base %s", kb_id)
        found = {}

    # Indexing continues after the LRO; stamp indexed_at into the future so the
    # API only reports "indexed" once the buffer has elapsed.
    indexed_at = _now() + timedelta(seconds=_INDEX_BUFFER_S)

    db = SessionLocal()
    try:
        for doc_id, uri in uri_by_doc.items():
            doc = db.get(AgentKnowledgeBaseDocument, doc_id)
            if not doc:
                continue
            de_id = found.get(uri)
            if de_id:
                doc.index_document_id = de_id
                doc.index_status = KB_DOC_INDEXED
                doc.index_error = None
                doc.indexed_at = indexed_at
            else:
                doc.index_status = KB_DOC_FAILED
                doc.index_error = (
                    "Document was not found in the data store after import "
                    f"(batch reported success={success} failure={failure})."
                )
            doc.lease_owner = None
            doc.lease_expires_at = None
        db.commit()
    finally:
        db.close()

    indexed_count = sum(1 for uri in uri_by_doc.values() if uri in found)
    if indexed_count:
        try:
            from kb_usage import EVENT_IMPORT, record_kb_usage

            record_kb_usage(
                agent_id=agent_id, event_type=EVENT_IMPORT, unit_count=indexed_count
            )
        except Exception:  # noqa: BLE001
            logger.debug("Usage recording skipped", exc_info=True)

    logger.info(
        "Knowledge base %s: %d/%d document(s) indexed",
        kb_id, indexed_count, len(uri_by_doc),
    )


def ingest_pending_for_kb(knowledge_base_id: str) -> None:
    """Import every pending document for one knowledge base, now.

    Called from the upload endpoint via ``BackgroundTasks`` so documents start
    indexing as soon as they are uploaded rather than waiting for the worker's
    next poll. Safe to run concurrently with the worker: both claim rows through
    the same compare-and-set lease, so a document is only ever imported once.

    The worker is still required as the durable path — if this process dies
    mid-import the lease simply expires and the worker re-claims the rows.
    """
    while True:
        claim = _claim_batch(only_kb_id=knowledge_base_id)
        if not claim:
            return
        _process_batch(*claim)


def _main_loop() -> None:
    logger.info(
        "KB ingest worker %s starting (poll=%ss, lease_ttl=%ss, batch=%s)",
        _WORKER_ID, _POLL_INTERVAL_S, _LEASE_TTL_S, _BATCH_LIMIT,
    )
    while True:
        try:
            claim = _claim_batch()
            if claim:
                _process_batch(*claim)
                continue  # look for more work immediately
        except Exception:  # noqa: BLE001
            logger.exception("KB ingest worker poll error")
        time.sleep(_POLL_INTERVAL_S)


def main() -> None:
    from logging_setup import configure_logging

    configure_logging()
    init_db()
    try:
        _main_loop()
    except KeyboardInterrupt:
        logger.info("KB ingest worker stopped")


if __name__ == "__main__":
    main()
