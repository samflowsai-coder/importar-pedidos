"""Rotas /api/admin/environments (CRUD)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo


@pytest.fixture
def setup(tmp_path: Path):
    import os

    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _client():
    from app.web.server import app

    return TestClient(app)


def test_list_empty(setup):
    r = _client().get("/api/admin/environments")
    assert r.status_code == 200
    assert r.json() == []


def test_create_returns_public_view(setup):
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(setup / "in"),
        "output_dir": str(setup / "out"),
        "fb_path": str(setup / "x.fdb"),
        "fb_password": "secret",
    }
    r = _client().post("/api/admin/environments", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "mm"
    assert body["name"] == "MM"
    assert body["is_active"] == 1
    # senha jamais retorna
    assert "fb_password" not in body
    assert "fb_password_enc" not in body


def test_create_rejects_duplicate_slug(setup):
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(setup),
        "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    c = _client()
    c.post("/api/admin/environments", json=payload)
    r = c.post("/api/admin/environments", json=payload)
    assert r.status_code == 409


def test_create_rejects_bad_slug(setup):
    payload = {
        "slug": "MM Calçados",
        "name": "MM",
        "watch_dir": str(setup),
        "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    r = _client().post("/api/admin/environments", json=payload)
    assert r.status_code == 400


def test_get_returns_404_for_missing(setup):
    r = _client().get("/api/admin/environments/nope")
    assert r.status_code == 404


def test_patch_keeps_slug_immutable(setup):
    c = _client()
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(setup),
        "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    created = c.post("/api/admin/environments", json=payload).json()
    env_id = created["id"]
    # PATCH não declara slug — UpdateEnvRequest omite o campo
    r = c.patch(f"/api/admin/environments/{env_id}", json={"name": "MM Renomeado"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "MM Renomeado"
    assert body["slug"] == "mm"


def test_patch_password_modes(setup):
    c = _client()
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(setup),
        "output_dir": str(setup),
        "fb_path": "/x.fdb",
        "fb_password": "orig",
    }
    created = c.post("/api/admin/environments", json=payload).json()
    env_id = created["id"]
    # 1) sem fb_password → mantém
    c.patch(f"/api/admin/environments/{env_id}", json={"name": "MM2"})
    assert environments_repo.get_password(env_id) == "orig"
    # 2) "" → limpa
    c.patch(f"/api/admin/environments/{env_id}", json={"fb_password": ""})
    assert environments_repo.get_password(env_id) is None
    # 3) valor → substitui
    c.patch(f"/api/admin/environments/{env_id}", json={"fb_password": "novasenha"})
    assert environments_repo.get_password(env_id) == "novasenha"


def test_delete_soft_removes_from_list(setup):
    c = _client()
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(setup),
        "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    env_id = c.post("/api/admin/environments", json=payload).json()["id"]
    r = c.delete(f"/api/admin/environments/{env_id}")
    assert r.status_code == 204
    after = c.get("/api/admin/environments").json()
    # list_all retorna inactive também, mas com is_active=0
    rec = next(e for e in after if e["id"] == env_id)
    assert rec["is_active"] == 0


def _create_env(c, setup, slug="mm"):
    return c.post(
        "/api/admin/environments",
        json={
            "slug": slug,
            "name": slug.upper(),
            "watch_dir": str(setup),
            "output_dir": str(setup),
            "fb_path": "/x.fdb",
        },
    ).json()["id"]


def test_set_flowpcp_config_round_trip(setup):
    c = _client()
    env_id = _create_env(c, setup)
    r = c.put(
        f"/api/admin/environments/{env_id}/flowpcp",
        json={
            "enabled": True,
            "base_url": "https://flow.test",
            "tenant_id": "uuid-mm",
            "dry_run": True,
            "poll_interval_s": 45,
            "service_token": "svc-tok",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["flowpcp_enabled"] == 1
    assert body["flowpcp_base_url"] == "https://flow.test"
    assert body["flowpcp_tenant_id"] == "uuid-mm"
    assert body["flowpcp_dry_run"] == 1
    assert body["flowpcp_poll_interval_s"] == 45
    # token cifrado jamais volta no JSON
    assert "flowpcp_service_token_enc" not in body
    assert environments_repo.get_flowpcp_token(env_id) == "svc-tok"


def test_set_flowpcp_clientes_push_round_trip(setup):
    """O gate flowpcp_clientes_push persiste pelo PUT (o que a UI agora sempre
    envia). Trava a ponta backend do fix do gate-reset: enviar True grava 1,
    enviar False grava 0."""
    c = _client()
    env_id = _create_env(c, setup)
    base = {"enabled": True, "base_url": "x", "tenant_id": "t"}
    r1 = c.put(f"/api/admin/environments/{env_id}/flowpcp", json={**base, "clientes_push": True})
    assert r1.status_code == 200
    assert r1.json()["flowpcp_clientes_push"] == 1
    r2 = c.put(f"/api/admin/environments/{env_id}/flowpcp", json={**base, "clientes_push": False})
    assert r2.status_code == 200
    assert r2.json()["flowpcp_clientes_push"] == 0


def test_set_flowpcp_keeps_token_when_omitted(setup):
    c = _client()
    env_id = _create_env(c, setup)
    c.put(
        f"/api/admin/environments/{env_id}/flowpcp",
        json={"enabled": True, "base_url": "x", "tenant_id": "t", "service_token": "orig"},
    )
    # PUT sem service_token → mantém o token atual (desligar não apaga)
    c.put(
        f"/api/admin/environments/{env_id}/flowpcp",
        json={"enabled": False, "base_url": "x", "tenant_id": "t"},
    )
    assert environments_repo.get(env_id)["flowpcp_enabled"] == 0
    assert environments_repo.get_flowpcp_token(env_id) == "orig"


def test_set_flowpcp_404_for_missing(setup):
    r = _client().put("/api/admin/environments/nope/flowpcp", json={"enabled": False})
    assert r.status_code == 404


def test_get_env_exposes_flowpcp_fields_not_token(setup):
    c = _client()
    env_id = _create_env(c, setup)
    c.put(
        f"/api/admin/environments/{env_id}/flowpcp",
        json={"enabled": True, "base_url": "x", "tenant_id": "t", "service_token": "sek"},
    )
    got = c.get(f"/api/admin/environments/{env_id}").json()
    assert got["flowpcp_enabled"] == 1
    assert got["flowpcp_base_url"] == "x"
    assert "flowpcp_service_token_enc" not in got


def test_test_endpoint_validates_paths(setup):
    c = _client()
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(setup / "naoexiste"),
        "output_dir": str(setup / "naoexiste-tb"),
        "fb_path": "/inexistente.fdb",
    }
    env_id = c.post("/api/admin/environments", json=payload).json()["id"]
    r = c.post(f"/api/admin/environments/{env_id}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["watch_dir_ok"] is False
    assert body["output_dir_ok"] is False
    assert body["firebird_ok"] is False
    assert body["firebird_error"]


def test_test_endpoint_validates_existing_paths(setup):
    in_dir = setup / "in"
    out_dir = setup / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    c = _client()
    payload = {
        "slug": "mm",
        "name": "MM",
        "watch_dir": str(in_dir),
        "output_dir": str(out_dir),
        "fb_path": "/inexistente.fdb",  # FB falha mas pastas OK
    }
    env_id = c.post("/api/admin/environments", json=payload).json()["id"]
    r = c.post(f"/api/admin/environments/{env_id}/test")
    body = r.json()
    assert body["watch_dir_ok"] is True
    assert body["output_dir_ok"] is True
    # FB ainda falha porque o .fdb não existe
    assert body["firebird_ok"] is False


# ── Full-load de catálogo (produtos Fire → Flow) ─────────────────────────────


class _FakeReport:
    """Stand-in de CatalogoReconciliacaoResponse — só precisa de model_dump()."""

    def model_dump(self):
        return {
            "dry_run": True,
            "full_sync": True,
            "fire_pk_presente": "todos",
            "contagens": {
                "fire_total": 3421,
                "flow_total": 827,
                "match_limpo": 261,
                "ambiguo": 0,
                "fire_only": 3160,
                "flow_only": 566,
            },
            "amostras": {"ambiguo": [], "fire_only": [], "flow_only": []},
        }


def _enable_flowpcp(env_id):
    environments_repo.set_flowpcp_config(
        env_id,
        enabled=True,
        base_url="https://gestor.samflowsai.com.br",
        tenant_id="1798c3c5-0fb6-4edb-a523-e13fb5bf52a0",
        service_token="tok",
    )


def test_sync_catalogo_retorna_relatorio(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.catalogo_sync as cs

    monkeypatch.setattr(cs, "run_catalogo_sync", lambda *a, **k: _FakeReport())

    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo")
    assert r.status_code == 200
    body = r.json()
    assert body["contagens"]["fire_total"] == 3421
    assert body["fire_pk_presente"] == "todos"


def test_sync_catalogo_409_se_flowpcp_desligado(setup):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo")
    assert r.status_code == 409


def test_sync_catalogo_404_ambiente_inexistente(setup):
    r = _client().post("/api/admin/environments/nao-existe/flowpcp/sync-catalogo")
    assert r.status_code == 404


def test_sync_catalogo_apply_promove_passa_dry_run_false(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.catalogo_sync as cs

    capturado = {}

    def fake(slug, **kw):
        capturado.update(kw)
        capturado["slug"] = slug
        return _FakeReport()

    monkeypatch.setattr(cs, "run_catalogo_sync", fake)
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo?apply=true")
    assert r.status_code == 200
    assert capturado["dry_run"] is False
    assert capturado["full_sync"] is True


def test_sync_catalogo_default_e_dry_run(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.catalogo_sync as cs

    capturado = {}
    monkeypatch.setattr(
        cs, "run_catalogo_sync", lambda slug, **kw: capturado.update(kw) or _FakeReport()
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo")
    assert r.status_code == 200
    assert capturado["dry_run"] is True


def test_sync_catalogo_local_only_quando_gate_off(setup, monkeypatch):
    """Gate OFF → 200 com local_only=True (catálogo só atualizado no importador)."""
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.catalogo_sync as cs

    monkeypatch.setattr(
        cs,
        "run_catalogo_sync",
        lambda *a, **k: cs.CatalogoLocalResult(itens=3421, extraido_em="2026-07-11T10:00:00Z"),
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo")
    assert r.status_code == 200
    body = r.json()
    assert body["local_only"] is True
    assert body["itens"] == 3421


def test_put_flowpcp_aceita_catalogo_push(setup):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    r = _client().put(
        f"/api/admin/environments/{env['id']}/flowpcp",
        json={"enabled": True, "base_url": "https://x", "tenant_id": "t", "catalogo_push": True},
    )
    assert r.status_code == 200
    assert r.json()["flowpcp_catalogo_push"] == 1


# ── Carga de clientes ativos (Fire → Flow) ───────────────────────────────────


class _FakeClientesReconciliacao:
    """Stand-in de ClientesReconciliacaoResponse — só precisa de model_dump()."""

    def model_dump(self):
        return {
            "dry_run": True,
            "fire_total": 120,
            "flow_total": 80,
            "match_limpo": 60,
        }


def _fake_clientes_result(**overrides):
    from app.integrations.flowpcp.clientes_sync import ClientesSyncResult

    kwargs = {
        "itens": 5,
        "extraido_em": "2026-07-17T12:00:00Z",
        "descartados_cpf": 3,
        "descartados_invalidos": 1,
        "colisoes_dedup": 2,
    }
    kwargs.update(overrides)
    return ClientesSyncResult(**kwargs)


def test_sync_clientes_404_ambiente_inexistente(setup):
    r = _client().post("/api/admin/environments/nao-existe/flowpcp/sync-clientes")
    assert r.status_code == 404


def test_sync_clientes_409_se_flowpcp_desligado(setup):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes")
    assert r.status_code == 409


def test_sync_clientes_apply_passa_dry_run_false_e_full_sync_false(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.clientes_sync as clientes_sync

    capturado = {}

    def fake(slug, **kw):
        capturado.update(kw)
        capturado["slug"] = slug
        return _fake_clientes_result()

    monkeypatch.setattr(clientes_sync, "run_clientes_sync", fake)
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes?apply=true")
    assert r.status_code == 200
    assert capturado["dry_run"] is False
    assert capturado["full_sync"] is False


def test_sync_clientes_default_e_dry_run_full_sync_false(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.clientes_sync as clientes_sync

    capturado = {}
    monkeypatch.setattr(
        clientes_sync,
        "run_clientes_sync",
        lambda slug, **kw: capturado.update(kw) or _fake_clientes_result(),
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes")
    assert r.status_code == 200
    assert capturado["dry_run"] is True
    assert capturado["full_sync"] is False


def test_sync_clientes_502_quando_run_clientes_sync_lanca(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.clientes_sync as clientes_sync

    def fake(*a, **k):
        raise RuntimeError("Firebird indisponível")

    monkeypatch.setattr(clientes_sync, "run_clientes_sync", fake)
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes")
    assert r.status_code == 502


def test_sync_clientes_409_quando_run_clientes_sync_retorna_none(setup, monkeypatch):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.clientes_sync as clientes_sync

    monkeypatch.setattr(clientes_sync, "run_clientes_sync", lambda *a, **k: None)
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes")
    assert r.status_code == 409


def test_sync_clientes_local_only_quando_reconciliacao_none(setup, monkeypatch):
    """Gate OFF (reconciliacao=None) → local_only=True e sem chave reconciliacao."""
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.clientes_sync as clientes_sync

    monkeypatch.setattr(
        clientes_sync, "run_clientes_sync", lambda *a, **k: _fake_clientes_result()
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes")
    assert r.status_code == 200
    body = r.json()
    assert body["local_only"] is True
    assert body["itens"] == 5
    assert body["descartados_cpf"] == 3
    assert body["descartados_invalidos"] == 1
    assert body["colisoes_dedup"] == 2
    assert body["extraido_em"] == "2026-07-17T12:00:00Z"
    assert body["skipped_empty"] is False
    assert "reconciliacao" not in body


def test_sync_clientes_retorna_reconciliacao_quando_gate_on(setup, monkeypatch):
    """Gate ON (reconciliacao presente) → local_only=False e reconciliacao no body."""
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    import app.integrations.flowpcp.clientes_sync as clientes_sync

    monkeypatch.setattr(
        clientes_sync,
        "run_clientes_sync",
        lambda *a, **k: _fake_clientes_result(reconciliacao=_FakeClientesReconciliacao()),
    )
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-clientes")
    assert r.status_code == 200
    body = r.json()
    assert body["local_only"] is False
    assert body["reconciliacao"] == {
        "dry_run": True,
        "fire_total": 120,
        "flow_total": 80,
        "match_limpo": 60,
    }


def test_put_flowpcp_aceita_catalogo_apenas_meias(setup):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    r = _client().put(
        f"/api/admin/environments/{env['id']}/flowpcp",
        json={
            "enabled": True,
            "base_url": "https://x",
            "tenant_id": "t",
            "catalogo_apenas_meias": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["flowpcp_catalogo_apenas_meias"] == 1
