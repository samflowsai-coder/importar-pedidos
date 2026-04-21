from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}
MAX_LOG_ENTRIES = 1000

app = FastAPI(title="Importar Pedidos", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Internal helpers ──────────────────────────────────────────────────────

def _get_cfg() -> dict:
    from app import config as app_config
    return app_config.load()


def _log_path(cfg: dict) -> Path:
    from app import config as app_config
    return app_config.imported_dir(cfg) / "import_log.json"


def _read_log(cfg: dict) -> List[dict]:
    path = _log_path(cfg)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _append_log(cfg: dict, entry: dict) -> None:
    from app import config as app_config
    entries = _read_log(cfg)
    entries.insert(0, entry)
    if len(entries) > MAX_LOG_ENTRIES:
        entries = entries[:MAX_LOG_ENTRIES]
    imp_dir = app_config.imported_dir(cfg)
    imp_dir.mkdir(parents=True, exist_ok=True)
    _log_path(cfg).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _make_log_entry(
    source_filename: str,
    order_number: Optional[str],
    customer: Optional[str],
    output_files: List[dict],
    status: str,
    error: Optional[str] = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source_filename": source_filename,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": order_number,
        "customer": customer,
        "output_files": output_files,
        "status": status,
        "error": error,
    }


def _process_file(file_path: Path, output_path: Path) -> dict:
    from app.exporters.erp_exporter import ERPExporter
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    ext = file_path.suffix.lower()
    raw = file_path.read_bytes()
    loaded = LoadedFile(path=file_path, extension=ext, raw=raw)
    order = process(loaded)
    if not order:
        raise ValueError("Formato não reconhecido ou pedido sem itens")

    exporter = ERPExporter()
    paths = exporter.export(order, str(output_path))
    return {
        "order_number": order.header.order_number,
        "customer": order.header.customer_name,
        "output_files": [{"name": p.name, "path": str(p)} for p in paths],
    }


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "importar-pedidos"})


@app.get("/api/config")
def get_config() -> JSONResponse:
    cfg = _get_cfg()
    return JSONResponse({"watchDir": cfg["watch_dir"], "outputDir": cfg["output_dir"]})


class ConfigUpdate(BaseModel):
    watchDir: Optional[str] = None
    outputDir: Optional[str] = None


@app.post("/api/config")
def update_config(body: ConfigUpdate) -> JSONResponse:
    from app import config as app_config
    watch_dir = str(Path(body.watchDir).expanduser().resolve()) if body.watchDir else None
    output_dir = str(Path(body.outputDir).expanduser().resolve()) if body.outputDir else None
    cfg = app_config.save(watch_dir=watch_dir, output_dir=output_dir)
    return JSONResponse({"watchDir": cfg["watch_dir"], "outputDir": cfg["output_dir"]})


@app.get("/api/pending")
def list_pending() -> JSONResponse:
    from app import config as app_config
    cfg = _get_cfg()
    watch = Path(cfg["watch_dir"])
    imp = app_config.imported_dir(cfg)

    if not watch.exists():
        return JSONResponse({"files": [], "watchDir": cfg["watch_dir"], "exists": False})

    files = []
    for f in sorted(watch.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
        if not f.is_file():
            continue
        if f.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        # Exclude anything inside "Pedidos importados" (safety, iterdir is not recursive)
        try:
            stat = f.stat()
            files.append({
                "name": f.name,
                "path": str(f),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "ext": f.suffix.lower().lstrip("."),
            })
        except Exception:
            pass

    return JSONResponse({"files": files, "watchDir": cfg["watch_dir"], "exists": True})


class ImportRequest(BaseModel):
    files: List[str]
    outputDir: Optional[str] = None


@app.post("/api/import")
def import_files(body: ImportRequest) -> JSONResponse:
    from app import config as app_config
    cfg = _get_cfg()
    watch = Path(cfg["watch_dir"])
    imp = app_config.imported_dir(cfg)

    output_path = (
        Path(body.outputDir).expanduser().resolve()
        if body.outputDir
        else Path(cfg["output_dir"]).expanduser().resolve()
    )

    try:
        output_path.mkdir(parents=True, exist_ok=True)
        imp.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao criar diretórios: {exc}")

    results = []
    errors = []

    for filename in body.files:
        name = Path(filename).name  # strip any path component — security
        src = watch / name

        if not src.exists() or not src.is_file():
            errors.append({"source": name, "error": "Arquivo não encontrado na pasta de entrada"})
            continue
        if src.suffix.lower() not in ALLOWED_EXTENSIONS:
            errors.append({"source": name, "error": "Extensão não permitida"})
            continue

        try:
            result = _process_file(src, output_path)

            dest = imp / name
            if dest.exists():
                stem, suffix = src.stem, src.suffix
                dest = imp / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
            shutil.move(str(src), str(dest))

            entry = _make_log_entry(
                source_filename=name,
                order_number=result["order_number"],
                customer=result["customer"],
                output_files=result["output_files"],
                status="success",
            )
            _append_log(cfg, entry)

            results.append({
                "source": name,
                "order": result["order_number"] or "—",
                "customer": result["customer"] or "—",
                "files": result["output_files"],
                "entry_id": entry["id"],
            })

        except Exception as exc:
            entry = _make_log_entry(
                source_filename=name,
                order_number=None,
                customer=None,
                output_files=[],
                status="error",
                error=str(exc),
            )
            _append_log(cfg, entry)
            errors.append({"source": name, "error": str(exc)})

    return JSONResponse({"results": results, "errors": errors})


@app.get("/api/imported")
def list_imported() -> JSONResponse:
    cfg = _get_cfg()
    entries = _read_log(cfg)
    return JSONResponse({"entries": entries})


class ReimportRequest(BaseModel):
    filename: str
    outputDir: Optional[str] = None


@app.post("/api/reimport")
def reimport_file(body: ReimportRequest) -> JSONResponse:
    from app import config as app_config
    cfg = _get_cfg()
    imp = app_config.imported_dir(cfg)

    name = Path(body.filename).name
    src = imp / name

    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado em 'Pedidos importados'")
    if src.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Extensão não permitida")

    output_path = (
        Path(body.outputDir).expanduser().resolve()
        if body.outputDir
        else Path(cfg["output_dir"]).expanduser().resolve()
    )

    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao criar diretório de saída: {exc}")

    try:
        result = _process_file(src, output_path)
        entry = _make_log_entry(
            source_filename=name,
            order_number=result["order_number"],
            customer=result["customer"],
            output_files=result["output_files"],
            status="success",
        )
        _append_log(cfg, entry)
        return JSONResponse({
            "source": name,
            "order": result["order_number"] or "—",
            "customer": result["customer"] or "—",
            "files": result["output_files"],
            "entry_id": entry["id"],
        })
    except Exception as exc:
        entry = _make_log_entry(
            source_filename=name,
            order_number=None,
            customer=None,
            output_files=[],
            status="error",
            error=str(exc),
        )
        _append_log(cfg, entry)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/download")
def download_file(path: str) -> FileResponse:
    file_path = Path(path).expanduser().resolve()
    if file_path.suffix.lower() != ".xlsx":
        raise HTTPException(status_code=403, detail="Apenas arquivos .xlsx podem ser baixados")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(
        str(file_path),
        filename=file_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/fs")
def browse_filesystem(path: str = "~") -> JSONResponse:
    try:
        p = Path(path).expanduser().resolve()
        while not p.exists() or not p.is_dir():
            parent = p.parent
            if parent == p:
                p = Path.home()
                break
            p = parent
        entries = sorted(
            [
                {"name": e.name, "path": str(e)}
                for e in p.iterdir()
                if e.is_dir() and not e.name.startswith(".")
            ],
            key=lambda x: x["name"].lower(),
        )
        parent = str(p.parent) if p != p.parent else None
        return JSONResponse({"current": str(p), "parent": parent, "entries": entries})
    except PermissionError:
        return JSONResponse({"error": "Sem permissão para acessar este diretório"}, status_code=403)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# Keep /api/process for backward compat (drag-drop upload flow)
@app.post("/api/process")
async def process_files(
    files: List[UploadFile] = File(...),
    output_dir: str = Form("output"),
) -> JSONResponse:
    from app.exporters.erp_exporter import ERPExporter
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    output_path = Path(output_dir).expanduser().resolve()
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return JSONResponse({
            "results": [],
            "errors": [{"source": "—", "error": f"Pasta inválida: {exc}"}],
        })

    exporter = ERPExporter()
    results = []
    errors = []

    for upload in files:
        filename = upload.filename or "arquivo"
        ext = Path(filename).suffix.lower()

        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"source": filename, "error": f"Tipo de arquivo não suportado: {ext}"})
            continue

        raw = await upload.read()

        if len(raw) > MAX_UPLOAD_BYTES:
            errors.append({
                "source": filename,
                "error": f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            })
            continue

        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)

            loaded = LoadedFile(path=tmp_path, extension=ext, raw=raw)
            order = process(loaded)

            if order:
                paths = exporter.export(order, str(output_path))
                results.append({
                    "source": filename,
                    "order": order.header.order_number or "—",
                    "files": [{"name": p.name, "path": str(p)} for p in paths],
                })
            else:
                errors.append({
                    "source": filename,
                    "error": "Formato não reconhecido ou pedido sem itens",
                })
        except Exception as exc:
            errors.append({"source": filename, "error": str(exc)})
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

    return JSONResponse({"results": results, "errors": errors})
