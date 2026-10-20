from __future__ import annotations

from app.models.order import Order, OrderHeader, OrderItem
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
