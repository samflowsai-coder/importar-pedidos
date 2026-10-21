from __future__ import annotations

import pytest

from app.persistence import roteamento_decisao_repo as repo
from app.persistence import router


@pytest.fixture
def shared_db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def _reg(import_id="i1", acao="automatico", escolhido="mm"):
    repo.registrar(
        import_id=import_id,
        modo="ligado",
        degrau="regra",
        env_sugerido="mm",
        env_escolhido=escolhido,
        decidido_por="sistema:regra",
        motivo="m",
        acao=acao,
    )


def test_registrar_e_listar_em_ordem(shared_db):
    _reg(acao="automatico")
    _reg(acao="troca_no_preview", escolhido="nasmar")
    _reg(import_id="outro")
    lista = repo.listar_por_import("i1")
    assert [d["acao"] for d in lista] == ["automatico", "troca_no_preview"]
    assert repo.ultima("i1")["env_escolhido"] == "nasmar"
    assert repo.ultima("nada") is None


def test_registrar_nunca_levanta(shared_db, monkeypatch):
    def boom():
        raise RuntimeError("banco fora")

    monkeypatch.setattr(router, "shared_connect", boom)
    assert _reg() is None


def test_acao_invalida_nao_grava_e_nao_levanta(shared_db):
    assert _reg(acao="inventada") is None
    assert repo.listar_por_import("i1") == []


def test_falha_no_registro_loga_o_tipo_do_erro(shared_db, monkeypatch):
    from app.utils.logger import logger

    avisos = []
    monkeypatch.setattr(logger, "warning", lambda msg, *a, **k: avisos.append(msg.format(*a)))

    def boom():
        raise RuntimeError("banco fora")

    monkeypatch.setattr(router, "shared_connect", boom)
    _reg()
    assert avisos == ["roteamento.decisao_falhou import_id=i1 erro=RuntimeError"]
