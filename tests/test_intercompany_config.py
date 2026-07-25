"""Config do de-para de cliente intercompany (colunas + repo + rota)."""

from __future__ import annotations

import pytest

from app.persistence import environments_repo, router


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
