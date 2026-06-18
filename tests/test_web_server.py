"""Integration tests for the FastAPI web server."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db
from app.web.server import app

client = TestClient(app, raise_server_exceptions=False)

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    """Redirect app_state.db to a per-test tmp file so tests never touch prod DB."""
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


# ── Basic smoke ──────────────────────────────────────────────────────────────

def test_index_returns_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Portal de Pedidos" in r.text


def test_config_returns_default_output_dir():
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "outputDir" in data
    assert "output" in data["outputDir"]
    assert "exportMode" in data
    assert data["exportMode"] in ("xlsx", "db", "both")


# ── Security: download endpoint ──────────────────────────────────────────────

def test_download_rejects_non_xlsx():
    r = client.get("/api/download?path=/etc/passwd")
    assert r.status_code == 403


def test_download_rejects_arbitrary_files():
    r = client.get("/api/download?path=/etc/hosts.xlsx")  # doesn't exist
    assert r.status_code == 404


def test_download_missing_xlsx_returns_404(tmp_path):
    r = client.get(f"/api/download?path={tmp_path / 'missing.xlsx'}")
    assert r.status_code == 404


# ── Security: upload validation ──────────────────────────────────────────────

def test_upload_rejects_disallowed_extension(tmp_path):
    r = client.post(
        "/api/process",
        data={"output_dir": str(tmp_path)},
        files=[("files", ("malware.exe", b"MZ payload", "application/octet-stream"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert data["results"] == []
    assert any("suportado" in e["error"].lower() for e in data["errors"])


def test_upload_rejects_oversized_file(tmp_path):
    big = b"0" * (51 * 1024 * 1024)  # 51 MB
    r = client.post(
        "/api/process",
        data={"output_dir": str(tmp_path)},
        files=[("files", ("big.pdf", big, "application/pdf"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert any("limite" in e["error"].lower() for e in data["errors"])


# ── Filesystem browser ───────────────────────────────────────────────────────

def test_fs_returns_directories():
    r = client.get("/api/fs?path=~")
    assert r.status_code == 200
    data = r.json()
    assert "current" in data
    assert isinstance(data["entries"], list)
    # Entries must all be directories (names only, no file content exposed)
    for entry in data["entries"]:
        assert "name" in entry
        assert "path" in entry


def test_fs_handles_nonexistent_path():
    r = client.get("/api/fs?path=/this/does/not/exist/ever")
    assert r.status_code == 200  # falls back to parent


def test_fs_requires_admin_auth_without_bypass(real_auth):
    c = TestClient(app)
    r = c.get("/api/fs?path=~&file_ext=.fdb")
    assert r.status_code == 401


# ── End-to-end: real PDFs ────────────────────────────────────────────────────

@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_process_calcenter_pdf(tmp_path):
    pdf = SAMPLES / "2600009562-2026-02-25.pdf"
    if not pdf.exists():
        pytest.skip("Sample PDF not available")

    r = client.post(
        "/api/process",
        data={"output_dir": str(tmp_path)},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 1
    result = data["results"][0]
    assert result["order"] == "2600009562"
    assert len(result["files"]) == 1
    assert result["files"][0]["name"].endswith(".xlsx")
    assert (tmp_path / result["files"][0]["name"]).exists()


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_process_riachuelo_splits_into_three(tmp_path):
    pdf = SAMPLES / "PEDIDO 6702604130.pdf"
    if not pdf.exists():
        pytest.skip("Sample PDF not available")

    r = client.post(
        "/api/process",
        data={"output_dir": str(tmp_path)},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 1
    assert len(data["results"][0]["files"]) == 3


# ── Preview → Commit flow ────────────────────────────────────────────────────

@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_preview_returns_structured_payload():
    pdf = SAMPLES / "2600009562-2026-02-25.pdf"
    if not pdf.exists():
        pytest.skip("Sample PDF not available")

    r = client.post(
        "/api/preview",
        files=[("file", (pdf.name, pdf.read_bytes(), "application/pdf"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert "preview_id" in data
    assert data["header"]["order_number"] == "2600009562"
    assert isinstance(data["items"], list)
    assert data["totals"]["items_count"] == len(data["items"])
    assert "groups" in data


def test_preview_rejects_disallowed_extension():
    r = client.post(
        "/api/preview",
        files=[("file", ("malware.exe", b"MZ", "application/octet-stream"))],
    )
    assert r.status_code == 400


def test_commit_rejects_unknown_preview_id():
    r = client.post("/api/commit", json={"preview_id": "does-not-exist"})
    assert r.status_code == 404


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_commit_consumes_preview_and_persists_log(tmp_path, monkeypatch):
    from app import config as app_config
    # Redirect config to tmp so we don't touch real log / output paths
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("EXPORT_MODE", "xlsx")
    monkeypatch.setattr(
        app_config,
        "load",
        lambda: {
            "watch_dir": str(tmp_path),
            "output_dir": str(tmp_path),
            "export_mode": "xlsx",
        },
    )
    monkeypatch.setattr(
        app_config,
        "imported_dir",
        lambda _cfg: tmp_path,
    )

    pdf = SAMPLES / "2600009562-2026-02-25.pdf"
    if not pdf.exists():
        pytest.skip("Sample PDF not available")

    r = client.post(
        "/api/preview",
        files=[("file", (pdf.name, pdf.read_bytes(), "application/pdf"))],
    )
    assert r.status_code == 200
    preview_id = r.json()["preview_id"]

    r2 = client.post("/api/commit", json={"preview_id": preview_id})
    assert r2.status_code == 200, r2.text
    assert r2.json()["order"] == "2600009562"
    assert r2.json()["portal_status"] == "parsed"

    # Second commit with same id must be rejected
    r3 = client.post("/api/commit", json={"preview_id": preview_id})
    assert r3.status_code == 409

    # Entry is persisted and queryable via /api/imported, with portal_status='parsed'
    r4 = client.get("/api/imported?limit=10")
    assert r4.status_code == 200
    body = r4.json()
    assert body["total"] >= 1
    found = [e for e in body["entries"] if e["order_number"] == "2600009562"]
    assert found
    assert found[0]["portal_status"] == "parsed"
    assert found[0]["fire_codigo"] is None


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_preview_pending_reads_from_watch_folder(tmp_path, monkeypatch):
    from app import config as app_config

    # Seed watch folder with a sample PDF
    sample = SAMPLES / "2600009562-2026-02-25.pdf"
    if not sample.exists():
        pytest.skip("Sample PDF not available")
    watch = tmp_path / "watch"
    watch.mkdir()
    imp = tmp_path / "imported"
    imp.mkdir()
    seeded = watch / sample.name
    seeded.write_bytes(sample.read_bytes())

    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": str(watch), "output_dir": str(tmp_path), "export_mode": "xlsx",
    })
    monkeypatch.setattr(app_config, "imported_dir", lambda _cfg: imp)

    r = client.post("/api/preview-pending", json={"filename": sample.name})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["header"]["order_number"] == "2600009562"
    preview_id = body["preview_id"]

    r2 = client.post("/api/commit", json={"preview_id": preview_id, "outputDir": str(tmp_path)})
    assert r2.status_code == 200, r2.text

    # File must have been moved from watch to imported folder
    assert not seeded.exists()
    assert (imp / sample.name).exists()


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_preview_pending_uses_selected_environment_watch_dir(tmp_path, monkeypatch):
    from app.persistence import environments_repo

    sample = SAMPLES / "2600009562-2026-02-25.pdf"
    if not sample.exists():
        pytest.skip("Sample PDF not available")

    legacy_watch = tmp_path / "legacy-watch"
    env_watch = tmp_path / "env-watch"
    legacy_watch.mkdir()
    env_watch.mkdir()
    seeded = env_watch / sample.name
    seeded.write_bytes(sample.read_bytes())

    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(env_watch),
        output_dir=str(tmp_path / "env-out"),
        fb_path="",
    )
    monkeypatch.setattr("app.config.load", lambda: {
        "watch_dir": str(legacy_watch),
        "output_dir": str(tmp_path / "legacy-out"),
        "export_mode": "xlsx",
    })

    client.cookies.set("portal_env", env["id"])
    try:
        r = client.post("/api/preview-pending", json={"filename": sample.name})
    finally:
        client.cookies.clear()
    assert r.status_code == 200, r.text
    assert r.json()["header"]["order_number"] == "2600009562"


def test_preview_pending_rejects_missing_file(tmp_path, monkeypatch):
    from app import config as app_config
    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": str(tmp_path), "output_dir": str(tmp_path), "export_mode": "xlsx",
    })
    r = client.post("/api/preview-pending", json={"filename": "nao-existe.pdf"})
    assert r.status_code == 404


def test_preview_pending_rejects_path_traversal(tmp_path, monkeypatch):
    from app import config as app_config
    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": str(tmp_path), "output_dir": str(tmp_path), "export_mode": "xlsx",
    })
    # Attempt to escape the watch folder
    r = client.post("/api/preview-pending", json={"filename": "../../../etc/passwd"})
    assert r.status_code in (400, 404)  # either ext-reject or not-found after basename strip


def test_cancel_parsed_order_marks_as_cancelled():
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "TEST-1",
        "customer": "ACME",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {"header": {"order_number": "TEST-1"}, "items": []},
    })

    r = client.post(f"/api/imported/{entry_id}/cancel", json={"reason": "duplicado"})
    assert r.status_code == 200
    assert r.json()["portal_status"] == "cancelled"

    got = repo.get_import(entry_id)
    assert got["portal_status"] == "cancelled"


def test_cancel_sent_to_fire_rejected():
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "TEST-2",
        "status": "success",
        "portal_status": "sent_to_fire",
        "fire_codigo": 999,
        "snapshot": {"header": {"order_number": "TEST-2"}, "items": []},
    })
    r = client.post(f"/api/imported/{entry_id}/cancel", json={})
    assert r.status_code == 409


def test_send_to_fire_rejects_wrong_portal_status():
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "TEST-3",
        "status": "success",
        "portal_status": "cancelled",
        "snapshot": {"header": {"order_number": "TEST-3"}, "items": []},
    })
    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 409


def test_send_to_fire_missing_order_returns_404():
    r = client.post("/api/imported/does-not-exist/send-to-fire")
    assert r.status_code == 404


def test_send_to_fire_inserts_when_success(monkeypatch):
    """Mock FirebirdExporter to simulate a real Fire insert without the DB."""
    from app.persistence import repo
    from app.exporters import firebird_exporter as fb_mod
    import uuid
    from datetime import datetime

    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "pedido.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "TEST-OK",
        "customer": "ACME",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {"order_number": "TEST-OK", "customer_name": "ACME"},
            "items": [{"description": "x", "quantity": 1.0, "ean": "1234"}],
            "source_file": "",
        },
    })

    def _fake_export(self, order, *, override_client_id=None):
        return fb_mod.FirebirdExportResult(
            order_number=order.header.order_number,
            items_inserted=1,
            fire_codigo=4242,
        )
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _fake_export)

    # also force export_mode to 'db' to skip XLSX export path
    from app import config as app_config
    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": ".", "output_dir": ".", "export_mode": "db",
    })

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 200, r.text
    assert r.json()["fire_codigo"] == 4242

    got = repo.get_import(entry_id)
    assert got["portal_status"] == "sent_to_fire"
    assert got["fire_codigo"] == 4242


def test_batch_send_to_fire_mixed_outcomes(monkeypatch):
    """Batch endpoint tolerates partial failures: some parsed, one cancelled, one not-found."""
    from app.persistence import repo
    from app.exporters import firebird_exporter as fb_mod
    from app import config as app_config
    import uuid
    from datetime import datetime

    parsed_ids = []
    for _ in range(2):
        entry_id = str(uuid.uuid4())
        repo.insert_import({
            "id": entry_id,
            "source_filename": "pedido.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": f"BATCH-{entry_id[:4]}",
            "customer": "ACME",
            "status": "success",
            "portal_status": "parsed",
            "snapshot": {
                "header": {"order_number": f"BATCH-{entry_id[:4]}", "customer_name": "ACME"},
                "items": [{"description": "x", "quantity": 1.0}],
                "source_file": "",
            },
        })
        parsed_ids.append(entry_id)

    cancelled_id = str(uuid.uuid4())
    repo.insert_import({
        "id": cancelled_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "CANC-1",
        "status": "success",
        "portal_status": "cancelled",
        "snapshot": {"header": {"order_number": "CANC-1"}, "items": []},
    })

    fire_seq = iter([4001, 4002])

    def _fake_export(self, order, *, override_client_id=None):
        return fb_mod.FirebirdExportResult(
            order_number=order.header.order_number,
            items_inserted=1,
            fire_codigo=next(fire_seq),
        )
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _fake_export)
    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": ".", "output_dir": ".", "export_mode": "db",
    })

    r = client.post("/api/batch/send-to-fire", json={
        "ids": parsed_ids + [cancelled_id, "unknown-id"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert body["ok"] == 2
    assert body["failed"] == 2

    by_id = {r["id"]: r for r in body["results"]}
    for pid in parsed_ids:
        assert by_id[pid]["ok"] is True
        assert by_id[pid]["fire_codigo"] in (4001, 4002)
    assert by_id[cancelled_id]["ok"] is False
    assert by_id[cancelled_id]["reason"] == "wrong_status"
    assert by_id["unknown-id"]["ok"] is False
    assert by_id["unknown-id"]["reason"] == "not_found"

    # Parsed rows are now sent_to_fire
    for pid in parsed_ids:
        got = repo.get_import(pid)
        assert got["portal_status"] == "sent_to_fire"


def test_export_xlsx_generates_files_without_calling_firebird(monkeypatch, tmp_path):
    """xlsx-only flow: ERPExporter runs, FirebirdExporter is NEVER instantiated/called."""
    from app.persistence import repo
    from app.exporters import erp_exporter as erp_mod
    from app.exporters import firebird_exporter as fb_mod
    from app import config as app_config
    import uuid
    from datetime import datetime

    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "pedido.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "XLSX-OK",
        "customer": "ACME",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {"order_number": "XLSX-OK", "customer_name": "ACME"},
            "items": [{"description": "x", "quantity": 1.0}],
            "source_file": "",
        },
    })

    # Stub ERPExporter to return predictable file paths without touching disk.
    erp_calls: list[str] = []

    def _fake_erp_export(self, order, output_dir):
        erp_calls.append(order.header.order_number or "?")
        out = Path(output_dir) / "ACME_XLSX-OK.xlsx"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.touch()
        return [out]
    monkeypatch.setattr(erp_mod.ERPExporter, "export", _fake_erp_export)

    # Hard-fail if Firebird is touched.
    def _explode(self, order, *, override_client_id=None):
        raise AssertionError("FirebirdExporter.export must not be called in xlsx mode")
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _explode)

    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": str(tmp_path), "output_dir": str(tmp_path), "export_mode": "xlsx",
    })

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entry_id"] == entry_id
    assert body["portal_status"] == "parsed"
    assert body["output_files"] and body["output_files"][0]["name"].endswith(".xlsx")
    assert erp_calls == ["XLSX-OK"]

    # State unchanged: still 'parsed', no fire_codigo set.
    got = repo.get_import(entry_id)
    assert got["portal_status"] == "parsed"
    assert got.get("fire_codigo") in (None, 0)


def test_export_xlsx_rejects_wrong_portal_status():
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "XLSX-WRONG",
        "status": "success",
        "portal_status": "cancelled",
        "snapshot": {"header": {"order_number": "XLSX-WRONG"}, "items": []},
    })
    r = client.post(f"/api/imported/{entry_id}/export-xlsx")
    assert r.status_code == 409


def test_batch_send_to_fire_rejects_empty_and_oversized():
    r = client.post("/api/batch/send-to-fire", json={"ids": []})
    assert r.status_code == 400

    r2 = client.post("/api/batch/send-to-fire", json={"ids": ["x"] * 101})
    assert r2.status_code == 400


def test_rehydrate_preview_returns_snapshot():
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "REHYDR-1",
        "customer": "ACME",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {"order_number": "REHYDR-1", "customer_name": "ACME"},
            "items": [{"description": "item A", "quantity": 3.0}],
            "source_file": "",
        },
        "check": {"available": False, "reason": "FB_DATABASE_NOT_SET", "client": {"match": False}, "items": [], "summary": {}},
    })
    r = client.get(f"/api/imported/{entry_id}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["preview_id"] == entry_id
    assert body["portal_status"] == "parsed"
    assert body["header"]["order_number"] == "REHYDR-1"
    assert body["check"]["available"] is False


def test_imported_filter_by_search_and_status():
    from app.persistence import repo
    # Seed a few rows directly
    import uuid
    from datetime import datetime
    for customer, status in [("Riachuelo SA", "success"), ("Beira Rio", "success"), ("Erro LTDA", "error")]:
        repo.insert_import({
            "id": str(uuid.uuid4()),
            "source_filename": "x.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": "X-1",
            "customer": customer,
            "status": status,
            "output_files": [],
            "error": None if status == "success" else "boom",
        })

    r = client.get("/api/imported?q=Riachuelo")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["entries"][0]["customer"] == "Riachuelo SA"

    r2 = client.get("/api/imported?status=error")
    assert r2.status_code == 200
    assert r2.json()["total"] == 1


# ── Manual cliente override (CLIENT_NOT_FOUND recovery) ──────────────────────

def _seed_parsed_entry(**overrides):
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = overrides.pop("id", str(uuid.uuid4()))
    repo.insert_import({
        "id": entry_id,
        "source_filename": "ovr.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": overrides.get("order_number", "OVR-1"),
        "customer": overrides.get("customer", "ACME"),
        "customer_cnpj": overrides.get("customer_cnpj", "11.222.333/0001-44"),
        "status": "success",
        "portal_status": overrides.get("portal_status", "parsed"),
        "snapshot": {
            "header": {
                "order_number": "OVR-1",
                "customer_name": "ACME",
                "customer_cnpj": "11.222.333/0001-44",
            },
            "items": [{"description": "x", "quantity": 1.0}],
            "source_file": "",
        },
        "check": {
            "available": True,
            "reason": None,
            "client": {
                "match": False, "fire_id": None,
                "razao_social": None, "cnpj": "11.222.333/0001-44",
            },
            "items": [],
            "summary": {
                "items_total": 1, "items_matched": 0,
                "items_missing": 1, "client_matched": False,
            },
        },
    })
    return entry_id


class _FakeFbCursor:
    """Minimal cursor double — replays a list of fetchone() rows in order."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executed: list[tuple] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        out, self._rows = self._rows, []
        return out

    def close(self):
        pass


def _patch_fb_with_rows(monkeypatch, rows, *, configured=True):
    """Patch FirebirdConnection so .is_configured() and .connect() return our fake."""
    from app.erp import connection as conn_mod

    def _ctor():
        inst = conn_mod.FirebirdConnection.__new__(conn_mod.FirebirdConnection)
        return inst

    cursor = _FakeFbCursor(rows)

    class _CtxConn:
        def cursor(self):
            return cursor

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    inst = _ctor()
    monkeypatch.setattr(inst, "is_configured", lambda: configured, raising=False)
    monkeypatch.setattr(inst, "connect", lambda: _CtxConn(), raising=False)

    # Patch the constructor so server.py's `FirebirdConnection()` returns our instance
    monkeypatch.setattr(conn_mod, "FirebirdConnection", lambda: inst)
    return cursor


def test_search_clientes_requires_min_length():
    r = client.get("/api/clientes/search?q=a")
    assert r.status_code == 400


def test_search_clientes_returns_503_when_fb_not_configured(monkeypatch):
    _patch_fb_with_rows(monkeypatch, [], configured=False)
    r = client.get("/api/clientes/search?q=acme")
    assert r.status_code == 503


def test_search_clientes_happy_path_returns_results(monkeypatch):
    cursor = _patch_fb_with_rows(monkeypatch, [
        (101, "ACME COMERCIO LTDA", "11.222.333/0001-44"),
        (102, "ACME INDUSTRIAS SA", "55.666.777/0001-88"),
    ])
    r = client.get("/api/clientes/search?q=acme")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_returned"] == 2
    assert body["results"][0]["codigo"] == 101
    assert body["results"][0]["razao_social"] == "ACME COMERCIO LTDA"
    # Must have run SEARCH_CLIENTS with razao + cnpj patterns.
    sql, params = cursor.executed[0]
    assert "CADASTRO" in sql and "RAZAO_SOCIAL" in sql
    assert params[0] == "%ACME%"


def test_search_clientes_uses_selected_environment_firebird(monkeypatch, tmp_path):
    from app.erp import connection as conn_mod
    from app.persistence import environments_repo

    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "in"),
        output_dir=str(tmp_path / "out"),
        fb_path=str(tmp_path / "mm.fdb"),
        fb_host="127.0.0.1",
        fb_port="3051",
        fb_user="SYSDBA",
        fb_charset="WIN1252",
        fb_password="secret",
    )
    cursor = _FakeFbCursor([(101, "ACME COMERCIO LTDA", "11.222.333/0001-44")])
    seen_cfg: dict = {}

    class _CtxConn:
        def cursor(self):
            return cursor

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    inst = conn_mod.FirebirdConnection.__new__(conn_mod.FirebirdConnection)
    monkeypatch.setattr(inst, "is_configured", lambda: False, raising=False)
    monkeypatch.setattr(
        inst,
        "connect",
        lambda: (_ for _ in ()).throw(AssertionError("legacy connect must not be used")),
        raising=False,
    )

    def _connect_with_config(cfg):
        seen_cfg.update(cfg)
        return _CtxConn()

    monkeypatch.setattr(inst, "connect_with_config", _connect_with_config, raising=False)
    monkeypatch.setattr(conn_mod, "FirebirdConnection", lambda: inst)

    client.cookies.set("portal_env", env["id"])
    try:
        r = client.get("/api/clientes/search?q=acme")
    finally:
        client.cookies.clear()
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["codigo"] == 101
    assert seen_cfg["path"] == str(tmp_path / "mm.fdb")
    assert seen_cfg["host"] == "127.0.0.1"


def test_search_clientes_strips_non_digits_for_cnpj_pattern(monkeypatch):
    cursor = _patch_fb_with_rows(monkeypatch, [])
    r = client.get("/api/clientes/search?q=11.222.333/0001-44")
    assert r.status_code == 200
    _sql, params = cursor.executed[0]
    assert params[1] == "%11222333000144%"


def test_override_cliente_happy_path(monkeypatch):
    from app.persistence import repo
    entry_id = _seed_parsed_entry()
    _patch_fb_with_rows(monkeypatch, [
        (4242, "ACME COMERCIO LTDA", "11222333000144"),  # FIND_CLIENT_BY_CODIGO
    ])

    r = client.post(
        f"/api/imported/{entry_id}/override-cliente",
        json={"cliente_codigo": 4242, "reason": "varejista mudou de razão social"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cliente_override_codigo"] == 4242
    assert body["cliente_override_razao"] == "ACME COMERCIO LTDA"

    got = repo.get_import(entry_id)
    assert got["cliente_override_codigo"] == 4242
    assert got["cliente_override_razao"] == "ACME COMERCIO LTDA"
    assert got["cliente_override_at"]
    # TEST_AUTH_BYPASS substitui require_user pelo _TEST_USER (test@portal.local).
    assert got["cliente_override_by"] == "test@portal.local"


def test_override_cliente_appends_audit_with_actor_email(monkeypatch):
    from app.persistence import repo
    entry_id = _seed_parsed_entry()
    _patch_fb_with_rows(monkeypatch, [(4242, "ACME LTDA", "11222333000144")])

    client.post(
        f"/api/imported/{entry_id}/override-cliente",
        json={"cliente_codigo": 4242, "reason": "manual fix"},
    )
    events = repo.list_audit(entry_id)
    e = next(ev for ev in events if ev["event_type"] == "cliente_override_selected")
    assert e["detail"]["cliente_codigo"] == 4242
    assert e["detail"]["cliente_razao"] == "ACME LTDA"
    assert e["detail"]["reason"] == "manual fix"
    assert e["detail"]["user_email"] == "test@portal.local"
    assert e["detail"]["user_id"] == 0
    assert e["detail"]["previous_cnpj"] == "11.222.333/0001-44"


def test_override_cliente_rejects_non_parsed(monkeypatch):
    entry_id = _seed_parsed_entry(portal_status="cancelled")
    r = client.post(
        f"/api/imported/{entry_id}/override-cliente",
        json={"cliente_codigo": 4242},
    )
    assert r.status_code == 409
    assert "revisão" in r.json()["detail"].lower()


def test_override_cliente_rejects_unknown_codigo(monkeypatch):
    entry_id = _seed_parsed_entry()
    _patch_fb_with_rows(monkeypatch, [None])  # FIND_CLIENT_BY_CODIGO sem match
    r = client.post(
        f"/api/imported/{entry_id}/override-cliente",
        json={"cliente_codigo": 99999999},
    )
    assert r.status_code == 422


def test_override_cliente_returns_404_when_entry_missing():
    r = client.post(
        "/api/imported/does-not-exist/override-cliente",
        json={"cliente_codigo": 1},
    )
    assert r.status_code == 404


def test_override_cliente_returns_503_when_fb_not_configured(monkeypatch):
    entry_id = _seed_parsed_entry()
    _patch_fb_with_rows(monkeypatch, [], configured=False)
    r = client.post(
        f"/api/imported/{entry_id}/override-cliente",
        json={"cliente_codigo": 4242},
    )
    assert r.status_code == 503


def test_rehydrate_preview_injects_override_into_check_block(monkeypatch):
    from app.persistence import repo
    entry_id = _seed_parsed_entry()
    repo.set_client_override(entry_id, codigo=4242, razao="ACME OVERRIDE LTDA")

    r = client.get(f"/api/imported/{entry_id}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["check"]["client"]["match"] is True
    assert body["check"]["client"]["fire_id"] == 4242
    assert body["check"]["client"]["override"] is True
    assert body["check"]["summary"]["client_matched"] is True
    assert body["cliente_override"]["codigo"] == 4242
    assert body["cliente_override"]["razao_social"] == "ACME OVERRIDE LTDA"
    assert body["cliente_override"]["by"] is None


def test_rehydrate_preview_omits_override_block_when_no_override():
    entry_id = _seed_parsed_entry()
    r = client.get(f"/api/imported/{entry_id}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["cliente_override"] is None
    # Original false-match preserved
    assert body["check"]["client"]["match"] is False


def test_send_to_fire_passes_override_to_exporter(monkeypatch):
    """Override stored em imports é lido e passado ao FirebirdExporter."""
    from app import config as app_config
    from app.exporters import firebird_exporter as fb_mod
    from app.persistence import repo

    entry_id = _seed_parsed_entry()
    repo.set_client_override(entry_id, codigo=4242, razao="ACME OVERRIDE")

    captured = {}

    def _fake_export(self, order, *, override_client_id=None):
        captured["override"] = override_client_id
        return fb_mod.FirebirdExportResult(
            order_number=order.header.order_number,
            items_inserted=1,
            fire_codigo=999,
        )

    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _fake_export)
    monkeypatch.setattr(app_config, "load", lambda: {
        "watch_dir": ".", "output_dir": ".", "export_mode": "db",
    })

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 200, r.text
    assert captured["override"] == 4242
    assert r.json()["fire_codigo"] == 999


@pytest.mark.skipif(not SAMPLES.exists(), reason="samples/ directory not found")
def test_process_sbf_centauro(tmp_path):
    pdf = SAMPLES / "Pedido_0029852483.pdf"
    if not pdf.exists():
        pytest.skip("Sample PDF not available")

    r = client.post(
        "/api/process",
        data={"output_dir": str(tmp_path)},
        files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))],
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["order"] == "29852483"


# ── Regressão: salvar config preserva sessão ─────────────────────────────────
# Antes do fix, o `app_state.db` era resolvido a partir de `watch_dir`, então
# `POST /api/config` movia o arquivo do banco e deixava a sessão órfã (cookie
# válido apontando para token em DB que o app não enxergava mais). O fix em
# `app/persistence/db.py` pinou o caminho em local estável independente de
# `watch_dir`.

def test_save_config_preserves_session(real_auth, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config as app_config
    from app.persistence import users_repo
    from app.web.server import app as fastapi_app

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    # Isolar config.json para não contaminar outros testes.
    monkeypatch.setattr(app_config, "_CONFIG_FILE", tmp_path / "config.json")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()

    users_repo.create_user(
        email="cfg@portal.local", password="strongpass1", role="admin",
    )

    c = TestClient(fastapi_app)
    r = c.post(
        "/api/auth/login",
        json={"email": "cfg@portal.local", "password": "strongpass1"},
    )
    assert r.status_code == 200, r.text
    cookie = r.cookies.get("portal_session")
    assert cookie

    headers = {"Cookie": f"portal_session={cookie}"}

    for i in range(3):
        new_watch = tmp_path / f"watch{i}"
        new_out = tmp_path / f"out{i}"
        r = c.post(
            "/api/config",
            json={
                "watchDir": str(new_watch),
                "outputDir": str(new_out),
                "exportMode": "xlsx",
            },
            headers=headers,
        )
        assert r.status_code == 200, f"save #{i} falhou: {r.text}"

    me = c.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == "cfg@portal.local"
