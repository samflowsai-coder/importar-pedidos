from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from app.models.order import Order


@dataclass
class PreviewEntry:
    preview_id: str
    order: Order
    source_filename: str
    source_bytes: bytes
    source_ext: str
    created_at: float
    source_path: Optional[str] = None  # set when preview came from watch folder
    check: Optional[dict] = None       # product-match report against Fire
    consumed: bool = False


class PreviewConsumedError(Exception):
    pass


class PreviewNotFoundError(Exception):
    pass


class PreviewCache:
    """
    In-memory preview store with TTL and LRU eviction.

    - Single-process. A uvicorn reload clears it (acceptable in dev).
    - `consume()` atomically marks an entry as taken so a double-click from the
      UI cannot trigger two commits against the same preview.
    """

    def __init__(self, ttl_seconds: int = 15 * 60, max_entries: int = 50) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: "OrderedDict[str, PreviewEntry]" = OrderedDict()
        self._lock = threading.Lock()

    def put(
        self,
        order: Order,
        source_filename: str,
        source_bytes: bytes,
        source_ext: str,
        source_path: Optional[str] = None,
        check: Optional[dict] = None,
    ) -> PreviewEntry:
        preview_id = str(uuid.uuid4())
        entry = PreviewEntry(
            preview_id=preview_id,
            order=order,
            source_filename=source_filename,
            source_bytes=source_bytes,
            source_ext=source_ext,
            created_at=time.time(),
            source_path=source_path,
            check=check,
        )
        with self._lock:
            self._evict_expired_locked()
            self._entries[preview_id] = entry
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return entry

    def get(self, preview_id: str) -> Optional[PreviewEntry]:
        with self._lock:
            self._evict_expired_locked()
            entry = self._entries.get(preview_id)
            if entry is None:
                return None
            self._entries.move_to_end(preview_id)
            return entry

    def consume(self, preview_id: str) -> PreviewEntry:
        with self._lock:
            self._evict_expired_locked()
            entry = self._entries.get(preview_id)
            if entry is None:
                raise PreviewNotFoundError(preview_id)
            if entry.consumed:
                raise PreviewConsumedError(preview_id)
            entry.consumed = True
            self._entries.move_to_end(preview_id)
            return entry

    def drop(self, preview_id: str) -> None:
        with self._lock:
            self._entries.pop(preview_id, None)

    def size(self) -> int:
        with self._lock:
            self._evict_expired_locked()
            return len(self._entries)

    def _evict_expired_locked(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [pid for pid, e in self._entries.items() if e.created_at < cutoff]
        for pid in stale:
            self._entries.pop(pid, None)


_default_cache: Optional[PreviewCache] = None


def get_cache() -> PreviewCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = PreviewCache()
    return _default_cache
