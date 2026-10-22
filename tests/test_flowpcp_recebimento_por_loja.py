"""Pedido sem CNPJ no header (Riachuelo, NBA): um /recebimento por loja, com o CNPJ da filial.

O Flow acha o cliente só pelo CNPJ e o Fire tem um pedido por loja. Todos os recebimentos levam o
MESMO externalId (= import_id); cada item leva `indiceOriginal` (posição no array combinado), e o
Flow grava source_id_externo = externalId:indiceOriginal, a chave que o vínculo do SP1 já usa.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.integrations.flowpcp import recebimento_por_loja as rpl
from app.integrations.flowpcp.mapper import build_recebimentos_por_loja
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo

LOJA_A = "33.200.056/0313-70"
LOJA_B = "33.200.056/0343-96"
LOJA_C = "33.200.056/0360-97"


def _item(i: int, cnpj: str | None, nome: str | None = None) -> OrderItem:
    return OrderItem(
        description=f"MEIA {i}",
        product_code=f"P{i}",
        quantity=12.0,
        unit_price=10.0,
        delivery_cnpj=cnpj,
        delivery_name=nome,
    )


def _order(cnpj_header: str | None = None, itens=None) -> Order:
    return Order(
        header=OrderHeader(
            order_number="6702667778",
            issue_date="02/10/2026",
            customer_name="LOJAS RIACHUELO SA",
            customer_cnpj=cnpj_header,
        ),
        items=itens
        if itens is not None
        else [
            _item(0, LOJA_A, "Loja A"),
            _item(1, LOJA_B, "Loja B"),
            _item(2, LOJA_A, "Loja A"),
            _item(3, LOJA_C, None),
        ],
    )


# ── divisão ───────────────────────────────────────────────────────────────────


def test_uma_requisicao_por_loja_na_ordem_da_primeira_aparicao():
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=_order(), tenant_id="t")
    assert [r.cliente.cnpj for r in reqs] == ["33200056031370", "33200056034396", "33200056036097"]
    assert {r.externalId for r in reqs} == {"imp-1"}
    assert {r.pedidoNumero for r in reqs} == {"6702667778"}


def test_indice_original_e_a_posicao_no_array_combinado():
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=_order(), tenant_id="t")
    assert [[i.indiceOriginal for i in r.itens] for r in reqs] == [[0, 2], [1], [3]]
    assert [[i.descricao for i in r.itens] for r in reqs][0] == ["MEIA 0", "MEIA 2"]


def test_nome_do_cliente_e_o_da_loja_quando_o_item_traz_senao_o_do_pedido():
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=_order(), tenant_id="t")
    assert [r.cliente.nome for r in reqs] == ["Loja A", "Loja B", "LOJAS RIACHUELO SA"]


def test_todo_item_aparece_exatamente_uma_vez():
    o = _order()
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=o, tenant_id="t")
    indices = sorted(i.indiceOriginal for r in reqs for i in r.itens)
    assert indices == list(range(len(o.items)))


def test_uma_loja_so_ainda_leva_o_cnpj_dela_e_o_indice():
    o = _order(itens=[_item(0, LOJA_A), _item(1, LOJA_A)])
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=o, tenant_id="t")
    assert len(reqs) == 1 and reqs[0].cliente.cnpj == "33200056031370"
    assert [i.indiceOriginal for i in reqs[0].itens] == [0, 1]


# ── quando NÃO divide ─────────────────────────────────────────────────────────


def test_com_cnpj_no_header_nao_divide():
    assert rpl.deve_dividir(_order(cnpj_header="12.345.678/0001-95")) is False


def test_item_sem_cnpj_de_loja_nao_divide():
    o = _order(itens=[_item(0, LOJA_A), _item(1, None)])
    assert rpl.deve_dividir(o) is False


def test_cnpj_de_loja_invalido_nao_divide():
    o = _order(itens=[_item(0, LOJA_A), _item(1, "A COMBINAR")])
    assert rpl.deve_dividir(o) is False


def test_sem_itens_nao_divide():
    assert rpl.deve_dividir(_order(itens=[])) is False


def test_riachuelo_sem_header_e_com_lojas_divide():
    assert rpl.deve_dividir(_order()) is True


# ── interruptor ───────────────────────────────────────────────────────────────


def test_interruptor_nasce_desligado(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", raising=False)
    assert rpl.ligado("mm") is False


def test_interruptor_por_slug(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm, Nasmar")
    assert rpl.ligado("mm") is True and rpl.ligado("nasmar") is True and rpl.ligado("x") is False


# ── fila de saída ─────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    e = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="x",
    )
    with env_context.active_env(e["id"], e["slug"]):
        repo.insert_import(
            {"id": "imp-1", "source_filename": "a.xlsx", "imported_at": "2026-10-02T09:00:00"}
        )
        yield e
    db.set_db_path(None)
    db.reset_init_cache()


def _exporter():
    from app.integrations.flowpcp.exporter import FlowPCPExporter

    return FlowPCPExporter(tenant_id="t")


def test_desligado_enfileira_uma_linha_como_sempre(env, monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", raising=False)
    assert _exporter().enqueue(_order(), import_id="imp-1", slug="mm") is True
    linhas = outbox_repo.list_for_import("imp-1")
    assert [linha.idempotency_key for linha in linhas] == ["send-imp-1"]
    assert linhas[0].payload["cliente"]["cnpj"] is None


def test_ligado_enfileira_uma_linha_por_loja(env, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    assert _exporter().enqueue(_order(), import_id="imp-1", slug="mm") is True
    linhas = sorted(outbox_repo.list_for_import("imp-1"), key=lambda linha: linha.idempotency_key)
    assert [linha.idempotency_key for linha in linhas] == [
        "send-imp-1-33200056031370",
        "send-imp-1-33200056034396",
        "send-imp-1-33200056036097",
    ]
    assert {linha.payload["externalId"] for linha in linhas} == {"imp-1"}


def test_ligado_enfileirar_de_novo_nao_duplica(env, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")
    assert len(outbox_repo.list_for_import("imp-1")) == 3


def test_ligado_mas_com_cnpj_no_header_segue_uma_linha(env, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    _exporter().enqueue(_order(cnpj_header="12.345.678/0001-95"), import_id="imp-1", slug="mm")
    assert [linha.idempotency_key for linha in outbox_repo.list_for_import("imp-1")] == [
        "send-imp-1"
    ]


def test_pedido_normal_nao_leva_indice_original_no_wire():
    """O Flow aceita o campo ausente, não `null`: fora da divisão a chave nem aparece."""
    from app.integrations.flowpcp.mapper import build_recebimento_payload

    o = _order(cnpj_header="12.345.678/0001-95")
    corpo = build_recebimento_payload(import_id="imp-1", order=o, tenant_id="t").model_dump(
        by_alias=True
    )
    assert all("indiceOriginal" not in item for item in corpo["itens"])


def test_indice_zero_vai_no_wire():
    """0 é posição válida: omitir só quando for None."""
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=_order(), tenant_id="t")
    corpo = reqs[0].model_dump(by_alias=True)
    assert corpo["itens"][0]["indiceOriginal"] == 0


def test_fornecedor_nao_vira_o_nome_da_loja():
    reqs = build_recebimentos_por_loja(import_id="imp-1", order=_order(), tenant_id="t")
    assert {r.fornecedor for r in reqs} == {"LOJAS RIACHUELO SA"}
