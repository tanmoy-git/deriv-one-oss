"""Queries for the per-user memory store (Memory, MemorySyncLog)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database import Memory, MemorySyncLog
from tenancy import get_tenant_or_404


class MemoryRepo:
    """Every query is scoped to one owner; there is no unscoped read."""

    def __init__(self, db: Session, owner_sub: str) -> None:
        self.db = db
        self.owner_sub = owner_sub

    def list_entries(self) -> list[Memory]:
        return (
            self.db.query(Memory)
            .filter(Memory.owner_sub == self.owner_sub)
            .order_by(Memory.category, Memory.updated_at.desc())
            .all()
        )

    def count(self) -> int:
        return self.db.query(Memory).filter(Memory.owner_sub == self.owner_sub).count()

    def last_sync(self) -> Optional[MemorySyncLog]:
        return (
            self.db.query(MemorySyncLog)
            .filter(MemorySyncLog.owner_sub == self.owner_sub)
            .order_by(MemorySyncLog.started_at.desc())
            .first()
        )

    def get_or_404(self, memory_id: str) -> Memory:
        """404 for both missing and someone else's entry, so ids can't be probed."""
        return get_tenant_or_404(self.db, Memory, memory_id, self.owner_sub, "Memory entry not found")

    def update(
        self,
        memory_id: str,
        *,
        status: Optional[str] = None,
        title: Optional[str] = None,
        content: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Memory:
        entry = self.get_or_404(memory_id)
        if status is not None:
            entry.status = status
        if title is not None:
            entry.title = title
        if content is not None:
            entry.content = content
        if category is not None:
            entry.category = category
        entry.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        return entry

    def delete(self, memory_id: str) -> None:
        self.db.delete(self.get_or_404(memory_id))
        self.db.commit()
