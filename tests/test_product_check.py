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
    Conta execute() em cur.execute.call_count.
    """
    cur = MagicMock()
    next_fetchall = []

    def execute_side_effect(sql, params=None):
        nonlocal next_fetchall
        if "FROM CADASTRO" in sql:
            next_fetchall = []  # cliente usa fetchone
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

    # 1 cliente + 1 eans + 1 codes = 3 execute (sem depara nesta fase)
    assert cur.execute.call_count <= 4
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
            estado["rows"] = [(c, s, d, p) for s, c, d, p in por_seq if c.strip() in params]
        elif "SEQ IN" in sql:
            estado["rows"] = [(s, d, p) for s, c, d, p in por_seq if s in params]
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
