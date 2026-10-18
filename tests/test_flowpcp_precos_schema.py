"""Schema do envio (rota b) e das decisões (rota c) do contrato precos v1, porta do zod."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.integrations.flowpcp import precos_schema as ps

EXEMPLOS = json.loads(
    (Path(__file__).parent / "fixtures" / "flow_precos_v1" / "exemplos.json").read_text("utf-8")
)


def _pedido():
    return copy.deepcopy(EXEMPLOS["aprovacaoPedido"])


@pytest.mark.parametrize("nome", ["aprovacaoPedido", "aprovacaoPedidoNasmar", "aprovacaoRetirada"])
def test_exemplos_do_envio_validam(nome):
    ps.validar_envio(EXEMPLOS[nome])


def test_exemplos_das_decisoes_validam():
    ps.DecisoesPaginaV1.model_validate(EXEMPLOS["decisoesPagina"])
    ps.DecisaoPrecoV1.model_validate(EXEMPLOS["decisaoRecusada"])


def _invalido(corpo):
    with pytest.raises(ValueError):
        ps.validar_envio(corpo)


def test_item_chave_repetida_recusa():
    c = _pedido()
    c["itens"][1]["itemChave"] = c["itens"][0]["itemChave"]
    _invalido(c)


def test_preco_tabela_id_acompanha_o_preco():
    c = _pedido()
    c["itens"][0]["precoTabelaId"] = None
    _invalido(c)


def test_item_ok_com_motivo_ou_preco_diferente_recusa():
    c = _pedido()
    c["itens"][0]["motivo"] = "preco_acima"
    _invalido(c)
    c = _pedido()
    c["itens"][0]["precoPedidoCentavos"] = 4591
    _invalido(c)


@pytest.mark.parametrize(
    ("motivo", "pedido", "tabela", "ok"),
    [
        ("rede_travada", None, None, True),
        ("pedido_sem_preco", None, 100, True),
        ("pedido_sem_preco", 100, 100, False),
        ("sem_preco_tabela", 100, None, True),
        ("sem_preco_tabela", None, None, False),
        ("preco_acima", 101, 100, True),
        ("preco_acima", 99, 100, False),
        ("preco_abaixo", 99, 100, True),
        ("preco_abaixo", 100, 100, False),
    ],
)
def test_coerencia_do_motivo(motivo, pedido, tabela, ok):
    c = _pedido()
    it = c["itens"][1]
    it.update(
        motivo=motivo,
        precoPedidoCentavos=pedido,
        precoTabelaCentavos=tabela,
        precoTabelaId=None if tabela is None else it["precoTabelaId"],
    )
    if ok:
        ps.validar_envio(c)
    else:
        _invalido(c)


def test_pedido_sem_item_na_fila_recusa():
    c = _pedido()
    c["itens"] = [c["itens"][0]]
    _invalido(c)


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("quantidade", 0),
        ("quantidade", 1.005),
        ("quantidade", True),
        ("codigo", "   "),
        ("descricao", ""),
        ("unidadeErp", " "),
        ("linha", -1),
        ("precoPedidoCentavos", 0),
        ("precoPedidoCentavos", 45.9),
        ("precoTabelaId", "nao-e-uuid"),
    ],
)
def test_campos_do_item(campo, valor):
    c = _pedido()
    c["itens"][1][campo] = valor
    _invalido(c)


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("revisao", 0),
        ("revisao", True),
        ("conteudoHash", "curto"),
        ("empresaFaturadoraCnpj", "11.111.111/0001-91"),
        ("recebidoEm", "2026-10-20T13:25:41-03:00"),  # zod datetime(): só Z
        ("emitidoEm", "2026-10-20"),
        ("redeId", "r1"),
        ("pedidoNumero", ""),
        ("tabelaCursor", ""),
    ],
)
def test_campos_do_pedido(campo, valor):
    c = _pedido()
    c[campo] = valor
    _invalido(c)


def test_emitido_em_nulo_vale():
    c = _pedido()
    c["emitidoEm"] = None
    ps.validar_envio(c)


def test_quantidade_inteira_e_com_duas_casas_valem():
    c = _pedido()
    c["itens"][0]["quantidade"] = 12.5
    c["itens"][1]["quantidade"] = 3.25
    ps.validar_envio(c)


def test_decisao_recusada_sem_motivo_recusa():
    d = copy.deepcopy(EXEMPLOS["decisaoRecusada"])
    d["motivoRecusa"] = None
    with pytest.raises(ValueError):
        ps.DecisaoPrecoV1.model_validate(d)


# ─── rota (a), resposta da (b) e confirmação da (c) ─────────────────────────


def test_exemplos_da_tabela_da_resposta_e_da_confirmacao_validam():
    ps.TabelaPaginaV1.model_validate(EXEMPLOS["tabelaPagina"])
    ps.AprovacaoRespostaV1.model_validate(EXEMPLOS["aprovacaoResposta"])
    ps.ConfirmacaoDecisoesV1.model_validate(EXEMPLOS["confirmacao"])
    ps.ConfirmacaoRespostaV1.model_validate(EXEMPLOS["confirmacaoResposta"])


def test_tabela_ignora_campo_novo_e_aceita_preco_base():
    p = copy.deepcopy(EXEMPLOS["tabelaPagina"])
    p["campoNovo"] = 1
    p["mudancas"][2]["redeId"] = None
    p["mudancas"][2]["outroCampo"] = "x"
    pagina = ps.TabelaPaginaV1.model_validate(p)
    assert pagina.mudancas[2].redeId is None


@pytest.mark.parametrize(
    ("indice", "campo", "valor"),
    [
        (0, "raizes", ["1122233"]),
        (1, "cnpjs", ["11.222.333/0009-99"]),
        (2, "vigenciaInicio", "2026-02-30"),
        (2, "registradoEm", "2026-10-20T10:42:13-03:00"),
        (2, "precoCentavos", 0),
        (2, "unidadeErp", ""),
        (2, "tipo", "outro"),
    ],
)
def test_mudanca_fora_do_contrato_recusa_a_pagina(indice, campo, valor):
    p = copy.deepcopy(EXEMPLOS["tabelaPagina"])
    p["mudancas"][indice][campo] = valor
    with pytest.raises(ValueError):
        ps.TabelaPaginaV1.model_validate(p)


def test_confirmacao_com_uuid_repetido_em_outra_caixa_recusa():
    c = copy.deepcopy(EXEMPLOS["confirmacao"])
    c["confirmacoes"][1]["decisaoId"] = c["confirmacoes"][0]["decisaoId"].upper()
    with pytest.raises(ValueError):
        ps.ConfirmacaoDecisoesV1.model_validate(c)


def test_resposta_da_retirada_com_aprovacao_nula_valida():
    r = {
        "aprovacaoId": "00000000-0000-0000-0000-000000000000",
        "externalId": "x",
        "empresaFaturadoraCnpj": "11111111000191",
        "revisao": 1,
        "situacao": "retirada",
        "jaRecebido": False,
        "itens": [],
    }
    assert ps.AprovacaoRespostaV1.model_validate(r).aprovacaoId == r["aprovacaoId"]
