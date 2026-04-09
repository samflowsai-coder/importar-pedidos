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
