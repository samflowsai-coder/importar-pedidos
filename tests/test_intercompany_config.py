"""Config do de-para de cliente intercompany (colunas + repo + rota)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


@pytest.fixture
def env(fresh_shared):
    return environments_repo.create(
        slug="mm", name="MM", watch_dir="/tmp/in", output_dir="/tmp/out", fb_path="/tmp/x.fdb"
    )


def test_default_e_desligado(env):
    assert env["intercompany_cnpj"] is None
    assert env["intercompany_env_slug"] is None


def test_grava_e_le_config(env):
    atualizado = environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    assert atualizado["intercompany_cnpj"] == "34513679000134"  # normalizado p/ dígitos
    assert atualizado["intercompany_env_slug"] == "nasmar"


def test_limpar_desliga(env):
    environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    atualizado = environments_repo.set_intercompany_config(env["id"], cnpj="", revenda_slug="")
    assert atualizado["intercompany_cnpj"] is None
    assert atualizado["intercompany_env_slug"] is None


def test_persiste_no_get(env):
    environments_repo.set_intercompany_config(
        env["id"], cnpj="34513679000134", revenda_slug="nasmar"
    )
    lido = environments_repo.get(env["id"])
    assert lido["intercompany_cnpj"] == "34513679000134"
    assert lido["intercompany_env_slug"] == "nasmar"


# ── Rota PUT /api/admin/environments/{id}/intercompany ──────────────────────


@pytest.fixture
def setup_rotas(tmp_path: Path):
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


def test_rota_grava_intercompany(setup_rotas):
    criado = (
        _client()
        .post(
            "/api/admin/environments",
            json={
                "slug": "mm",
                "name": "MM",
                "watch_dir": str(setup_rotas / "in"),
                "output_dir": str(setup_rotas / "out"),
                "fb_path": str(setup_rotas / "x.fdb"),
            },
        )
        .json()
    )
    r = _client().put(
        f"/api/admin/environments/{criado['id']}/intercompany",
        json={"cnpj": "34.513.679/0001-34", "revenda_slug": "nasmar"},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_cnpj"] == "34513679000134"
    assert r.json()["intercompany_env_slug"] == "nasmar"


def test_rota_404_em_ambiente_inexistente(setup_rotas):
    r = _client().put(
        "/api/admin/environments/nao-existe/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": "nasmar"},
    )
    assert r.status_code == 404


def test_rota_grava_slug_de_ambiente_existente(setup_rotas):
    """Fecha o achado da revisão da Task 2: o form é um <select> alimentado pelos
    ambientes reais — este teste prova que a rota persiste o slug exatamente como
    o de um ambiente que de fato existe (não um valor digitado à mão)."""
    c = _client()
    mm = c.post(
        "/api/admin/environments",
        json={
            "slug": "mm",
            "name": "MM",
            "watch_dir": str(setup_rotas / "in"),
            "output_dir": str(setup_rotas / "out"),
            "fb_path": str(setup_rotas / "x.fdb"),
        },
    ).json()
    nasmar = c.post(
        "/api/admin/environments",
        json={
            "slug": "nasmar",
            "name": "Nasmar",
            "watch_dir": str(setup_rotas / "in2"),
            "output_dir": str(setup_rotas / "out2"),
            "fb_path": str(setup_rotas / "y.fdb"),
        },
    ).json()
    r = c.put(
        f"/api/admin/environments/{mm['id']}/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": nasmar["slug"]},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_env_slug"] == nasmar["slug"] == "nasmar"


def test_rota_normaliza_case_do_slug(setup_rotas):
    """Defesa mínima na rota: mesmo que algo escape do <select> (edição manual do
    payload), o slug é normalizado pra minúsculas antes de gravar — slugs de
    ambiente são sempre lowercase (SLUG_RE)."""
    criado = (
        _client()
        .post(
            "/api/admin/environments",
            json={
                "slug": "mm",
                "name": "MM",
                "watch_dir": str(setup_rotas / "in"),
                "output_dir": str(setup_rotas / "out"),
                "fb_path": str(setup_rotas / "x.fdb"),
            },
        )
        .json()
    )
    r = _client().put(
        f"/api/admin/environments/{criado['id']}/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": "NASMAR"},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_env_slug"] == "nasmar"
