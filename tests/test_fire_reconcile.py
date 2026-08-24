"""Reconciliação: achar no Fire o pedido que a operação cadastrou à mão.

Regra que atravessa o arquivo: a chave é SEMPRE dupla — número do pedido E
identidade do cliente. Casar por número sozinho tira pedido da fila de trabalho
sem ele estar no ERP, que é o pior desfecho possível desta feature.
"""

from __future__ import annotations

import pytest

from app.erp import fire_reconcile
from app.erp.fire_reconcile import Candidato, buscar_no_fire


class _FakeCursor:
    def __init__(self, linhas):
        self._linhas = linhas
        self.executados = []

    def execute(self, sql, params=None):
        self.executados.append((sql, list(params or [])))

    def fetchall(self):
        return self._linhas

    def close(self):
        pass


class _FakeConn:
    def __init__(self, linhas):
        self._cursor = _FakeCursor(linhas)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _limpa():
    fire_reconcile.limpar_cache()
    yield
    fire_reconcile.limpar_cache()


def _plugar(monkeypatch, linhas, *, erro=None):
    """Substitui a conexão e o lookup de ambiente por fakes.

    Devolve o `_FakeConn` criado — o teste de lote inspeciona
    `.cursor().executados` nele diretamente, sem depender de nenhum gancho de
    teste exposto pelo módulo de produção.
    """
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    fake_conn = _FakeConn(linhas)

    def _connect(self, cfg):
        if erro:
            raise erro
        return fake_conn

    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection, "connect_with_config", _connect
    )
    return fake_conn


# (PEDIDO_CLIENTE, V.CODIGO, STATUS, DATA_PEDIDO, C.CODIGO, CPF_CNPJ)
def _linha(numero, codigo, cnpj, *, status="PEDIDO", data="2026-08-01", cliente=77):
    return (numero, codigo, status, data, cliente, cnpj)


def test_caminho_2_casa_por_cnpj_do_header(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "12.345.678/0001-99")])
    cand = Candidato(
        import_id="i1",
        numero="6702645869",
        cliente_codigo=None,
        cnpj_header="12.345.678/0001-99",
        cnpjs_entrega=(),
        data_pedido="2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].fire_codigo == 900
    assert achados["i1"].caminho == 2


def test_cnpj_divergente_nao_casa(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "99.999.999/0001-11")])
    cand = Candidato("i1", "6702645869", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_1_override_ganha_e_dispensa_cnpj(monkeypatch):
    _plugar(monkeypatch, [_linha("K01", 901, "", cliente=4242)])
    cand = Candidato("i1", "K01", 4242, None, (), "2026-08-01")
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].caminho == 1


def test_caminho_3_marca_so_quando_todas_as_lojas_casam(monkeypatch):
    """Riachuelo: 3 lojas no pedido, 2 no Fire => NÃO marca."""
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22", "33.333.333/0003-33"),
        "2026-08-01",
    )
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_3_marca_quando_todas_as_lojas_casam(monkeypatch):
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22"),
        "2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].caminho == 3
    assert achados["i1"].lojas_casadas == 2
    assert achados["i1"].fire_codigo == 900  # menor CODIGO


def test_variante_sem_sufixo_casa_caso_sams(monkeypatch):
    _plugar(monkeypatch, [_linha("06654993", 902, "12.345.678/0001-99")])
    cand = Candidato("i1", "06654993-0000", None, "12.345.678/0001-99", (), "2026-08-01")
    assert "i1" in buscar_no_fire([cand], env_slug="mm")


def test_guarda_temporal_barra_numero_reusado(monkeypatch):
    """K01 do ano passado, mesmo cliente. Chave dupla não fecha; a data fecha."""
    _plugar(monkeypatch, [_linha("K01", 903, "12.345.678/0001-99", data="2024-01-10")])
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_firebird_fora_devolve_vazio_sem_levantar(monkeypatch):
    _plugar(monkeypatch, [], erro=RuntimeError("host inalcançável"))
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_cool_down_evita_segunda_tentativa(monkeypatch):
    tentativas = []

    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    def _connect(self, cfg):
        tentativas.append(1)
        raise RuntimeError("fora")

    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection, "connect_with_config", _connect
    )

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    buscar_no_fire([cand], env_slug="mm")
    buscar_no_fire([cand], env_slug="mm")
    assert len(tentativas) == 1


def test_lote_acima_de_200_quebra_em_blocos(monkeypatch):
    fake_conn = _plugar(monkeypatch, [])
    cands = [
        Candidato(f"i{i}", f"P{i}", None, "12.345.678/0001-99", (), "2026-08-01")
        for i in range(250)
    ]
    buscar_no_fire(cands, env_slug="mm")
    # 250 números viram 2 execuções, não 250
    assert len(fake_conn.cursor().executados) == 2
