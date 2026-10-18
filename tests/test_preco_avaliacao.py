from datetime import UTC, date, datetime, timedelta

from app.erp.preco_avaliacao import avaliar, cnpj_da_rede, data_ref_do_pedido
from app.erp.preco_regra import PrecoInvalido, PrecoLinha
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence.precos_repo import Rede

AGORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SYNC_OK = AGORA - timedelta(hours=1)
CNPJ = "12345678000199"
REDE = Rede(id="r1", nome="Rede X", ativa=True, travada=False)


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


def _check(n, ids=None, unidade="KIT"):
    ids = ids if ids is not None else [1000 + i for i in range(n)]
    return {"items": [{"fire_product_id": x, "fire_unidade": unidade} for x in ids]}


def _linha(prod, centavos, *, rede_id="r1", unidade="KIT", preco_id=None):
    return PrecoLinha(
        preco_id=preco_id or f"p-{prod}",
        rede_id=rede_id,
        empresa_cnpj="11111111000191",
        erp_produto_id=prod,
        unidade_erp=unidade,
        preco_centavos=centavos,
        vigencia_inicio=date(2026, 1, 1),
        registrado_em="2026-01-01T00:00:00.000Z",
        cancelado=False,
    )


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
    data_ref=AGORA.date(),
    redes=None,
):
    tabela = tabela if tabela is not None else {}

    def pv(rede_id, prod, data):
        if vigente is not None:
            return vigente(rede_id, prod, data)
        c = tabela.get(prod)
        return None if c is None else _linha(prod, c, rede_id=rede_id)

    return avaliar(
        order,
        check,
        cnpj_cliente=cnpj_cliente,
        resolver_rede=(lambda c: redes.get(c)) if redes is not None else (lambda c: rede),
        preco_vigente=pv,
        cnpjs_grupo=grupo or set(),
        ultima_sync=sync,
        agora=AGORA,
        data_ref=data_ref,
    )


def test_igual_ao_centavo_entra():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200})
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("ok", "ok")
    assert it.preco_pedido_centavos == 5200 and it.preco_tabela_centavos == 5200


def test_um_centavo_acima_vai_para_fila():
    a = _run(_order([52.01]), _check(1), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "preco_acima")
    assert a.pendentes == a.itens


def test_abaixo_vai_para_fila():
    a = _run(_order([51.99]), _check(1), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "preco_abaixo")


def test_rede_travada_manda_tudo_para_fila_mesmo_igual():
    r = Rede(id="r1", nome="X", ativa=True, travada=True)
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, rede=r)
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "rede_travada")


def test_cliente_sem_rede_nao_e_avaliado():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, rede=None)
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("nao_avaliado", "cliente_sem_rede")
    assert a.rede is None
    assert a.bloqueados == a.itens and a.pendentes == []


def test_sem_preco_na_tabela():
    a = _run(_order([52.00]), _check(1), tabela={})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "sem_preco_tabela")


def test_sem_preco_no_pedido():
    a = _run(_order([None]), _check(1), tabela={"1000": 5200})
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("fila", "pedido_sem_preco")
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


def test_riachuelo_sem_cnpj_usa_o_menor_cnpj_das_lojas():
    o = _order(
        [1.0, 2.0],
        cnpj=None,
        delivery_cnpjs=["33447100000222", "33.447.100/0001-11"],
    )
    assert cnpj_da_rede(o, None) == "33447100000111"


def test_riachuelo_loja_sem_cnpj_valido_e_none():
    o = _order([1.0, 2.0], cnpj=None, delivery_cnpjs=["33447100000111", "33447100"])
    assert cnpj_da_rede(o, None) is None
    o = _order([1.0, 2.0], cnpj=None, delivery_cnpjs=["33447100000111", None])
    assert cnpj_da_rede(o, None) is None


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
        ("ok", "ok"),
        ("fila", "preco_acima"),
    ]


def test_intercompany_fica_fora():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 5200}, grupo={"12345678"})
    assert a.itens[0].situacao == "fora_intercompany"
    assert a.itens[0].motivo == "intercompany"


def test_seq_int_do_check_casa_com_preco_str():
    a = _run(_order([52.00]), _check(1, ids=[1234]), tabela={"1234": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("ok", "ok")
    assert a.itens[0].fire_produto_id == "1234"


def test_vigencia_pela_data_de_referencia_nao_pelo_agora():
    vistas = []

    def vig(rede_id, prod, data):
        vistas.append(data)
        return _linha(prod, 5200 if data < date(2026, 11, 1) else 5600)

    a = _run(_order([52.00]), _check(1), vigente=vig, data_ref=date(2026, 10, 30))
    assert a.itens[0].situacao == "ok"
    assert vistas == [date(2026, 10, 30)]


def test_data_ref_pelo_issue_date_e_nao_pelo_agora():
    o = _order([1.0], issue_date="30/09/2026")
    assert data_ref_do_pedido(o, "2026-10-17T09:00:00", AGORA) == date(2026, 9, 30)


def test_data_ref_sem_emissao_usa_recebimento_em_sao_paulo():
    # 23:30 em SP (naive, hora do servidor) é 02:30Z do dia seguinte: vale o dia de SP.
    o = _order([1.0], issue_date="lixo")
    assert data_ref_do_pedido(o, "2026-10-16T23:30:00", AGORA) == date(2026, 10, 16)


def test_data_ref_sem_emissao_nem_recebimento_usa_agora_em_sao_paulo():
    o = _order([1.0])
    agora = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)  # 22:00 de 17/10 em SP
    assert data_ref_do_pedido(o, None, agora) == date(2026, 10, 17)
    assert data_ref_do_pedido(o, "lixo", agora) == date(2026, 10, 17)


def test_intercompany_decide_antes_da_copia_velha():
    a = _run(_order([52.00]), _check(1), grupo={"12345678"}, sync=None)
    assert [(i.situacao, i.motivo) for i in a.itens] == [("fora_intercompany", "intercompany")]


def test_cnpj_cliente_invalido_cai_para_as_lojas():
    o = _order([1.0], cnpj=None, delivery_cnpjs=["33447100000111"])
    assert cnpj_da_rede(o, "123.456.789-09") == "33447100000111"
    assert cnpj_da_rede(o, "123") == "33447100000111"


def test_cnpj_cliente_invalido_sem_lojas_e_none():
    assert cnpj_da_rede(_order([1.0], cnpj=None), "12345678909") is None


def test_cnpj_cliente_de_8_digitos_nao_e_cnpj():
    assert cnpj_da_rede(_order([1.0], cnpj=None), "12345678") is None


def test_preco_de_tabela_zero_conta_como_sem_preco():
    a = _run(_order([52.00]), _check(1), tabela={"1000": 0})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "sem_preco_tabela")


# ── Contrato precos v1 (Task 7.2) ───────────────────────────────────────────


def test_precedencia_rede_travada_vence_pedido_sem_preco():
    r = Rede(id="r1", nome="X", ativa=True, travada=True)
    a = _run(_order([None]), _check(1), tabela={"1000": 5200}, rede=r)
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("fila", "rede_travada")


def test_precedencia_pedido_sem_preco_vence_sem_preco_tabela():
    a = _run(_order([None]), _check(1), tabela={})
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("fila", "pedido_sem_preco")
    assert it.preco_tabela_centavos is None and it.preco_tabela_id is None


def test_preco_zero_no_pedido_e_pedido_sem_preco():
    a = _run(_order([0.0]), _check(1), tabela={"1000": 5200})
    assert a.itens[0].motivo == "pedido_sem_preco"
    assert a.itens[0].preco_pedido_centavos is None


def test_unidade_divergente_nao_avalia():
    a = _run(_order([52.00]), _check(1, unidade="PACK"), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("nao_avaliado", "unidade_divergente")


def test_unidade_compara_sem_espaco_nem_caixa():
    a = _run(_order([52.00]), _check(1, unidade=" kit "), tabela={"1000": 5200})
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("ok", "ok")
    assert a.itens[0].unidade_erp == "KIT"


def test_fire_unidade_ausente_e_unidade_divergente():
    for unidade in (None, "", "   "):
        a = _run(_order([52.00]), _check(1, unidade=unidade), tabela={"1000": 5200})
        assert (a.itens[0].situacao, a.itens[0].motivo) == ("nao_avaliado", "unidade_divergente")
    a = _run(_order([52.00]), {"items": [{"fire_product_id": 1000}]}, tabela={"1000": 5200})
    assert a.itens[0].motivo == "unidade_divergente"


def test_unidade_erp_nula_na_tabela_e_unidade_divergente():
    a = _run(
        _order([52.00]),
        _check(1),
        vigente=lambda rede_id, prod, data: _linha(prod, 5200, unidade=None),
    )
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("nao_avaliado", "unidade_divergente")


def test_unidade_divergente_vence_rede_travada():
    r = Rede(id="r1", nome="X", ativa=True, travada=True)
    a = _run(_order([52.00]), _check(1, unidade="PACK"), tabela={"1000": 5200}, rede=r)
    assert a.itens[0].motivo == "unidade_divergente"


def test_preco_base_vale_quando_a_rede_nao_tem_o_seu():
    vistos = []

    def vig(rede_id, prod, data):
        vistos.append(rede_id)
        return _linha(prod, 5200, rede_id=None, preco_id="base-1")

    a = _run(_order([52.00]), _check(1), vigente=vig)
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("ok", "ok")
    assert it.preco_tabela_id == "base-1" and it.preco_tabela_centavos == 5200
    assert vistos == ["r1"]


def test_preco_invalido_nao_avalia_o_item():
    def vig(rede_id, prod, data):
        if prod == "1000":
            raise PrecoInvalido("registradoEm inválido no preço x")
        return _linha(prod, 5200)

    a = _run(_order([52.00, 52.00]), _check(2), vigente=vig)
    assert [(i.situacao, i.motivo) for i in a.itens] == [
        ("nao_avaliado", "preco_invalido"),
        ("ok", "ok"),
    ]


def test_meia_para_cima_45895_contra_4590_entra():
    a = _run(_order([45.895]), _check(1), tabela={"1000": 4590})
    it = a.itens[0]
    assert (it.situacao, it.motivo) == ("ok", "ok")
    assert it.preco_pedido_centavos == 4590


def test_item_leva_preco_id_e_unidade():
    a = _run(_order([54.00]), _check(1), tabela={"1000": 5200})
    it = a.itens[0]
    assert it.preco_tabela_id == "p-1000"
    assert it.unidade_erp == "KIT"
    assert it.preco_tabela_centavos == 5200


def test_cnpj_alfanumerico_do_cliente_casa_a_raiz_alfanumerica():
    alfa = "12ABC34501DE35"
    r = Rede(id="ra", nome="Alfa", ativa=True, travada=False)
    vistos = []

    def resolver(c):
        vistos.append(c)
        return r if c[:8] == "12ABC345" else None

    o = _order([52.00], cnpj="12.abc.345/01de-35")
    a = avaliar(
        o,
        _check(1),
        cnpj_cliente="12.abc.345/01de-35",
        resolver_rede=resolver,
        preco_vigente=lambda rede_id, prod, data: _linha(prod, 5200, rede_id=rede_id),
        cnpjs_grupo=set(),
        ultima_sync=SYNC_OK,
        agora=AGORA,
        data_ref=AGORA.date(),
    )
    assert vistos == [alfa]
    assert a.cnpj_rede == alfa and a.rede == r
    assert (a.itens[0].situacao, a.itens[0].motivo) == ("ok", "ok")


def test_intercompany_compara_raiz_alfanumerica():
    a = _run(
        _order([52.00], cnpj="12ABC34501DE35"),
        _check(1),
        cnpj_cliente="12abc34501de35",
        grupo={"12ABC345"},
    )
    assert a.itens[0].situacao == "fora_intercompany"
