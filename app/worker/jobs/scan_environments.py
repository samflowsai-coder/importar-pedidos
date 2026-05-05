"""Scan job multi-ambiente — varre watch_dir de cada ambiente.

Para cada ambiente ativo:
1. Lista arquivos no `watch_dir` (filtra extensões .pdf/.xls/.xlsx)
2. Para cada arquivo:
   a. Calcula sha256 — se já existe em `imports.file_sha256`, skip (move
      pra `Pedidos importados/`).
   b. Roda pipeline (parse → normalize → validate). Falha vira import
      com status='error' e o arquivo vai pra `Pedidos importados/com_erro/`.
   c. Insere em `imports` via `repo.insert_import` (que pega
      environment_id do contextvar) com status='success' e
      portal_status='parsed' — fica esperando o operador commitar.
   d. Move arquivo para `Pedidos importados/` do env.

Idempotência: chave = (environment_id, sha256). Mesmo arquivo recolocado
não duplica.

Erro processando um arquivo NÃO interrompe o scan — continua para o próximo.
"""
from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from app.ingestion.file_loader import LoadedFile
from app.observability.trace import new_trace_id, with_trace_id
from app.persistence import context as env_context
from app.persistence import environments_repo, repo, router
from app.pipeline import process as pipeline_process
from app.utils.logger import logger

VALID_EXTS = (".pdf", ".xls", ".xlsx")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_imported(slug: str, sha: str) -> bool:
    with router.env_connect(slug) as conn:
        row = conn.execute(
            "SELECT 1 FROM imports WHERE file_sha256 = ? LIMIT 1", (sha,)
        ).fetchone()
    return row is not None


def _candidate_files(watch_dir: Path) -> list[Path]:
    if not watch_dir.is_dir():
        return []
    return [
        p for p in sorted(watch_dir.iterdir())
        if p.is_file() and p.suffix.lower() in VALID_EXTS
    ]


def _move_to_imported(p: Path, watch_dir: Path, *, errored: bool = False) -> Path:
    sub = watch_dir / "Pedidos importados"
    if errored:
        sub = sub / "com_erro"
    sub.mkdir(parents=True, exist_ok=True)
    dst = sub / p.name
    if dst.exists():
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        dst = sub / f"{dst.stem}.{ts}{dst.suffix}"
    shutil.move(str(p), str(dst))
    return dst


def _process_file(env: dict, p: Path) -> None:
    """Processa um arquivo: parse + insert + move."""
    sha = _sha256(p)
    watch_dir = Path(env["watch_dir"])
    if _already_imported(env["slug"], sha):
        logger.info(
            "scan.skip_duplicate sha={} env={} file={}",
            sha[:12], env["slug"], p.name,
        )
        _move_to_imported(p, watch_dir)
        return

    raw = p.read_bytes()
    loaded = LoadedFile(path=p, extension=p.suffix.lower(), raw=raw)

    trace_id = new_trace_id()
    with with_trace_id(trace_id):
        order = None
        try:
            order = pipeline_process(loaded)
        except Exception as exc:
            logger.error(
                "scan.parse_error env={} file={} error={!r}",
                env["slug"], p.name, exc,
            )

        import_id = str(uuid.uuid4())
        if order is None:
            entry = {
                "id": import_id,
                "source_filename": p.name,
                "imported_at": datetime.now().isoformat(timespec="seconds"),
                "status": "error",
                "error": "pipeline retornou None — formato não reconhecido",
                "trace_id": trace_id,
                "file_sha256": sha,
            }
            try:
                repo.insert_import(entry)
            except Exception as e:
                logger.error("scan.insert_error_failed env={} {!r}", env["slug"], e)
            _move_to_imported(p, watch_dir, errored=True)
            return

        entry = {
            "id": import_id,
            "source_filename": p.name,
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": order.header.order_number,
            "customer_cnpj": order.header.customer_cnpj,
            "customer_name": order.header.customer_name,
            "snapshot": order.model_dump(),
            "status": "success",
            "portal_status": "parsed",
            "trace_id": trace_id,
            "file_sha256": sha,
        }
        try:
            repo.insert_import(entry)
            logger.info(
                "scan.imported env={} file={} order={} import_id={}",
                env["slug"], p.name, order.header.order_number, import_id,
            )
            _move_to_imported(p, watch_dir)
        except Exception as e:
            logger.error("scan.insert_failed env={} file={} {!r}", env["slug"], p.name, e)


def run_scan() -> None:
    """Uma passada: para cada ambiente ativo, processa arquivos novos."""
    for slug in router.list_env_slugs():
        env = environments_repo.get_by_slug(slug)
        if env is None:
            continue
        with env_context.active_env(env["id"], env["slug"]):
            watch_dir = Path(env["watch_dir"])
            for p in _candidate_files(watch_dir):
                try:
                    _process_file(env, p)
                except Exception as exc:
                    logger.error(
                        "scan.fatal env={} file={} {!r}",
                        env["slug"], p.name, exc,
                    )
