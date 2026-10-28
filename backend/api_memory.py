"""Reading and editing a user's memory entries.

The sync endpoints (/api/memory/sync, /api/memory/sync-debug) are still in
main.py next to the mining job they run.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth_middleware import deps_owner_sub
from database import DB, Memory
from memory_repo import MemoryRepo

router = APIRouter()


class MemoryUpdate(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None


def _memory_to_dict(m: Memory) -> dict:
    return {
        "id": m.id,
        "key": m.key,
        "title": m.title,
        "category": m.category,
        "content": m.content,
        "source": m.source,
        "status": m.status,
        "last_confirmed_at": m.last_confirmed_at.isoformat(),
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
    }


@router.get("/api/memory")
def list_memory(db: DB, owner_sub: str = Depends(deps_owner_sub)):
    repo = MemoryRepo(db, owner_sub)
    last_log = repo.last_sync()
    return {
        "entries": [_memory_to_dict(e) for e in repo.list_entries()],
        "last_sync": {
            "started_at": last_log.started_at.isoformat() if last_log else None,
            "finished_at": last_log.finished_at.isoformat() if last_log and last_log.finished_at else None,
            "status": last_log.status if last_log else None,
        },
    }


@router.put("/api/memory/{memory_id}")
def update_memory(memory_id: str, body: MemoryUpdate, db: DB, owner_sub: str = Depends(deps_owner_sub)):
    entry = MemoryRepo(db, owner_sub).update(
        memory_id,
        status=body.status,
        title=body.title,
        content=body.content,
        category=body.category,
    )
    return _memory_to_dict(entry)


@router.delete("/api/memory/{memory_id}")
def delete_memory(memory_id: str, db: DB, owner_sub: str = Depends(deps_owner_sub)):
    MemoryRepo(db, owner_sub).delete(memory_id)
    return {"ok": True}
