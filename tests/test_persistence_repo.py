"""Tests for app.persistence.repo (SQLite import history)."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from app.persistence import db, repo


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _entry(**overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "source_filename": "pedido.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "12345",
        "customer": "ACME LTDA",
        "customer_cnpj": "00000000000100",
        "output_files": [{"name": "out.xlsx", "path": "/tmp/out.xlsx"}],
        "db_result": {"order_number": "12345", "items_inserted": 3, "fire_codigo": 99},
        "fire_codigo": 99,
        "snapshot": {"header": {"order_number": "12345", "customer_cnpj": "00000000000100"}},
        "status": "success",
        "error": None,
    }
    base.update(overrides)
    return base


def test_insert_and_get_roundtrip(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    got = repo.get_import(e["id"])
    assert got is not None
    assert got["order_number"] == "12345"
    assert got["customer"] == "ACME LTDA"
    assert got["fire_codigo"] == 99
    assert got["output_files"][0]["name"] == "out.xlsx"
    assert got["db_result"]["items_inserted"] == 3
    assert got["snapshot"]["header"]["order_number"] == "12345"
    assert got["production_status"] == "none"


def test_insert_is_idempotent_upsert(sqlite_tmp):
    e = _entry(status="success")
    repo.insert_import(e)
    e["status"] = "error"
    e["error"] = "boom"
    repo.insert_import(e)
    got = repo.get_import(e["id"])
    assert got["status"] == "error"
    assert got["error"] == "boom"


def test_list_orders_by_imported_at_desc(sqlite_tmp):
    for i, ts in enumerate(["2026-01-01T10:00:00", "2026-03-01T10:00:00", "2026-02-01T10:00:00"]):
        repo.insert_import(_entry(imported_at=ts, source_filename=f"f{i}.pdf"))
    rows = repo.list_imports(limit=10)
    timestamps = [r["imported_at"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_filters_by_status(sqlite_tmp):
    repo.insert_import(_entry(status="success"))
    repo.insert_import(_entry(status="error"))
    assert repo.count_imports(status="success") == 1
    assert repo.count_imports(status="error") == 1
    assert {r["status"] for r in repo.list_imports(status="error")} == {"error"}


def test_list_search_by_customer_cnpj_or_order(sqlite_tmp):
    repo.insert_import(_entry(order_number="AAA", customer="Riachuelo"))
    repo.insert_import(_entry(order_number="BBB", customer="Beira Rio"))

    assert repo.count_imports(customer_search="Riachuelo") == 1
    assert repo.count_imports(customer_search="BBB") == 1
    assert repo.count_imports(customer_search="nao-existe") == 0


def test_list_search_is_parameterized_and_safe(sqlite_tmp):
    repo.insert_import(_entry(customer="ACME", order_number="PED-1"))
    # Classic SQL-injection attempt: should find nothing, not error out.
    needle = "'; DROP TABLE imports; --"
    assert repo.count_imports(customer_search=needle) == 0
    # Table still works
    assert repo.count_imports() == 1


def test_pagination(sqlite_tmp):
    for i in range(7):
        repo.insert_import(_entry(imported_at=f"2026-04-{22 - i:02d}T10:00:00"))
    page1 = repo.list_imports(limit=3, offset=0)
    page2 = repo.list_imports(limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 3
    assert page1[-1]["imported_at"] > page2[0]["imported_at"]


def test_list_caps_limit():
    # Even if caller passes huge limit, repo caps to MAX_PAGE_SIZE
    # This is a static check — no DB needed.
    from app.persistence.repo import _MAX_PAGE_SIZE
    assert _MAX_PAGE_SIZE == 500


def test_derives_cnpj_from_snapshot_when_missing(sqlite_tmp):
    e = _entry()
    e.pop("customer_cnpj", None)
    repo.insert_import(e)
    got = repo.get_import(e["id"])
    assert got["customer_cnpj"] == "00000000000100"


def test_audit_log_appended_and_listed(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.append_audit(e["id"], "imported", {"source": "commit", "items": 3})
    repo.append_audit(e["id"], "released_for_production", {"by": "user"})

    events = repo.list_audit(e["id"])
    assert len(events) == 2
    assert events[0]["event_type"] == "released_for_production"
    assert events[0]["detail"]["by"] == "user"
    assert events[1]["event_type"] == "imported"
    assert events[1]["detail"]["items"] == 3


def test_audit_cascade_on_import_delete(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.append_audit(e["id"], "imported", None)
    with db.connect() as conn:
        conn.execute("DELETE FROM imports WHERE id = ?", (e["id"],))
    assert repo.list_audit(e["id"]) == []
