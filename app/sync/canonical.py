"""Canonical JSON + sha256 — deterministic hashing for delta detection.

Sorted keys, no whitespace, ASCII-safe (ensure_ascii=False so unicode survives).
None preserved (drop-vs-keep changes the hash, which is intentional —
shape changes are semantic).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


__all__ = ["canonical_hash", "canonical_json"]
