import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    from app.persistence import db
    db.reset_init_cache()
    yield tmp_path
    db.reset_init_cache()


def _client():
    from app.web.server import app
    return TestClient(app)


def _good_zip(deps_sha) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("portal-pedidos/manifest.json", json.dumps({
            "name": "portal-pedidos", "version": "20260714-1030",
            "built_at": "2026-07-14T10:30:00Z", "git_commit": "deadbee",
            "deps_sha256": deps_sha}))
        z.writestr("portal-pedidos/ui.py", b"# ui\n")
    return buf.getvalue()


def test_status_idle(setup):
    r = _client().get("/api/admin/update/status")
    assert r.status_code == 200 and r.json()["status"] == "idle"


def test_upload_nao_zip_400(setup):
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 400


def test_upload_zip_invalido_422(setup, monkeypatch):
    # zip válido de bytes mas sem manifesto → 422 com motivo
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("portal-pedidos/ui.py", b"x")
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("p.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 422 and "manifest" in r.json()["detail"].lower()


def test_upload_valido_200_resumo(setup, monkeypatch):
    from app.updates import package
    # força deps_changed=False fazendo o hash local == o do manifesto
    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("p.zip", _good_zip("SHA"), "application/zip")})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "20260714-1030" and body["deps_changed"] is False
    assert body["update_id"]


def test_apply_update_id_errado_404(setup):
    r = _client().post("/api/admin/update/apply", json={"update_id": "nao-existe"})
    assert r.status_code in (404, 409)  # sem staged → 404


def test_apply_dispara_updater(setup, monkeypatch):
    from app.updates import package
    from app.web import routes_update
    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    up = _client().post("/api/admin/update/upload",
                        files={"file": ("p.zip", _good_zip("SHA"), "application/zip")}).json()
    called = {}
    monkeypatch.setattr(routes_update, "_start_updater_task",
                        lambda: called.setdefault("ran", True) or True)
    r = _client().post("/api/admin/update/apply", json={"update_id": up["update_id"]})
    assert r.status_code == 202 and called.get("ran")
