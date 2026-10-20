from __future__ import annotations

import pytest

from app.erp import queries
from app.models.order import Order, OrderHeader, OrderItem
from app.routing import regra
from app.routing.regra import Politica, RedeFixa, UfLida, avaliar, rede_fixa_do_pedido

CLIENTE = "11222333000181"
SAMS_CD = "00000000004864"
OUTRA_LOJA = "99888777000155"


def _pedido(*, cliente=CLIENTE, entregas=()):
    return Order(
        header=OrderHeader(order_number="4711", customer_cnpj=cliente, customer_name="X"),
        items=[OrderItem(description="Meia", quantity=1, delivery_cnpj=c) for c in entregas]
        or [OrderItem(description="Meia", quantity=1)],
    )


def _politica(redes=()):
    return Politica(env_sp="mm", env_fora_sp="nasmar", redes=tuple(redes))


def _nao_ler(cnpj):
    raise AssertionError("ler_uf não deveria ser chamado")


def _ler(**ufs_por_env):
    def ler(cnpj):  # noqa: ARG001
        return tuple(UfLida(env_slug=slug, ufs=frozenset(ufs)) for slug, ufs in ufs_por_env.items())

    return ler


def test_rede_fixa_por_raiz_resolve_sem_ler_uf():
    rede = RedeFixa("Centauro", "mm", frozenset({CLIENTE[:8]}), frozenset())
    r = avaliar(_pedido(), _politica([rede]), _nao_ler)
    assert r is not None
    assert r.env_slug == "mm"
    assert "Centauro" in r.explicacao


def test_rede_fixa_por_cnpj_completo_vence_raiz():
    sams = RedeFixa("Sam's", "mm", frozenset(), frozenset({SAMS_CD}))
    outra = RedeFixa("Outra", "nasmar", frozenset({SAMS_CD[:8]}), frozenset())
    assert rede_fixa_do_pedido(_pedido(cliente=SAMS_CD), _politica([outra, sams])) is sams


def test_rede_fixa_casa_por_cnpj_de_loja_sem_cabecalho():
    rede = RedeFixa("Rede", "mm", frozenset({OUTRA_LOJA[:8]}), frozenset())
    p = _pedido(cliente=None, entregas=[OUTRA_LOJA])
    assert rede_fixa_do_pedido(p, _politica([rede])) is rede


def test_dois_cnpjs_em_redes_diferentes_nao_responde_como_fixa():
    a = RedeFixa("A", "mm", frozenset({CLIENTE[:8]}), frozenset())
    b = RedeFixa("B", "nasmar", frozenset({OUTRA_LOJA[:8]}), frozenset())
    p = _pedido(entregas=[OUTRA_LOJA])
    pol = _politica([a, b])
    assert rede_fixa_do_pedido(p, pol) is None
    # cai no passo 2 (UF do cabeçalho)
    r = avaliar(p, pol, _ler(mm={"SP"}))
    assert r is not None and "em SP" in r.explicacao


def test_sp_vai_para_env_sp():
    r = avaliar(_pedido(), _politica(), _ler(mm={"SP"}))
    assert r is not None
    assert r.env_slug == "mm"
    assert "em SP" in r.explicacao


def test_fora_de_sp_vai_para_env_fora_sp():
    r = avaliar(_pedido(), _politica(), _ler(mm={"MG"}))
    assert r is not None and r.env_slug == "nasmar"


def test_uf_suja_normalizada_vale():
    r = avaliar(_pedido(), _politica(), _ler(mm={"SP"}, nasmar={"SP"}))
    assert r is not None and r.env_slug == "mm"


def test_sem_customer_cnpj_nao_responde():
    assert avaliar(_pedido(cliente=None), _politica(), _nao_ler) is None


def test_nao_achou_em_base_nenhuma_nao_responde():
    assert avaliar(_pedido(), _politica(), _ler(mm=set(), nasmar=set())) is None


def test_linha_sem_uf_nao_responde():
    assert avaliar(_pedido(), _politica(), _ler(mm={""})) is None


def test_ufs_diferentes_entre_bases_nao_responde():
    assert avaliar(_pedido(), _politica(), _ler(mm={"SP"}, nasmar={"MG"})) is None


def test_base_indisponivel_nao_responde_mesmo_com_outra_achando():
    def ler(cnpj):  # noqa: ARG001
        return (
            UfLida("mm", frozenset({"SP"})),
            UfLida("nasmar", frozenset(), indisponivel=True),
        )

    assert avaliar(_pedido(), _politica(), ler) is None


# ── Task 3: leitura da UF ────────────────────────────────────────────────────
ENVS = [{"slug": "mm"}, {"slug": "nasmar"}]


def test_ler_uf_normaliza_cnpj_mascarado_de_entrada():
    vistos = []

    def ler(env, cnpj):
        vistos.append(cnpj)
        return ["SP"]

    regra.ler_uf("12.345.678/0001-90", envs=ENVS, ler=ler)
    assert vistos == ["12345678000190", "12345678000190"]


def test_ler_uf_normaliza_valores():
    r = regra.ler_uf(CLIENTE, envs=[{"slug": "mm"}], ler=lambda e, c: ["sp ", None])
    assert r == (UfLida("mm", frozenset({"SP", ""})),)


def test_ler_uf_mesmo_cnpj_duas_linhas_ufs_diferentes():
    leituras = regra.ler_uf(CLIENTE, envs=[{"slug": "mm"}], ler=lambda e, c: ["SP", "MG"])
    assert leituras[0].ufs == frozenset({"SP", "MG"})
    assert avaliar(_pedido(), _politica(), lambda c: leituras) is None


def test_ler_uf_erro_vira_indisponivel():
    def ler(env, cnpj):
        if env["slug"] == "nasmar":
            raise RuntimeError("fire fora")
        return ["SP"]

    r = regra.ler_uf(CLIENTE, envs=ENVS, ler=ler)
    assert r[0] == UfLida("mm", frozenset({"SP"}))
    assert r[1].indisponivel is True
    assert avaliar(_pedido(), _politica(), lambda c: r) is None


def test_cache_indisponivel_60s_e_ok_600s(monkeypatch):
    chamadas = []
    estado = {"falha": True, "t": 1000.0}

    def fake_ler_uf(cnpj, *, envs, ler=None):
        chamadas.append(cnpj)
        return tuple(UfLida(e["slug"], frozenset(), indisponivel=estado["falha"]) for e in envs)

    monkeypatch.setattr(regra, "ler_uf", fake_ler_uf)
    monkeypatch.setattr(regra.time, "monotonic", lambda: estado["t"])
    regra._cache_uf.clear()

    regra.ler_uf_com_cache(CLIENTE, ENVS)
    regra.ler_uf_com_cache(CLIENTE, ENVS)
    assert len(chamadas) == 1  # indisponível guardado, mas só por 60 s

    estado["t"] += 61
    estado["falha"] = False
    regra.ler_uf_com_cache(CLIENTE, ENVS)
    assert len(chamadas) == 2  # a falha expirou em 60 s

    estado["t"] += 599
    regra.ler_uf_com_cache(CLIENTE, ENVS)
    assert len(chamadas) == 2  # o ok vale 600 s

    estado["t"] += 2
    regra.ler_uf_com_cache(CLIENTE, ENVS)
    assert len(chamadas) == 3  # expirou
    regra._cache_uf.clear()


def test_explicacao_usa_o_nome_da_empresa():
    pol = Politica(
        env_sp="mm",
        env_fora_sp="nasmar",
        redes=(RedeFixa("Centauro", "mm", frozenset({CLIENTE[:8]}), frozenset()),),
        nomes={"mm": "MM", "nasmar": "Nasmar"},
    )
    r = avaliar(_pedido(), pol, _nao_ler)
    assert r.explicacao == "Regra comercial: Centauro fatura sempre pela MM"
    pol_uf = Politica(env_sp="mm", env_fora_sp="nasmar", redes=(), nomes=pol.nomes)
    r = avaliar(_pedido(), pol_uf, _ler(mm={"MG"}))
    assert r.explicacao == "Regra comercial: CNPJ faturado 11.222.333/0001-81 em MG → Nasmar"


def test_explicacao_sem_nome_cai_no_slug():
    r = avaliar(_pedido(), _politica(), _ler(mm={"SP"}))
    assert r.explicacao.endswith("em SP → mm")


# ── Revisão final: UF válida e SQL por CPF_CNPJ indexado ─────────────────────


@pytest.mark.parametrize("uf", ["XX", "SAO PAULO", "S", "BR", "0"])
def test_uf_invalida_nao_responde(uf):
    """UF fora das 27 + EX não vira "fora de SP"."""
    assert avaliar(_pedido(), _politica(), _ler(mm={uf})) is None


def test_uf_ex_vale_como_fora_de_sp():
    r = avaliar(_pedido(), _politica(), _ler(mm={"EX"}))
    assert r is not None and r.env_slug == "nasmar"


def test_uf_no_fire_binda_cnpj_mascarado_e_so_digitos(monkeypatch):
    from contextlib import contextmanager

    from app.erp import connection
    from app.persistence import environments_repo

    executados = []

    class _Cur:
        def execute(self, sql, params):
            executados.append((sql, params))

        def fetchall(self):
            return [("SP",)]

        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()

    class _Fb:
        @contextmanager
        def connect_somente_leitura(self, cfg):
            yield _Conn()

    monkeypatch.setattr(connection, "FirebirdConnection", _Fb)
    monkeypatch.setattr(environments_repo, "to_fb_config", lambda env: {})

    assert regra._uf_no_fire({"slug": "mm"}, CLIENTE) == ["SP"]
    ((sql, params),) = executados
    assert sql is queries.UF_CLIENTE_POR_CNPJ
    assert params == ("11.222.333/0001-81", CLIENTE)
