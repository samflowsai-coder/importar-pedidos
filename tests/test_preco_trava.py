"""Portão único de preço no envio (app/erp/preco_trava.py)."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

import app.erp.product_check as pc
from app.erp import preco_trava
from app.integrations.flowpcp import precos_schema
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import db, environments_repo, precos_repo, repo
from app.persistence import preco_revisao_repo as revs

AGORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
CNPJ_REDE = "11222333000144"
OUTRO_CNPJ = "99888777000166"
EMPRESA = "11111111000191"  # CNPJ do ambiente = empresa faturadora
R1 = "a1a1a1a1-0000-4000-8000-000000000001"
R2 = "a1a1a1a1-0000-4000-8000-000000000002"
_NS = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000000")


def _preco_id(rede_id, pid):
    return str(uuid.uuid5(_NS, f"{rede_id}-{pid}"))


@pytest.fixture(autouse=True)
def ambiente(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _order(precos=(54.0,), *, cnpj=CNPJ_REDE, delivery_cnpj=None):
    return Order(
        header=OrderHeader(order_number="P1", customer_name="Rede X", customer_cnpj=cnpj),
        items=[
            OrderItem(
                description="x",
                ean=f"789{i}",
                product_code=f"C{i}",
                quantity=10,
                unit_price=p,
                delivery_cnpj=delivery_cnpj,
            )
            for i, p in enumerate(precos)
        ],
    )


def _check(n=1, *, available=True, status="match", ids=None):
    ids = ids if ids is not None else [123 + i for i in range(n)]
    return {
        "available": available,
        "items": [
            {
                "ean": f"789{i}",
                "product_code": f"C{i}",
                "fire_product_id": pid,
                "fire_unidade": "KIT",
                "price_status": status,
                "unit_price_order": 54.0,
                "fire_preco_venda": 50.0,
            }
            for i, pid in enumerate(ids)
        ],
        "summary": {},
    }


def _seed(order, **extra):
    import_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": import_id,
            "source_filename": "p.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": "P1",
            "status": "success",
            "portal_status": "parsed",
            "snapshot": order.model_dump(),
        }
    )
    entry = repo.get_import(import_id)
    entry.update(extra)
    return import_id, entry


def _pagina_v1(conn, redes, precos, *, gerado, cursor="c1", empresa_preco=EMPRESA):
    """Página da rota (a) do contrato precos v1 com as redes e preços dados."""
    mudancas = [
        {
            "tipo": "rede",
            "redeId": rede_id,
            "nome": nome,
            "ativa": True,
            "travada": False,
            "raizes": [raiz],
            "cnpjs": [],
        }
        for rede_id, nome, raiz in redes
    ] + [
        {
            "tipo": "preco",
            "precoId": _preco_id(rede_id, pid),
            "redeId": rede_id,
            "empresaFaturadoraCnpj": empresa_preco,
            "erpProdutoId": pid,
            "unidadeErp": "KIT",
            "precoCentavos": c,
            "vigenciaInicio": "2026-01-01",
            "registradoEm": "2026-01-01T00:00:00.000Z",
            "cancelado": False,
        }
        for rede_id, pid, c in precos
    ]
    precos_repo.aplicar_pagina(
        conn,
        {
            "schema": "precos.tabela.v1",
            "capacidades": ["precos.tabela.v1", "precos.aprovacao.v1"],
            "geradoEm": gerado.isoformat(),
            "empresaFaturadoraCnpj": EMPRESA,
            "reiniciar": False,
            "mudancas": mudancas,
            "cursor": cursor,
            "temMais": False,
        },
        empresa_cnpj=EMPRESA,
    )


def _copia(preco_centavos=5000, *, sync=AGORA - timedelta(hours=1), produtos=("123",)):
    with db.connect() as conn:
        _pagina_v1(
            conn,
            [(R1, "Rede X", CNPJ_REDE[:8])],
            [(R1, p, preco_centavos) for p in produtos],
            gerado=sync,
        )


def _env(modo):
    return {"id": "test-env-id", "slug": "test", "preco_trava_modo": modo, "cnpj": EMPRESA}


def _live(monkeypatch, check):
    monkeypatch.setattr(pc, "check_order", lambda order, **kw: copy.deepcopy(check))


def _audits(import_id, tipo=None):
    out = repo.list_audit(import_id)
    return [a for a in out if tipo is None or a["event_type"] == tipo]


def _decidir(
    import_id,
    item_index=0,
    *,
    resultado="acordo_comercial",
    preco=None,
    motivo=None,
    revisao=None,
    decisao_id=None,
):
    """Decisão do Flow para a `itemChave` na revisão vigente (ou na dada)."""
    with db.connect() as conn:
        vig = revs.vigente(conn, import_id)
        if revisao is None:
            revisao = vig["revisao"]
        produto = next(
            i["erpProdutoId"]
            for i in vig["corpo"]["itens"]
            if i["itemChave"] == f"{import_id}:{item_index}"
        )
        decisao_id = decisao_id or str(uuid.uuid4())
        d = {
            "decisaoId": decisao_id,
            "externalId": import_id,
            "empresaFaturadoraCnpj": EMPRESA,
            "revisao": revisao,
            "itemChave": f"{import_id}:{item_index}",
            "erpProdutoId": produto,
            "resultado": resultado,
            "precoAprovadoCentavos": None if resultado == "recusado" else preco,
            "precoTabelaNoMomentoCentavos": 5000,
            "precoIdCriado": str(uuid.uuid4()) if resultado == "virar_regra" else None,
            "motivoRecusa": (motivo or "sem acordo") if resultado == "recusado" else None,
            "observacao": None,
            "decisor": "automatico"
            if resultado in ("conforme_tabela", "aprovado_automatico")
            else "aprovador",
            "decididoEm": "2026-10-17T12:30:00.000Z",
        }
        precos_schema.DecisaoPrecoV1.model_validate(d)
        assert revs.gravar_decisao(conn, d, agora=AGORA) == "aplicada"
    return decisao_id


def _revisoes(import_id):
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM preco_revisao WHERE import_id = ? ORDER BY revisao", (import_id,)
            )
        ]


def _corpo(import_id):
    import json

    return json.loads(_revisoes(import_id)[-1]["corpo_json"])


def _decisoes(import_id):
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM preco_decisao WHERE import_id = ? ORDER BY decisao_id",
                (import_id,),
            )
        ]


# ── Oráculo: o bloco que vivia em server.py (_send_one_to_fire/_export_one_xlsx) ──


def _hoje(check, ack_items):
    blocked, block_detail = pc.is_blocking(check, ack_items=ack_items)
    if not blocked:
        return False, None, block_detail, []
    labels = []
    if block_detail["items_mismatch"]:
        labels.append("price_mismatch")
    if block_detail["items_no_order_price"]:
        labels.append("missing_order_price")
    if block_detail["items_no_price_unacked"]:
        labels.append("no_price_unacked")
    parts = []
    if block_detail["items_mismatch"]:
        parts.append(f"{len(block_detail['items_mismatch'])} item(ns) com preço divergente do Fire")
    if block_detail["items_no_order_price"]:
        parts.append(f"{len(block_detail['items_no_order_price'])} item(ns) sem preço no pedido")
    if block_detail["items_no_price_unacked"]:
        parts.append(
            f"{len(block_detail['items_no_price_unacked'])} item(ns) sem preço no cadastro do Fire (sem confirmação)"
        )
    return True, "Pedido bloqueado: " + "; ".join(parts) + ".", block_detail, labels


def _misto():
    c = _check(3)
    c["items"][0]["price_status"] = "mismatch"
    c["items"][1]["price_status"] = "no_order_price"
    c["items"][2]["price_status"] = "no_price_in_fire"
    return c


CENARIOS_HOJE = {
    "mismatch": _check(status="mismatch"),
    "no_order_price": _check(status="no_order_price"),
    "no_price_unacked": _check(status="no_price_in_fire"),
    "os_tres": _misto(),
}


@pytest.mark.parametrize("modo", [None, "desligado"])
@pytest.mark.parametrize("nome", list(CENARIOS_HOJE))
def test_desligado_e_bit_a_bit_hoje(monkeypatch, modo, nome):
    check = CENARIOS_HOJE[nome]
    order = _order((54.0,) * len(check["items"]))
    import_id, entry = _seed(order)
    _live(monkeypatch, check)
    env = None if modo is None else _env(modo)

    d = preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)

    blocked, detalhe, block_detail, labels = _hoje(check, [])
    assert blocked is True
    assert d.bloqueado is True
    assert d.reason == "price_check_failed"
    assert d.detalhe == detalhe
    assert d.block_detail == block_detail
    assert d.metric_labels == labels
    assert d.precos_aplicar == {}
    assert d.avaliacao is None
    assert _audits(import_id) == []  # o chamador grava o audit, não o portão
    assert _revisoes(import_id) == []


def test_desligado_respeita_ack_e_check_indisponivel(monkeypatch):
    order = _order()
    import_id, entry = _seed(order, sem_preco_ack_items=[{"ean": "7890", "product_code": None}])
    _live(monkeypatch, _check(status="no_price_in_fire"))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("desligado"), agora=AGORA)
    assert d.bloqueado is False

    _live(monkeypatch, _check(available=False, status="mismatch"))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("desligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.precos_aplicar == {}


def test_observando_nao_bloqueia_e_audita(monkeypatch):
    _copia(5000)
    order = _order((54.0,))  # 54 ≠ 50 da tabela → fila
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("observando"), agora=AGORA)

    assert d.bloqueado is False
    assert d.avaliacao is not None and len(d.avaliacao.pendentes) == 1
    obs = _audits(import_id, "preco_observado")
    assert len(obs) == 1
    assert obs[0]["detail"]["itens"][0]["situacao"] == "fila"
    assert _revisoes(import_id) == []  # observando não cria revisão (nada vai ao Flow)


def test_observando_mantem_a_regra_de_hoje(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    check = _check(status="mismatch")
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("observando"), agora=AGORA)

    _, detalhe, block_detail, labels = _hoje(check, [])
    assert d.bloqueado is True
    assert (d.reason, d.detalhe, d.block_detail, d.metric_labels) == (
        "price_check_failed",
        detalhe,
        block_detail,
        labels,
    )
    assert len(_audits(import_id, "preco_observado")) == 1


def test_observando_com_falha_na_avaliacao_nao_derruba(monkeypatch):
    order = _order()
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    def _boom(*a, **kw):
        raise RuntimeError("sqlite caiu")

    monkeypatch.setattr(precos_repo, "ultima_sync_ok", _boom)
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("observando"), agora=AGORA)
    assert d.bloqueado is False


def test_ligado_item_na_fila_bloqueia(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(status="mismatch"))  # regra de hoje NÃO é usada em ligado

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"
    assert d.detalhe == "Pedido aguardando aprovação de preço no Flow: 1 item(ns)."
    assert d.metric_labels == ["preco_aguardando"]
    assert d.block_detail["reason"] == "preco_aguardando"
    linhas = _revisoes(import_id)
    assert [(r["revisao"], r["situacao"]) for r in linhas] == [(1, "pendente_envio")]
    item = _corpo(import_id)["itens"][0]
    assert (item["situacao"], item["motivo"]) == ("na_fila", "preco_acima")


def test_ligado_preco_igual_a_tabela_libera_mesmo_com_regra_de_hoje_bloqueando(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(status="mismatch"))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    # 100% ok contra a cópia: segue sem falar com o Flow, preço do pedido intocado
    assert d.bloqueado is False
    assert d.precos_aplicar == {}
    assert d.aplicados == []
    assert _revisoes(import_id) == []


def test_ligado_aprovado_libera_com_o_preco_aprovado(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    assert preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA
    ).bloqueado

    did = _decidir(import_id, resultado="acordo_comercial", preco=5400)
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {0: 54.0}  # precoAprovadoCentavos/100 de cada decisão
    assert d.aplicados == [{"item_index": 0, "preco": 54.0, "flow_id": did}]
    assert _audits(import_id, "preco_aplicado") == []


def test_ligado_recusado_bloqueia_com_motivo(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, resultado="recusado", motivo="fora da política da rede")

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_recusado"
    assert d.detalhe == "Preço recusado no Flow: fora da política da rede."


def test_ligado_sem_preco_no_pedido_aplica_preco_do_aprovador(monkeypatch):
    _copia(5000)
    order = _order((None,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    first = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert first.bloqueado
    item = _corpo(import_id)["itens"][0]
    assert (item["motivo"], item["precoPedidoCentavos"]) == ("pedido_sem_preco", None)
    did = _decidir(import_id, resultado="acordo_comercial", preco=4800)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {0: 48.0}
    assert d.aplicados == [{"item_index": 0, "preco": 48.0, "flow_id": did}]
    # o portão não afirma que o preço foi aplicado: quem grava é o chamador, após sair
    assert _audits(import_id, "preco_aplicado") == []


def test_item_ok_local_com_conforme_tabela_usa_o_preco_aprovado(monkeypatch):
    _copia(5000, produtos=("123", "124"))
    order = _order((50.0, 54.0))  # item 0 ok local, item 1 na fila
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(2))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    # com revisão, o item ok local também espera a decisão do Flow
    assert d.detalhe == "Pedido aguardando aprovação de preço no Flow: 2 item(ns)."
    _decidir(import_id, 0, resultado="conforme_tabela", preco=5000)
    _decidir(import_id, 1, resultado="virar_regra", preco=5400)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {0: 50.0, 1: 54.0}


def test_um_item_recusado_e_outro_aprovado_bloqueia_com_o_motivo(monkeypatch):
    _copia(5000, produtos=("123", "124"))
    order = _order((50.0, 54.0))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(2))
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, 0, resultado="conforme_tabela", preco=5000)
    _decidir(import_id, 1, resultado="recusado", motivo="abaixo do acordo; corrigir para 50,00")

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_recusado"
    assert d.detalhe == "Preço recusado no Flow: abaixo do acordo; corrigir para 50,00."
    assert d.precos_aplicar == {}


def test_a_b_a_gera_tres_revisoes(monkeypatch):
    _copia(5000)
    import_id, entry = _seed(_order((54.0,)))
    _live(monkeypatch, _check())
    for preco in (54.0, 55.0, 54.0):
        preco_trava.decidir_envio(
            import_id, _order((preco,)), entry, env=_env("ligado"), agora=AGORA
        )
    assert [(r["revisao"], r["situacao"]) for r in _revisoes(import_id)] == [
        (1, "substituida"),
        (2, "substituida"),
        (3, "pendente_envio"),
    ]


def test_decisao_da_revisao_velha_nao_libera_a_nova(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    editado = _order((55.0,))
    preco_trava.decidir_envio(import_id, editado, entry, env=_env("ligado"), agora=AGORA)

    with db.connect() as conn:
        d1 = {
            "decisaoId": str(uuid.uuid4()),
            "externalId": import_id,
            "revisao": 1,
            "itemChave": f"{import_id}:0",
            "resultado": "acordo_comercial",
            "precoAprovadoCentavos": 5400,
            "decididoEm": "2026-10-17T12:30:00.000Z",
        }
        assert revs.gravar_decisao(conn, d1, agora=AGORA) == "descartada"
    d = preco_trava.decidir_envio(import_id, editado, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"


def test_recusada_flow_e_novo_envio_cria_revisao_seguinte(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    with db.connect() as conn:
        revs.marcar_recusa_flow(conn, import_id, 1, "rede_desconhecida", em=AGORA)  # 422

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"
    assert "A revisão anterior foi recusada pelo Flow: rede_desconhecida." in d.detalhe
    linhas = _revisoes(import_id)
    assert [(r["revisao"], r["situacao"]) for r in linhas] == [
        (1, "substituida"),
        (2, "pendente_envio"),
    ]
    assert linhas[0]["conteudo_hash"] == linhas[1]["conteudo_hash"]


def test_bloqueio_local_bloqueia_sem_recriar_revisao_e_conteudo_novo_libera_o_envio(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    with db.connect() as conn:
        revs.marcar_bloqueio_local(
            conn, import_id, 1, "corpo fora do contrato, não enviado ao Flow", em=AGORA
        )

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert "corpo fora do contrato, não enviado ao Flow" in d.detalhe
    assert "recusad" not in d.detalhe
    assert [(r["revisao"], r["situacao"]) for r in _revisoes(import_id)] == [(1, "pendente_envio")]

    editado = _order((55.0,))
    d = preco_trava.decidir_envio(import_id, editado, entry, env=_env("ligado"), agora=AGORA)
    assert d.reason == "preco_aguardando"
    linhas = _revisoes(import_id)
    assert [(r["revisao"], r["situacao"]) for r in linhas] == [
        (1, "substituida"),
        (2, "pendente_envio"),
    ]
    assert linhas[1]["bloqueio_local"] is None


def test_recusa_do_flow_e_edicao_que_zera_a_fila_libera_e_enfileira_retirada(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    with db.connect() as conn:
        revs.marcar_enviada(conn, import_id, 1, {"situacao": "pendente"}, em=AGORA)
    _decidir(import_id, resultado="recusado", motivo="corrigir para 50,00")

    corrigido = _order((50.0,))  # bate com a tabela
    d = preco_trava.decidir_envio(import_id, corrigido, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {}
    with db.connect() as conn:
        assert revs.vigente(conn, import_id) is None
        pend = revs.retiradas_pendentes(conn, 10)
    assert [(p["import_id"], p["revisao"]) for p in pend] == [(import_id, 1)]


def test_corpo_invalido_bloqueia_local_e_nao_grava_revisao(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    order.header.order_number = None  # pedidoNumero obrigatório no contrato
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == (
        "Pedido não pode ir ao Flow para aprovação de preço: número do pedido ausente."
    )
    assert _revisoes(import_id) == []


def test_bloqueio_de_pedido_nao_toca_revisao_nem_decisao(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    with db.connect() as conn:
        revs.marcar_enviada(conn, import_id, 1, {}, em=AGORA)
    _decidir(import_id, resultado="acordo_comercial", preco=5400)
    antes = (_revisoes(import_id), _decisoes(import_id))

    for env in ({**_env("ligado"), "cnpj": None}, _env("ligado")):
        sync = AGORA - timedelta(hours=25) if env["cnpj"] else None
        if sync:
            _copia(5000, sync=sync)  # cópia desatualizada
        d = preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)
        assert d.bloqueado is True
        assert d.reason == "preco_nao_avaliado"
        assert (_revisoes(import_id), _decisoes(import_id)) == antes


def test_aplicar_precos_muta_o_preco_do_item():
    order = _order((None, 10.0))
    preco_trava.aplicar_precos(order, {0: 48.0})
    assert order.items[0].unit_price == 48.0
    assert order.items[1].unit_price == 10.0
    preco_trava.aplicar_precos(order, {})
    assert order.items[0].unit_price == 48.0


def test_fire_fora_usa_check_salvo(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=_check())
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.avaliacao.itens[0].situacao == "ok"


def test_fire_fora_sem_check_salvo_bloqueia(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=None)
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == "1 item(ns) sem avaliação de preço: produto não identificado no Fire."
    assert d.avaliacao.itens[0].motivo == "produto_nao_identificado"


def test_fire_fora_com_check_salvo_de_outra_versao_do_pedido_bloqueia(monkeypatch):
    _copia(5400)
    order = _order((54.0, 54.0))
    import_id, entry = _seed(order, check=_check(1))  # salvo com 1 item, pedido tem 2
    _live(monkeypatch, _check(available=False, ids=[None, None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"


def test_override_de_cliente_define_a_rede(monkeypatch):
    _copia(5400)
    order = _order((54.0,), cnpj=OUTRO_CNPJ)
    _live(monkeypatch, _check())

    import_id, entry = _seed(order)
    sem = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert sem.bloqueado is True  # OUTRO_CNPJ não tem rede → fila

    import_id, entry = _seed(
        order, cliente_override_codigo=4242, cliente_override_cnpj="11.222.333/0001-44"
    )
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.avaliacao.cnpj_rede == CNPJ_REDE


def test_override_antigo_sem_cnpj_nao_avalia(monkeypatch):
    _copia(5400)
    # entrega na rede: sem o curto-circuito, o avaliador cairia na raiz das lojas
    order = _order((54.0,), cnpj=CNPJ_REDE, delivery_cnpj=CNPJ_REDE)
    import_id, entry = _seed(order, cliente_override_codigo=4242, cliente_override_cnpj=None)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert {i.motivo for i in d.avaliacao.itens} == {"cliente_sem_cnpj"}
    assert d.detalhe == "1 item(ns) sem avaliação de preço: cliente sem CNPJ."


def _env_grupo(slug, *, cnpj=None, intercompany=None):
    e = environments_repo.create(
        slug=slug,
        name=slug.upper(),
        watch_dir="/tmp/in",
        output_dir="/tmp/out",
        fb_path="",
        cnpj=cnpj,
    )
    if intercompany:
        environments_repo.set_intercompany_config(e["id"], cnpj=intercompany, revenda_slug=slug)
    return e


def test_cnpjs_grupo_vem_de_todos_os_ambientes():
    _env_grupo("mm", cnpj="55.666.777/0001-88")
    _env_grupo("nasmar", intercompany="44.333.222/0001-11")
    _env_grupo("vazio")
    assert preco_trava.cnpjs_grupo() == {"55666777", "44333222"}


def test_intercompany_em_ligado_ainda_passa_pela_regra_de_hoje(monkeypatch):
    _copia(5400)
    _env_grupo("nasmar", intercompany=CNPJ_REDE)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    check = _check(status="mismatch")
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    _, detalhe, block_detail, labels = _hoje(check, [])
    assert d.bloqueado is True
    assert (d.reason, d.detalhe, d.block_detail, d.metric_labels) == (
        "price_check_failed",
        detalhe,
        block_detail,
        labels,
    )
    assert len(_audits(import_id, "preco_intercompany_fora_da_trava")) == 1

    _live(monkeypatch, _check(status="match"))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.avaliacao.itens[0].situacao == "fora_intercompany"


def test_intercompany_com_empresa_do_proprio_grupo(monkeypatch):
    _copia(5400)
    _env_grupo("mm", cnpj=CNPJ_REDE)
    order = _order((99.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.avaliacao.itens[0].situacao == "fora_intercompany"


def test_ligado_pedido_sem_itens_bloqueia(monkeypatch):
    _copia(5400)
    order = _order(())
    import_id, entry = _seed(order, check=_check(0))
    _live(monkeypatch, _check(0))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == "Pedido sem itens: nada para avaliar."


def test_intercompany_com_fire_fora_e_sem_check_salvo_bloqueia_com_mensagem_propria(monkeypatch):
    _copia(5400)
    _env_grupo("nasmar", intercompany=CNPJ_REDE)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=None)
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == "Pedido intercompany: não foi possível checar no Fire agora; tente de novo."


def test_aprovacao_de_uma_rede_nao_vale_depois_do_override_para_outra(monkeypatch):
    _copia(5000)
    with db.connect() as conn:
        _pagina_v1(
            conn,
            [(R2, "Rede Y", "55666777")],
            [(R2, "123", 5000)],
            gerado=AGORA - timedelta(hours=1),
            cursor="c2",
        )
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, resultado="acordo_comercial", preco=5400)
    assert not preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA
    ).bloqueado

    entry["cliente_override_codigo"] = 777
    entry["cliente_override_cnpj"] = "55666777000188"
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"
    linhas = _revisoes(import_id)
    assert [(r["revisao"], r["situacao"]) for r in linhas] == [
        (1, "substituida"),
        (2, "pendente_envio"),
    ]
    assert _corpo(import_id)["cliente"]["cnpj"] == "55666777000188"
    assert _corpo(import_id)["redeId"] == R2


def test_ligado_item_editado_invalida_a_decisao(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, resultado="acordo_comercial", preco=5400)

    editado = _order((55.0,))
    d = preco_trava.decidir_envio(import_id, editado, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"
    linhas = _revisoes(import_id)
    assert [(r["revisao"], r["situacao"]) for r in linhas] == [
        (1, "substituida"),
        (2, "pendente_envio"),
    ]


# ── Hash do conteúdo (decisão 3) ─────────────────────────────────────────────


def _hash(order, ids=(123,), unidade="KIT", cliente=CNPJ_REDE):
    from app.erp.preco_avaliacao import Avaliacao, ItemAvaliado
    from app.erp.preco_regra import centavos

    av = Avaliacao(
        None,
        cliente,
        [
            ItemAvaliado(
                i,
                str(ids[i]),
                "fila",
                "preco_acima",
                centavos(it.unit_price),
                5000,
                unidade_erp=unidade,
            )
            for i, it in enumerate(order.items)
        ],
    )
    return preco_trava.conteudo_hash(order, av, cliente_cnpj=cliente, empresa_cnpj=EMPRESA)


def test_hash_muda_com_preco_quantidade_produto_unidade_cliente_e_nao_com_descricao():
    base = _hash(_order((54.0,)))
    assert base.startswith("sha256:") and len(base) == 71
    assert _hash(_order((54.0,))) == base

    outra_desc = _order((54.0,))
    outra_desc.items[0].description = "outra descrição"
    assert _hash(outra_desc) == base

    qtd = _order((54.0,))
    qtd.items[0].quantity = 11
    assert _hash(qtd) != base
    assert _hash(_order((54.01,))) != base
    assert _hash(_order((54.0,)), ids=(999,)) != base
    assert _hash(_order((54.0,)), unidade="PAR") != base
    assert _hash(_order((54.0,)), cliente=OUTRO_CNPJ) != base

    # 10 e 10.0 são a mesma quantidade (texto a 2 casas)
    q10 = _order((54.0,))
    q10.items[0].quantity = 10.0
    assert _hash(q10) == base


# ── Corpo do envio contra o contrato ─────────────────────────────────────────


def test_corpo_bate_com_o_exemplo_do_contrato(monkeypatch):
    import json
    from pathlib import Path

    exemplo = json.loads(
        (Path(__file__).parent / "fixtures" / "flow_precos_v1" / "exemplos.json").read_text("utf-8")
    )["aprovacaoPedido"]
    _copia(5000, produtos=("123", "124", "125"))
    order = _order((50.0, 54.0, None))  # ok, preco_acima, pedido_sem_preco
    order.header.issue_date = "20/10/2026"
    order.header.customer_name = "Rede X Loja 12"
    order.items[2].description = ""  # cai na descrição do Fire
    import_id, entry = _seed(order)
    entry["imported_at"] = "2026-10-20T10:25:41"  # hora de São Paulo
    check = _check(3)
    check["items"][2]["fire_description"] = "KIT 6 PARES"
    _live(monkeypatch, check)

    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    corpo = _corpo(import_id)

    def _tipos(o):
        if isinstance(o, dict):
            return {k: _tipos(v) for k, v in o.items()}
        return type(o).__name__

    assert set(corpo) == set(exemplo)
    assert set(corpo["cliente"]) == set(exemplo["cliente"])
    for item in corpo["itens"]:
        assert set(item) == set(exemplo["itens"][0])
    precos_schema.validar_envio(corpo)
    assert corpo["revisao"] == 1
    assert corpo["externalId"] == import_id
    assert corpo["empresaFaturadoraCnpj"] == EMPRESA
    assert corpo["pedidoNumero"] == "P1"
    assert corpo["emitidoEm"] == "2026-10-20T03:00:00.000Z"
    assert corpo["recebidoEm"] == "2026-10-20T13:25:41.000Z"
    assert corpo["cliente"] == {"nome": "Rede X Loja 12", "cnpj": CNPJ_REDE}
    assert corpo["redeId"] == R1
    assert corpo["tabelaCursor"] == "c1"
    assert corpo["conteudoHash"] == _revisoes(import_id)[0]["conteudo_hash"]
    assert [
        (
            i["itemChave"],
            i["linha"],
            i["erpProdutoId"],
            i["codigo"],
            i["unidadeErp"],
            i["quantidade"],
            i["situacao"],
            i["motivo"],
            i["precoPedidoCentavos"],
            i["precoTabelaCentavos"],
            i["precoTabelaId"],
        )
        for i in corpo["itens"]
    ] == [
        (
            f"{import_id}:0",
            0,
            "123",
            "C0",
            "KIT",
            10.0,
            "ok",
            None,
            5000,
            5000,
            _preco_id(R1, "123"),
        ),
        (
            f"{import_id}:1",
            1,
            "124",
            "C1",
            "KIT",
            10.0,
            "na_fila",
            "preco_acima",
            5400,
            5000,
            _preco_id(R1, "124"),
        ),
        (
            f"{import_id}:2",
            2,
            "125",
            "C2",
            "KIT",
            10.0,
            "na_fila",
            "pedido_sem_preco",
            None,
            5000,
            _preco_id(R1, "125"),
        ),
    ]
    assert [i["descricao"] for i in corpo["itens"]] == ["x", "x", "KIT 6 PARES"]
    assert _tipos(corpo["itens"][0]) == {
        k: ("float" if k == "quantidade" else v) for k, v in _tipos(exemplo["itens"][0]).items()
    }


def test_corpo_sem_nome_no_cabecalho_usa_a_razao_do_fire(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    order.header.customer_name = None
    order.items[0].product_code = None  # código cai no erpProdutoId
    import_id, entry = _seed(order)
    check = _check()
    check["client"] = {"razao_social": "REDE X COMERCIO LTDA"}
    _live(monkeypatch, check)

    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    corpo = _corpo(import_id)
    assert corpo["cliente"]["nome"] == "REDE X COMERCIO LTDA"
    assert corpo["itens"][0]["codigo"] == "123"
    assert corpo["emitidoEm"] is None


def test_ligado_copia_desatualizada_bloqueia(monkeypatch):
    _copia(5400, sync=AGORA - timedelta(hours=25))
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert "mais de 24 h" in d.detalhe


def test_ligado_falha_na_avaliacao_bloqueia(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    def _boom(*a, **kw):
        raise RuntimeError("sqlite caiu")

    monkeypatch.setattr(precos_repo, "ultima_sync_ok", _boom)
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"


def test_avaliar_entry_sem_fire_sem_audit_e_sem_pendencia(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=_check())

    def _sem_fire(*a, **kw):
        raise AssertionError("avaliar_entry não pode consultar o Fire")

    monkeypatch.setattr(pc, "check_order", _sem_fire)

    assert preco_trava.avaliar_entry(entry, order, env=_env("desligado"), agora=AGORA) is None
    assert preco_trava.avaliar_entry(entry, order, env=None, agora=AGORA) is None
    av = preco_trava.avaliar_entry(entry, order, env=_env("ligado"), agora=AGORA)
    assert av is not None and av.itens[0].situacao == "fila"
    assert preco_trava.preview_preco(import_id, entry, order, env=_env("ligado"), agora=AGORA)
    assert _audits(import_id) == []
    assert _revisoes(import_id) == []  # preview nunca cria revisão


# ── Caminho web: audit e métrica idênticos aos de hoje em `desligado` ──────────


def _contadores():
    from app.observability import metrics

    return {
        r: metrics.price_check_blocks_total.labels(reason=r)._value.get()
        for r in ("price_mismatch", "missing_order_price", "no_price_unacked")
    }


@pytest.mark.parametrize(
    ("rota", "evento"),
    [("send-to-fire", "send_to_fire_blocked"), ("export-xlsx", "xlsx_export_blocked")],
)
def test_desligado_grava_mesmo_audit_e_metrica(monkeypatch, rota, evento):
    from fastapi.testclient import TestClient

    from app.web.server import app

    client = TestClient(app, raise_server_exceptions=False)
    check = _misto()
    order = _order((54.0, 54.0, 54.0))
    import_id, _ = _seed(order)
    _live(monkeypatch, check)
    antes = _contadores()

    r = client.post(f"/api/imported/{import_id}/{rota}")

    _, detalhe, block_detail, _labels = _hoje(check, [])
    assert r.status_code == 409
    assert r.json()["detail"] == detalhe
    bloqueios = _audits(import_id, evento)
    assert len(bloqueios) == 1
    assert bloqueios[0]["detail"] == block_detail
    depois = _contadores()
    assert {k: depois[k] - antes[k] for k in depois} == {
        "price_mismatch": 1,
        "missing_order_price": 1,
        "no_price_unacked": 1,
    }


# ── Paridade preview × portão ────────────────────────────────────────────────


def _liberado_no_preview(preco):
    return preco["bloqueio_pedido"] is None and all(
        i["estado"] in ("ok", "aprovado", "intercompany") for i in preco["itens"]
    )


@pytest.mark.parametrize(
    "cenario",
    [
        "igual",
        "acima",
        "aprovado",
        "aprovado_sem_preco",
        "recusado",
        "intercompany",
        "sem_itens",
        "copia_velha",
        "editado_depois_da_decisao",
        "recusada_flow",
        "bloqueada_local",
        "fila_zerada_depois_do_envio",
    ],
)
def test_preview_concorda_com_o_portao(monkeypatch, cenario):
    precos = {"igual": (50.0,), "sem_itens": (), "aprovado_sem_preco": (None,)}.get(
        cenario, (54.0,)
    )
    _copia(5400 if cenario == "intercompany" else 5000)
    if cenario == "copia_velha":
        _copia(5000, sync=AGORA - timedelta(hours=25))
    if cenario == "intercompany":
        _env_grupo("nasmar", intercompany=CNPJ_REDE)
    order = _order(precos)
    check = _check(len(precos))
    import_id, entry = _seed(order, check=check)
    _live(monkeypatch, check)
    env = _env("ligado")

    preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)  # abre a revisão
    decisao = {
        "aprovado": ("acordo_comercial", 5400),
        "aprovado_sem_preco": ("acordo_comercial", 4800),
        "recusado": ("recusado", None),
        "editado_depois_da_decisao": ("acordo_comercial", 5400),
        "fila_zerada_depois_do_envio": ("recusado", None),
    }.get(cenario)
    if decisao:
        _decidir(import_id, resultado=decisao[0], preco=decisao[1], motivo="x")
    if cenario == "recusada_flow":
        with db.connect() as conn:
            revs.marcar_recusa_flow(conn, import_id, 1, "produto_desconhecido", em=AGORA)
    if cenario == "bloqueada_local":
        with db.connect() as conn:
            revs.marcar_bloqueio_local(
                conn, import_id, 1, "pedido grande demais para o Flow", em=AGORA
            )
    if cenario == "editado_depois_da_decisao":
        order = _order((55.0,))
    if cenario == "fila_zerada_depois_do_envio":
        order = _order((50.0,))

    antes = _revisoes(import_id)
    preco = preco_trava.preview_preco(import_id, entry, order, env=env, agora=AGORA)
    assert _revisoes(import_id) == antes  # preview é só leitura
    d = preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)

    assert _liberado_no_preview(preco) is (not d.bloqueado), (cenario, preco)
    if cenario == "sem_itens":
        assert preco["bloqueio_pedido"] == d.detalhe
    if cenario == "recusada_flow":
        assert preco["revisao"] == {
            "numero": 1,
            "situacao": "recusada_flow",
            "erro_flow": "produto_desconhecido",
        }
        assert preco["itens"][0]["estado"] == "recusado"
    if cenario == "bloqueada_local":
        assert preco["bloqueio_pedido"] == d.detalhe
        assert "pedido grande demais para o Flow" in d.detalhe
        assert preco["itens"][0]["estado"] == "nao_avaliado"


# ── Revisão final: produto do portão = produto gravado (de-para) ──────────────


def _precos_por_produto(precos: dict[str, int]):
    with db.connect() as conn:
        _pagina_v1(
            conn,
            [(R1, "Rede X", CNPJ_REDE[:8])],
            [(R1, pid, c) for pid, c in precos.items()],
            gerado=AGORA - timedelta(hours=1),
        )


@pytest.mark.parametrize("modo", ["ligado", "observando"])
def test_vinculo_do_de_para_e_o_produto_avaliado(monkeypatch, modo):
    # Item casa direto no Fire (SEQ 123 = A) mas tem vínculo de-para para o SEQ 456 (B):
    # o Fire recebe B, então o portão avalia B. A unidade do check é a de A:
    # a de B é desconhecida, e unidade desconhecida bloqueia (contrato §3).
    _precos_por_produto({"123": 5400, "456": 5000})
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(
        import_id, order, entry, env=_env(modo), agora=AGORA, produtos_por_item={0: 456}
    )

    assert d.avaliacao.itens[0].fire_produto_id == "456"
    assert d.avaliacao.itens[0].motivo == "unidade_divergente"
    if modo == "ligado":
        assert d.bloqueado is True
        assert d.reason == "preco_nao_avaliado"


def test_vinculo_com_o_mesmo_seq_do_check_avalia_com_a_unidade(monkeypatch):
    # check_order resolveu o item pelo degrau de-para: mesmo SEQ, unidade dele.
    _precos_por_produto({"456": 5000})
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(ids=[456]))

    d = preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA, produtos_por_item={0: 456}
    )

    assert d.avaliacao.itens[0].motivo == "preco_acima"
    assert d.reason == "preco_aguardando"


def test_vinculo_do_de_para_libera_pelo_preco_de_b(monkeypatch):
    _precos_por_produto({"123": 9999, "456": 5400})
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(ids=[456]))

    d = preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA, produtos_por_item={0: 456}
    )

    assert d.bloqueado is False
    assert d.avaliacao.itens[0].fire_produto_id == "456"


def test_vinculo_do_de_para_vale_no_check_salvo(monkeypatch):
    _precos_por_produto({"123": 5400, "456": 5000})
    order = _order((54.0,))
    import_id, entry = _seed(order, check=_check())
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA, produtos_por_item={0: 456}
    )

    assert d.bloqueado is True
    assert d.avaliacao.itens[0].fire_produto_id == "456"


@pytest.mark.parametrize("nome", list(CENARIOS_HOJE))
def test_desligado_ignora_o_vinculo(monkeypatch, nome):
    check = CENARIOS_HOJE[nome]
    order = _order((54.0,) * len(check["items"]))
    import_id, entry = _seed(order)
    _live(monkeypatch, check)

    sem = preco_trava.decidir_envio(import_id, order, entry, env=_env("desligado"), agora=AGORA)
    com = preco_trava.decidir_envio(
        import_id, order, entry, env=_env("desligado"), agora=AGORA, produtos_por_item={0: 456}
    )

    assert (com.bloqueado, com.reason, com.detalhe, com.block_detail) == (
        sem.bloqueado,
        sem.reason,
        sem.detalhe,
        sem.block_detail,
    )
    assert com.avaliacao is None


def test_preview_avalia_o_produto_do_vinculo():
    _precos_por_produto({"123": 5400, "456": 5000})
    order = _order((54.0,))
    import_id, entry = _seed(order, check=_check())

    preco = preco_trava.preview_preco(
        import_id, entry, order, env=_env("ligado"), agora=AGORA, produtos_por_item={0: 456}
    )

    # SEQ do vínculo (B) avaliado; a unidade do check é a de A → não avaliado.
    assert preco["itens"][0]["motivo"] == "unidade_divergente"
    assert preco["itens"][0]["estado"] == "nao_avaliado"


# ── Revisão final: Fire fora sem check salvo não mexe nas aprovações ─────────


def test_fire_fora_sem_check_salvo_preserva_a_decisao(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, resultado="acordo_comercial", preco=5400)
    antes = (_revisoes(import_id), _decisoes(import_id))

    _live(monkeypatch, _check(available=False, ids=[None]))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert (_revisoes(import_id), _decisoes(import_id)) == antes

    _live(monkeypatch, _check())
    assert not preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA
    ).bloqueado


def test_fire_fora_sem_check_nao_cria_revisao(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=None)
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert _revisoes(import_id) == []


# ── Fix round 1: empresa faturadora ─────────────────────────────────────────


@pytest.mark.parametrize("cnpj_env", [None, "", "123"])
def test_ligado_sem_cnpj_da_empresa_bloqueia_sem_pendencia(monkeypatch, cnpj_env):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    env = {**_env("ligado"), "cnpj": cnpj_env}

    d = preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert [(i.situacao, i.motivo) for i in d.avaliacao.itens] == [
        ("nao_avaliado", "empresa_sem_cnpj")
    ]
    assert "CNPJ da empresa não cadastrado no ambiente" in d.detalhe
    assert _revisoes(import_id) == []


def test_preco_de_outra_empresa_nao_libera(monkeypatch):
    with db.connect() as conn:
        _pagina_v1(
            conn,
            [(R1, "Rede X", CNPJ_REDE[:8])],
            [(R1, "123", 5400)],
            gerado=AGORA - timedelta(hours=1),
            empresa_preco="22222222000182",
        )
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.avaliacao.itens[0].preco_tabela_centavos is None


# ── Task 7.2: avaliador no contrato ──────────────────────────────────────────


def test_ligado_cliente_sem_rede_bloqueia_sem_fila(monkeypatch):
    _copia(5400)
    order = _order((54.0,), cnpj=OUTRO_CNPJ)
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == (
        "1 item(ns) sem avaliação de preço: cliente sem rede na tabela de preço do Flow."
    )
    assert _revisoes(import_id) == []


def test_ligado_unidade_divergente_bloqueia(monkeypatch):
    _copia(5400)  # tabela em KIT
    order = _order((54.0,))
    import_id, entry = _seed(order)
    check = _check()
    check["items"][0]["fire_unidade"] = "PACK"
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert {i.motivo for i in d.avaliacao.itens} == {"unidade_divergente"}
    assert _revisoes(import_id) == []


def test_riachuelo_sem_cnpj_casa_a_rede_pelo_menor_cnpj_das_lojas(monkeypatch):
    _copia(5400)
    order = _order((54.0, 54.0), cnpj=None)
    order.items[0].delivery_cnpj = "11.222.333/0009-99"
    order.items[1].delivery_cnpj = "11222333000144"
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(2, ids=[123, 123]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.avaliacao.cnpj_rede == "11222333000144"
    assert d.bloqueado is False


def test_data_de_referencia_vem_da_emissao_e_nao_do_agora(monkeypatch):
    vistas = []
    original = precos_repo.preco_vigente

    def _espia(conn, **kw):
        vistas.append(kw["data"])
        return original(conn, **kw)

    monkeypatch.setattr(precos_repo, "preco_vigente", _espia)
    _copia(5400)
    order = _order((54.0,))
    order.header.issue_date = "30/09/2026"
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert vistas == [date(2026, 9, 30)]


def test_sem_emissao_data_de_referencia_e_o_recebimento_em_sao_paulo(monkeypatch):
    vistas = []
    original = precos_repo.preco_vigente

    def _espia(conn, **kw):
        vistas.append(kw["data"])
        return original(conn, **kw)

    monkeypatch.setattr(precos_repo, "preco_vigente", _espia)
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    entry["imported_at"] = "2026-10-15T23:50:00"  # naive = hora de São Paulo
    _live(monkeypatch, _check())

    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert vistas == [date(2026, 10, 15)]


def test_cnpjs_grupo_normaliza_pelo_contrato(monkeypatch):
    monkeypatch.setattr(
        environments_repo,
        "list_all",
        lambda: [
            {"cnpj": "12.abc.345/01de-35", "intercompany_cnpj": None},
            {"cnpj": "11.111.111/0001-91", "intercompany_cnpj": "123"},
        ],
    )
    assert preco_trava.cnpjs_grupo() == {"12ABC345", "11111111"}


# ── Task 7.2, fix round 1 ────────────────────────────────────────────────────


def test_todos_preenche_unidade_erp_como_o_avaliador(monkeypatch):
    order = _order((54.0,))
    import_id, entry = _seed(order)
    env = {**_env("ligado"), "cnpj": None}  # empresa_sem_cnpj → _todos
    check = _check()
    check["items"][0]["fire_unidade"] = " kit "
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)

    assert d.avaliacao.itens[0].motivo == "empresa_sem_cnpj"
    assert d.avaliacao.itens[0].unidade_erp == "KIT"


def test_unidade_divergente_texto_cobre_unidade_nao_informada(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    check = _check()
    check["items"][0]["fire_unidade"] = None
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.detalhe == (
        "1 item(ns) sem avaliação de preço: unidade do produto no Fire não confere com a "
        "tabela de preço (diferente ou não informada)."
    )


def test_cnpjs_grupo_avisa_cnpj_fora_do_contrato_sem_o_valor(monkeypatch):
    from app.utils.logger import logger

    monkeypatch.setattr(
        environments_repo,
        "list_all",
        lambda: [{"slug": "nasmar", "cnpj": "11111111000191", "intercompany_cnpj": "99887766"}],
    )
    mensagens: list[str] = []
    sink = logger.add(lambda m: mensagens.append(str(m)), level="WARNING")
    try:
        assert preco_trava.cnpjs_grupo() == {"11111111"}
    finally:
        logger.remove(sink)

    assert len(mensagens) == 1
    assert "intercompany_cnpj" in mensagens[0] and "nasmar" in mensagens[0]
    assert "99887766" not in mensagens[0]


# ── Task 7.3, fix round 1 ────────────────────────────────────────────────────


def _tabela_passa_a_bater():
    _copia(5400)  # mesmo precoId, preço novo: o pedido de 54,00 fica ok


def test_sem_fila_e_vigente_nunca_tentada_retira_local_e_libera(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _tabela_passa_a_bater()

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {}
    with db.connect() as conn:
        assert revs.vigente(conn, import_id) is None
        assert revs.retiradas_pendentes(conn, 10) == []  # nada vai ao Flow


def test_sem_fila_e_vigente_ja_tentada_espera_a_decisao(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    with db.connect() as conn:
        assert revs.marcar_tentada(conn, import_id, 1, em=AGORA)
    _tabela_passa_a_bater()

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"


def test_pedido_que_vira_intercompany_retira_a_vigente(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    with db.connect() as conn:
        revs.marcar_enviada(conn, import_id, 1, {}, em=AGORA)
    _env_grupo("nasmar", intercompany=CNPJ_REDE)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.avaliacao.itens[0].situacao == "fora_intercompany"
    with db.connect() as conn:
        assert revs.vigente(conn, import_id) is None
        assert [p["revisao"] for p in revs.retiradas_pendentes(conn, 10)] == [1]


def test_descricao_cai_no_codigo(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    order.items[0].description = " "
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())  # check sem fire_description

    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert _corpo(import_id)["itens"][0]["descricao"] == "C0"


def test_numero_do_pedido_longo_bloqueia_com_texto_legivel(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    order.header.order_number = "9" * 65
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.detalhe == (
        "Pedido não pode ir ao Flow para aprovação de preço: "
        "número do pedido com mais de 64 caracteres."
    )
    assert _revisoes(import_id) == []


def test_quantidade_com_mais_de_duas_casas_bloqueia_local(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    order.items[0].quantity = 10.555
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.detalhe == (
        "Pedido não pode ir ao Flow para aprovação de preço: "
        "item 1 com quantidade de mais de 2 casas decimais (10.555)."
    )
    assert _revisoes(import_id) == []


def test_nome_longo_e_truncado_em_unidades_utf16(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    order.header.customer_name = "\U0001f600" * 200  # 400 unidades UTF-16
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.reason == "preco_aguardando"
    nome = _corpo(import_id)["cliente"]["nome"]
    assert len(nome.encode("utf-16-le")) // 2 <= 255
