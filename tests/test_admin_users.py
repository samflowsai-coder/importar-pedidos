"""Tests for bootstrap signup + admin user-management endpoints.

Bootstrap is a special case: it must work BEFORE any user exists. So this
test module uses the `real_auth` fixture (no bypass) and starts from an
empty DB.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, sessions_repo, users_repo


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch, real_auth):
    """Fresh DB + real auth flow (no bypass)."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _login(client: TestClient, email: str, password: str) -> bool:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    return r.status_code == 200


# ── Bootstrap status + signup ────────────────────────────────────────────


def test_bootstrap_required_when_db_empty(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/api/auth/bootstrap-status")
    assert r.status_code == 200
    assert r.json() == {"required": True}


def test_bootstrap_creates_admin_and_logs_in(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/bootstrap", json={
        "email": "founder@portal.com", "password": "supersecret1",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["email"] == "founder@portal.com"
    # Session cookie set
    assert "portal_session" in c.cookies
    # User exists in DB
    u = users_repo.find_by_email("founder@portal.com")
    assert u is not None
    assert u.role == "admin"
    assert u.last_login_at is not None


def test_bootstrap_status_flips_to_false_after_first_admin(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={
        "email": "founder@portal.com", "password": "supersecret1",
    })
    r = c.get("/api/auth/bootstrap-status")
    assert r.json() == {"required": False}


def test_bootstrap_blocked_after_first_admin(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={
        "email": "first@x.com", "password": "supersecret1",
    })
    # Second attempt — even a different email — must fail
    r = c.post("/api/auth/bootstrap", json={
        "email": "second@x.com", "password": "anothersecret1",
    })
    assert r.status_code == 403


def test_bootstrap_required_again_if_admin_deactivated(isolated_app):
    """Edge case: if the only admin gets deactivated, bootstrap reopens.

    This is the recovery path when an admin gets locked out by accident.
    """
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={
        "email": "first@x.com", "password": "supersecret1",
    })
    u = users_repo.find_by_email("first@x.com")
    users_repo.deactivate(u.id)
    r = c.get("/api/auth/bootstrap-status")
    assert r.json() == {"required": True}


def test_bootstrap_rejects_weak_password(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/bootstrap", json={
        "email": "x@x.com", "password": "short",
    })
    assert r.status_code == 422


def test_bootstrap_rejects_invalid_email(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/bootstrap", json={
        "email": "not-an-email", "password": "supersecret1",
    })
    assert r.status_code == 422


# ── /api/admin/users — list ─────────────────────────────────────────────


def _bootstrap_admin(c: TestClient) -> None:
    c.post("/api/auth/bootstrap", json={
        "email": "admin@x.com", "password": "supersecret1",
    })


def test_admin_list_users(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.get("/api/admin/users")
    assert r.status_code == 200
    body = r.json()
    assert len(body["users"]) == 1
    assert body["users"][0]["email"] == "admin@x.com"
    assert body["users"][0]["role"] == "admin"


def test_admin_list_requires_login(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/api/admin/users")
    assert r.status_code == 401


def test_admin_endpoints_require_admin_role(isolated_app):
    """Operator user (not admin) must be 403, not 401."""
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    # Admin creates an operator
    c.post("/api/admin/users", json={
        "email": "op@x.com", "password": "operpass1", "role": "operator",
    })
    # Logout admin, login as operator
    c.post("/api/auth/logout")
    assert _login(c, "op@x.com", "operpass1")
    # Operator hits an admin endpoint
    r = c.get("/api/admin/users")
    assert r.status_code == 403


# ── Create user ─────────────────────────────────────────────────────────


def test_admin_create_user(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/users", json={
        "email": "newop@x.com", "password": "newpass12", "role": "operator",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "newop@x.com"
    assert body["user"]["role"] == "operator"
    assert body["user"]["active"] is True


def test_admin_create_user_rejects_duplicate(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "dup@x.com", "password": "anypass12",
    })
    r = c.post("/api/admin/users", json={
        "email": "dup@x.com", "password": "anypass12",
    })
    assert r.status_code == 409


def test_admin_create_user_rejects_weak_password(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/users", json={
        "email": "weak@x.com", "password": "short",
    })
    assert r.status_code == 422


def test_admin_create_user_rejects_invalid_role(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/users", json={
        "email": "x@x.com", "password": "validpass1", "role": "superuser",
    })
    assert r.status_code == 422


def test_created_user_can_log_in(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "newop@x.com", "password": "newpass12",
    })
    c.post("/api/auth/logout")
    # New session for the operator
    c2 = TestClient(app)
    assert _login(c2, "newop@x.com", "newpass12")


# ── Reset password ──────────────────────────────────────────────────────


def test_admin_reset_password_invalidates_old_password(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "user@x.com", "password": "originalpw1",
    })
    target = users_repo.find_by_email("user@x.com")

    r = c.post(f"/api/admin/users/{target.id}/reset-password", json={
        "password": "freshpass99",
    })
    assert r.status_code == 200
    # Old password no longer works
    c2 = TestClient(app)
    assert not _login(c2, "user@x.com", "originalpw1")
    # New one does
    assert _login(c2, "user@x.com", "freshpass99")


def test_admin_reset_password_kills_active_sessions(isolated_app):
    """When admin resets a user's password, their active sessions are killed."""
    from app.web.server import app
    admin_c = TestClient(app)
    _bootstrap_admin(admin_c)
    admin_c.post("/api/admin/users", json={
        "email": "victim@x.com", "password": "originalpw1",
    })
    # Victim logs in
    victim_c = TestClient(app)
    _login(victim_c, "victim@x.com", "originalpw1")
    victim_token = victim_c.cookies["portal_session"]
    assert sessions_repo.get_active(victim_token) is not None

    # Admin resets victim's password
    target = users_repo.find_by_email("victim@x.com")
    admin_c.post(f"/api/admin/users/{target.id}/reset-password", json={
        "password": "newrandompw1",
    })
    # Victim's session is gone
    assert sessions_repo.get_active(victim_token) is None


def test_admin_reset_password_404_for_unknown_user(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/admin/users/99999/reset-password", json={
        "password": "anything12",
    })
    assert r.status_code == 404


def test_admin_reset_password_rejects_weak(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={"email": "u@x.com", "password": "originalpw1"})
    target = users_repo.find_by_email("u@x.com")
    r = c.post(f"/api/admin/users/{target.id}/reset-password", json={"password": "x"})
    assert r.status_code == 422


# ── Deactivate / reactivate ─────────────────────────────────────────────


def test_admin_deactivate_user_blocks_login(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "tobedeactivated@x.com", "password": "originalpw1",
    })
    target = users_repo.find_by_email("tobedeactivated@x.com")
    r = c.post(f"/api/admin/users/{target.id}/deactivate")
    assert r.status_code == 200
    # Cannot log in anymore
    c2 = TestClient(app)
    assert not _login(c2, "tobedeactivated@x.com", "originalpw1")


def test_admin_cannot_deactivate_self(isolated_app):
    """Self-lockout protection — admin must not be able to deactivate themselves."""
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    me = users_repo.find_by_email("admin@x.com")
    r = c.post(f"/api/admin/users/{me.id}/deactivate")
    assert r.status_code == 409
    # Still active
    assert users_repo.find_by_id(me.id).active is True


def test_admin_reactivate(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={"email": "u@x.com", "password": "originalpw1"})
    target = users_repo.find_by_email("u@x.com")
    c.post(f"/api/admin/users/{target.id}/deactivate")
    r = c.post(f"/api/admin/users/{target.id}/reactivate")
    assert r.status_code == 200
    assert users_repo.find_by_id(target.id).active is True


def test_admin_deactivate_kills_active_sessions(isolated_app):
    from app.web.server import app
    admin_c = TestClient(app)
    _bootstrap_admin(admin_c)
    admin_c.post("/api/admin/users", json={
        "email": "victim@x.com", "password": "originalpw1",
    })
    victim_c = TestClient(app)
    _login(victim_c, "victim@x.com", "originalpw1")
    token = victim_c.cookies["portal_session"]
    assert sessions_repo.get_active(token) is not None

    target = users_repo.find_by_email("victim@x.com")
    admin_c.post(f"/api/admin/users/{target.id}/deactivate")
    assert sessions_repo.get_active(token) is None


# ── Static page route ───────────────────────────────────────────────────


def test_admin_users_page_is_served(isolated_app):
    """The /admin/usuarios HTML is reachable. Auth is enforced inside the
    page itself by calling /api/auth/me."""
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/admin/usuarios")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
