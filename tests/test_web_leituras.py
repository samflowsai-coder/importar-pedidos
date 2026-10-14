"""Log de leituras nas rotas web: uma linha por arquivo lido, por origem."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app.observability import leituras
from app.persistence import db
from app.web.server import app

client = TestClient(app, raise_server_exceptions=False)

SAMPLES = Path(__file__).parent.parent / "samples"
AF072 = SAMPLES / "Pedido Authentic Feet AF072 QUAN.xlsx"

pytestmark = pytest.mark.skipif(not AF072.exists(), reason="sample AF072 ausente")


@pytest.fixture(autouse=True)
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EXPORT_MODE", "xlsx")
    watch = tmp_path / "watch"
    imp = tmp_path / "imp"
    out = tmp_path / "out"
    for d in (watch, imp, out):
        d.mkdir()
    cfg = {"watch_dir": str(watch), "output_dir": str(out), "export_mode": "xlsx"}
    monkeypatch.setattr(app_config, "load", lambda: cfg)
    monkeypatch.setattr(app_config, "imported_dir", lambda _cfg: imp)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    yield {"watch": watch, "imp": imp, "out": out}
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def linhas(monkeypatch):
    capturadas: list[dict] = []
    monkeypatch.setattr(leituras, "escrever", lambda linha: capturadas.append(linha))
    return capturadas


def _xlsx_bytes() -> bytes:
    return AF072.read_bytes()


def test_upload_gera_linha_e_trace_fecha_ate_o_commit(linhas):
    r = client.post(
        "/api/preview", files=[("file", (AF072.name, _xlsx_bytes(), "application/octet-stream"))]
    )
    assert r.status_code == 200, r.text
    assert len(linhas) == 1
    ln = linhas[0]
    assert ln["origem"] == "upload"
    assert ln["etapa"] == "parseado"
    assert ln["parser"]["usado"] == "NasmarTemplateParser"
    assert ln["resultado"] == "preview"
    assert ln["suspeito"]["sinais"] == []
    assert ln["trace_id"]
    assert ln["usuario"]
    assert ln["arquivo"]["copia"]
    assert ln["fire_check"] is not None

    r2 = client.post("/api/commit", json={"preview_id": r.json()["preview_id"]})
    assert r2.status_code == 200, r2.text
    imports = client.get("/api/imported?limit=10").json()
    itens = imports if isinstance(imports, list) else imports.get("items") or imports.get("entries")
    assert itens[0]["trace_id"] == ln["trace_id"]


def test_extensao_invalida_no_preview_vira_rejeitado(linhas):
    r = client.post("/api/preview", files=[("file", ("x.txt", b"oi", "text/plain"))])
    assert r.status_code == 400
    assert len(linhas) == 1
    assert linhas[0]["etapa"] == "rejeitado"
    assert linhas[0]["rejeicao"]["motivo"] == "extensao"


def test_xlsx_vazio_vira_422(linhas, tmp_path):
    import openpyxl

    p = tmp_path / "vazio.xlsx"
    openpyxl.Workbook().save(p)
    r = client.post(
        "/api/preview", files=[("file", ("vazio.xlsx", p.read_bytes(), "application/octet-stream"))]
    )
    assert r.status_code == 422
    assert len(linhas) == 1
    assert linhas[0]["origem"] == "upload"
    assert linhas[0]["resultado"] == "422"


def test_lote_reimport_e_legado_uma_linha_cada(linhas, ambiente):
    (ambiente["watch"] / AF072.name).write_bytes(_xlsx_bytes())
    r = client.post("/api/import", json={"files": [AF072.name]})
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == []
    assert [ln["origem"] for ln in linhas] == ["lote"]
    assert linhas[0]["resultado"] == "importado"

    assert (ambiente["imp"] / AF072.name).exists()
    r = client.post("/api/reimport", json={"filename": AF072.name})
    assert r.status_code == 200, r.text
    assert [ln["origem"] for ln in linhas] == ["lote", "reimport"]

    r = client.post(
        "/api/process",
        data={"output_dir": str(ambiente["out"])},
        files=[("files", (AF072.name, _xlsx_bytes(), "application/octet-stream"))],
    )
    assert r.status_code == 200, r.text
    assert [ln["origem"] for ln in linhas] == ["lote", "reimport", "process-legado"]


def test_lote_rejeita_extensao(linhas, ambiente):
    (ambiente["watch"] / "lixo.txt").write_text("oi")
    r = client.post("/api/import", json={"files": ["lixo.txt"]})
    assert r.status_code == 200
    assert len(r.json()["errors"]) == 1
    assert len(linhas) == 1
    assert linhas[0]["origem"] == "lote"
    assert linhas[0]["etapa"] == "rejeitado"
    assert linhas[0]["rejeicao"]["motivo"] == "extensao"


def _imports_trace(nome: str) -> str | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT trace_id FROM imports WHERE source_filename = ? ORDER BY imported_at DESC",
            (nome,),
        ).fetchone()
    return row[0] if row else None


def test_preview_pending_linha_completa_e_trace_ate_o_commit(linhas, ambiente):
    (ambiente["watch"] / AF072.name).write_bytes(_xlsx_bytes())
    r = client.post("/api/preview-pending", json={"filename": AF072.name})
    assert r.status_code == 200, r.text
    assert len(linhas) == 1
    ln = linhas[0]
    assert ln["origem"] == "preview-pending"
    assert ln["resultado"] == "preview"
    assert ln["usuario"]
    assert ln["arquivo"]["copia"]
    assert ln["fire_check"] is not None
    assert ln["trace_id"]

    r2 = client.post("/api/commit", json={"preview_id": r.json()["preview_id"]})
    assert r2.status_code == 200, r2.text
    assert _imports_trace(AF072.name) == ln["trace_id"]


def test_preview_pending_inexistente_vira_rejeitado(linhas):
    r = client.post("/api/preview-pending", json={"filename": "nao-existe.xlsx"})
    assert r.status_code == 404
    assert len(linhas) == 1
    assert linhas[0]["etapa"] == "rejeitado"
    assert linhas[0]["origem"] == "preview-pending"
    assert linhas[0]["rejeicao"]["motivo"] == "nao_encontrado"


def test_upload_grande_demais_vira_rejeitado(linhas, monkeypatch):
    from app.web import server

    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 10)
    r = client.post(
        "/api/preview", files=[("file", (AF072.name, _xlsx_bytes(), "application/octet-stream"))]
    )
    assert r.status_code == 413
    assert len(linhas) == 1
    assert linhas[0]["etapa"] == "rejeitado"
    assert linhas[0]["rejeicao"]["motivo"] == "tamanho"


def _guarda_falha(*_a, **_k):
    from fastapi import HTTPException

    raise HTTPException(status_code=500, detail="disco cheio")


def test_guarda_falhou_no_upload_lote_e_legado_viram_rejeitado(linhas, ambiente, monkeypatch):
    from app.web import server

    monkeypatch.setattr(server, "_guardar_original", _guarda_falha)

    r = client.post(
        "/api/preview", files=[("file", (AF072.name, _xlsx_bytes(), "application/octet-stream"))]
    )
    assert r.status_code == 500

    (ambiente["watch"] / AF072.name).write_bytes(_xlsx_bytes())
    r = client.post("/api/import", json={"files": [AF072.name]})
    assert r.status_code == 200
    assert len(r.json()["errors"]) == 1

    r = client.post(
        "/api/process",
        data={"output_dir": str(ambiente["out"])},
        files=[("files", (AF072.name, _xlsx_bytes(), "application/octet-stream"))],
    )
    assert r.status_code == 200
    assert len(r.json()["errors"]) == 1

    assert [(ln["origem"], ln["etapa"], ln["rejeicao"]["motivo"]) for ln in linhas] == [
        ("upload", "rejeitado", "guarda_falhou"),
        ("lote", "rejeitado", "guarda_falhou"),
        ("process-legado", "rejeitado", "guarda_falhou"),
    ]


def test_reimport_grava_origem_e_resultado(linhas, ambiente):
    (ambiente["imp"] / AF072.name).write_bytes(_xlsx_bytes())
    r = client.post("/api/reimport", json={"filename": AF072.name})
    assert r.status_code == 200, r.text
    assert len(linhas) == 1
    assert linhas[0]["origem"] == "reimport"
    assert linhas[0]["resultado"] == "importado"
    assert linhas[0]["trace_id"]


def test_lote_linha_tem_usuario_copia_e_trace_do_imports(linhas, ambiente):
    (ambiente["watch"] / AF072.name).write_bytes(_xlsx_bytes())
    r = client.post("/api/import", json={"files": [AF072.name]})
    assert r.status_code == 200, r.text
    assert r.json()["errors"] == []
    ln = linhas[0]
    assert ln["usuario"]
    assert ln["arquivo"]["copia"]
    assert ln["trace_id"]
    assert _imports_trace(AF072.name) == ln["trace_id"]


def test_export_que_falha_aparece_na_linha_do_lote(linhas, ambiente, monkeypatch):
    from app.web import server

    def _boom(*_a, **_k):
        raise RuntimeError("export quebrou")

    monkeypatch.setattr(server, "_run_exporters", _boom)
    (ambiente["watch"] / AF072.name).write_bytes(_xlsx_bytes())
    r = client.post("/api/import", json={"files": [AF072.name]})
    assert r.status_code == 200
    assert r.json()["errors"][0]["error"] == "export quebrou"
    assert len(linhas) == 1
    assert linhas[0]["resultado"] == "erro"
    assert linhas[0]["excecao"]["tipo"] == "RuntimeError"


def test_export_que_falha_aparece_na_linha_do_legado(linhas, ambiente, monkeypatch):
    from app.exporters.erp_exporter import ERPExporter

    def _boom(*_a, **_k):
        raise RuntimeError("export quebrou")

    monkeypatch.setattr(ERPExporter, "export", _boom)
    r = client.post(
        "/api/process",
        data={"output_dir": str(ambiente["out"])},
        files=[("files", (AF072.name, _xlsx_bytes(), "application/octet-stream"))],
    )
    assert r.status_code == 200
    assert r.json()["errors"][0]["error"] == "export quebrou"
    assert len(linhas) == 1
    assert linhas[0]["resultado"] == "erro"
    assert linhas[0]["excecao"]["tipo"] == "RuntimeError"
