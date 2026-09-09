from __future__ import annotations

import pytest

from app.persistence import decisao_ambiente_repo as memoria
from app.persistence import router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def test_memoria_vazia_e_estado_valido(fresh_shared):
    """Nao e erro, nao levanta, nao tem default. So nao sabe."""
    assert memoria.lembrada("11222333000181") is None
    assert memoria.listar() == []


def test_lembra_e_devolve_com_autor_e_data(fresh_shared):
    memoria.lembrar(cnpj_cliente="11.222.333/0001-81", env_slug="nasmar", por="grazi@mm")
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "nasmar"
    assert d["decidido_por"] == "grazi@mm"
    assert d["decidido_em"]
    assert d["divergiu_em"] is None


def test_lembrar_de_novo_sobrescreve_e_registra_quem(fresh_shared):
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="grazi@mm")
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="americanense", por="camila@mm")
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "americanense"
    assert d["decidido_por"] == "camila@mm"
    assert len(memoria.listar()) == 1  # UNIQUE por CNPJ, nao acumula


def test_divergencia_e_registrada_nao_silenciada(fresh_shared):
    """O documento chegou e contradisse a memoria: fica a marca."""
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="grazi@mm")
    memoria.marcar_divergencia(cnpj_cliente="11222333000181", de="americanense")
    d = memoria.lembrada("11222333000181")
    assert d["divergiu_de"] == "americanense"
    assert d["divergiu_em"]
    assert d["env_slug"] == "nasmar"  # a memoria NAO e reescrita pela divergencia


def test_marcar_divergencia_em_cnpj_desconhecido_nao_levanta(fresh_shared):
    memoria.marcar_divergencia(cnpj_cliente="99999999000199", de="mm")
    assert memoria.lembrada("99999999000199") is None


def test_cnpj_e_normalizado_na_escrita_e_na_leitura(fresh_shared):
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="x")
    assert memoria.lembrada("11.222.333/0001-81") is not None
