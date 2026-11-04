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
    _catalogo(env_conn, 77, "ALT77")
    order = _order()
    changed = depara_apply.apply(order, conn=env_conn)

    assert order.items[0].product_code == "ALT77"
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
    _catalogo(env_conn, 70, "70123")
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


def test_apply_devolve_o_seq_do_vinculo(env_conn):
    """O insert direto no Fire usa o SEQ do vínculo (BACKLOG §2.1)."""
    produto_depara_repo.upsert(
        env_conn,
        client_key=produto_depara_repo.client_key("11111111000100", None),
        chave_tipo="codigo",
        chave_valor="REF-X",
        fire_produto_id="77",
        fire_codigo="77",
        fire_ean=None,
        fire_nome="TENIS",
        criado_em="t",
        criado_por="grazi",
    )
    _catalogo(env_conn, 77, "ALT77")
    changed = depara_apply.apply(_order(), conn=env_conn)
    assert changed == [
        {"item_index": 0, "fire_produto_id": "77", "fire_codigo": "ALT77", "orig": "REF-X"}
    ]


def _catalogo(conn, seq, codprod_altern):
    conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, codprod_altern, extraido_em) "
        "VALUES (?, ?, 'P', ?, 't')",
        (str(seq), str(seq), codprod_altern),
    )


def _vincular(conn, seq, fire_codigo):
    produto_depara_repo.upsert(
        conn,
        client_key=produto_depara_repo.client_key("11111111000100", None),
        chave_tipo="codigo",
        chave_valor="REF-X",
        fire_produto_id=str(seq),
        fire_codigo=fire_codigo,
        fire_ean=None,
        fire_nome="CHINELO",
        criado_em="t",
        criado_por="grazi",
    )


def test_planilha_leva_o_codprod_altern_do_catalogo_nao_o_seq(env_conn):
    """v1 gravado com str(SEQ) fazia a planilha sair com o SEQ, que o
    importador do Fire casaria por prefixo de `CODPROD_ALTERN` (PR #126)."""
    _vincular(env_conn, 4058, "4058")
    _catalogo(env_conn, 4058, "16637410001_RIAC")
    order = _order()
    vinculos = depara_apply.resolver(order, conn=env_conn)
    assert vinculos[0]["fire_codigo"] == "16637410001_RIAC"
    assert not vinculos[0].get("sem_codigo_fire")
    assert vinculos[0]["fire_produto_id"] == "4058"  # o insert segue pelo SEQ
    depara_apply.aplicar(order, vinculos)
    assert order.items[0].product_code == "16637410001_RIAC"


def test_codigo_do_catalogo_vale_mais_que_o_gravado_no_vinculo(env_conn):
    _vincular(env_conn, 77, "CODIGO-ANTIGO")
    _catalogo(env_conn, 77, "CODIGO-NOVO")
    assert depara_apply.resolver(_order(), conn=env_conn)[0]["fire_codigo"] == "CODIGO-NOVO"


@pytest.mark.parametrize("catalogado", [False, True])
def test_sem_codigo_no_catalogo_marca_sem_codigo_fire(env_conn, catalogado):
    _vincular(env_conn, 4058, "4058")
    if catalogado:
        _catalogo(env_conn, 4058, "  ")
    vinculos = depara_apply.resolver(_order(), conn=env_conn)
    assert vinculos[0]["sem_codigo_fire"] is True
    assert vinculos[0]["fire_codigo"] != "4058"
    assert depara_apply.produtos_por_item(vinculos) == {0: 4058}
    assert depara_apply.pontuais_sem_codigo(vinculos) == vinculos


def test_recusa_da_planilha_pega_vinculo_v1_sem_codigo_e_o_insert_passa(env_conn, monkeypatch):
    """R21 estendido ao v1: planilha recusa com `sem_codigo_fire`; o insert
    direto (`planilha=False`) segue pelo SEQ."""
    from types import SimpleNamespace

    from app.persistence import repo
    from app.web import server

    _vincular(env_conn, 4058, "4058")
    vinculos = depara_apply.resolver(_order(), conn=env_conn)
    auditados: list = []
    monkeypatch.setattr(repo, "append_audit", lambda *a, **k: auditados.append(a))
    decisao = SimpleNamespace(check_vivo=None)

    recusa = server._recusa_planilha_pontual("imp-1", vinculos, decisao, "xlsx_export_blocked")
    assert recusa is not None and recusa[0] == "sem_codigo_fire"
    assert "4058" in recusa[1] and "linha 1" in recusa[1]
    assert auditados

    assert (
        server._recusa_planilha_pontual(
            "imp-1", vinculos, decisao, "send_to_fire_blocked", planilha=False
        )
        is None
    )
    assert depara_apply.produtos_por_item(vinculos) == {0: 4058}


def test_motivo_do_v1_e_o_da_pontual_sao_diferentes():
    v1 = {"fire_produto_id": 4058, "item_index": 2}
    pontual = {**v1, "fonte": "escolha_pontual"}
    assert depara_apply.motivo_pontual_sem_codigo(v1) == (
        "O produto 4058 vinculado à linha 3 está sem código no catálogo do portal, "
        "então a planilha não consegue apontá-lo. "
        "Refaça o vínculo dessa linha ou atualize o catálogo."
    )
    assert "escolhido para a linha 3" in depara_apply.motivo_pontual_sem_codigo(pontual)
    assert "envio direto" in depara_apply.motivo_pontual_sem_codigo(pontual)
