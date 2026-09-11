"""Tests for the real auth flow: /api/auth/login, /api/auth/logout, /api/auth/me,
and enforcement of `Depends(require_user)` on mutation routes.

These tests use the `real_auth` fixture to disable the global TEST_AUTH_BYPASS
and exercise the cookie-based session flow end-to-end.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, sessions_repo, users_repo


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch, real_auth):
    """Fresh DB + real auth (no bypass)."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _make_user(email: str = "alice@example.com", password: str = "strongpass1"):
    return users_repo.create_user(email=email, password=password)


# ── /api/auth/login ──────────────────────────────────────────────────────


def test_login_with_valid_credentials_sets_cookie(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "alice@example.com"
    assert "portal_session" in c.cookies
    # Session in DB
    token = c.cookies["portal_session"]
    assert sessions_repo.get_active(token) is not None


def test_login_wrong_password_rejected(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "wrongpass1",
    })
    assert r.status_code == 401
    assert "portal_session" not in c.cookies


def test_login_unknown_email_rejected(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={
        "email": "ghost@nowhere.com", "password": "anything1",
    })
    assert r.status_code == 401


def test_login_inactive_user_rejected(isolated_app):
    u = _make_user()
    users_repo.deactivate(u.id)
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    assert r.status_code == 401


def test_login_email_is_case_insensitive(isolated_app):
    _make_user(email="Alice@Example.com")
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={
        "email": "ALICE@EXAMPLE.COM", "password": "strongpass1",
    })
    assert r.status_code == 200


def test_login_updates_last_login_at(isolated_app):
    u = _make_user()
    assert u.last_login_at is None
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    refreshed = users_repo.find_by_id(u.id)
    assert refreshed.last_login_at is not None


def test_login_cookie_is_httponly_strict_samesite(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    set_cookie_header = r.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie_header
    assert "samesite=strict" in set_cookie_header


# ── /api/auth/me ─────────────────────────────────────────────────────────


def test_me_returns_null_without_cookie(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json() == {"user": None, "environment": None}


def test_me_returns_user_after_login(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "alice@example.com"


def test_me_returns_null_after_session_revoked(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    token = c.cookies["portal_session"]
    sessions_repo.delete(token)
    r = c.get("/api/auth/me")
    assert r.json()["user"] is None


# ── /api/auth/logout ─────────────────────────────────────────────────────


def test_logout_deletes_session(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    token = c.cookies["portal_session"]
    r = c.post("/api/auth/logout")
    assert r.status_code == 200
    assert sessions_repo.get_active(token) is None


def test_logout_requires_auth(isolated_app):
    """Logout without a session is a 401, not a silent no-op."""
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/auth/logout")
    assert r.status_code == 401


# ── Mutation route enforcement ───────────────────────────────────────────


def test_mutation_route_rejects_anonymous(isolated_app):
    """POST /api/config without a session returns 401."""
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/config", json={"watchDir": "/tmp"})
    assert r.status_code == 401


def test_mutation_route_accepts_authenticated(isolated_app):
    _make_user()
    from app.web.server import app
    c = TestClient(app)
    c.post("/api/auth/login", json={
        "email": "alice@example.com", "password": "strongpass1",
    })
    r = c.post("/api/config", json={"watchDir": "/tmp"})
    # 200 (or whatever the real handler returns); the key is NOT 401
    assert r.status_code != 401


def test_read_only_routes_remain_open(isolated_app):
    """Read endpoints stay open in Phase 4b — only writes are gated."""
    from app.web.server import app
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert c.get("/api/config").status_code == 200


def test_imported_list_requires_session(isolated_app):
    """`/api/imported` NÃO fica na lista acima desde a Task 15 (roteamento
    intercompany): com `roteamento_modo='ligado'` e sem cookie `portal_env`,
    a rota soma pedidos de TODAS as empresas numa resposta só — a exceção
    de "leitura fica aberta" do Phase 4b não previa esse caso, e sem
    `Depends(require_user)` um chamador sem sessão nenhuma veria arquivo,
    número do pedido, cliente e status de qualquer empresa. Ganhou o mesmo
    `Depends(require_user)` das rotas irmãs de escrita."""
    from app.web.server import app
    c = TestClient(app)
    assert c.get("/api/imported").status_code == 401


def test_pending_list_requires_session(isolated_app):
    """`/api/pending` nunca teve `Depends(require_user)` — achado da task
    "ambiente é propriedade do pedido" (fluxo da pasta de entrada). Mesma
    classe de buraco do `test_imported_list_requires_session` acima: com
    `roteamento_modo='ligado'` e sem cookie `portal_env`, a rota soma as
    pastas de TODAS as empresas ativas — sem sessão, um chamador anônimo via
    nome de arquivo, tamanho e data de qualquer empresa. Ganhou o mesmo
    `Depends(require_user)` das rotas irmãs de ação (`/api/import`,
    `/api/reimport`, `/api/preview-pending`, que já exigiam sessão).

    `tests/test_pasta_cross_env.py` não cobre isso: as 9 fixtures de lá
    setam `TEST_AUTH_BYPASS=1`, então nenhuma delas consegue exercitar
    `require_user` de verdade — só este arquivo (via `real_auth`/
    `isolated_app`) desliga o bypass."""
    from app.web.server import app
    c = TestClient(app)
    assert c.get("/api/pending").status_code == 401


# ── Webhook stays open (HMAC-protected separately) ───────────────────────


def test_webhook_route_does_not_require_session(isolated_app):
    """POST /api/webhooks/gestor uses HMAC, not session — should be 401 from
    HMAC layer (missing signature), NOT from auth layer (would be the same
    code but different reason). We just verify auth doesn't intercept."""
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/webhooks/gestor", json={"event_id": "x"})
    # 401 from HMAC layer for missing signature (NOT for missing session)
    assert r.status_code == 401
    assert "X-Signature" in r.json()["detail"] or "Signature" in r.json()["detail"]
