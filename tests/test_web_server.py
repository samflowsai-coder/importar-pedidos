"""Integration tests for the FastAPI web server."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.server import app

client = TestClient(app, raise_server_exceptions=False)

SAMPLES = Path(__file__).parent.parent / "samples"


# ── Basic smoke ──────────────────────────────────────────────────────────────

def test_index_returns_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Importar Pedidos" in r.text


def test_config_returns_default_output_dir():
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "defaultOutputDir" in data
    assert "output" in data["defaultOutputDir"]


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
