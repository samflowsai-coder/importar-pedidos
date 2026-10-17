from datetime import UTC, date, datetime, timedelta

from app.erp.preco_avaliacao import avaliar, cnpj_da_rede
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence.precos_repo import Rede

AGORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SYNC_OK = AGORA - timedelta(hours=1)
CNPJ = "12345678000199"
REDE = Rede(id="r1", nome="Rede X", travada=False, travada_motivo=None)


def _order(precos, *, cnpj=CNPJ, issue_date=None, delivery_cnpjs=None):
    items = []
    for i, p in enumerate(precos):
        items.append(
            OrderItem(
                product_code=f"P{i}",
                quantity=1,
                unit_price=p,
                delivery_cnpj=(delivery_cnpjs[i] if delivery_cnpjs else None),
            )
        )
    return Order(header=OrderHeader(customer_cnpj=cnpj, issue_date=issue_date), items=items)


def _check(n, ids=None):
    ids = ids if ids is not None else [1000 + i for i in range(n)]
    return {"items": [{"fire_product_id": x} for x in ids]}


def _run(
    order,
    check,
    *,
    tabela=None,
    rede=REDE,
    cnpj_cliente=CNPJ,
    grupo=None,
    sync=SYNC_OK,
    vigente=None,
):
    tabela = tabela if tabela is not None else {}

    def pv(rede_id, prod, data):
        if vigente is not None:
            return vigente(rede_id, prod, data)
        return tabela.get(prod)

    return avaliar(
        order,
        check,
        cnpj_cliente=cnpj_cliente,
        resolver_rede=lambda c: rede,
        preco_vigente=pv,
        cnpjs_grupo=grupo or set(),
        ultima_sync=sync,
        agora=AGORA,
    )


def test_igual_ao_centavo_entra():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200})
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("ok", "igual")
    assert it.preco_pedido_centavos == 5200 and it.preco_tabela_centavos == 5200


def test_um_centavo_acima_vai_para_fila():
    a = _run(_order([52.01]), _check(1), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "acima")
    assert a.pendentes == a.itens


def test_abaixo_vai_para_fila():
    a = _run(_order([51.99]), _check(1), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "abaixo")


def test_rede_travada_manda_tudo_para_fila_mesmo_igual():
    r = Rede(id="r1", nome="X", travada=True, travada_motivo="negociando")
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, rede=r)
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "rede_travada")


def test_rede_sem_tabela():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, rede=None)
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "rede_sem_tabela")
    assert a.rede is None


def test_sem_preco_na_tabela():
    a = _run(_order([52.00]), _check(1), tabela={})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "sem_preco_tabela")


def test_sem_preco_no_pedido():
    a = _run(_order([None]), _check(1), tabela={"1000": 5200})
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("fila", "sem_preco_pedido")
    assert it.preco_pedido_centavos is None and it.preco_tabela_centavos == 5200


def test_produto_nao_identificado_bloqueia():
    a = _run(_order([52.00]), _check(1, ids=[None]), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == (
        "nao_avaliado",
        "produto_nao_identificado",
    )
    assert a.bloqueados == a.itens


def test_copia_com_25h_bloqueia_tudo():
    a = _run(
        _order([52.00, 10.0]),
        _check(2),
        tabela={"1000": 5200},
        sync=AGORA - timedelta(hours=25),
    )
    assert all((i.situacao, i.motivo) == ("nao_avaliado", "copia_desatualizada") for i in a.itens)


def test_copia_com_exatamente_24h_ainda_vale():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, sync=AGORA - timedelta(hours=24))
    assert a.itens[0].situacao == "ok"


def test_sem_sync_nenhuma_bloqueia():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, sync=None)
    assert a.itens[0].motivo == "copia_desatualizada"


def test_cliente_sem_cnpj_bloqueia():
    a = _run(_order([52.00], cnpj=None), _check(1), tabela={"1000": 5200}, cnpj_cliente=None)
    assert (a.itens[0].situacao, a.itens[0].motivo) == (
        "nao_avaliado",
        "cliente_sem_cnpj",
    )
    assert a.cnpj_rede is None


def test_riachuelo_sem_cnpj_usa_raiz_das_lojas():
    o = _order(
        [1.0, 2.0],
        cnpj=None,
        delivery_cnpjs=["33.447.100/0001-11", "33447100000222"],
    )
    assert cnpj_da_rede(o, None)[:8] == "33447100"


def test_cnpj_cliente_tem_precedencia():
    o = _order([1.0], delivery_cnpjs=["33447100000111"])
    assert cnpj_da_rede(o, "98.765.432/0001-10") == "98765432000110"


def test_lojas_de_raizes_diferentes_bloqueia():
    o = _order(
        [52.0, 52.0],
        cnpj=None,
        delivery_cnpjs=["33447100000111", "11222333000144"],
    )
    assert cnpj_da_rede(o, None) is None
    a = _run(o, _check(2), tabela={"1000": 5200, "1001": 5200}, cnpj_cliente=None)
    assert all(i.motivo == "cliente_sem_cnpj" for i in a.itens)


def test_mesmo_produto_duas_linhas_avalia_cada_uma():
    a = _run(
        _order([52.00, 54.00]),
        _check(2, ids=[777, 777]),
        tabela={"777": 5200},
    )
    assert [(i.situacao, i.motivo) for i in a.itens] == [
        ("ok", "igual"),
        ("fila", "acima"),
    ]


def test_intercompany_fica_fora():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, grupo={"12345678"})
    assert a.itens[0].situacao == "fora_intercompany"
    assert a.itens[0].motivo == "intercompany"


def test_seq_int_do_check_casa_com_preco_str():
    a = _run(_order([52.00]), _check(1, ids=[1234]), tabela={"1234": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("ok", "igual")
    assert a.itens[0].fire_produto_id == "1234"


def test_vigencia_pela_data_de_emissao():
    def vig(rede_id, prod, data):
        return 5200 if data < date(2026, 11, 1) else 5600

    a = _run(_order([52.00], issue_date="30/10/2026"), _check(1), vigente=vig)
    assert a.itens[0].situacao == "ok"


def test_sem_data_usa_agora():
    vistas = []

    def vig(rede_id, prod, data):
        vistas.append(data)
        return 5200

    _run(_order([52.00]), _check(1), vigente=vig)
    _run(_order([52.00], issue_date="lixo"), _check(1), vigente=vig)
    assert vistas == [AGORA.date(), AGORA.date()]


def test_intercompany_decide_antes_da_copia_velha():
    a = _run(_order([52.00]), _check(1), grupo={"12345678"}, sync=None)
    assert [(i.situacao, i.motivo) for i in a.itens] == [("fora_intercompany", "intercompany")]


def test_cnpj_cliente_invalido_cai_para_raiz_das_lojas():
    o = _order([1.0], cnpj=None, delivery_cnpjs=["33447100000111"])
    assert cnpj_da_rede(o, "123.456.789-09") == "33447100"
    assert cnpj_da_rede(o, "123") == "33447100"


def test_cnpj_cliente_invalido_sem_lojas_e_none():
    assert cnpj_da_rede(_order([1.0], cnpj=None), "12345678909") is None


def test_cnpj_cliente_de_8_digitos_vale():
    assert cnpj_da_rede(_order([1.0], cnpj=None), "12345678") == "12345678"


def test_preco_de_tabela_zero_conta_como_sem_preco():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 0})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "sem_preco_tabela")
