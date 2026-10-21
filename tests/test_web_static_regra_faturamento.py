"""Task 10: as telas da regra comercial de faturamento.

Sem infra de JS no repo (mesmo padrão de `test_web_static_produto.py`): o teste
lê os arquivos estáticos e trava as rotas consumidas, os textos fixos que as
telas prometem e a ausência de `confirm()` nas confirmações novas.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"


def _ler(nome: str) -> str:
    return (STATIC / nome).read_text(encoding="utf-8")


def _corpo_da_funcao(fonte: str, nome: str) -> str:
    i = fonte.index(f"function {nome}(")
    recuo = i - fonte.rindex("\n", 0, i) - 1
    if fonte[i - 6 : i] == "async ":
        recuo -= 6
    return fonte[i : fonte.index("\n" + " " * recuo + "}\n", i)]


def test_rotulo_do_degrau_regra():
    assert "regra: 'Regra comercial'" in _ler("js/roteamento-labels.js")


@pytest.mark.parametrize(
    "trecho",
    [
        "${API}/regra",
        "/redes/${id}",
        'id="regra-sp"',
        'id="regra-fora"',
        'id="regra-switch"',
        'role="switch"',
        "Regra ligada",
        'id="regra-erro"',
        "Sem chave: a regra não liga.",
        "Adicionar chave",
        "Salvar rede",
        # fix round 1: empresas salvas juntas; rede sem chave com a regra ligada
        'id="regra-salvar-empresas"',
        "Salvar empresas",
        "Sem chave: os pedidos desta rede vão pela UF.",
    ],
)
def test_admin_tem_trecho(trecho):
    assert trecho in _ler("admin-roteamento.html"), trecho


@pytest.mark.parametrize(
    "trecho",
    [
        # preview: sugestão contrariada, troca com confirmação inline, aviso R8
        "O pedido traz o CNPJ da",
        "rot.pode_trocar",
        'id="pvRoutingTroca"',
        "Confirmar troca",
        "Os produtos serão conferidos de novo no Fire da",
        "body.environment_slug = routingChoice",
        # detalhe: linha do tempo e mover
        'id="pvRoutingHistory"',
        "/api/roteamento/decisoes?import_id=",
        "/mover-empresa",
        "Mover para ",
        # fix round 1: guarda do mover vem do servidor; empresa por slug; nota R8
        "data.pode_mover",
        "data.motivo_nao_move",
        "data.env_slug",
        "function routingNotaConferencia(",
        "Os produtos serão conferidos no Fire da",
    ],
)
def test_index_tem_trecho(trecho):
    assert trecho in _ler("index.html"), trecho


@pytest.mark.parametrize(
    "funcao",
    ["onRoutingTrocaChange", "confirmarTrocaEmpresa", "pedirMoverEmpresa", "moverEmpresa"],
)
def test_confirmacoes_sem_confirm_nativo(funcao):
    corpo = _corpo_da_funcao(_ler("index.html"), funcao)
    assert "confirm(" not in corpo and "alert(" not in corpo


@pytest.mark.parametrize("funcao", ["salvarRegraConfig", "salvarRede"])
def test_admin_sem_confirm_nativo(funcao):
    corpo = _corpo_da_funcao(_ler("admin-roteamento.html"), funcao)
    assert "confirm(" not in corpo and "alert(" not in corpo


def test_nota_r8_nos_dois_ramos_do_seletor():
    fonte = _ler("index.html")
    corpo = _corpo_da_funcao(fonte, "renderRoutingBanner")
    assert "routingNotaConferencia(routingChoice" in corpo
    assert "routingNotaConferencia(" in _corpo_da_funcao(fonte, "renderRoutingTroca")


def test_empresa_do_pedido_nao_casa_por_nome():
    corpo = _corpo_da_funcao(_ler("index.html"), "desenharRoutingHistory")
    assert "env_name" not in corpo and "env_name" not in _corpo_da_funcao(
        _ler("index.html"), "renderRoutingHistory"
    )


def test_selects_da_regra_nao_salvam_no_change():
    corpo = _corpo_da_funcao(_ler("admin-roteamento.html"), "onRegraEmpresaChange")
    assert "salvarRegraConfig" not in corpo


def test_rastro_do_pedido_nao_busca_nada_em_desligado():
    """C1: o modo vem no payload do detalhe; em desligado (ou sem modo) a tela
    não faz requisição nenhuma do rastro."""
    corpo = _corpo_da_funcao(_ler("index.html"), "renderRoutingHistory")
    assert "/api/roteamento/modo" not in corpo
    guarda = corpo.index("data.roteamento_modo")
    assert guarda < corpo.index("api(")
    assert "'observando'" in corpo[guarda : corpo.index("api(")]
