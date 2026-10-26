"""Consulta por pedido: o que o portal sabe do envio ao Flow (módulo puro)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app.erp_eventos import consulta
from app.persistence import schema_env

AGORA = datetime(2026, 10, 26, 19, 30, tzinfo=UTC)  # 16:30 em São Paulo (segunda)
BASE = "mm"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(schema_env.TABLES_SQL)
    yield c
    c.close()


def estado(
    conn,
    codigo,
    *,
    pc="UY44549110",
    status="PEDIDO",
    situacao="aberto",
    visto="2026-10-20T10:00:00.000+00:00",
    atualizado=None,
    itens=2,
    base=BASE,
):
    js = {
        "erp_codigo": codigo,
        "pedido_cliente": pc,
        "cliente_erp_codigo": 7,
        "cliente_erp_cnpj": "11222333000144",
        "status": status,
        "itens": [{"erp_item_codigo": i} for i in range(itens)],
    }
    conn.execute(
        "INSERT INTO erp_pedido_estado (base, erp_codigo, situacao, estado_json, visto_em, atualizado_em) "
        "VALUES (?,?,?,?,?,?)",
        (base, codigo, situacao, json.dumps(js), visto, atualizado or visto),
    )


def evento(
    conn,
    codigo,
    ordem_id,
    tipo="pedido_visto",
    payload=None,
    situacao="pendente",
    enviado_em=None,
    lote_id=None,
    respondido_em=None,
    motivo=None,
    unidade=None,
):
    conn.execute(
        "INSERT INTO erp_eventos_saida (id, base, erp_codigo, seq, tipo, unidade, payload_json, observado_em, "
        "situacao, motivo, lote_id, enviado_em, respondido_em, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ordem_id,
            BASE,
            codigo,
            1,
            tipo,
            unidade or f"u{ordem_id}",
            json.dumps(payload or {}),
            "2026-10-20T10:00:00.000+00:00",
            situacao,
            motivo,
            lote_id,
            enviado_em,
            respondido_em,
            "2026-10-20T10:00:00.000+00:00",
        ),
    )


def lote(conn, lote_id, origem="janela", quem=None, enviado_em="2026-10-21T11:00:30.000+00:00"):
    conn.execute(
        "INSERT INTO erp_envio_lote (lote_id, base, origem, quem, enviado_em, eventos) VALUES (?,?,?,?,?,1)",
        (lote_id, BASE, origem, quem, enviado_em),
    )


def det(conn, codigo=100, **kw):
    kw.setdefault("ultima_vigia_em", "2026-10-26T19:55:00.000+00:00")
    kw.setdefault("bloqueio", None)
    kw.setdefault("agora", AGORA)
    return consulta.detalhe(conn, BASE, codigo, **kw)


def test_busca_por_codigo_fire(conn):
    estado(conn, 100, pc="ABC")
    estado(conn, 101, pc="XYZ")
    assert [r.erp_codigo for r in consulta.buscar(conn, BASE, "100")] == [100]


def test_busca_pedido_cliente_com_digitos(conn):
    estado(conn, 100, pc="UY44549110")
    r = consulta.buscar(conn, BASE, "UY44549110")
    assert [x.erp_codigo for x in r] == [100]
    estado(conn, 200, pc="44549110")  # só dígitos casa pelo pedido do cliente
    assert [x.erp_codigo for x in consulta.buscar(conn, BASE, "44549110")] == [200]


def test_busca_ignora_espaco_e_caixa(conn):
    estado(conn, 100, pc=" UY44549110 ")
    assert [x.erp_codigo for x in consulta.buscar(conn, BASE, " uy44549110 ")] == [100]


def test_busca_repetido_lista_limitada(conn):
    for i in range(60):
        estado(conn, 1000 + i, pc="DUP", visto=f"2026-10-01T10:{i:02d}:00.000+00:00")
    r = consulta.buscar(conn, BASE, "dup")
    assert len(r) == 50
    assert r[0].erp_codigo == 1059


def test_busca_so_da_base(conn):
    estado(conn, 100, pc="ABC", base="nasmar")
    assert consulta.buscar(conn, BASE, "ABC") == []


def test_inexistente_lista_vazia(conn):
    estado(conn, 100)
    assert consulta.buscar(conn, BASE, "nada") == []


def test_detalhe_inexistente_levanta(conn):
    with pytest.raises(LookupError):
        det(conn, 999)


@pytest.mark.parametrize(
    ("linhas", "bloqueio", "esperada"),
    [
        (
            [("aceito", None, "2026-10-21T11:00:40.000+00:00")],
            None,
            "O Flow aceitou todos os eventos deste pedido. Último aceite: 21/10 08:00, Janela das 8h.",
        ),
        (
            [("pendente", None, None), ("aceito", None, "x")],
            None,
            "O Flow ainda não recebeu 1 evento. Saem na janela de hoje às 17:00 ou agora pelo Enviar agora.",
        ),
        (
            [("pendente", "2026-10-26T19:00:00.000+00:00", None), ("pendente", None, None)],
            None,
            "O Flow ainda não recebeu 2 eventos. Saem na janela de amanhã às 08:00 ou agora pelo Enviar agora.",
        ),
        (
            [("rejeitado", None, "x")],
            None,
            "O Flow recusou 1 evento. Recusa não é reenviada: corrija a causa no Fire; a próxima mudança gera um evento novo.",
        ),
        (
            [("pendente", None, None)],
            "credencial",
            "O envio está parado: o Flow recusou a credencial (401). Salve uma credencial nova.",
        ),
    ],
)
def test_detalhe_frases(conn, linhas, bloqueio, esperada):
    estado(conn, 100)
    lote(conn, "L1")
    for n, (sit, env, resp) in enumerate(linhas):
        evento(
            conn,
            100,
            f"e{n}",
            situacao=sit,
            enviado_em=env or ("2026-10-21T11:00:30.000+00:00" if resp else None),
            lote_id="L1" if resp else None,
            respondido_em=resp if resp and resp != "x" else None,
        )
    ag = AGORA if "17:00" in esperada else datetime(2026, 10, 26, 21, 0, tzinfo=UTC)
    assert det(conn, bloqueio=bloqueio, agora=ag).frase == esperada


def test_frase_bloqueio_sem_texto_usa_http(conn):
    estado(conn, 100)
    d = det(conn, bloqueio="novo_codigo", ultimo_http=418)
    assert d.frase == "O envio está parado: HTTP 418."


def test_proxima_dia_da_semana(conn):
    estado(conn, 100)
    evento(conn, 100, "e0")
    sabado = datetime(2026, 10, 31, 20, 0, tzinfo=UTC)  # sáb 17:00 SP -> próxima seg
    assert "seg às 08:00" in det(conn, agora=sabado).frase


def test_leitura_aberto_usa_ultima_vigia(conn):
    estado(conn, 100, atualizado="2026-10-21T10:00:00.000+00:00")
    d = det(conn, ultima_vigia_em="2026-10-26T19:55:00.000+00:00")
    assert (d.aberto, d.leitura_em, d.leitura_tipo) == (
        True,
        "2026-10-26T19:55:00.000+00:00",
        "ultima_leitura",
    )


def test_leitura_encerrado_usa_atualizado_em(conn):
    estado(
        conn,
        100,
        status="FATURADO",
        situacao="encerrado",
        atualizado="2026-10-21T10:00:00.000+00:00",
    )
    d = det(conn)
    assert (d.aberto, d.leitura_em, d.leitura_tipo) == (
        False,
        "2026-10-21T10:00:00.000+00:00",
        "ultima_mudanca",
    )


def test_excluido_nao_e_aberto(conn):
    estado(conn, 100, situacao="excluido")
    assert det(conn).aberto is False


def test_excluido_por_evento_nao_e_aberto(conn):
    estado(conn, 100)
    evento(conn, 100, "e0", tipo="pedido_excluido")
    assert det(conn).aberto is False


def test_origem_portal_por_imports(conn):
    estado(conn, 100)
    estado(conn, 101)
    conn.execute(
        "INSERT INTO imports (id, environment_id, source_filename, imported_at, fire_codigo, customer_name, status) "
        "VALUES ('i1','e','f.pdf','2026-10-20',100,'Loja Alfa','ok')"
    )
    d = det(conn, 100)
    assert (d.origem_pedido, d.cliente_nome) == ("portal", "Loja Alfa")
    d2 = det(conn, 101)
    assert (d2.origem_pedido, d2.cliente_nome) == ("fire", None)


def test_join_vazio_origem_nao_registrada(conn):
    estado(conn, 100)
    evento(
        conn,
        100,
        "e0",
        situacao="aceito",
        lote_id="SUMIU",
        enviado_em="2026-10-21T11:00:00.000+00:00",
    )
    evento(conn, 100, "e1")
    ev = det(conn).eventos
    assert [e.origem_lote for e in ev] == ["origem não registrada", None]


def test_origens_de_lote(conn):
    estado(conn, 100)
    lote(conn, "LJ", "janela", enviado_em="2026-10-21T14:00:10.000+00:00")  # 11h SP
    lote(conn, "LF", "foto")
    lote(conn, "LM", "manual", quem="ana@x.com")
    for n, lid in enumerate(["LJ", "LF", "LM"]):
        evento(conn, 100, f"e{n}", lote_id=lid, enviado_em="2026-10-21T14:00:10.000+00:00")
    assert [e.origem_lote for e in det(conn).eventos] == [
        "Janela das 11h",
        "Antes da foto",
        "Manual · ana@x.com",
    ]


def test_retidos_quando_fila_vazia(conn):
    estado(conn, 100)
    d = det(conn)
    assert d.retidos is True
    assert d.eventos == []
    evento(conn, 100, "e0")
    assert det(conn).retidos is False


def test_eventos_view_detalhes_e_situacao(conn):
    estado(conn, 100)
    evento(conn, 100, "a", tipo="pedido_visto", unidade="U")
    evento(conn, 100, "b", tipo="item_incluido", unidade="U")
    evento(conn, 100, "c", tipo="item_incluido", unidade="U")
    evento(
        conn, 100, "d", tipo="status_alterado", payload={"antes": "PEDIDO", "depois": "FATURADO"}
    )
    evento(
        conn,
        100,
        "e",
        tipo="data_entrega_alterada",
        payload={"antes": "2026-10-30", "depois": "2026-11-05"},
    )
    evento(
        conn,
        100,
        "f",
        tipo="quantidade_alterada",
        payload={"antes": "10", "depois": "12"},
        situacao="rejeitado",
        motivo="m",
    )
    evento(conn, 100, "g", tipo="faturamento_detectado", enviado_em="2026-10-21T11:00:00.000+00:00")
    ev = det(conn).eventos
    assert [e.detalhe for e in ev] == [
        "2 itens",
        "",
        "",
        "PEDIDO → FATURADO",
        "30/10 → 05/11",
        "10 → 12",
        "",
    ]
    assert [e.situacao_tela for e in ev] == ["na_fila"] * 5 + ["recusado", "enviado"]
    assert ev[0].rotulo == "Pedido novo"
    assert ev[5].motivo == "m"


def test_rotulo_desconhecido_cru():
    assert consulta.rotulo("tipo_novo") == "tipo_novo"
    assert consulta.rotulo("preco_alterado") == "Preço mudou"
