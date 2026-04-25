from __future__ import annotations

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

from app.web.preview_cache import PreviewConsumedError, PreviewNotFoundError, get_cache

STATIC_DIR = Path(__file__).parent / "static"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}
MAX_PAGE_SIZE = 500

app = FastAPI(title="Portal de Pedidos", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Internal helpers ──────────────────────────────────────────────────────

def _get_cfg() -> dict:
    from app import config as app_config
    return app_config.load()


def _append_log(cfg: dict, entry: dict) -> None:
    """Persist to SQLite. `cfg` kept for signature compatibility."""
    del cfg  # unused — repo resolves db path on its own
    from app.persistence import repo
    repo.insert_import(entry)


def _make_log_entry(
    source_filename: str,
    order_number: Optional[str],
    customer: Optional[str],
    output_files: List[dict],
    status: str,
    error: Optional[str] = None,
    snapshot: Optional[dict] = None,
    fire_codigo: Optional[int] = None,
    db_result: Optional[dict] = None,
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
        "snapshot": snapshot,
        "fire_codigo": fire_codigo,
        "db_result": db_result,
    }


# ── Preview helpers ───────────────────────────────────────────────────────

def _build_preview_payload(preview_id: str, source_filename: str, order, check: Optional[dict] = None) -> dict:
    """Shape an Order for the preview modal: items, per-store groups, totals, product check."""
    items = []
    for it in order.items:
        items.append({
            "description": it.description,
            "product_code": it.product_code,
            "ean": it.ean,
            "quantity": it.quantity,
            "unit_price": it.unit_price,
            "total_price": it.total_price,
            "obs": it.obs,
            "delivery_date": it.delivery_date,
            "delivery_cnpj": it.delivery_cnpj,
            "delivery_name": it.delivery_name,
        })

    groups: dict[str, dict] = {}
    for it in order.items:
        if it.delivery_cnpj and it.delivery_cnpj != order.header.customer_cnpj:
            key = f"cnpj:{it.delivery_cnpj}"
            label = it.delivery_name or it.delivery_cnpj
        elif it.delivery_name:
            key = f"name:{it.delivery_name}"
            label = it.delivery_name
        else:
            key = "default"
            label = order.header.customer_name or "Pedido"
        g = groups.setdefault(key, {
            "key": key,
            "label": label,
            "cnpj": it.delivery_cnpj,
            "items_count": 0,
            "total_qty": 0.0,
            "total_value": 0.0,
        })
        g["items_count"] += 1
        g["total_qty"] += float(it.quantity or 0)
        g["total_value"] += float(it.total_price or (it.quantity or 0) * (it.unit_price or 0))

    totals = {
        "items_count": len(order.items),
        "total_qty": sum(float(it.quantity or 0) for it in order.items),
        "total_value": sum(
            float(it.total_price or (it.quantity or 0) * (it.unit_price or 0))
            for it in order.items
        ),
    }

    return {
        "preview_id": preview_id,
        "source_filename": source_filename,
        "header": {
            "order_number": order.header.order_number,
            "issue_date": order.header.issue_date,
            "customer_name": order.header.customer_name,
            "customer_cnpj": order.header.customer_cnpj,
        },
        "items": items,
        "groups": sorted(groups.values(), key=lambda g: g["label"] or ""),
        "totals": totals,
        "check": check,
    }


def _run_exporters(order, output_path: Path) -> dict:
    """Execute XLSX + Firebird exporters per export_mode; return summary dict."""
    from app import config as app_config
    from app.exporters.erp_exporter import ERPExporter
    from app.exporters.firebird_exporter import FirebirdExporter

    cfg = app_config.load()
    export_mode = cfg.get("export_mode", "xlsx")

    output_files: List[dict] = []
    db_result_dict: Optional[dict] = None
    fire_codigo: Optional[int] = None

    if export_mode in ("xlsx", "both"):
        exporter = ERPExporter()
        paths = exporter.export(order, str(output_path))
        output_files = [{"name": p.name, "path": str(p)} for p in paths]

    if export_mode in ("db", "both"):
        db_exp = FirebirdExporter()
        db_result = db_exp.export(order)
        db_result_dict = db_result.to_dict()
        fire_codigo = db_result.fire_codigo

    return {
        "output_files": output_files,
        "db_result": db_result_dict,
        "fire_codigo": fire_codigo,
    }


def _process_file(file_path: Path, output_path: Path) -> dict:
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    ext = file_path.suffix.lower()
    raw = file_path.read_bytes()
    loaded = LoadedFile(path=file_path, extension=ext, raw=raw)
    order = process(loaded)
    if not order:
        raise ValueError("Formato não reconhecido ou pedido sem itens")

    exported = _run_exporters(order, output_path)

    return {
        "order_number": order.header.order_number,
        "customer": order.header.customer_name,
        "snapshot": order.model_dump(),
        **exported,
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
    return JSONResponse({
        "watchDir": cfg["watch_dir"],
        "outputDir": cfg["output_dir"],
        "exportMode": cfg.get("export_mode", "xlsx"),
        "firebirdConfigured": bool(os.environ.get("FB_DATABASE")),
    })


class ConfigUpdate(BaseModel):
    watchDir: Optional[str] = None
    outputDir: Optional[str] = None
    exportMode: Optional[str] = None


@app.post("/api/config")
def update_config(body: ConfigUpdate) -> JSONResponse:
    from app import config as app_config
    watch_dir = str(Path(body.watchDir).expanduser().resolve()) if body.watchDir else None
    output_dir = str(Path(body.outputDir).expanduser().resolve()) if body.outputDir else None
    cfg = app_config.save(watch_dir=watch_dir, output_dir=output_dir, export_mode=body.exportMode)
    return JSONResponse({
        "watchDir": cfg["watch_dir"],
        "outputDir": cfg["output_dir"],
        "exportMode": cfg.get("export_mode", "xlsx"),
    })


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
                snapshot=result.get("snapshot"),
                fire_codigo=result.get("fire_codigo"),
                db_result=result.get("db_result"),
            )
            _append_log(cfg, entry)

            results.append({
                "source": name,
                "order": result["order_number"] or "—",
                "customer": result["customer"] or "—",
                "files": result["output_files"],
                "fire_codigo": result.get("fire_codigo"),
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
def list_imported(
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    portal_status: Optional[str] = None,
    production_status: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> JSONResponse:
    from app.persistence import repo
    entries = repo.list_imports(
        limit=limit,
        offset=offset,
        status=status,
        portal_status=portal_status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    total = repo.count_imports(
        status=status,
        portal_status=portal_status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    return JSONResponse({"entries": entries, "total": total, "limit": limit, "offset": offset})


@app.get("/api/imported/{import_id}")
def get_imported(import_id: str) -> JSONResponse:
    from app.persistence import repo
    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Importação não encontrada")
    audit = repo.list_audit(import_id)
    return JSONResponse({"entry": entry, "audit": audit})


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
            snapshot=result.get("snapshot"),
            fire_codigo=result.get("fire_codigo"),
            db_result=result.get("db_result"),
        )
        _append_log(cfg, entry)
        return JSONResponse({
            "source": name,
            "order": result["order_number"] or "—",
            "customer": result["customer"] or "—",
            "files": result["output_files"],
            "fire_codigo": result.get("fire_codigo"),
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


# ── Preview → Commit flow ─────────────────────────────────────────────────

@app.post("/api/preview")
async def preview_file(file: UploadFile = File(...)) -> JSONResponse:
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    filename = file.filename or "arquivo"
    ext = Path(filename).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {ext}")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)

        loaded = LoadedFile(path=tmp_path, extension=ext, raw=raw)
        order = process(loaded)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()

    if not order:
        raise HTTPException(
            status_code=422,
            detail="Formato não reconhecido ou pedido sem itens",
        )

    from app.erp.product_check import check_order
    check = check_order(order)
    entry = get_cache().put(
        order=order, source_filename=filename, source_bytes=raw, source_ext=ext, check=check,
    )
    payload = _build_preview_payload(entry.preview_id, filename, order, check)
    return JSONResponse(payload)


class PreviewPendingRequest(BaseModel):
    filename: str


@app.post("/api/preview-pending")
def preview_pending(body: PreviewPendingRequest) -> JSONResponse:
    """Preview a file already in the watch folder (no upload)."""
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    cfg = _get_cfg()
    watch = Path(cfg["watch_dir"])

    name = Path(body.filename).name  # strip path components
    src = watch / name
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado na pasta de entrada")
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {ext}")
    if src.stat().st_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo excede o limite")

    raw = src.read_bytes()
    loaded = LoadedFile(path=src, extension=ext, raw=raw)
    order = process(loaded)

    if not order:
        raise HTTPException(status_code=422, detail="Formato não reconhecido ou pedido sem itens")

    from app.erp.product_check import check_order
    check = check_order(order)
    entry = get_cache().put(
        order=order,
        source_filename=name,
        source_bytes=raw,
        source_ext=ext,
        source_path=str(src),
        check=check,
    )
    payload = _build_preview_payload(entry.preview_id, name, order, check)
    return JSONResponse(payload)


class CommitRequest(BaseModel):
    preview_id: str


@app.post("/api/commit")
def commit_preview(body: CommitRequest) -> JSONResponse:
    """Salva o pedido no portal como 'em revisão'. NÃO grava no Fire.
    O usuário revisa o match na aba Pedidos e só depois clica em 'Cadastrar no Fire'.
    """
    cfg = _get_cfg()
    try:
        entry = get_cache().consume(body.preview_id)
    except PreviewNotFoundError:
        raise HTTPException(status_code=404, detail="Preview expirado ou inexistente")
    except PreviewConsumedError:
        raise HTTPException(status_code=409, detail="Preview já foi importado")

    order = entry.order

    log_entry = _make_log_entry(
        source_filename=entry.source_filename,
        order_number=order.header.order_number,
        customer=order.header.customer_name,
        output_files=[],
        status="success",
        snapshot=order.model_dump(),
    )
    log_entry["portal_status"] = "parsed"
    log_entry["check"] = entry.check

    # DB first — if this fails, the file stays in the watch folder and user
    # can retry without losing the original document.
    from app.persistence import repo
    repo.insert_import(log_entry)
    repo.append_audit(
        log_entry["id"],
        "imported_to_portal",
        {
            "source": "preview_commit",
            "items": len(order.items),
            "from_watch": entry.source_path is not None,
            "check": entry.check.get("summary") if entry.check else None,
        },
    )

    # Only move the source after persistence succeeded.
    if entry.source_path:
        from app import config as app_config
        src = Path(entry.source_path)
        if src.exists():
            imp = app_config.imported_dir(cfg)
            imp.mkdir(parents=True, exist_ok=True)
            dest = imp / src.name
            if dest.exists():
                stem, suffix = src.stem, src.suffix
                dest = imp / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
            shutil.move(str(src), str(dest))

    return JSONResponse({
        "entry_id": log_entry["id"],
        "order": order.header.order_number or "—",
        "customer": order.header.customer_name or "—",
        "portal_status": "parsed",
    })


# ── Per-order actions ───────────────────────────────────────────────────

class _FireSendOutcome:
    """Internal result of _send_one_to_fire. HTTP layer translates to status."""
    __slots__ = ("ok", "reason", "http_status", "fire_codigo", "items_inserted", "detail")

    def __init__(
        self,
        ok: bool,
        reason: Optional[str] = None,
        http_status: int = 200,
        fire_codigo: Optional[int] = None,
        items_inserted: int = 0,
        detail: Optional[str] = None,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.http_status = http_status
        self.fire_codigo = fire_codigo
        self.items_inserted = items_inserted
        self.detail = detail


def _send_one_to_fire(import_id: str, cfg: dict) -> _FireSendOutcome:
    """Insert a parsed order into Fire. Returns structured outcome (no HTTP exceptions)
    so batch callers can aggregate per-item results."""
    from app.exporters.firebird_exporter import FirebirdExporter
    from app.persistence import repo
    from app.models.order import Order

    entry = repo.get_import(import_id)
    if entry is None:
        return _FireSendOutcome(False, reason="not_found", http_status=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        return _FireSendOutcome(
            False,
            reason="wrong_status",
            http_status=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        return _FireSendOutcome(
            False, reason="no_snapshot", http_status=422, detail="Snapshot do pedido indisponível"
        )

    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        return _FireSendOutcome(
            False, reason="invalid_snapshot", http_status=422, detail=f"Snapshot inválido: {exc}"
        )

    output_path = Path(cfg["output_dir"]).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    export_mode = cfg.get("export_mode", "xlsx")
    output_files: list[dict] = []
    if export_mode in ("xlsx", "both"):
        from app.exporters.erp_exporter import ERPExporter
        paths = ERPExporter().export(order, str(output_path))
        output_files = [{"name": p.name, "path": str(p)} for p in paths]

    result = FirebirdExporter().export(order)
    db_result = result.to_dict()

    if result.skipped or result.fire_codigo is None:
        repo.append_audit(
            import_id,
            "send_to_fire_failed",
            {"skip_reason": result.skip_reason, "items_inserted": result.items_inserted},
        )
        return _FireSendOutcome(
            False,
            reason=result.skip_reason or "no_fire_codigo",
            http_status=409,
            detail=f"Fire rejeitou o pedido: {result.skip_reason or 'sem fire_codigo'}",
        )

    now = datetime.now().isoformat(timespec="seconds")
    updated = dict(entry)
    updated["portal_status"] = "sent_to_fire"
    updated["sent_to_fire_at"] = now
    updated["fire_codigo"] = result.fire_codigo
    updated["db_result"] = db_result
    updated["output_files"] = output_files or entry.get("output_files") or []
    repo.insert_import(updated)
    repo.append_audit(
        import_id,
        "sent_to_fire",
        {"fire_codigo": result.fire_codigo, "items_inserted": result.items_inserted},
    )

    return _FireSendOutcome(
        True,
        fire_codigo=result.fire_codigo,
        items_inserted=result.items_inserted,
    )


@app.post("/api/imported/{import_id}/send-to-fire")
def send_to_fire(import_id: str) -> JSONResponse:
    cfg = _get_cfg()
    outcome = _send_one_to_fire(import_id, cfg)
    if not outcome.ok:
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    return JSONResponse({
        "entry_id": import_id,
        "fire_codigo": outcome.fire_codigo,
        "items_inserted": outcome.items_inserted,
        "portal_status": "sent_to_fire",
    })


class BatchSendRequest(BaseModel):
    ids: List[str]


@app.post("/api/batch/send-to-fire")
def batch_send_to_fire(body: BatchSendRequest) -> JSONResponse:
    """Send multiple parsed orders to Fire. Tolerates partial failures — each id is
    attempted independently; response lists per-id outcome."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="Lista de ids vazia")
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 pedidos por lote")

    cfg = _get_cfg()
    results: list[dict] = []
    ok_count = 0
    fail_count = 0
    for import_id in body.ids:
        outcome = _send_one_to_fire(import_id, cfg)
        if outcome.ok:
            ok_count += 1
            results.append({
                "id": import_id,
                "ok": True,
                "fire_codigo": outcome.fire_codigo,
                "items_inserted": outcome.items_inserted,
            })
        else:
            fail_count += 1
            results.append({
                "id": import_id,
                "ok": False,
                "reason": outcome.reason,
                "detail": outcome.detail,
            })

    return JSONResponse({
        "total": len(body.ids),
        "ok": ok_count,
        "failed": fail_count,
        "results": results,
    })


class CancelRequest(BaseModel):
    reason: Optional[str] = None


@app.post("/api/imported/{import_id}/cancel")
def cancel_import(import_id: str, body: CancelRequest | None = None) -> JSONResponse:
    from app.persistence import repo
    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") == "sent_to_fire":
        raise HTTPException(status_code=409, detail="Pedido já foi enviado ao Fire — não pode ser cancelado pelo portal")

    updated = dict(entry)
    updated["portal_status"] = "cancelled"
    repo.insert_import(updated)
    repo.append_audit(
        import_id,
        "cancelled",
        {"reason": (body.reason if body else None)},
    )
    return JSONResponse({"entry_id": import_id, "portal_status": "cancelled"})


@app.get("/api/imported/{import_id}/preview")
def rehydrate_preview(import_id: str) -> JSONResponse:
    """Rebuild the preview payload for a stored order (for the review modal)."""
    from app.persistence import repo
    from app.models.order import Order

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc

    check = entry.get("check")
    payload = _build_preview_payload(import_id, entry.get("source_filename", ""), order, check)
    payload["portal_status"] = entry.get("portal_status")
    payload["fire_codigo"] = entry.get("fire_codigo")
    return JSONResponse(payload)


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
