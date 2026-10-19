"""Cópia local da tabela de preço do Flow no formato do contrato precos v1
(app/persistence/precos_repo.py). Os casos `rede` e `preco` de vetores.json
rodam contra a cópia carregada por `aplicar_pagina`."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.erp.preco_regra import PrecoInvalido
from app.persistence import precos_repo, router

VETORES = json.loads(
    (Path(__file__).parent / "fixtures" / "flow_precos_v1" / "vetores.json").read_text(
        encoding="utf-8"
    )
)
MM = "11111111000191"
GERADO = "2026-10-20T11:00:00.000Z"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as c:
        yield c


def _pagina(mudancas, *, cursor="c1", tem_mais=False, gerado=GERADO, empresa=MM):
    return {
        "schema": "precos.tabela.v1",
        "capacidades": ["precos.tabela.v1", "precos.aprovacao.v1"],
        "geradoEm": gerado,
        "empresaFaturadoraCnpj": empresa,
        "reiniciar": False,
        "mudancas": mudancas,
        "cursor": cursor,
        "temMais": tem_mais,
    }


def _rede(rede_id="r1", *, raizes=("11222333",), cnpjs=(), ativa=True, travada=False):
    return {
        "tipo": "rede",
        "redeId": rede_id,
        "nome": f"Rede {rede_id}",
        "ativa": ativa,
        "travada": travada,
        "raizes": list(raizes),
        "cnpjs": list(cnpjs),
    }


def _preco(preco_id="p1", *, centavos=4590, cancelado=False, rede_id="r1", produto="48213"):
    return {
        "tipo": "preco",
        "precoId": preco_id,
        "redeId": rede_id,
        "empresaFaturadoraCnpj": MM,
        "erpProdutoId": produto,
        "unidadeErp": "KIT",
        "precoCentavos": centavos,
        "vigenciaInicio": "2026-10-19",
        "registradoEm": "2026-10-20T10:00:00.000Z",
        "cancelado": cancelado,
    }


@pytest.fixture
def tabela_vetores(conn):
    t = VETORES["tabela"]
    precos_repo.aplicar_pagina(conn, _pagina(t["redes"] + t["precos"]), empresa_cnpj=MM)
    return conn


@pytest.mark.parametrize("caso", VETORES["rede"], ids=lambda c: c["caso"])
def test_vetores_rede(tabela_vetores, caso):
    rede = precos_repo.resolver_rede(tabela_vetores, caso["cnpj"])
    assert (rede.id if rede else None) == caso["esperadoRedeId"]


@pytest.mark.parametrize("caso", VETORES["preco"], ids=lambda c: c["caso"])
def test_vetores_preco(tabela_vetores, caso):
    linha = precos_repo.preco_vigente(
        tabela_vetores,
        empresa_cnpj=caso["empresaFaturadoraCnpj"],
        rede_id=caso["redeId"],
        erp_produto_id=caso["erpProdutoId"],
        data=date.fromisoformat(caso["dataReferencia"]),
    )
    assert (linha.preco_id if linha else None) == caso["esperadoPrecoId"]


def test_rede_devolve_estado(tabela_vetores):
    b = precos_repo.resolver_rede(tabela_vetores, "11222333000999")
    assert b == precos_repo.Rede(
        id="a1a1a1a1-0000-4000-8000-00000000000b",
        nome="Bandeira B (sob a raiz da A)",
        ativa=True,
        travada=True,
    )


def test_preco_vigente_devolve_linha_inteira(tabela_vetores):
    linha = precos_repo.preco_vigente(
        tabela_vetores,
        empresa_cnpj=MM,
        rede_id="a1a1a1a1-0000-4000-8000-00000000000a",
        erp_produto_id="48213",
        data=date(2026, 10, 20),
    )
    assert linha.unidade_erp == "KIT"
    assert linha.preco_centavos == 4590
    assert linha.vigencia_inicio == date(2026, 10, 19)
    assert linha.registrado_em == "2026-10-20T10:00:00.000Z"
    assert linha.cancelado is False
    assert linha.empresa_cnpj == MM


def test_preco_vigente_registrado_sem_fuso_falha(conn):
    p = _preco()
    p["registradoEm"] = "2026-10-20T10:00:00"
    precos_repo.aplicar_pagina(conn, _pagina([_rede(), p]), empresa_cnpj=MM)
    with pytest.raises(PrecoInvalido):
        precos_repo.preco_vigente(
            conn, empresa_cnpj=MM, rede_id="r1", erp_produto_id="48213", data=date(2026, 10, 20)
        )


def test_tem_mais_nao_grava_completo_em(conn):
    precos_repo.aplicar_pagina(conn, _pagina([_rede()], tem_mais=True), empresa_cnpj=MM)
    estado = precos_repo.estado_sync(conn)
    assert estado == precos_repo.EstadoSync(empresa_cnpj=MM, cursor="c1", completo_em=None)
    assert precos_repo.ultima_sync_ok(conn) is None

    precos_repo.aplicar_pagina(conn, _pagina([], cursor="c2"), empresa_cnpj=MM)
    completo = datetime(2026, 10, 20, 11, 0, tzinfo=UTC)
    assert precos_repo.estado_sync(conn) == precos_repo.EstadoSync(MM, "c2", completo)
    assert precos_repo.ultima_sync_ok(conn) == completo


def test_pagina_com_tem_mais_mantem_completo_anterior(conn):
    precos_repo.aplicar_pagina(conn, _pagina([], cursor="c1"), empresa_cnpj=MM)
    precos_repo.aplicar_pagina(
        conn,
        _pagina([], cursor="c2", tem_mais=True, gerado="2026-10-21T11:00:00.000Z"),
        empresa_cnpj=MM,
    )
    estado = precos_repo.estado_sync(conn)
    assert estado.cursor == "c2"
    assert estado.completo_em == datetime(2026, 10, 20, 11, 0, tzinfo=UTC)


def test_estado_sync_sem_nada(conn):
    assert precos_repo.estado_sync(conn) == precos_repo.EstadoSync(None, None, None)
    assert precos_repo.ultima_sync_ok(conn) is None


def test_estado_sync_devolve_a_empresa_gravada(conn):
    # Cursor de outra empresa: o chamador compara empresa_cnpj e reinicia.
    outra = "22222222000182"
    precos_repo.aplicar_pagina(conn, _pagina([], empresa=outra), empresa_cnpj=outra)
    assert precos_repo.estado_sync(conn).empresa_cnpj == outra


def test_pagina_que_falha_no_meio_nao_avanca_cursor(conn):
    precos_repo.aplicar_pagina(conn, _pagina([_rede(), _preco()], cursor="c1"), empresa_cnpj=MM)
    quebrado = _preco("p2", produto="999")
    del quebrado["precoCentavos"]
    with pytest.raises(KeyError):
        precos_repo.aplicar_pagina(
            conn,
            _pagina([_rede("r2", raizes=("44555666",)), quebrado], cursor="c2"),
            empresa_cnpj=MM,
        )
    assert precos_repo.estado_sync(conn).cursor == "c1"
    assert precos_repo.resolver_rede(conn, "44555666000100") is None
    assert conn.execute("SELECT COUNT(*) FROM preco_tabela_v1").fetchone()[0] == 1


def test_rede_reaparece_sem_uma_raiz(conn):
    precos_repo.aplicar_pagina(
        conn, _pagina([_rede(raizes=("11222333", "44555666"))]), empresa_cnpj=MM
    )
    assert precos_repo.resolver_rede(conn, "44555666000100").id == "r1"
    precos_repo.aplicar_pagina(
        conn, _pagina([_rede(raizes=("11222333",))], cursor="c2"), empresa_cnpj=MM
    )
    assert precos_repo.resolver_rede(conn, "44555666000100") is None
    assert precos_repo.resolver_rede(conn, "11222333000100").id == "r1"


def test_rede_desativada_nao_casa(conn):
    precos_repo.aplicar_pagina(conn, _pagina([_rede()]), empresa_cnpj=MM)
    precos_repo.aplicar_pagina(conn, _pagina([_rede(ativa=False)], cursor="c2"), empresa_cnpj=MM)
    assert precos_repo.resolver_rede(conn, "11222333000100") is None


def test_preco_cancelado_substitui_pelo_mesmo_preco_id(conn):
    args = {
        "empresa_cnpj": MM,
        "rede_id": "r1",
        "erp_produto_id": "48213",
        "data": date(2026, 10, 20),
    }
    precos_repo.aplicar_pagina(conn, _pagina([_rede(), _preco()]), empresa_cnpj=MM)
    assert precos_repo.preco_vigente(conn, **args).preco_id == "p1"
    precos_repo.aplicar_pagina(
        conn, _pagina([_preco(cancelado=True)], cursor="c2"), empresa_cnpj=MM
    )
    assert precos_repo.preco_vigente(conn, **args) is None
    assert conn.execute("SELECT COUNT(*) FROM preco_tabela_v1").fetchone()[0] == 1


def test_reaplicar_pagina_e_idempotente(conn):
    pagina = _pagina([_rede(cnpjs=("99000111000100",)), _preco()])
    precos_repo.aplicar_pagina(conn, pagina, empresa_cnpj=MM)
    antes = _dump(conn)
    precos_repo.aplicar_pagina(conn, pagina, empresa_cnpj=MM)
    assert _dump(conn) == antes


def test_reiniciar_zera_tudo(conn):
    precos_repo.aplicar_pagina(conn, _pagina([_rede(), _preco()]), empresa_cnpj=MM)
    precos_repo.reiniciar(conn, empresa_cnpj=MM)
    for t in ("preco_rede_v1", "preco_rede_chave_v1", "preco_tabela_v1"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    estado = precos_repo.estado_sync(conn)
    assert estado.cursor is None and estado.completo_em is None
    assert precos_repo.ultima_sync_ok(conn) is None
    assert precos_repo.resolver_rede(conn, "11222333000100") is None


@pytest.mark.parametrize("entrada", ["11.222.333/0001-00", "11222333", "", "11222333000a00"])
def test_resolver_rede_entrada_fora_do_formato(conn, entrada):
    precos_repo.aplicar_pagina(conn, _pagina([_rede()]), empresa_cnpj=MM)
    assert precos_repo.resolver_rede(conn, entrada) is None


def _dump(conn):
    return [
        sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall())
        for t in ("preco_rede_v1", "preco_rede_chave_v1", "preco_tabela_v1", "preco_sync_v1")
    ]


def test_bootstrap_derruba_as_tabelas_antigas(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    velho = router.env_db_path("mm")
    velho.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(velho) as c:
        c.executescript(
            "CREATE TABLE preco_rede (id TEXT PRIMARY KEY, nome TEXT, travada INTEGER,"
            " travada_motivo TEXT, atualizado_em TEXT);"
            "CREATE TABLE preco_rede_cnpj (cnpj TEXT PRIMARY KEY, rede_id TEXT);"
            "CREATE TABLE preco_tabela (rede_id TEXT, fire_produto_id TEXT,"
            " vigente_desde TEXT, preco_centavos INTEGER, vigente_ate TEXT);"
            "CREATE TABLE preco_sync (recurso TEXT PRIMARY KEY, cursor TEXT, ultima_ok_em TEXT);"
            "INSERT INTO preco_sync VALUES ('precos', 'x', '2026-10-17T12:00:00+00:00');"
        )
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        nomes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"preco_rede", "preco_rede_cnpj", "preco_tabela", "preco_sync"} & nomes
        novas = {"preco_rede_v1", "preco_rede_chave_v1", "preco_tabela_v1", "preco_sync_v1"}
        assert novas <= nomes
        assert precos_repo.ultima_sync_ok(conn) is None


def test_pagina_de_outra_empresa_e_recusada(conn):
    precos_repo.aplicar_pagina(conn, _pagina([_rede()], cursor="c1"), empresa_cnpj=MM)
    with pytest.raises(ValueError):
        precos_repo.aplicar_pagina(
            conn,
            _pagina(
                [_rede("r2", raizes=("44555666",)), _preco()], cursor="c2", empresa="22222222000182"
            ),
            empresa_cnpj=MM,
        )
    assert precos_repo.estado_sync(conn).cursor == "c1"
    assert precos_repo.resolver_rede(conn, "44555666000100") is None
    assert conn.execute("SELECT COUNT(*) FROM preco_tabela_v1").fetchone()[0] == 0


def test_pagina_sem_filtro_de_empresa_aplica(conn):
    precos_repo.aplicar_pagina(conn, _pagina([_rede()], empresa=None), empresa_cnpj=MM)
    assert precos_repo.estado_sync(conn).cursor == "c1"
