"""Enriquecimento do XLS com a identidade do Fire via de-para (app/erp/depara_apply.py)."""

from __future__ import annotations

import pytest

from app.erp import depara_apply
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import produto_depara_repo, router


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _order(cnpj="11111111000100", name="X"):
    return Order(
        header=OrderHeader(order_number="T1", customer_cnpj=cnpj, customer_name=name),
        items=[
            OrderItem(description="A", product_code="REF-X", quantity=1),
            OrderItem(description="B", product_code="SEM-VINCULO", quantity=1),
        ],
    )


def test_apply_reescreve_item_vinculado(env_conn):
    produto_depara_repo.upsert(
        env_conn,
        client_key=produto_depara_repo.client_key("11111111000100", None),
        chave_tipo="codigo",
        chave_valor="REF-X",
        fire_produto_id="77",
        fire_codigo="77",
        fire_ean="7890",
        fire_nome="TENIS",
        criado_em="t",
        criado_por="grazi",
    )
    order = _order()
    changed = depara_apply.apply(order, conn=env_conn)

    assert order.items[0].product_code == "77"
    assert order.items[0].ean == "7890"
    assert "REF-X" in (order.items[0].obs or "")
    assert order.items[1].product_code == "SEM-VINCULO"  # intacto
    assert len(changed) == 1


def test_apply_sem_vinculo_nenhum_retorna_lista_vazia(env_conn):
    order = _order()
    changed = depara_apply.apply(order, conn=env_conn)
    assert changed == []
    assert order.items[0].product_code == "REF-X"
    assert order.items[1].product_code == "SEM-VINCULO"


def test_apply_riachuelo_sem_cnpj_usa_nome_como_chave(env_conn):
    """Caso dominante real: header sem CNPJ (Riachuelo — o CNPJ real é por
    loja, não aparece no header do pedido). A chave de lookup precisa cair
    pro nome normalizado, senão o de-para nunca resolve nada pro maior
    cliente do portal."""
    ckey = produto_depara_repo.client_key(None, "Lojas Riachuelo Sa")
    produto_depara_repo.upsert(
        env_conn,
        client_key=ckey,
        chave_tipo="codigo",
        chave_valor="15968243002",
        fire_produto_id="70",
        fire_codigo="70123",
        fire_ean=None,
        fire_nome="TENIS RIACHUELO",
        criado_em="t",
        criado_por="grazi",
    )
    order = Order(
        header=OrderHeader(
            order_number="T2", customer_cnpj=None, customer_name="Lojas Riachuelo Sa"
        ),
        items=[OrderItem(description="A", product_code="15968243002", quantity=1)],
    )
    changed = depara_apply.apply(order, conn=env_conn)

    assert order.items[0].product_code == "70123"
    assert order.items[0].ean is None  # fire_ean ausente: não sobrescreve
    assert "15968243002" in (order.items[0].obs or "")
    assert len(changed) == 1
