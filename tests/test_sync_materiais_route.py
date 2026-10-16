"""Rota POST /{env_id}/flowpcp/sync-materiais."""

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


def _post(env_id, apply=False):
    from app.web.server import app

    return TestClient(app).post(
        f"/api/admin/environments/{env_id}/flowpcp/sync-materiais?apply={'true' if apply else 'false'}"
    )


def _env(setup, flow=True):
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    if flow:
        environments_repo.set_flowpcp_config(
            env["id"], enabled=True, base_url="https://x", tenant_id="t", service_token="tok"
        )
    return env


def test_404(setup):
    assert _post("nao-existe").status_code == 404


def test_409_sem_flowpcp(setup):
    assert _post(_env(setup, flow=False)["id"]).status_code == 409


def test_422_apply_sem_capacidade(setup, monkeypatch):
    import app.integrations.flowpcp.materiais_sync as m

    def boom(*a, **k):
        raise m.ApplySemCapacidadeError("sem materiais.v1")

    monkeypatch.setattr(m, "run_materiais_sync", boom)
    r = _post(_env(setup)["id"], apply=True)
    assert r.status_code == 422 and "materiais.v1" in r.json()["detail"]


def test_409_apply_em_andamento(setup, monkeypatch):
    import app.integrations.flowpcp.materiais_sync as m
    from app.integrations.flowpcp.client import FlowPCPApplyEmAndamentoError

    def boom(*a, **k):
        raise FlowPCPApplyEmAndamentoError("em andamento", status_code=409)

    monkeypatch.setattr(m, "run_materiais_sync", boom)
    assert _post(_env(setup)["id"], apply=True).status_code == 409


def test_502_outra_falha_mostra_mensagem(setup, monkeypatch):
    import app.integrations.flowpcp.materiais_sync as m

    def boom(*a, **k):
        raise RuntimeError("fire fora")

    monkeypatch.setattr(m, "run_materiais_sync", boom)
    r = _post(_env(setup)["id"])
    assert r.status_code == 502 and "Falha na carga de materiais: fire fora" in r.json()["detail"]


def test_local_only(setup, monkeypatch):
    import app.integrations.flowpcp.materiais_sync as m

    monkeypatch.setattr(
        m,
        "run_materiais_sync",
        lambda *a, **k: m.MateriaisLocalResult(itens=381, extraido_em="2026-10-16T00:00:00Z"),
    )
    r = _post(_env(setup)["id"])
    assert r.status_code == 200
    assert r.json() == {"local_only": True, "itens": 381, "extraido_em": "2026-10-16T00:00:00Z"}


def test_caminho_feliz_devolve_relatorio_com_extras(setup, monkeypatch):
    import app.integrations.flowpcp.materiais_sync as m
    from app.integrations.flowpcp.materiais_schema import MateriaisResposta

    rep = MateriaisResposta(dryRun=True, resumo={"criar": 3}, capacidades=["materiais.dryrun.v1"])
    rep.materiais_portal = {"enviados": 3}
    monkeypatch.setattr(m, "run_materiais_sync", lambda *a, **k: rep)
    r = _post(_env(setup)["id"])
    assert r.status_code == 200
    body = r.json()
    assert body["dryRun"] is True and body["resumo"] == {"criar": 3}
    assert body["materiais_portal"] == {"enviados": 3}
