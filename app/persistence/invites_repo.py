"""User invitations — admin issues one-shot tokens; invitee sets password.

Lifecycle:
    create()           → status = pending  (token + expires_at set)
    accept_for_user()  → accepted_at + accepted_user_id stamped (terminal)
    revoke()           → revoked_at stamped (terminal)

`is_pending` covers all three rejection criteria in one check:
    not accepted, not revoked, not expired.

We don't enforce "only one pending per email" at DB level (SQLite partial
unique index supports it, but we keep schema simple). Application-level
check in `create()` raises `OpenInviteExistsError` if there's already a
pending row for the email.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.persistence import db
from app.persistence.users_repo import VALID_ROLES, InvalidRoleError

DEFAULT_TTL_HOURS = 24 * 7  # 7 days
TOKEN_BYTES = 32  # URL-safe base64 ~43 chars


class OpenInviteExistsError(Exception):
    """A pending (non-accepted, non-revoked, non-expired) invite already
    exists for this email. Caller should revoke the old one first or use it.
    """


class InviteNotFoundError(Exception):
    """No invite for the given token (or it was deleted)."""


class InviteUnusableError(Exception):
    """Invite exists but cannot be accepted: expired, revoked, or already accepted."""


@dataclass(frozen=True)
class Invite:
    token: str
    email: str
    role: str
    invited_by_user_id: int
    created_at: str
    expires_at: str
    accepted_at: str | None
    accepted_user_id: int | None
    revoked_at: str | None

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, *, now: str | None = None) -> bool:
        cutoff = now or datetime.now().isoformat(timespec="seconds")
        return self.expires_at <= cutoff

    @property
    def is_pending(self) -> bool:
        return not self.is_accepted and not self.is_revoked and not self.is_expired()


def _row(r: sqlite3.Row) -> Invite:
    return Invite(
        token=r["token"],
        email=r["email"],
        role=r["role"],
        invited_by_user_id=int(r["invited_by_user_id"]),
        created_at=r["created_at"],
        expires_at=r["expires_at"],
        accepted_at=r["accepted_at"],
        accepted_user_id=(
            int(r["accepted_user_id"]) if r["accepted_user_id"] is not None else None
        ),
        revoked_at=r["revoked_at"],
    )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_email(email: str) -> str:
    return email.strip().lower()


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def create(
    *,
    email: str,
    role: str,
    invited_by_user_id: int,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> Invite:
    if role not in VALID_ROLES:
        raise InvalidRoleError(f"role must be one of {sorted(VALID_ROLES)}")
    norm = _norm_email(email)
    if not norm or "@" not in norm:
        raise ValueError("invalid email")
    # Application-level "single open invite" check
    existing = find_pending_for_email(norm)
    if existing is not None:
        raise OpenInviteExistsError(
            f"open invite already exists for {norm}; revoke it before creating another"
        )
    now = datetime.now()
    expires = now + timedelta(hours=ttl_hours)
    token = new_token()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO user_invites
                (token, email, role, invited_by_user_id,
                 created_at, expires_at, accepted_at, accepted_user_id, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (
                token, norm, role, invited_by_user_id,
                now.isoformat(timespec="seconds"),
                expires.isoformat(timespec="seconds"),
            ),
        )
        row = conn.execute(
            "SELECT * FROM user_invites WHERE token = ?", (token,)
        ).fetchone()
    return _row(row)


def get_by_token(token: str) -> Invite | None:
    if not token:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_invites WHERE token = ?", (token,)
        ).fetchone()
    return _row(row) if row else None


def find_pending_for_email(email: str) -> Invite | None:
    norm = _norm_email(email)
    now = _now_iso()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM user_invites
             WHERE email = ? COLLATE NOCASE
               AND accepted_at IS NULL
               AND revoked_at  IS NULL
               AND expires_at  > ?
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (norm, now),
        ).fetchone()
    return _row(row) if row else None


def list_pending() -> list[Invite]:
    """All invites the admin should still see in the UI: pending OR expired
    (so admin notices to revoke). Excludes already-accepted and revoked.
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM user_invites
             WHERE accepted_at IS NULL
               AND revoked_at  IS NULL
             ORDER BY created_at DESC
            """
        ).fetchall()
    return [_row(r) for r in rows]


def accept_for_user(token: str, *, accepted_user_id: int) -> Invite:
    """Stamp accepted_at + accepted_user_id. Atomic — uses a conditional
    UPDATE so concurrent calls only one wins.
    """
    invite = get_by_token(token)
    if invite is None:
        raise InviteNotFoundError(f"invite not found: {token[:8]}...")
    if not invite.is_pending:
        raise InviteUnusableError(
            "invite already accepted, revoked, or expired"
        )
    now = _now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE user_invites
               SET accepted_at = ?,
                   accepted_user_id = ?
             WHERE token = ?
               AND accepted_at IS NULL
               AND revoked_at  IS NULL
               AND expires_at  > ?
            """,
            (now, accepted_user_id, token, now),
        )
        if cur.rowcount == 0:
            # Lost race: another caller accepted/revoked between our read and update.
            raise InviteUnusableError("invite no longer pending")
        row = conn.execute(
            "SELECT * FROM user_invites WHERE token = ?", (token,)
        ).fetchone()
    return _row(row)


def revoke(token: str) -> bool:
    """Mark revoked_at. Returns True if it changed something. Idempotent
    on already-terminal invites (accepted: returns False; revoked: returns False).
    """
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE user_invites
               SET revoked_at = ?
             WHERE token = ?
               AND accepted_at IS NULL
               AND revoked_at  IS NULL
            """,
            (_now_iso(), token),
        )
    return cur.rowcount > 0


def prune_old(*, older_than_days: int = 30) -> int:
    """Delete accepted/revoked invites older than N days. Worker job (Phase 5+).
    Pending-but-expired are left alone — admin should see and revoke explicitly.
    """
    cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM user_invites
             WHERE (accepted_at IS NOT NULL AND accepted_at < ?)
                OR (revoked_at  IS NOT NULL AND revoked_at  < ?)
            """,
            (cutoff, cutoff),
        )
    return cur.rowcount


__all__ = [
    "DEFAULT_TTL_HOURS",
    "Invite",
    "InviteNotFoundError",
    "InviteUnusableError",
    "OpenInviteExistsError",
    "accept_for_user",
    "create",
    "find_pending_for_email",
    "get_by_token",
    "list_pending",
    "new_token",
    "prune_old",
    "revoke",
]
