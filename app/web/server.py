from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"

# Max upload size: 50 MB per file
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Extensions allowed for upload
ALLOWED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}

app = FastAPI(title="Importar Pedidos", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "importar-pedidos"})


@app.get("/api/config")
def config() -> JSONResponse:
    default_output = str(Path(os.getcwd()) / "output")
    return JSONResponse({"defaultOutputDir": default_output})


@app.post("/api/process")
async def process_files(
    files: List[UploadFile] = File(...),
    output_dir: str = Form("output"),
) -> JSONResponse:
    from app.exporters.erp_exporter import ERPExporter
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    # Validate output directory: must be absolute or resolve safely
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

        # Server-side extension validation
        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"source": filename, "error": f"Tipo de arquivo não suportado: {ext}"})
            continue

        raw = await upload.read()

        # File size guard
        if len(raw) > MAX_UPLOAD_BYTES:
            errors.append({
                "source": filename,
                "error": f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024*1024)} MB",
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


@app.get("/api/download")
def download_file(path: str) -> FileResponse:
    file_path = Path(path).expanduser().resolve()

    # Only serve .xlsx files — nothing else
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
        # Walk up until we land on an existing directory; fall back to home
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
