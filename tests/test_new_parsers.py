"""Integration tests for the 5 new parsers and 2 EAN fixes."""
from __future__ import annotations

import pytest
from pathlib import Path

SAMPLES = Path(__file__).parent.parent / "samples"


def _load(filename: str):
    from app.ingestion.file_loader import LoadedFile
    p = SAMPLES / filename
    if not p.exists():
        pytest.skip(f"Sample not found: {filename}")
    return LoadedFile(path=p, extension=p.suffix.lower(), raw=p.read_bytes())


def _process(filename: str):
    from app.pipeline import process
    return process(_load(filename))


# ── FIX: Centauro EAN ────────────────────────────────────────────────────────

def test_centauro_ean_extracted():
    order = _process("PEDIDO CENTAURO.pdf")
    assert order is not None
    assert order.items[0].ean == "7909607654377"


def test_centauro_correct_fields():
    order = _process("PEDIDO CENTAURO.pdf")
    assert order.header.order_number == "29852927"
    assert order.header.customer_cnpj is not None
    assert "06.347.409" in order.header.customer_cnpj
    assert order.items[0].quantity == 4545.0
    assert order.items[0].unit_price == 11.37


# ── FIX: Studio Z EAN ────────────────────────────────────────────────────────

def test_studio_z_ean_extracted():
    order = _process("PEDIDO STUDIO Z.pdf")
    assert order is not None
    assert order.items[0].ean == "7909901749663"


def test_studio_z_correct_fields():
    order = _process("PEDIDO STUDIO Z.pdf")
    assert order.header.order_number == "2600009863"
    assert order.items[0].quantity == 1500.0
    assert order.items[0].unit_price == 11.33


# ── BeiranRioParser ──────────────────────────────────────────────────────────

def test_beira_rio_item_count():
    """5 item codes × 2 size ranges + 2 extra color variants = 14 rows."""
    order = _process("PEDIDO BEIRA RIO.pdf")
    assert order is not None
    assert len(order.items) == 14


def test_beira_rio_header():
    order = _process("PEDIDO BEIRA RIO.pdf")
    assert order.header.order_number == "12909889"
    assert "88.379.771" in (order.header.customer_cnpj or "")


def test_beira_rio_first_items():
    order = _process("PEDIDO BEIRA RIO.pdf")
    first = order.items[0]
    assert first.product_code == "1000626853"
    assert first.quantity == 9000.0
    assert first.unit_price == 8.19
    assert first.obs == "33/38"
    assert first.delivery_date == "17/02/2026"


def test_beira_rio_has_two_size_ranges():
    order = _process("PEDIDO BEIRA RIO.pdf")
    codes = [i.product_code for i in order.items]
    # Each item code appears at least twice (33/38 + 39/44)
    assert codes.count("1000626853") == 2
    assert codes.count("1000626854") == 2


def test_beira_rio_multi_variant_item():
    """1000626856 has 2 color variants → 4 rows total."""
    order = _process("PEDIDO BEIRA RIO.pdf")
    assert [i.product_code for i in order.items].count("1000626856") == 4


# ── KoloshParser ─────────────────────────────────────────────────────────────

def test_kolosh_item_count():
    order = _process("PEDIDO KOLOSH.pdf")
    assert order is not None
    assert len(order.items) == 15


def test_kolosh_header():
    order = _process("PEDIDO KOLOSH.pdf")
    assert order.header.order_number == "77900C"
    assert "00.465.813" in (order.header.customer_cnpj or "")


def test_kolosh_prices():
    order = _process("PEDIDO KOLOSH.pdf")
    prices = {i.unit_price for i in order.items if i.unit_price}
    # Two distinct prices: 9.97 and 11.23 and 9.08
    assert 9.97 in prices
    assert 11.23 in prices
    assert 9.08 in prices


def test_kolosh_delivery_date():
    order = _process("PEDIDO KOLOSH.pdf")
    dates = {i.delivery_date for i in order.items if i.delivery_date}
    # Should have 17/04/2026
    assert any("17/04" in d for d in dates)


# ── SamsClubParser ───────────────────────────────────────────────────────────

def test_sams_club_item_count():
    order = _process("PEDIDO SAMS CLUB.pdf")
    assert order is not None
    assert len(order.items) == 18


def test_sams_club_header():
    order = _process("PEDIDO SAMS CLUB.pdf")
    assert order.header.order_number == "06654993-0000"
    assert "00.063.960" in (order.header.customer_cnpj or "")


def test_sams_club_eans_present():
    order = _process("PEDIDO SAMS CLUB.pdf")
    # All items should have EANs (EAN is the product code in Sam's Club)
    for item in order.items:
        assert item.ean is not None
        assert len(item.ean) == 13


def test_sams_club_first_item():
    order = _process("PEDIDO SAMS CLUB.pdf")
    first = order.items[0]
    assert first.ean == "7898686876711"
    assert first.quantity == 117.0
    assert first.unit_price == 26.36


def test_sams_club_delivery_date():
    order = _process("PEDIDO SAMS CLUB.pdf")
    dates = {i.delivery_date for i in order.items if i.delivery_date}
    assert any("30/01/2026" in d for d in dates)


# ── SamsClubParser GRADE (Cross Docking) ─────────────────────────────────────

GRADE_FILE = "PEDIDO SAMS CLUB GRADE.pdf"
GRADE_LOJA_EANS = {"7891737001698", "7891737012779", "7891737676568"}
GRADE_LOJA_CNPJS = {
    "00.063.960/0094-08",
    "00.063.960/0570-46",
    "00.063.960/0576-31",
}


def test_sams_grade_header():
    order = _process(GRADE_FILE)
    assert order.header.order_number == "06611415-0000"
    assert order.header.customer_name is not None
    assert "M.M" in order.header.customer_name


def test_sams_grade_item_count():
    """3 lojas decompondo 19 SKUs (uma loja não recebe 1 SKU) → 56 OrderItems."""
    order = _process(GRADE_FILE)
    assert len(order.items) == 56


def test_sams_grade_delivery_ean_populated():
    order = _process(GRADE_FILE)
    eans = {i.delivery_ean for i in order.items}
    assert eans == GRADE_LOJA_EANS


def test_sams_grade_delivery_cnpj_populated():
    order = _process(GRADE_FILE)
    cnpjs = {i.delivery_cnpj for i in order.items}
    assert cnpjs == GRADE_LOJA_CNPJS


def test_sams_grade_qty_sum_matches_consolidated():
    """Soma por SKU bate com a tabela superior (ex: 7898686876711 → 16+16+27=59)."""
    order = _process(GRADE_FILE)
    sums: dict[str, float] = {}
    for it in order.items:
        sums[it.ean] = sums.get(it.ean, 0.0) + it.quantity
    assert sums["7898686876711"] == 59.0
    assert sums["7898686876728"] == 153.0
    assert sums["7898686876735"] == 234.0
    # SKU com pack=36 → grade expressa em embalagens, multiplica por 36
    assert sums["7898686879194"] == 72.0  # 1 + 1 embalagens × 36
    assert sums["7898686879200"] == 144.0  # 1 + 2 + 1 embalagens × 36


def test_sams_grade_unit_price_lookup():
    """Preço unitário vem da tabela 'Itens do Pedido' via lookup pelo EAN do produto."""
    order = _process(GRADE_FILE)
    for it in order.items:
        if it.ean == "7898686876711":
            assert it.unit_price == 26.36
        if it.ean == "7898686879194":
            assert it.unit_price == 730.44


def test_sams_grade_pack_size_multiplier():
    """SKUs com pack > 1: cada item da grade deve refletir packs × pack_size."""
    order = _process(GRADE_FILE)
    pack36 = [i for i in order.items if i.ean == "7898686879194"]
    # 2 lojas × 1 embalagem cada → 2 itens, todos com qty=36
    assert len(pack36) == 2
    assert all(i.quantity == 36.0 for i in pack36)


def test_sams_grade_per_store_split():
    """Cada loja recebe um arquivo XLSX próprio, identificado por SAMS_LOJA_<filial>."""
    from app.exporters.erp_exporter import ERPExporter
    import tempfile

    order = _process(GRADE_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        names = sorted(p.name for p in paths)
        assert len(paths) == 3
        assert any("SAMS_LOJA_0094_08" in n for n in names)
        assert any("SAMS_LOJA_0570_46" in n for n in names)
        assert any("SAMS_LOJA_0576_31" in n for n in names)


# ── KallanXlsParser ──────────────────────────────────────────────────────────

def test_kallan_item_count():
    order = _process("PEDIDO KALLAN K01.xlsx")
    assert order is not None
    assert len(order.items) == 9


def test_kallan_header():
    order = _process("PEDIDO KALLAN K01.xlsx")
    assert "51.540.219" in (order.header.customer_cnpj or "")
    assert order.header.order_number is not None


def test_kallan_first_item():
    order = _process("PEDIDO KALLAN K01.xlsx")
    item = order.items[0]
    assert item.product_code is not None
    assert item.quantity == 36.0
    assert item.unit_price is not None and item.unit_price > 0


def test_kallan_all_quantities_positive():
    order = _process("PEDIDO KALLAN K01.xlsx")
    for item in order.items:
        assert item.quantity is not None and item.quantity > 0


# ── DesmembramentoXlsParser ──────────────────────────────────────────────────

def test_magic_feet_splits_by_store():
    order = _process("Desmembramento Magic Feet.xlsx")
    assert order is not None
    # Magic Feet has 9 stores with CNPJs → should generate 9 output files
    from app.exporters.erp_exporter import ERPExporter
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        assert len(paths) == 9


def test_authentic_feet_items():
    order = _process("Desmembramento Authentic feet (1).xlsx")
    assert order is not None
    assert len(order.items) > 0
    # Items should have product codes
    for item in order.items:
        assert item.product_code is not None


# ── AuthenticFeetParser (single-customer "Pedido") ──────────────────────────

def test_authentic_fit_basic():
    order = _process("Pedido Authentic Fit.xlsx")
    assert order is not None
    assert len(order.items) == 12
    assert order.header.customer_cnpj == "62.513.076/0001-78"
    assert order.header.customer_name and "MULTIX" in order.header.customer_name.upper()
    # Soma dos TOTAL KITS deve bater com totalizador da linha 25 (540)
    assert sum(int(it.quantity) for it in order.items) == 540


def test_authentic_fit_first_item():
    order = _process("Pedido Authentic Fit.xlsx")
    item = order.items[0]
    assert item.product_code == "AFK3S-A-100-3338"
    assert item.quantity == 50
    assert item.unit_price == 11.96
    assert item.total_price == 598
    desc = (item.description or "").upper()
    assert "SAPATILHA" in desc
    assert "BRANCO" in desc


def test_authentic_fit_does_not_match_desmembramento():
    """Não-regressão: o sample de desmembramento continua indo para
    DesmembramentoXlsParser, mesmo com AuthenticFeetParser registrado antes."""
    order = _process("Desmembramento Authentic feet (1).xlsx")
    assert order is not None
    assert any(it.delivery_cnpj or it.delivery_name for it in order.items)


def test_authentic_fit_single_output_file():
    """Pedido single-customer → exportador gera 1 arquivo (sem split)."""
    order = _process("Pedido Authentic Fit.xlsx")
    assert order is not None
    from app.exporters.erp_exporter import ERPExporter
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        assert len(paths) == 1


def test_nba_item_count():
    order = _process("PEDIDO NBA 3.xlsx")
    assert order is not None
    assert len(order.items) > 0


def test_nba_has_product_codes():
    order = _process("PEDIDO NBA 3.xlsx")
    for item in order.items:
        assert item.product_code is not None


def test_nba_splits_by_store():
    """Each store column becomes a separate output file."""
    order = _process("PEDIDO NBA 3.xlsx")
    assert order is not None
    from app.exporters.erp_exporter import ERPExporter
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        assert len(paths) == 21, f"Expected 21 store files, got {len(paths)}: {[p.name for p in paths]}"


def test_nba_store_name_in_filename():
    """Store name appears in the output filename."""
    order = _process("PEDIDO NBA 3.xlsx")
    from app.exporters.erp_exporter import ERPExporter
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        names = [p.name for p in paths]
        assert any("Gramado" in n for n in names), f"No Gramado file found: {names}"


def test_nba_store_as_customer_name():
    """Each file's items use the store name as NOME_CLIENTE."""
    order = _process("PEDIDO NBA 3.xlsx")
    store_names = {i.delivery_name for i in order.items if i.delivery_name}
    # Every item should have a delivery_name set
    assert len(store_names) == 21, f"Expected 21 distinct store names, got {len(store_names)}"


# ── FIX: Riachuelo ME — page-footer URL not imported as item ─────────────────

def test_riachuelo_me_no_url_items():
    """URL footer artifact from PDF page break must not appear as a product item."""
    order = _process("RIACHUELO - PEDIDO.pdf")
    assert order is not None
    for item in order.items:
        assert item.description is not None
        assert not item.description.startswith("http"), (
            f"URL artifact imported as item: {item.description!r}"
        )


def test_riachuelo_me_no_null_product_codes():
    """Every item exported by the ME parser must have a product code."""
    order = _process("RIACHUELO - PEDIDO.pdf")
    assert order is not None
    for item in order.items:
        assert item.product_code is not None, (
            f"Item with no product_code: description={item.description!r}"
        )
