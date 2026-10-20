"""Tests for app.erp.product_check — match e price_status."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.erp import product_check, queries
from app.models.order import Order, OrderHeader, OrderItem


def _order(items_kwargs: list[dict], *, customer_cnpj: str = "00000000000100") -> Order:
    return Order(
        header=OrderHeader(order_number="T1", customer_cnpj=customer_cnpj, customer_name="ACME"),
        items=[OrderItem(quantity=1.0, **kw) for kw in items_kwargs],
    )


def _make_fb_ctx_batched(*, client_row=None, ean_rows=None, code_rows=None, seq_rows=None):
    """Cursor fake para o check batelado.

    ean_rows/code_rows/seq_rows: listas de tuplas já no formato SELECT
    (key primeiro). client_row: tupla (CODIGO, RAZAO) ou None.
    Conta execute() em cur.execute.call_count. Linha sem a UNIDADE (última
    coluna) ganha "PAR".
    """
    cur = MagicMock()
    next_fetchall = []
    ean_rows = [r if len(r) == 5 else (*r, "PAR") for r in ean_rows or []]
    code_rows = [r if len(r) == 5 else (*r, "PAR") for r in code_rows or []]
    seq_rows = [r if len(r) == 4 else (*r, "PAR") for r in seq_rows or []]

    def execute_side_effect(sql, params=None):
        nonlocal next_fetchall
        if "FROM CADASTRO" in sql:
            next_fetchall = []  # cliente usa fetchone
        elif "BLOQUEADO" in sql:
            next_fetchall = []  # busca de candidatos (chave exata): ver _make_fb_ctx_chave
        elif "CODIGO_EAN13" in sql and " IN " in sql:
            next_fetchall = list(ean_rows or [])
        elif "CODPROD_ALTERN" in sql and " IN " in sql:
            next_fetchall = list(code_rows or [])
        elif "SEQ IN" in sql:
            next_fetchall = list(seq_rows or [])
        else:
            next_fetchall = []

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.side_effect = lambda: client_row
    cur.fetchall.side_effect = lambda: next_fetchall

    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


@patch("app.erp.product_check.FirebirdConnection")
def test_check_order_batches_queries(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(
        client_row=(1, "ACME"),
        ean_rows=[("7891", 10, "TENIS A", 89.90), ("7892", 11, "TENIS B", 50.0)],
        code_rows=[("ABC", 12, "TENIS C", 30.0)],
    )
    mock_fb.return_value.connect.return_value = ctx

    order = _order(
        [
            {"ean": "7891", "unit_price": 89.90},
            {"ean": "7892", "unit_price": 50.0},
            {"product_code": "ABC", "unit_price": 30.0},
            {"ean": "9999", "unit_price": 1.0},  # sem match
        ]
    )
    report = product_check.check_order(order)

    # 1 cliente + 1 eans + 1 codes + 1 previsão, e a passada da chave exata
    # (1 eans + 1 codes): batelado, nunca 1 query por item.
    assert cur.execute.call_count <= 6
    assert report["summary"]["items_matched"] == 3
    assert report["items"][0]["match_source"] == "ean"
    assert report["items"][2]["match_source"] == "codprod_altern"
    assert report["items"][3]["match"] is False


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_match_exact(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        client_row=(1, "ACME"),
        ean_rows=[("7891", 10, "TENIS", 89.90)],
    )
    mock_fb.return_value.connect.return_value = ctx

    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)

    item = report["items"][0]
    assert item["price_status"] == "match"
    assert item["unit_price_order"] == 89.90
    assert item["fire_preco_venda"] == 89.90
    assert item["price_diff"] == 0.0


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_mismatch_one_cent(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[("7891", 10, "TENIS", 89.91)],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["price_status"] == "mismatch"
    assert item["price_diff"] == 0.01


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_mismatch_round_value(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[("7891", 10, "TENIS", 100.00)],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "7891", "unit_price": 99.00}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "mismatch"
    assert report["items"][0]["price_diff"] == 1.0


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_price_in_fire_null(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[("7891", 10, "TENIS", None)],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_price_in_fire"
    assert report["items"][0]["fire_preco_venda"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_price_in_fire_zero(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[("7891", 10, "TENIS", 0.0)],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_price_in_fire"


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_order_price(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[("7891", 10, "TENIS", 50.0)],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "7891", "unit_price": None}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_order_price"


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_product_match(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[],  # nada no Fire
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["match"] is False
    assert item["price_status"] == "no_product_match"
    assert item["price_diff"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_summary_aggregates_price_counts(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[
            ("A", 1, "X", 10.0),  # match
            ("B", 2, "Y", 12.0),  # mismatch
            ("C", 3, "Z", None),  # no_price_in_fire
            ("D", 4, "W", 50.0),  # no_order_price
            # E não cadastrado → no_product_match
        ],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order(
        [
            {"ean": "A", "unit_price": 10.0},
            {"ean": "B", "unit_price": 11.0},
            {"ean": "C", "unit_price": 30.0},
            {"ean": "D", "unit_price": None},
            {"ean": "E", "unit_price": 5.0},
        ]
    )
    summary = product_check.check_order(order)["summary"]["price_summary"]
    assert summary == {
        "items_match": 1,
        "items_mismatch": 1,
        "items_no_price_in_fire": 1,
        "items_no_order_price": 1,
    }


# ---------------------------------------------------------------------------
# Fix A — TRIM simétrico + first-wins determinístico no match EAN batelado
# ---------------------------------------------------------------------------


def test_queries_batch_trazem_unidade_na_ultima_coluna():
    for sql in (
        queries.find_products_by_eans_sql(2),
        queries.find_products_by_codes_sql(2),
    ):
        assert "PRECO_VENDA, UNIDADE FROM PRODUTOS" in sql
    assert "SELECT SEQ, DESCRICAO, PRECO_VENDA, UNIDADE FROM" in queries.find_products_by_seqs_sql(
        2
    )


@patch("app.erp.product_check.FirebirdConnection")
def test_check_order_preenche_fire_unidade(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, _cur = _make_fb_ctx_batched(
        client_row=(1, "ACME"),
        ean_rows=[("7891", 10, "KIT A", 45.9, "KIT  ")],
        code_rows=[("ABC", 12, "MEIA", 3.0, "PAR")],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order(
        [
            {"ean": "7891", "unit_price": 45.9},
            {"product_code": "ABC", "unit_price": 3.0},
            {"ean": "9999", "unit_price": 1.0},
        ]
    )
    report = product_check.check_order(order)
    assert [it["fire_unidade"] for it in report["items"]] == ["KIT", "PAR", None]


def test_find_products_by_eans_sql_trims_and_orders():
    sql = queries.find_products_by_eans_sql(2)
    assert "TRIM(CODIGO_EAN13)" in sql
    assert "ORDER BY SEQ" in sql


@patch("app.erp.product_check.FirebirdConnection")
def test_ean_match_first_wins_on_duplicate_key(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    # Mesma chave EAN aparece 2x no catálogo (duplicata real de produção) — a
    # query traz ORDER BY SEQ, então o primeiro resultado (SEQ 10) deve vencer.
    ctx, _cur = _make_fb_ctx_batched(
        ean_rows=[("789", 10, "A", 1.0), ("789", 20, "B", 2.0)],
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "789", "unit_price": 1.0}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["fire_product_id"] == 10
    assert item["fire_description"] == "A"


# ---------------------------------------------------------------------------
# Task 5 — 3º degrau de match: de-para por cliente → resolve SEQ no Fire
# ---------------------------------------------------------------------------


@patch("app.erp.product_check.produto_depara_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_order_terceiro_degrau_depara(mock_db, mock_fb, mock_depara):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(
        client_row=(1, "ACME"),
        ean_rows=[],
        code_rows=[],
        seq_rows=[(77, "TENIS DEPARA", 120.0)],  # resolve SEQ do vínculo
    )
    mock_fb.return_value.connect.return_value = ctx
    # db.connect() não é mockado pelo brief — o degrau de-para chama
    # `with db.connect() as sconn:`, então patchamos `product_check.db`
    # diretamente para devolver um context manager dummy (o conn nunca é
    # usado de verdade porque produto_depara_repo.lookup também está
    # mockado e ignora o argumento).
    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    # de-para: código "REF-X" do cliente → SEQ 77
    mock_depara.lookup.return_value = {
        ("codigo", "REF-X"): {"fire_produto_id": "77", "fire_codigo": "77"},
    }

    order = _order([{"product_code": "REF-X", "unit_price": 120.0}])
    report = product_check.check_order(order)

    item = report["items"][0]
    assert item["match"] is True
    assert item["match_source"] == "depara"
    assert item["fire_product_id"] == 77
    assert item["price_status"] == "match"
    assert item["fire_unidade"] == "PAR"


@patch("app.erp.product_check.produto_depara_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_depara_orfao_vira_sem_match(mock_db, mock_fb, mock_depara):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(client_row=(1, "ACME"), seq_rows=[])  # SEQ sumiu
    mock_fb.return_value.connect.return_value = ctx
    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_depara.lookup.return_value = {
        ("codigo", "REF-X"): {"fire_produto_id": "999", "fire_codigo": "999"},
    }
    order = _order([{"product_code": "REF-X", "unit_price": 1.0}])
    report = product_check.check_order(order)
    assert report["items"][0]["match"] is False


# ---------------------------------------------------------------------------
# Task 4 — is_blocking() helper
# ---------------------------------------------------------------------------


def _check_with(items: list[dict]) -> dict:
    return {"available": True, "items": items, "summary": {}}


def test_is_blocking_passes_match_only():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "match"}])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is False
    assert detail["items_mismatch"] == []
    assert detail["items_no_order_price"] == []
    assert detail["items_no_price_unacked"] == []


def test_is_blocking_blocks_on_mismatch():
    check = _check_with(
        [
            {
                "ean": "A",
                "product_code": "p1",
                "price_status": "mismatch",
                "unit_price_order": 11.0,
                "fire_preco_venda": 10.0,
            },
        ]
    )
    blocked, detail = product_check.is_blocking(check)
    assert blocked is True
    assert detail["items_mismatch"] == [
        {"ean": "A", "product_code": "p1", "order_price": 11.0, "fire_price": 10.0},
    ]


def test_is_blocking_blocks_on_no_order_price():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_order_price"}])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is True
    assert detail["items_no_order_price"] == [{"ean": "A", "product_code": "p1"}]


def test_is_blocking_blocks_on_no_price_unacked():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, detail = product_check.is_blocking(check, ack_items=None)
    assert blocked is True
    assert detail["items_no_price_unacked"] == [{"ean": "A", "product_code": "p1"}]


def test_is_blocking_passes_with_ack_by_ean():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, _ = product_check.is_blocking(check, ack_items=[{"ean": "A", "product_code": None}])
    assert blocked is False


def test_is_blocking_passes_with_ack_by_code():
    check = _check_with([{"ean": None, "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, _ = product_check.is_blocking(check, ack_items=[{"ean": None, "product_code": "p1"}])
    assert blocked is False


def test_is_blocking_partial_ack_still_blocks():
    check = _check_with(
        [
            {"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"},
            {"ean": "B", "product_code": "p2", "price_status": "no_price_in_fire"},
        ]
    )
    blocked, detail = product_check.is_blocking(
        check,
        ack_items=[{"ean": "A", "product_code": "p1"}],
    )
    assert blocked is True
    assert detail["items_no_price_unacked"] == [{"ean": "B", "product_code": "p2"}]


def test_is_blocking_ignores_no_product_match():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_product_match"}])
    blocked, _ = product_check.is_blocking(check)
    assert blocked is False  # comportamento atual mantido — sem match não bloqueia aqui


def test_is_blocking_returns_false_when_check_unavailable():
    blocked, _ = product_check.is_blocking({"available": False, "items": []})
    assert blocked is False  # check off → segue (best-effort)


# ---------------------------------------------------------------------------
# BACKLOG 2.15 — palpite do importador de Excel do Fire (casa por PREFIXO)
# ---------------------------------------------------------------------------
#
# O importador do Fire (`MECANICO=99`) fica com o primeiro produto da ordem
# física cujo CODPROD_ALTERN COMEÇA com o código da planilha. O fake abaixo
# guarda o catálogo em ordem física e responde cada query como o Firebird.


def _fire_fisico(produtos, *, client_row=(1, "ACME"), falha_previsao=False):
    """produtos: [(seq, codprod_altern, descricao, preco)] NA ORDEM FÍSICA."""
    cur = MagicMock()
    estado = {"rows": [], "previsoes": []}
    por_seq = sorted(produtos)

    def execute(sql, params=()):
        if "FROM CADASTRO" in sql:
            estado["rows"] = []
        elif "STARTING WITH" in sql:
            if falha_previsao:
                raise RuntimeError("conexão caiu")
            assert all(len(p) <= 30 for p in params), "Firebird: string right truncation"
            estado["previsoes"].append(list(params))
            estado["rows"] = [
                (cod, seq, desc)
                for seq, cod, desc, _ in produtos
                if any(cod.startswith(p) for p in params)
            ]
        elif "CODPROD_ALTERN" in sql and " IN " in sql:
            estado["rows"] = [(c, s, d, p, "PAR") for s, c, d, p in por_seq if c.strip() in params]
        elif "SEQ IN" in sql:
            estado["rows"] = [(s, d, p, "PAR") for s, c, d, p in por_seq if s in params]
        else:
            estado["rows"] = []

    cur.execute.side_effect = execute
    cur.fetchone.side_effect = lambda: client_row
    cur.fetchall.side_effect = lambda: estado["rows"]
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, estado


def _check_fisico(mock_fb, produtos, codigos, **kw):
    mock_fb.return_value.is_configured.return_value = True
    ctx, estado = _fire_fisico(produtos, **kw)
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"product_code": c, "unit_price": 1.0} for c in codigos])
    return product_check.check_order(order), estado


@patch("app.erp.product_check.FirebirdConnection")
def test_troca_no_fire_quando_o_prefixo_vem_antes_na_ordem_fisica(mock_fb):
    """Pedido 4939: `NB01-3G` entrou no Fire como `NB01-3GG` (SEQ 3103)."""
    produtos = [(3103, "NB01-3GG", "KIT NB01 GG", 1.0), (3102, "NB01-3G", "KIT NB01 G", 1.0)]
    report, _ = _check_fisico(mock_fb, produtos, ["NB01-3G"])
    item = report["items"][0]
    assert item["fire_product_id"] == 3102  # o portal casa o exato
    assert item["troca_no_fire"] == {
        "fire_product_id": 3103,
        "codigo": "NB01-3GG",
        "descricao": "KIT NB01 GG",
        "conferido": True,
    }
    assert report["summary"]["items_troca_no_fire"] == 1


@patch("app.erp.product_check.FirebirdConnection")
def test_sem_troca_quando_o_exato_vem_primeiro(mock_fb):
    produtos = [(3102, "NB01-3G", "KIT NB01 G", 1.0), (3103, "NB01-3GG", "KIT NB01 GG", 1.0)]
    report, _ = _check_fisico(mock_fb, produtos, ["NB01-3G"])
    assert report["items"][0]["troca_no_fire"] is None
    assert report["summary"]["items_troca_no_fire"] == 0


@patch("app.erp.product_check.FirebirdConnection")
def test_item_sem_match_que_o_fire_importaria_calado_como_outro(mock_fb):
    """Pedido 4932: `NB01` não existe, e o Fire importou como o 1º `NB01-*`."""
    produtos = [(3095, "NB01-1M", "KIT NB01 1M", 1.0), (3096, "NB01-1G", "KIT NB01 1G", 1.0)]
    report, _ = _check_fisico(mock_fb, produtos, ["NB01"])
    item = report["items"][0]
    assert item["match"] is False
    assert item["troca_no_fire"]["codigo"] == "NB01-1M"


@patch("app.erp.product_check.FirebirdConnection")
def test_item_sem_match_e_sem_prefixo_nao_avisa(mock_fb):
    report, _ = _check_fisico(mock_fb, [(1, "KG07BR", "KIT", 1.0)], ["KG07 004"])
    assert report["items"][0]["troca_no_fire"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_codigo_duplicado_no_fire_tambem_troca(mock_fb):
    """Medido na Fire viva: dois produtos `5041G`. O portal fica com o menor SEQ
    (ORDER BY SEQ); o Fire, com o primeiro da ordem física."""
    produtos = [(20, "5041G", "MEIA NOVA", 1.0), (10, "5041G", "MEIA ANTIGA", 1.0)]
    report, _ = _check_fisico(mock_fb, produtos, ["5041G"])
    item = report["items"][0]
    assert item["fire_product_id"] == 10
    assert item["troca_no_fire"]["fire_product_id"] == 20
    assert item["troca_no_fire"]["codigo"] == "5041G"


@patch("app.erp.product_check.produto_depara_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_item_vinculado_consulta_o_codigo_que_vai_no_xlsx(mock_db, mock_fb, mock_depara):
    """O `depara_apply` troca o código do cliente pelo `fire_codigo` no export.
    É esse que o Fire lê, então é esse que a previsão consulta."""
    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_depara.lookup.return_value = {
        ("codigo", "REF-X"): {"fire_produto_id": "77", "fire_codigo": "77"},
    }
    produtos = [(5, "77A", "OUTRO", 1.0), (77, "KIT77", "KIT CERTO", 1.0)]
    report, estado = _check_fisico(mock_fb, produtos, ["REF-X"])
    item = report["items"][0]
    assert item["match_source"] == "depara"
    assert estado["previsoes"] == [["77"]]
    assert item["troca_no_fire"]["fire_product_id"] == 5
    # Não se sabe se o Fire tenta o SEQ antes do prefixo: pede conferência.
    assert item["troca_no_fire"]["conferido"] is False


@patch("app.erp.product_check.FirebirdConnection")
def test_previsao_em_lotes_de_150(mock_fb):
    codigos = [f"C{i:03d}" for i in range(200)]
    produtos = [(i, f"C{i:03d}X", f"P{i}", 1.0) for i in range(200)]
    report, estado = _check_fisico(mock_fb, produtos, codigos)
    assert [len(lote) for lote in estado["previsoes"]] == [150, 50]
    assert report["summary"]["items_troca_no_fire"] == 200
    assert report["items"][199]["troca_no_fire"]["codigo"] == "C199X"


@patch("app.erp.product_check.FirebirdConnection")
def test_codigo_maior_que_a_coluna_nao_vai_pra_previsao(mock_fb):
    """CODPROD_ALTERN é VARCHAR(30): parâmetro maior estoura truncamento no
    Firebird. Código assim não é prefixo de nada — fica fora da query."""
    longo = "X" * 31
    report, estado = _check_fisico(mock_fb, [(1, "NB01-3G", "KIT", 1.0)], [longo, "NB01"])
    assert estado["previsoes"] == [["NB01"]]
    assert report["summary"]["troca_no_fire_checked"] is True


@patch("app.erp.product_check.FirebirdConnection")
def test_falha_na_previsao_nao_derruba_o_check(mock_fb):
    produtos = [(3102, "NB01-3G", "KIT NB01 G", 1.0)]
    report, _ = _check_fisico(mock_fb, produtos, ["NB01-3G"], falha_previsao=True)
    assert report["available"] is True
    assert report["items"][0]["fire_product_id"] == 3102
    assert report["items"][0]["troca_no_fire"] is None
    assert report["summary"]["troca_no_fire_checked"] is False


@patch("app.erp.product_check.FirebirdConnection")
def test_codigos_que_sao_prefixo_um_do_outro_no_mesmo_lote(mock_fb):
    produtos = [(3103, "NB01-3GG", "GG", 1.0), (3102, "NB01-3G", "G", 1.0)]
    report, estado = _check_fisico(mock_fb, produtos, ["NB01", "NB01-3G", "NB01-3G"])
    assert len(estado["previsoes"]) == 1
    assert [i["troca_no_fire"]["fire_product_id"] for i in report["items"]] == [3103] * 3
    assert report["summary"]["items_troca_no_fire"] == 3


@patch("app.erp.product_check.FirebirdConnection")
def test_item_casado_por_ean_nao_afirma_troca(mock_fb):
    """O XLSX leva a coluna EAN; se o Fire usa o EAN antes, não há troca."""
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(ean_rows=[("789", 100, "CERTO", 1.0)])
    execute = cur.execute.side_effect
    fetchall = cur.fetchall.side_effect
    linhas = {}

    def execute_com_previsao(sql, params=()):
        linhas["previsao"] = "STARTING WITH" in sql
        return execute(sql, params)

    cur.execute.side_effect = execute_com_previsao
    cur.fetchall.side_effect = lambda: (
        [("C1-X", 200, "OUTRO")] if linhas.get("previsao") else fetchall()
    )
    mock_fb.return_value.connect.return_value = ctx
    order = _order([{"ean": "789", "product_code": "C1", "unit_price": 1.0}])
    item = product_check.check_order(order)["items"][0]
    assert item["match_source"] == "ean"
    assert item["troca_no_fire"]["fire_product_id"] == 200
    assert item["troca_no_fire"]["conferido"] is False


def test_check_indisponivel_traz_as_chaves_novas():
    with patch("app.erp.product_check.FirebirdConnection") as mock_fb:
        mock_fb.return_value.is_configured.return_value = False
        report = product_check.check_order(_order([{"product_code": "X"}]))
    assert report["items"][0]["troca_no_fire"] is None
    assert report["summary"]["items_troca_no_fire"] == 0
    assert report["summary"]["troca_no_fire_checked"] is False


def test_troca_no_fire_nao_bloqueia_a_exportacao():
    """O XLSX está certo; o erro acontece dentro do Fire. Avisa, não trava."""
    check = _check_with(
        [
            {
                "product_code": "NB01-3G",
                "price_status": "match",
                "troca_no_fire": {"fire_product_id": 3103, "codigo": "NB01-3GG", "descricao": "X"},
            }
        ]
    )
    blocked, _ = product_check.is_blocking(check)
    assert blocked is False


# ---------------------------------------------------------------------------
# Chave exata (observada): item["produto"] e summary["items_produto_pendente"]
# ---------------------------------------------------------------------------


def _make_fb_ctx_chave(*, ean_rows=None, code_rows=None, seq_rows=None, client_row=None):
    """Cursor fake roteado pelos builders da chave exata.

    Linhas de 7 colunas: (chave, SEQ, DESCRICAO, PRECO_VENDA, UNIDADE, BLOQUEADO,
    CODPROD_ALTERN). As queries antigas recebem as mesmas linhas cortadas no
    formato delas, então os campos legados saem do mesmo catálogo fake.
    """
    ean_rows = list(ean_rows or [])
    code_rows = list(code_rows or [])
    seq_rows = list(seq_rows or [])
    cur = MagicMock()
    state = {"rows": []}

    def execute_side_effect(sql, params=None):
        if "FROM CADASTRO" in sql:
            rows = []
        elif "BLOQUEADO" in sql:
            if "TRIM(CODIGO_EAN13) IN" in sql:
                rows = ean_rows
            elif "TRIM(CODPROD_ALTERN) IN" in sql:
                rows = code_rows
            elif "WHERE SEQ IN" in sql:
                rows = seq_rows
            else:
                raise AssertionError(f"query de candidato inesperada: {sql}")
        elif "TRIM(CODIGO_EAN13) IN" in sql:
            rows = [r[:5] for r in ean_rows]
        elif "TRIM(CODPROD_ALTERN) IN" in sql:
            rows = [r[:5] for r in code_rows]
        elif "WHERE SEQ IN" in sql:
            rows = [r[1:5] for r in seq_rows]
        else:
            rows = []
        state["rows"] = list(rows)

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.side_effect = lambda: client_row
    cur.fetchall.side_effect = lambda: state["rows"]
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


def _check(mock_fb, order, **rows):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_chave(**rows)
    mock_fb.return_value.connect.return_value = ctx
    return product_check.check_order(order), cur


def test_builders_da_chave_trazem_bloqueado_e_codprod_altern():
    for builder, onde in (
        (queries.find_produtos_chave_por_ean_sql, "TRIM(CODIGO_EAN13) IN (?, ?)"),
        (queries.find_produtos_chave_por_codigo_sql, "TRIM(CODPROD_ALTERN) IN (?, ?)"),
        (queries.find_produtos_chave_por_seq_sql, "WHERE SEQ IN (?, ?)"),
    ):
        sql = builder(2)
        assert "SEQ, DESCRICAO, PRECO_VENDA, UNIDADE, BLOQUEADO, CODPROD_ALTERN" in sql
        assert onde in sql
        assert sql.rstrip().endswith("ORDER BY SEQ")


@patch("app.erp.product_check.FirebirdConnection")
def test_check_grava_produto_ok_por_codprod_altern(mock_fb):
    report, _ = _check(
        mock_fb,
        _order([{"product_code": " kg02pr ", "unit_price": 1.0}]),
        code_rows=[("KG02PR", 42, "KIT GRAZI", 1.0, "KIT", "Nao", "KG02PR")],
    )
    produto = report["items"][0]["produto"]
    assert produto == {
        "estado": "ok",
        "fire_seq": 42,
        "fonte": "codprod_altern",
        "motivo": "",
        "candidatos": [
            {"seq": 42, "descricao": "KIT GRAZI", "codprod_altern": "KG02PR", "bloqueado": False}
        ],
    }
    assert report["summary"]["items_produto_pendente"] == 0
    assert report["produto_conferencia_incompleta"] is False
    assert report["summary"]["produto_conferencia_incompleta"] is False


@patch("app.erp.product_check.FirebirdConnection")
def test_check_ean_duplicado_nao_escolhe_menor_seq(mock_fb):
    report, _ = _check(
        mock_fb,
        _order([{"ean": "789", "unit_price": 1.0}]),
        ean_rows=[
            ("789", 10, "A", 1.0, "PAR", "Nao", "A1"),
            ("789", 20, "B", 2.0, "PAR", None, "B1"),
        ],
    )
    item = report["items"][0]
    assert item["match_source"] == "ean"  # legado inalterado
    assert item["fire_product_id"] == 10
    assert item["produto"]["estado"] == "ambiguo"
    assert item["produto"]["fire_seq"] is None
    assert item["produto"]["candidatos"] == [
        {"seq": 10, "descricao": "A", "codprod_altern": "A1", "bloqueado": False},
        {"seq": 20, "descricao": "B", "codprod_altern": "B1", "bloqueado": False},
    ]
    assert report["summary"]["items_produto_pendente"] == 1


@patch("app.erp.product_check.FirebirdConnection")
def test_check_ean14_com_zero_nao_casa_ean13(mock_fb):
    report, cur = _check(
        mock_fb,
        _order([{"ean": "07891234567890", "unit_price": 1.0}]),
        ean_rows=[("7891234567890", 10, "A", 1.0, "PAR", "Nao", "A1")],
    )
    assert report["items"][0]["produto"]["estado"] == "a_identificar"
    params = [c.args[1] for c in cur.execute.call_args_list if "BLOQUEADO" in c.args[0]]
    assert ("07891234567890",) in params


@patch("app.erp.product_check.FirebirdConnection")
def test_check_codigo_com_espaco_interno_nao_casa(mock_fb):
    report, cur = _check(
        mock_fb,
        _order([{"product_code": "KG 02PR", "unit_price": 1.0}]),
        code_rows=[("KG02PR", 42, "KIT", 1.0, "KIT", "Nao", "KG02PR")],
    )
    produto = report["items"][0]["produto"]
    assert produto["estado"] == "a_identificar"
    assert produto["motivo"] == (
        "O cliente mandou o código KG 02PR, que não está cadastrado no Fire."
    )
    params = [c.args[1] for c in cur.execute.call_args_list if "BLOQUEADO" in c.args[0]]
    assert ("KG 02PR",) in params


@patch("app.erp.product_check.FirebirdConnection")
def test_check_produto_bloqueado(mock_fb):
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "ABC", "unit_price": 1.0}]),
        code_rows=[("ABC", 7, "VELHO", 1.0, "PAR", "Sim", "ABC")],
    )
    produto = report["items"][0]["produto"]
    assert produto["estado"] == "produto_bloqueado"
    assert produto["candidatos"][0]["bloqueado"] is True


def test_check_indisponivel_marca_fire_indisponivel():
    with patch("app.erp.product_check.FirebirdConnection") as mock_fb:
        mock_fb.return_value.is_configured.return_value = False
        report = product_check.check_order(_order([{"product_code": "X"}, {"ean": "1"}]))
    assert report["available"] is False
    for item in report["items"]:
        assert item["produto"] == {
            "estado": "fire_indisponivel",
            "fire_seq": None,
            "fonte": None,
            "motivo": product_check.MOTIVO_FIRE_INDISPONIVEL,
            "candidatos": [],
        }
        assert item["match"] is False  # legado inalterado
    assert report["summary"]["items_produto_pendente"] == 2


@patch("app.erp.product_check.FirebirdConnection")
def test_check_falha_na_busca_da_chave_nao_derruba_o_legado(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(ean_rows=[("789", 10, "A", 1.0)])
    original = cur.execute.side_effect

    def explode(sql, params=None):
        if "BLOQUEADO" in sql:
            raise RuntimeError("Fire caiu no meio")
        return original(sql, params)

    cur.execute.side_effect = explode
    mock_fb.return_value.connect.return_value = ctx
    report = product_check.check_order(_order([{"ean": "789", "unit_price": 1.0}]))
    item = report["items"][0]
    assert report["available"] is True
    assert item["match_source"] == "ean"
    assert item["produto"]["estado"] == "fire_indisponivel"
    assert report["summary"]["items_produto_pendente"] == 1


@patch("app.persistence.precos_repo.resolver_rede")
@patch("app.erp.product_check.produto_vinculo_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_usa_vinculo_v2_por_escopo(mock_db, mock_fb, mock_vinc, mock_rede):
    from types import SimpleNamespace

    from app.persistence.produto_vinculo_repo import norm_chave

    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_rede.return_value = SimpleNamespace(id="R7")
    mock_vinc.norm_chave.side_effect = norm_chave
    mock_vinc.lookup.return_value = {
        ("ean", "789"): SimpleNamespace(fire_seq=20),
    }
    report, _ = _check(
        mock_fb,
        _order([{"ean": "789", "unit_price": 1.0}], customer_cnpj="12.345.678/0001-95"),
        ean_rows=[
            ("789", 10, "A", 1.0, "PAR", "Nao", "A1"),
            ("789", 20, "B", 2.0, "PAR", "Nao", "B1"),
        ],
        seq_rows=[(20, 20, "B", 2.0, "PAR", "Nao", "B1")],
    )
    args, kwargs = mock_vinc.lookup.call_args
    assert args[1] == ["rede:R7", "raiz:12345678"]
    assert kwargs == {"codigos": [], "eans": ["789"]}
    produto = report["items"][0]["produto"]
    assert (produto["estado"], produto["fire_seq"], produto["fonte"]) == ("ok", 20, "depara")


@patch("app.persistence.precos_repo.resolver_rede")
@patch("app.erp.product_check.produto_vinculo_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_vinculo_v2_para_seq_sumido_invalido(mock_db, mock_fb, mock_vinc, mock_rede):
    from types import SimpleNamespace

    from app.persistence.produto_vinculo_repo import norm_chave

    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_rede.return_value = None
    mock_vinc.norm_chave.side_effect = norm_chave
    mock_vinc.lookup.return_value = {("codigo", "REF-X"): SimpleNamespace(fire_seq=999)}
    report, _ = _check(mock_fb, _order([{"product_code": "ref-x", "unit_price": 1.0}]))
    args, kwargs = mock_vinc.lookup.call_args
    assert args[1] == ["raiz:00000000"]
    assert kwargs == {"codigos": ["REF-X"], "eans": []}
    assert report["items"][0]["produto"]["estado"] == "vinculo_invalido"


@patch("app.erp.product_check.FirebirdConnection")
def test_check_sem_ambiente_ativo_segue_sem_vinculo(mock_fb):
    """db.connect() sem ambiente levanta: o vínculo some, o check não cai."""
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "ABC", "unit_price": 1.0}]),
        code_rows=[("ABC", 5, "X", 1.0, "PAR", "Nao", "ABC")],
    )
    assert report["available"] is True
    assert report["items"][0]["produto"]["estado"] == "ok"


@patch("app.persistence.precos_repo.resolver_rede")
@patch("app.erp.product_check.produto_vinculo_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_vinculos_do_codigo_e_do_ean_discordam(mock_db, mock_fb, mock_vinc, mock_rede):
    from types import SimpleNamespace

    from app.persistence.produto_vinculo_repo import norm_chave

    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_rede.return_value = None
    mock_vinc.norm_chave.side_effect = norm_chave
    mock_vinc.lookup.return_value = {
        ("codigo", "REF-X"): SimpleNamespace(fire_seq=21),
        ("ean", "789"): SimpleNamespace(fire_seq=20),
    }
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "REF-X", "ean": "789", "unit_price": 1.0}]),
        seq_rows=[
            (20, 20, "B", 2.0, "PAR", "Nao", "B1"),
            (21, 21, "C", 2.0, "PAR", "Nao", "C1"),
        ],
    )
    produto = report["items"][0]["produto"]
    assert produto["estado"] == "chaves_discordam"
    assert produto["fire_seq"] is None
    assert [c["seq"] for c in produto["candidatos"]] == [20, 21]
    assert produto["motivo"] == product_check.MOTIVO_CHAVES_DISCORDAM


def _vinculos_patch(mock_db, mock_vinc, mock_rede, achados):
    from app.persistence.produto_vinculo_repo import norm_chave

    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_rede.return_value = None
    mock_vinc.norm_chave.side_effect = norm_chave
    mock_vinc.lookup.return_value = achados


@patch("app.persistence.precos_repo.resolver_rede")
@patch("app.erp.product_check.produto_vinculo_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_vinculos_discordam_com_um_sumido_e_invalido(mock_db, mock_fb, mock_vinc, mock_rede):
    from types import SimpleNamespace

    _vinculos_patch(
        mock_db,
        mock_vinc,
        mock_rede,
        {
            ("codigo", "REF-X"): SimpleNamespace(fire_seq=21),  # sumiu do Fire
            ("ean", "789"): SimpleNamespace(fire_seq=20),
        },
    )
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "REF-X", "ean": "789", "unit_price": 1.0}]),
        seq_rows=[(20, 20, "B", 2.0, "PAR", "Nao", "B1")],
    )
    produto = report["items"][0]["produto"]
    assert produto["estado"] == "vinculo_invalido"
    assert produto["candidatos"] == []
    assert "21" in produto["motivo"]


@patch("app.persistence.precos_repo.resolver_rede")
@patch("app.erp.product_check.produto_vinculo_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_vinculos_discordam_com_um_bloqueado_e_invalido(
    mock_db, mock_fb, mock_vinc, mock_rede
):
    from types import SimpleNamespace

    _vinculos_patch(
        mock_db,
        mock_vinc,
        mock_rede,
        {
            ("codigo", "REF-X"): SimpleNamespace(fire_seq=21),
            ("ean", "789"): SimpleNamespace(fire_seq=20),
        },
    )
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "REF-X", "ean": "789", "unit_price": 1.0}]),
        seq_rows=[
            (20, 20, "B", 2.0, "PAR", "Sim", "B1"),
            (21, 21, "C", 2.0, "PAR", "Nao", "C1"),
        ],
    )
    produto = report["items"][0]["produto"]
    assert produto["estado"] == "vinculo_invalido"
    assert [c["seq"] for c in produto["candidatos"]] == [20]


@patch("app.persistence.precos_repo.resolver_rede")
@patch("app.erp.product_check.produto_vinculo_repo")
@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_vinculo_invalido_vence_codigo_que_casa(mock_db, mock_fb, mock_vinc, mock_rede):
    from types import SimpleNamespace

    _vinculos_patch(
        mock_db, mock_vinc, mock_rede, {("codigo", "ABC"): SimpleNamespace(fire_seq=999)}
    )
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "ABC", "unit_price": 1.0}]),
        code_rows=[("ABC", 5, "X", 1.0, "PAR", "Nao", "ABC")],
    )
    item = report["items"][0]
    assert item["match_source"] == "codprod_altern"  # legado inalterado
    assert item["produto"]["estado"] == "vinculo_invalido"


@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_sem_ambiente_ativo_conferencia_completa(mock_db, mock_fb):
    from app.persistence.context import NoActiveEnvironmentError

    mock_db.connect.side_effect = NoActiveEnvironmentError("sem ambiente")
    report, _ = _check(
        mock_fb,
        _order([{"product_code": "ABC", "unit_price": 1.0}]),
        code_rows=[("ABC", 5, "X", 1.0, "PAR", "Nao", "ABC")],
    )
    assert report["items"][0]["produto"]["estado"] == "ok"
    assert report["produto_conferencia_incompleta"] is False
    assert report["summary"]["produto_conferencia_incompleta"] is False


@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_banco_do_ambiente_fora_marca_conferencia_incompleta(mock_db, mock_fb):
    import sqlite3

    mock_db.connect.side_effect = sqlite3.OperationalError("database is locked")
    with patch("app.utils.logger.logger.warning") as warn:
        report, _ = _check(
            mock_fb,
            _order([{"product_code": "ABC", "unit_price": 1.0}]),
            code_rows=[("ABC", 5, "X", 1.0, "PAR", "Nao", "ABC")],
        )
    assert report["available"] is True
    assert report["items"][0]["produto"]["estado"] == "ok"  # mantém o calculado
    assert report["produto_conferencia_incompleta"] is True
    assert report["summary"]["produto_conferencia_incompleta"] is True
    assert any("database is locked" in str(c.args[0]) for c in warn.call_args_list)


@patch("app.erp.product_check.FirebirdConnection")
@patch("app.erp.product_check.db")
def test_check_intercompany_sem_resposta_marca_conferencia_incompleta(mock_db, mock_fb):
    from app.erp.depara_cliente import ResolucaoCliente

    mock_db.connect.return_value.__enter__.return_value = MagicMock()
    mock_fb.return_value.connect_with_config.return_value = _make_fb_ctx_chave()[0]
    with (
        patch("app.persistence.environments_repo.to_fb_config", return_value={"path": "x"}),
        patch(
            "app.integrations.flowpcp.intercompany.resolucao_para",
            return_value=ResolucaoCliente(False, motivo="erro_conexao"),
        ),
        patch("app.erp.product_check.produto_vinculo_repo.lookup", return_value={}),
        patch("app.persistence.precos_repo.resolver_rede", return_value=None),
    ):
        report = product_check.check_order(
            _order([{"product_code": "ABC", "unit_price": 1.0}]), env={"slug": "mm"}
        )
    assert report["produto_conferencia_incompleta"] is True


def test_check_indisponivel_conferencia_nao_marcada():
    with patch("app.erp.product_check.FirebirdConnection") as mock_fb:
        mock_fb.return_value.is_configured.return_value = False
        report = product_check.check_order(_order([{"product_code": "X"}]))
    assert report["produto_conferencia_incompleta"] is False
    assert report["summary"]["produto_conferencia_incompleta"] is False
