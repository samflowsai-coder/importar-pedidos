"""Figura fiscal do pedido no insert direto.

A nota do Fire sai da figura do cabeçalho, e CFOP/alíquota/redução vêm da
`FIGURA_FISCAL` dela (medido em 26/10 contra 433 notas autorizadas da .7). A
escolha segue o histórico da própria empresa e nunca chuta.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.erp import figura_fiscal, queries
from app.erp.exceptions import FirebirdFiguraFiscalIndefinidaError
from app.erp.figura_fiscal import Figura, escolher
from app.erp.fiscal import perfil_para

# Recorte real da FIGURA_FISCAL da .7 (MM) e da .4 (Nasmar).
_SETE = ";AC;AL;AP;AM;BA;CE;DF;ES;GO;MA;MT;MS;PA;PB;PE;PI;RN;RO;RR;SE;TO;"
_DOZE = ";MG;PR;RJ;RS;SC;"


def _fig(codigo, cfop, icms, red, ufs):
    return Figura(codigo, cfop, Decimal(icms), Decimal(red), figura_fiscal.ufs_de(ufs))


MM = {
    1: _fig(1, "5.101", "18", "33.33", ";SP;"),
    7: _fig(7, "5.101", "18", "0", ";SP;"),
    8: _fig(8, "6.101", "12", "0", _DOZE),
    17: _fig(17, "6.101", "7", "0", _SETE),
    23: _fig(23, "6.109", "7", "0", ";AM;"),
}
NASMAR = {
    4: _fig(4, "6.102", "7", "0", _SETE),
    5: _fig(5, "6.102", "12", "0", _DOZE),
}


def test_ufs_de_le_a_lista_do_fire():
    assert figura_fiscal.ufs_de(";SP;") == frozenset({"SP"})
    assert figura_fiscal.ufs_de(None) == frozenset()
    assert figura_fiscal.ufs_de(" ;mg; pr ;") == frozenset({"MG", "PR"})


def test_cliente_com_uma_figura_so_usa_a_dele():
    # Cliente de SP que sempre comprou como não contribuinte (7) não vira 1.
    assert escolher("SP", {7: 4}, {1: 900, 7: 4}, 1, MM.get) == (7, "cliente")


def test_cliente_com_duas_figuras_desce_para_a_uf():
    assert escolher("SP", {1: 3, 7: 1}, {1: 900, 7: 4}, 1, MM.get) == (1, "uf")


def test_cliente_novo_usa_a_figura_dominante_da_uf():
    assert escolher("RN", {}, {17: 78}, 1, MM.get) == (17, "uf")
    assert escolher("SC", {}, {5: 100}, 5, NASMAR.get) == (5, "uf")
    assert escolher("PE", {}, {4: 9, 5: 1}, 5, NASMAR.get) == (4, "uf")


def test_uf_sem_dominancia_recusa():
    # Nasmar AM em 2026: 3 pedidos na 4 e 1 na 5 (75%): não decide.
    assert escolher("AM", {}, {4: 3, 5: 1}, 5, NASMAR.get) is None


def test_uf_com_amostra_pequena_nao_decide_e_cai_no_padrao_se_ele_cobre_a_uf():
    # MM tem 1 pedido de MG (figura 8): amostra pequena; padrão 1 não cobre MG.
    assert escolher("MG", {}, {8: 1}, 1, MM.get) is None
    # Nasmar: padrão 5 cobre RS.
    assert escolher("RS", {}, {}, 5, NASMAR.get) == (5, "padrao")


def test_padrao_que_nao_cobre_a_uf_recusa():
    # O defeito de antes: figura 1 (SP) para cliente de fora de SP.
    assert escolher("RN", {}, {}, 1, MM.get) is None
    assert escolher("BA", {}, {}, 5, NASMAR.get) is None


def test_figura_que_nao_cobre_a_uf_do_cliente_e_descartada_em_todo_degrau():
    # Cliente que mudou de estado: o histórico dele (1, SP) não vale para RN.
    assert escolher("RN", {1: 5}, {17: 78}, 1, MM.get) == (17, "uf")
    # Figura sumida da tabela também é descartada.
    assert escolher("RN", {99: 5}, {}, 1, MM.get) is None


def test_sem_uf_recusa():
    assert escolher("", {1: 5}, {}, 1, MM.get) is None
    assert escolher(None, {1: 5}, {}, 1, MM.get) is None


def _cursor(uf, por_cliente, por_uf, figuras):
    """Cursor falso que responde cada SQL do módulo pelo texto."""
    cur = MagicMock()
    estado = {}

    def execute(sql, params=()):
        estado["sql"], estado["params"] = sql, params

    def fetchone():
        if estado["sql"] == queries.FIND_CLIENT_UF:
            return (uf,)
        if estado["sql"] == queries.FIGURA_FISCAL_POR_CODIGO:
            f = figuras.get(estado["params"][0])
            return (
                None
                if f is None
                else (f.codigo, f.cfop, f.icms, f.reducao, ";" + ";".join(f.ufs) + ";")
            )
        raise AssertionError(estado["sql"])

    def fetchall():
        if estado["sql"] == queries.FIGURAS_DO_CLIENTE:
            return list(por_cliente.items())
        if estado["sql"] == queries.FIGURAS_DA_UF:
            return list(por_uf.items())
        raise AssertionError(estado["sql"])

    cur.execute.side_effect = execute
    cur.fetchone.side_effect = fetchone
    cur.fetchall.side_effect = fetchall
    return cur


def test_resolver_troca_figura_cfop_icms_e_reducao_pelo_da_figura():
    cur = _cursor("RN", {}, {17: 78}, MM)

    perfil = figura_fiscal.resolver(cur, 4242, perfil_para(None))

    assert (perfil.codfigfiscal, perfil.cfop_principal) == (17, "6.101")
    assert (perfil.icms_porc, perfil.reducao) == (Decimal("7"), Decimal("0"))
    # O resto do perfil (cobrança, classe financeira) não muda.
    assert perfil.tipo_cob == perfil_para(None).tipo_cob


def test_resolver_sp_usa_a_reducao_da_tabela():
    cur = _cursor("SP", {1: 12}, {1: 900}, MM)

    perfil = figura_fiscal.resolver(cur, 4242, perfil_para(None))

    assert perfil.codfigfiscal == 1
    assert perfil.reducao == Decimal("33.33")


def test_resolver_sem_decisao_levanta_com_a_uf():
    cur = _cursor("AM", {}, {4: 3, 5: 1}, NASMAR)

    with pytest.raises(FirebirdFiguraFiscalIndefinidaError) as exc:
        figura_fiscal.resolver(cur, 4242, perfil_para({"fiscal_codfigfiscal": 5}))

    assert exc.value.uf == "AM"
    assert exc.value.cliente == 4242


def test_historico_ignora_o_proprio_portal_e_cancelados():
    for sql in (queries.FIGURAS_DO_CLIENTE, queries.FIGURAS_DA_UF):
        assert "IMPORTADOR" in sql
        assert "CANCELADO" in sql
        assert "CODFIGFISCAL IS NOT NULL" in sql


# ── Integração com o FirebirdExporter ───────────────────────────────────────


def _exporter_com(cur, monkeypatch):
    from app.exporters.firebird_exporter import FirebirdExporter

    exporter = FirebirdExporter()
    conn = MagicMock()
    conn.cursor.return_value = cur

    class _Ctx:
        def __enter__(self):  # noqa: N805
            return conn

        def __exit__(self, *a):  # noqa: N805
            return False

    monkeypatch.setattr(exporter, "_conn", MagicMock())
    exporter._conn.is_configured.return_value = True
    exporter._conn.connect.return_value = _Ctx()
    return exporter


def _cursor_do_pedido(uf, por_uf, figuras):
    """Responde o fluxo inteiro do insert: cliente, duplicado, figura, PKs, produto."""
    cur = _cursor(uf, {}, por_uf, figuras)
    figura_fetchone = cur.fetchone.side_effect
    estado = cur.execute.side_effect
    ultimo = {}

    def execute(sql, params=()):
        ultimo["sql"] = sql
        estado(sql, params)

    def fetchone():
        sql = ultimo["sql"]
        if sql == queries.FIND_CLIENT_BY_CNPJ:
            return (4242, "CLIENTE")
        if sql == queries.CHECK_ORDER_EXISTS:
            return (0,)
        if sql == queries.GET_NEXT_CABVENDAS_CODIGO:
            return (100,)
        if sql == queries.GET_NEXT_CORPOVENDAS_CODIGO:
            return (200,)
        if sql == queries.FIND_PRODUCT_BY_EAN:
            return (501, "KIT", 0, "KIT")
        return figura_fetchone()

    cur.execute.side_effect = execute
    cur.fetchone.side_effect = fetchone
    return cur


def _pedido():
    from app.models.order import Order, OrderHeader, OrderItem

    return Order(
        header=OrderHeader(order_number="PED-1", customer_cnpj="12.345.678/0001-90"),
        items=[OrderItem(description="KIT", quantity=2, unit_price=10.0, ean="7891234567890")],
    )


def _params(cur, sql):
    return [c.args[1:] for c in cur.execute.call_args_list if c.args[0] == sql]


def test_pedido_de_fora_de_sp_grava_a_figura_e_o_fiscal_da_figura(monkeypatch):
    cur = _cursor_do_pedido("RN", {17: 78}, MM)

    result = _exporter_com(cur, monkeypatch).export(_pedido())

    assert result.skipped is False
    ((cab,),) = _params(cur, queries.INSERT_CAB_VENDAS)
    assert cab[16] == 17  # CODFIGFISCAL (17o da tupla de order_to_cabvendas)
    ((item,),) = _params(cur, queries.INSERT_CORPO_VENDAS)
    assert (item[9], item[11], item[15]) == (Decimal("7"), Decimal("0"), "6.101")


def test_sem_decisao_de_figura_nao_grava_nada(monkeypatch):
    cur = _cursor_do_pedido("AM", {4: 3, 5: 1}, NASMAR)

    result = _exporter_com(cur, monkeypatch).export(_pedido())

    assert (result.skipped, result.skip_reason) == (True, "FIGURA_FISCAL_INDEFINIDA")
    assert _params(cur, queries.INSERT_CAB_VENDAS) == []
    assert _params(cur, queries.INSERT_CORPO_VENDAS) == []
    assert _params(cur, queries.GET_NEXT_CABVENDAS_CODIGO) == []
