"""Tests for the invite flow: invites_repo + admin endpoints + public accept.

The accept route is public (no session required) so it's the only place a
user can ever be created from the UI after the first admin exists.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, invites_repo, sessions_repo, users_repo
from app.persistence.invites_repo import (
    InviteUnusableError,
    OpenInviteExistsError,
)
from app.persistence.users_repo import InvalidRoleError


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    """Repo-level fixture (no real auth needed)."""
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch, real_auth):
    """Fresh DB + real auth. Used for HTTP-level tests."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


# ── Helpers ──────────────────────────────────────────────────────────────


def _bootstrap_admin(c: TestClient, email: str = "admin@x.com") -> None:
    c.post("/api/auth/bootstrap", json={"email": email, "password": "supersecret1"})


def _new_admin_user_id() -> int:
    u = users_repo.create_user(email="admin@x.com", password="supersecret1", role="admin")
    return u.id


# ── invites_repo ─────────────────────────────────────────────────────────


def test_repo_create_returns_pending(sqlite_tmp):
    admin_id = _new_admin_user_id()
    inv = invites_repo.create(
        email="alice@x.com", role="operator", invited_by_user_id=admin_id,
    )
    assert inv.email == "alice@x.com"
    assert inv.role == "operator"
    assert inv.is_pending
    assert not inv.is_accepted
    assert not inv.is_revoked
    assert inv.token  # opaque, but non-empty


def test_repo_normalizes_email_lowercase(sqlite_tmp):
    admin_id = _new_admin_user_id()
    inv = invites_repo.create(
        email="Alice@Example.COM", role="operator", invited_by_user_id=admin_id,
    )
    assert inv.email == "alice@example.com"


def test_repo_rejects_invalid_role(sqlite_tmp):
    admin_id = _new_admin_user_id()
    with pytest.raises(InvalidRoleError):
        invites_repo.create(email="x@x.com", role="superuser", invited_by_user_id=admin_id)


def test_repo_rejects_invalid_email(sqlite_tmp):
    admin_id = _new_admin_user_id()
    with pytest.raises(ValueError):
        invites_repo.create(email="not-an-email", role="operator", invited_by_user_id=admin_id)


def test_repo_rejects_second_open_invite_for_same_email(sqlite_tmp):
    admin_id = _new_admin_user_id()
    invites_repo.create(email="dup@x.com", role="operator", invited_by_user_id=admin_id)
    with pytest.raises(OpenInviteExistsError):
        invites_repo.create(email="dup@x.com", role="admin", invited_by_user_id=admin_id)
    # Even with different case
    with pytest.raises(OpenInviteExistsError):
        invites_repo.create(email="DUP@X.COM", role="operator", invited_by_user_id=admin_id)


def test_repo_revoke_then_create_again(sqlite_tmp):
    admin_id = _new_admin_user_id()
    inv1 = invites_repo.create(email="a@x.com", role="operator", invited_by_user_id=admin_id)
    invites_repo.revoke(inv1.token)
    # After revoke, a fresh invite is allowed
    inv2 = invites_repo.create(email="a@x.com", role="operator", invited_by_user_id=admin_id)
    assert inv2.token != inv1.token


def test_repo_accept_sets_terminal_state(sqlite_tmp):
    admin_id = _new_admin_user_id()
    inv = invites_repo.create(
        email="invitee@x.com", role="operator", invited_by_user_id=admin_id,
    )
    user = users_repo.create_user(email="invitee@x.com", password="strongpass1")
    accepted = invites_repo.accept_for_user(inv.token, accepted_user_id=user.id)
    assert accepted.is_accepted
    assert accepted.accepted_user_id == user.id
    assert not accepted.is_pending
    # Second accept on same token raises
    with pytest.raises(InviteUnusableError):
        invites_repo.accept_for_user(inv.token, accepted_user_id=user.id)


def test_repo_revoked_invite_not_acceptable(sqlite_tmp):
    admin_id = _new_admin_user_id()
    inv = invites_repo.create(email="x@x.com", role="operator", invited_by_user_id=admin_id)
    invites_repo.revoke(inv.token)
    user = users_repo.create_user(email="x@x.com", password="strongpass1")
    with pytest.raises(InviteUnusableError):
        invites_repo.accept_for_user(inv.token, accepted_user_id=user.id)


def test_repo_expired_invite_not_pending(sqlite_tmp):
    admin_id = _new_admin_user_id()
    # Insert directly with expired timestamp
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    now  = datetime.now().isoformat(timespec="seconds")
    with db.connect_shared() as conn:
        conn.execute(
            """
            INSERT INTO user_invites
                (token, email, role, invited_by_user_id, created_at, expires_at)
            VALUES ('expired-tok', 'x@x.com', 'operator', ?, ?, ?)
            """,
            (admin_id, now, past),
        )
    inv = invites_repo.get_by_token("expired-tok")
    assert inv is not None
    assert inv.is_expired()
    assert not inv.is_pending


def test_repo_revoke_idempotent(sqlite_tmp):
    admin_id = _new_admin_user_id()
    inv = invites_repo.create(email="x@x.com", role="operator", invited_by_user_id=admin_id)
    assert invites_repo.revoke(inv.token) is True   # first time changes
    assert invites_repo.revoke(inv.token) is False  # already revoked


def test_repo_list_pending_excludes_accepted_and_revoked(sqlite_tmp):
    admin_id = _new_admin_user_id()
    p1 = invites_repo.create(email="p1@x.com", role="operator", invited_by_user_id=admin_id)
    invites_repo.create(email="p2@x.com", role="operator", invited_by_user_id=admin_id)
    invites_repo.revoke(p1.token)
    pending = invites_repo.list_pending()
    emails = {inv.email for inv in pending}
    assert emails == {"p2@x.com"}


# ── Admin endpoints ──────────────────────────────────────────────────────


def test_admin_create_invite_returns_url(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={
        "email": "new@x.com", "role": "operator",
    })
    assert r.status_code == 201, r.text
    inv = r.json()["invite"]
    assert inv["email"] == "new@x.com"
    assert inv["accept_url"].endswith(f"/invite/{inv['token']}")
    assert inv["expired"] is False


def test_admin_create_invite_requires_admin(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    # Issue invite for an operator
    r = c.post("/api/admin/invites", json={"email": "op@x.com", "role": "operator"})
    inv_token = r.json()["invite"]["token"]
    # Operator accepts
    c2 = TestClient(app)
    c2.post(f"/api/invites/{inv_token}/accept", json={"password": "operpass1"})
    # Operator tries to invite — must be 403
    r = c2.post("/api/admin/invites", json={"email": "x@x.com", "role": "operator"})
    assert r.status_code == 403


def test_admin_create_invite_anonymous_rejected(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/auth/logout")
    r = c.post("/api/admin/invites", json={"email": "x@x.com"})
    assert r.status_code == 401


def test_admin_create_invite_for_existing_user_409(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    # admin@x.com is already a user
    r = c.post("/api/admin/invites", json={"email": "admin@x.com"})
    assert r.status_code == 409


def test_admin_create_invite_duplicate_pending_409(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/invites", json={"email": "dup@x.com"})
    r = c.post("/api/admin/invites", json={"email": "dup@x.com"})
    assert r.status_code == 409


def test_admin_list_invites(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/invites", json={"email": "a@x.com"})
    c.post("/api/admin/invites", json={"email": "b@x.com"})
    r = c.get("/api/admin/invites")
    assert r.status_code == 200
    invites = r.json()["invites"]
    assert len(invites) == 2
    assert all("accept_url" in inv for inv in invites)


def test_admin_revoke_invite(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "rev@x.com"})
    token = r.json()["invite"]["token"]

    r = c.delete(f"/api/admin/invites/{token}")
    assert r.status_code == 200

    # Pending list now excludes it
    r = c.get("/api/admin/invites")
    assert r.json()["invites"] == []


def test_admin_revoke_unknown_token_404(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.delete("/api/admin/invites/no-such-token")
    assert r.status_code == 404


# ── Public accept flow ───────────────────────────────────────────────────


def test_public_get_invite_returns_minimal_info(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "new@x.com", "role": "viewer"})
    token = r.json()["invite"]["token"]

    # Public client (no cookies)
    pub = TestClient(app)
    r = pub.get(f"/api/invites/{token}")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "email": "new@x.com",
        "role": "viewer",
        "expires_at": r.json()["expires_at"],  # only check key presence
    }


def test_public_get_invite_404_for_unknown(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/api/invites/does-not-exist")
    assert r.status_code == 404


def test_public_get_invite_404_for_revoked(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "rev@x.com"})
    token = r.json()["invite"]["token"]
    c.delete(f"/api/admin/invites/{token}")

    pub = TestClient(app)
    r = pub.get(f"/api/invites/{token}")
    assert r.status_code == 404


def test_public_get_invite_410_for_expired(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    admin = users_repo.find_by_email("admin@x.com")
    # Insert expired invite directly
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    now  = datetime.now().isoformat(timespec="seconds")
    with db.connect_shared() as conn:
        conn.execute(
            """
            INSERT INTO user_invites
                (token, email, role, invited_by_user_id, created_at, expires_at)
            VALUES ('expired-tok', 'x@x.com', 'operator', ?, ?, ?)
            """,
            (admin.id, now, past),
        )

    pub = TestClient(app)
    r = pub.get("/api/invites/expired-tok")
    assert r.status_code == 410


def test_public_accept_creates_user_and_logs_in(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "newop@x.com", "role": "operator"})
    token = r.json()["invite"]["token"]

    pub = TestClient(app)
    r = pub.post(f"/api/invites/{token}/accept", json={"password": "freshpass1"})
    assert r.status_code == 201, r.text
    assert r.json()["user"]["email"] == "newop@x.com"
    assert r.json()["user"]["role"] == "operator"
    assert "portal_session" in pub.cookies
    # User is real
    u = users_repo.find_by_email("newop@x.com")
    assert u is not None
    assert u.last_login_at is not None


def test_public_accept_marks_invite_terminal(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "n@x.com"})
    token = r.json()["invite"]["token"]
    pub = TestClient(app)
    pub.post(f"/api/invites/{token}/accept", json={"password": "freshpass1"})

    # Invite no longer in pending list
    r = c.get("/api/admin/invites")
    assert r.json()["invites"] == []
    # Public GET now 404 (already accepted)
    r = pub.get(f"/api/invites/{token}")
    assert r.status_code == 404


def test_public_accept_token_is_single_use(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "n@x.com"})
    token = r.json()["invite"]["token"]
    pub = TestClient(app)
    r1 = pub.post(f"/api/invites/{token}/accept", json={"password": "freshpass1"})
    assert r1.status_code == 201

    pub2 = TestClient(app)
    r2 = pub2.post(f"/api/invites/{token}/accept", json={"password": "anotherpass1"})
    assert r2.status_code == 404


def test_public_accept_rejects_weak_password(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "n@x.com"})
    token = r.json()["invite"]["token"]
    pub = TestClient(app)
    r = pub.post(f"/api/invites/{token}/accept", json={"password": "short"})
    assert r.status_code == 422
    # Invite stays pending — user did not get created
    assert users_repo.find_by_email("n@x.com") is None
    inv = invites_repo.get_by_token(token)
    assert inv.is_pending


def test_public_accept_revoked_token_404(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "rev@x.com"})
    token = r.json()["invite"]["token"]
    c.delete(f"/api/admin/invites/{token}")
    pub = TestClient(app)
    r = pub.post(f"/api/invites/{token}/accept", json={"password": "freshpass1"})
    assert r.status_code == 404


def test_public_accept_expired_token_410(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    admin = users_repo.find_by_email("admin@x.com")
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    now  = datetime.now().isoformat(timespec="seconds")
    with db.connect_shared() as conn:
        conn.execute(
            """
            INSERT INTO user_invites
                (token, email, role, invited_by_user_id, created_at, expires_at)
            VALUES ('exp-tok', 'old@x.com', 'operator', ?, ?, ?)
            """,
            (admin.id, now, past),
        )
    pub = TestClient(app)
    r = pub.post("/api/invites/exp-tok/accept", json={"password": "freshpass1"})
    assert r.status_code == 410


def test_public_accept_when_user_already_exists_409(isolated_app):
    """Edge: admin manually created the same email between issue and accept."""
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "race@x.com"})
    token = r.json()["invite"]["token"]
    # Admin manually creates the user via the legacy endpoint
    c.post("/api/admin/users", json={"email": "race@x.com", "password": "manualpass1"})
    pub = TestClient(app)
    r = pub.post(f"/api/invites/{token}/accept", json={"password": "freshpass1"})
    assert r.status_code == 409


# ── Static page route ───────────────────────────────────────────────────


def test_invite_page_is_served(isolated_app):
    """The /invite/{token} HTML is reachable without auth — page itself
    renders state based on /api/invites/{token}."""
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/invite/anything-here")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ── Sessions: invitee gets logged in correctly ──────────────────────────


def test_accepted_invitee_can_use_session_immediately(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/invites", json={"email": "u@x.com", "role": "operator"})
    token = r.json()["invite"]["token"]

    pub = TestClient(app)
    pub.post(f"/api/invites/{token}/accept", json={"password": "freshpass1"})
    # Cookie set; /api/auth/me returns the user
    r = pub.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "u@x.com"
    # Session in DB
    token_cookie = pub.cookies["portal_session"]
    assert sessions_repo.get_active(token_cookie) is not None
