"""Sessions repository — server-side cookie store.

The cookie value is a 32-byte random token (URL-safe base64). Lookups are
O(1) on the PK, so we don't need to cache anywhere — every request is a
single point query.

TTL is absolute (`expires_at` set at creation time). On each `get_active`
we check the clock; expired rows are deleted lazily. A periodic prune
job (Phase 5 worker) handles old expired rows in bulk.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.persistence import db

DEFAULT_TTL_HOURS = 24
TOKEN_BYTES = 32  # 256-bit, base64-encoded


@dataclass(frozen=True)
class Session:
    token: str
    user_id: int
    created_at: str
    expires_at: str
    ip: str | None
    user_agent: str | None


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        token=row["token"],
        user_id=int(row["user_id"]),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        ip=row["ip"],
        user_agent=row["user_agent"],
    )


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def new_token() -> str:
    """High-entropy URL-safe token suitable for a cookie value."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def create_session(
    *,
    user_id: int,
    ip: str | None = None,
    user_agent: str | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Session:
    now = _now()
    token = new_token()
    expires_at = now + timedelta(hours=ttl_hours)
    with db.connect_shared() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (token, user_id, _iso(now), _iso(expires_at), ip, (user_agent or "")[:500]),
        )
    return Session(
        token=token,
        user_id=user_id,
        created_at=_iso(now),
        expires_at=_iso(expires_at),
        ip=ip,
        user_agent=user_agent,
    )


def get_active(token: str) -> Session | None:
    """Return the session if not expired; else lazily delete and return None."""
    if not token:
        return None
    with db.connect_shared() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        sess = _row_to_session(row)
        # Compare ISO strings — they sort lexicographically when same format.
        if sess.expires_at <= _iso(_now()):
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    return sess


def delete(token: str) -> None:
    if not token:
        return
    with db.connect_shared() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def delete_all_for_user(user_id: int) -> int:
    """Sign out everywhere — used for password change, account suspension, etc."""
    with db.connect_shared() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return cur.rowcount


def prune_expired() -> int:
    """Bulk delete expired sessions. Worker calls this periodically (Phase 5)."""
    with db.connect_shared() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?", (_iso(_now()),)
        )
    return cur.rowcount


__all__ = [
    "DEFAULT_TTL_HOURS",
    "Session",
    "create_session",
    "delete",
    "delete_all_for_user",
    "get_active",
    "new_token",
    "prune_expired",
]
