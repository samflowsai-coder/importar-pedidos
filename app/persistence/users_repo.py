"""Users repository — bcrypt hash stored in `users.password_hash`.

Email is case-insensitive (COLLATE NOCASE in schema). All lookups normalize
to lowercase to keep behavior consistent across SQLite versions and clients.

Roles are 'admin' | 'operator' | 'viewer'. Today only "is logged in" is
enforced; role-based gating is reserved for Phase 6.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.persistence import db
from app.security.passwords import hash_password as _hash

VALID_ROLES = frozenset({"admin", "operator", "viewer"})


class DuplicateEmailError(Exception):
    """Raised when create_user collides with an existing email."""


class InvalidRoleError(ValueError):
    """Role not in VALID_ROLES."""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    role: str
    active: bool
    created_at: str
    last_login_at: str | None
    password_hash: str  # exposed because the auth flow needs it; never serialize


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=int(row["id"]),
        email=row["email"],
        role=row["role"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        password_hash=row["password_hash"],
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_email(email: str) -> str:
    return email.strip().lower()


def create_user(
    *,
    email: str,
    password: str,
    role: str = "operator",
) -> User:
    if role not in VALID_ROLES:
        raise InvalidRoleError(f"role must be one of {sorted(VALID_ROLES)}")
    norm = _norm_email(email)
    if not norm or "@" not in norm:
        raise ValueError("invalid email")
    pwd_hash = _hash(password)
    try:
        with db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO users (email, password_hash, role, active, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (norm, pwd_hash, role, _now()),
            )
            uid = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return _row_to_user(row)
    except sqlite3.IntegrityError as exc:
        if "users.email" in str(exc) or "UNIQUE" in str(exc):
            raise DuplicateEmailError(f"email already registered: {norm}") from exc
        raise


def find_by_email(email: str) -> User | None:
    norm = _norm_email(email)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE",
            (norm,),
        ).fetchone()
    return _row_to_user(row) if row else None


def find_by_id(user_id: int) -> User | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return _row_to_user(row) if row else None


def update_last_login(user_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (_now(), user_id),
        )


def update_password_hash(user_id: int, new_hash: str) -> None:
    """Used by opportunistic rehash-on-login (rounds upgrade)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user_id),
        )


def deactivate(user_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET active = 0 WHERE id = ?", (user_id,)
        )


def list_users(limit: int = 100) -> list[User]:
    limit = max(1, min(int(limit), 500))
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_user(r) for r in rows]


def count_active_users() -> int:
    """Used by bootstrap flow: 0 active users → first-admin signup is open."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE active = 1"
        ).fetchone()
    return int(row["n"])


def reactivate(user_id: int) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET active = 1 WHERE id = ?", (user_id,)
        )


__all__ = [
    "VALID_ROLES",
    "DuplicateEmailError",
    "InvalidRoleError",
    "User",
    "count_active_users",
    "create_user",
    "deactivate",
    "find_by_email",
    "find_by_id",
    "list_users",
    "reactivate",
    "update_last_login",
    "update_password_hash",
]
