from __future__ import annotations

from datetime import date

import pytest

from app.erp import queries
from app.routing import historico
from app.routing.historico import HistoricoAmbiente

ENVS = [
    {"slug": "nasmar", "name": "Nasmar", "id": "1"},
    {"slug": "americanense", "name": "MM Americanense", "id": "2"},
]


def _fake_contar(mapa):
    """mapa: {env_slug: (pedidos, ultimo_em)} — substitui o Firebird."""

    def contar(env, cnpjs, desde):  # noqa: ARG001
        return mapa.get(env["slug"], (0, None))

    return contar


def test_cliente_so_num_banco_resolve():
    hist = historico.consultar(
        ["11222333000181"],
        envs=ENVS,
        contar=_fake_contar({"nasmar": (28, "2026-08-24")}),
    )
    assert historico.resolver(hist) == "nasmar"


def test_cliente_nos_dois_bancos_recusa():
    """1 cliente em 277. O historico se cala em vez de votar no maior volume."""
    hist = historico.consultar(
        ["11222333000181"],
        envs=ENVS,
        contar=_fake_contar({"nasmar": (28, "2026-08-24"), "americanense": (6, "2026-05-28")}),
    )
    assert historico.resolver(hist) is None


def test_cliente_sem_historico_devolve_none():
    hist = historico.consultar(["11222333000181"], envs=ENVS, contar=_fake_contar({}))
    assert historico.resolver(hist) is None
    assert all(h.pedidos == 0 for h in hist)


def test_janela_de_12_meses_e_a_data_passada_ao_banco():
    """Caso Beira Rio: pedidos da MM ate 05/2025 ficam fora da janela."""
    vistos = {}

    def contar(env, cnpjs, desde):
        vistos[env["slug"]] = desde
        return (0, None)

    historico.consultar(
        ["11222333000181"],
        envs=ENVS,
        meses=12,
        hoje=date(2026, 9, 9),
        contar=contar,
    )
    assert vistos["nasmar"] == "2025-09-09"
    assert vistos["americanense"] == "2025-09-09"


def test_consulta_com_varios_cnpjs_soma_no_mesmo_ambiente():
    """Desmembramento: as lojas sao CNPJs diferentes do mesmo comprador."""
    recebidos = {}

    def contar(env, cnpjs, desde):  # noqa: ARG001
        recebidos[env["slug"]] = list(cnpjs)
        return (3, "2026-07-01") if env["slug"] == "nasmar" else (0, None)

    hist = historico.consultar(
        ["05055599002985", "05055599002632"],
        envs=ENVS,
        contar=contar,
    )
    assert recebidos["nasmar"] == ["05055599002985", "05055599002632"]
    assert historico.resolver(hist) == "nasmar"


def test_cnpj_vazio_nao_consulta_nada():
    """Sem CNPJ de cliente nao ha o que perguntar — nao abre conexao a toa."""
    chamou = False

    def contar(env, cnpjs, desde):  # noqa: ARG001
        nonlocal chamou
        chamou = True
        return (0, None)

    hist = historico.consultar([None, "", "  "], envs=ENVS, contar=contar)
    assert hist == ()
    assert chamou is False


def test_erro_de_conexao_num_ambiente_nao_derruba_o_outro():
    """VPN cai no meio da consulta: o ambiente que respondeu continua na tupla
    com seus dados, e o que falhou entra marcado 'indisponivel' — nao some,
    e o historico se recusa a responder com o que sobrou."""

    def contar(env, cnpjs, desde):  # noqa: ARG001
        if env["slug"] == "americanense":
            raise OSError("connection refused")
        return (5, "2026-08-01")

    hist = historico.consultar(["11222333000181"], envs=ENVS, contar=contar)
    por_slug = {h.env_slug: h for h in hist}
    assert por_slug.keys() == {"nasmar", "americanense"}
    assert por_slug["nasmar"] == HistoricoAmbiente("nasmar", "Nasmar", 5, "2026-08-01")
    assert por_slug["americanense"].indisponivel is True
    assert por_slug["americanense"].pedidos == 0
    assert por_slug["americanense"].ultimo_em is None
    assert historico.resolver(hist) is None


def test_ambiente_indisponivel_recusa_mesmo_quando_o_outro_tem_pedidos():
    """O caso que o achado 1 aponta: cliente tem pedido nos DOIS ambientes de
    verdade (ambiguo), a VPN de um cai, o outro responde. O historico nao
    pode aproveitar a resposta que sobrou como se fosse resposta unica."""

    def contar(env, cnpjs, desde):  # noqa: ARG001
        if env["slug"] == "nasmar":
            raise OSError("vpn down")
        return (5, "2026-08-01")

    hist = historico.consultar(["11222333000181"], envs=ENVS, contar=contar)
    assert historico.resolver(hist) is None


def test_todos_os_ambientes_indisponiveis_ainda_recusa():
    """Os dois Firebird caem: resolver continua None, mas agora com dois
    ambientes marcados na tupla, nao com tupla vazia."""

    def contar(env, cnpjs, desde):  # noqa: ARG001
        raise OSError("vpn down")

    hist = historico.consultar(["11222333000181"], envs=ENVS, contar=contar)
    assert len(hist) == 2
    assert all(h.indisponivel for h in hist)
    assert all(h.pedidos == 0 and h.ultimo_em is None for h in hist)
    assert historico.resolver(hist) is None


def test_resolver_ignora_ambiente_com_zero_pedidos():
    hist = (
        HistoricoAmbiente("nasmar", "Nasmar", 4, "2026-05-07"),
        HistoricoAmbiente("americanense", "MM", 0, None),
    )
    assert historico.resolver(hist) == "nasmar"


def test_janela_31_05_menos_3_meses_cai_em_fevereiro_sem_levantar():
    """31/03 menos 12 meses cai em 31/03, mas 31/05 menos 3 meses cairia em
    31/02 e date() levantaria — o clamp via monthrange evita isso."""
    desde = historico._janela_desde(3, date(2026, 5, 31))
    assert desde == "2026-02-28"


def test_janela_29_02_bissexto_menos_12_meses_nao_levanta():
    desde = historico._janela_desde(12, date(2024, 2, 29))
    assert desde == "2023-02-28"


def test_janela_meses_negativo_levanta_valueerror():
    with pytest.raises(ValueError):
        historico._janela_desde(-1, date(2026, 9, 9))


def test_count_pedidos_cliente_desde_sql_zero_cnpjs_levanta_valueerror():
    with pytest.raises(ValueError):
        queries.count_pedidos_cliente_desde_sql(0)


def test_count_pedidos_cliente_desde_sql_n_placeholders_de_cnpj():
    """n=3 CNPJs + o bind de `desde` = 4 '?' na query inteira."""
    sql = queries.count_pedidos_cliente_desde_sql(3)
    assert sql.count("?") == 4
