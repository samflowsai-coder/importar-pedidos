"""Rotas admin da SP1: /api/admin/environments/{id}/erp-base."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, erp_bases_repo, erp_estado_repo, router
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "fpe_" + "d" * 43


@pytest.fixture
def env_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="",
    )
    yield env["id"]
    db.set_db_path(None)
    db.reset_init_cache()


def _client():
    from app.web.server import app

    return TestClient(app)


def _payload(**kw):
    p = {
        "base": "mm",
        "papel": "produtora",
        "tenant_id": TENANT,
        "destino_url": "https://flow.test",
        "foto_hora": 23,
        "ativo": True,
        "token": TOKEN,
    }
    p.update(kw)
    return p


def test_salva_e_nunca_devolve_a_credencial(env_id):
    c = _client()
    r = c.put(f"/api/admin/environments/{env_id}/erp-base", json=_payload())
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
    corpo = c.get(f"/api/admin/environments/{env_id}/erp-base").json()
    assert corpo["config"]["tem_token"] is True and corpo["config"]["base"] == "mm"
    assert TOKEN not in str(corpo)


def test_config_invalida_vira_422(env_id):
    r = _client().put(f"/api/admin/environments/{env_id}/erp-base", json=_payload(papel="dona"))
    assert r.status_code == 422


def test_credencial_nova_tira_o_bloqueio_de_401_e_desbloquear_tira_os_outros(env_id):
    c = _client()
    c.put(f"/api/admin/environments/{env_id}/erp-base", json=_payload())
    with router.env_connect("mm") as conn:
        erp_estado_repo.bloquear(
            conn, "mm", "2026-10-09T13:00:00.000+00:00", motivo="credencial", http=401, erro=""
        )
    corpo = c.put(
        f"/api/admin/environments/{env_id}/erp-base", json=_payload(token="fpe_" + "e" * 43)
    ).json()
    assert corpo["envio"]["bloqueio"] is None
    with router.env_connect("mm") as conn:
        erp_estado_repo.bloquear(
            conn, "mm", "2026-10-09T13:00:00.000+00:00", motivo="lote_invalido", http=400, erro=""
        )
    corpo = c.post(f"/api/admin/environments/{env_id}/erp-base/desbloquear").json()
    assert corpo["envio"]["bloqueio"] is None


def test_ambiente_inexistente_404(env_id):
    assert _client().get("/api/admin/environments/nao-existe/erp-base").status_code == 404


# ── base_atendida: o predicado único de "a thread atende esta base" ───────────


def _salvar(env_id, **kw):
    args = dict(
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=23,
        ativo=True,
        token=TOKEN,
    )
    args.update(kw)
    erp_bases_repo.salvar(env_id, **args)
    return erp_bases_repo.obter(env_id)


def test_base_atendida_ok(env_id):
    assert erp_bases_repo.base_atendida(_salvar(env_id)) is None


def test_base_desligada(env_id):
    assert erp_bases_repo.base_atendida(_salvar(env_id, ativo=False)) == "desligada"


def test_ambiente_inativo(env_id):
    cfg = _salvar(env_id)
    environments_repo.soft_delete(env_id)
    assert erp_bases_repo.base_atendida(erp_bases_repo.obter(env_id)) == "desligada"
    assert cfg.ambiente_ativo is True


def test_sem_credencial(env_id):
    assert erp_bases_repo.base_atendida(_salvar(env_id, token="")) == "sem_credencial"


def test_sem_config_nao_e_atendida():
    assert erp_bases_repo.base_atendida(None) == "desligada"


def test_listar_ativas_concorda_com_base_atendida(env_id):
    # (base ativa, token, ambiente ativo): ok, desligada, sem credencial, ambiente inativo
    for ativo, token, amb_ativo in [
        (True, TOKEN, 1),
        (False, TOKEN, 1),
        (True, "", 1),
        (True, TOKEN, 0),
    ]:
        with router.shared_connect() as conn:
            conn.execute("UPDATE environments SET is_active = ? WHERE id = ?", (amb_ativo, env_id))
        cfg = _salvar(env_id, ativo=ativo, token=token)
        assert (cfg in erp_bases_repo.listar_ativas()) == (
            erp_bases_repo.base_atendida(cfg) is None
        )
