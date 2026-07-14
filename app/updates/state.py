from __future__ import annotations

import json
import os
from pathlib import Path

_STATUS = "status.json"
_LOCK = "update.lock"
_HIST = "history.jsonl"


def _ensure(updates_dir: Path) -> None:
    updates_dir.mkdir(parents=True, exist_ok=True)


def read_status(updates_dir: Path) -> dict:
    p = updates_dir / _STATUS
    if not p.exists():
        return {"status": "idle"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle"}


def write_status(updates_dir: Path, **fields) -> None:
    _ensure(updates_dir)
    cur = read_status(updates_dir)
    cur.update(fields)
    tmp = updates_dir / (_STATUS + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    tmp.replace(updates_dir / _STATUS)


def append_history(updates_dir: Path, entry: dict) -> None:
    _ensure(updates_dir)
    with open(updates_dir / _HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def lock_path(updates_dir: Path) -> Path:
    return updates_dir / _LOCK


def is_locked(updates_dir: Path) -> bool:
    return lock_path(updates_dir).exists()


def lock_age_seconds(updates_dir: Path, now_ts: float) -> float | None:
    p = lock_path(updates_dir)
    if not p.exists():
        return None
    return now_ts - os.path.getmtime(p)
