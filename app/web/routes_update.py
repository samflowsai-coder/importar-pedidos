# app/web/routes_update.py
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.updates import package, state
from app.web.auth import require_admin

router = APIRouter(prefix="/api/admin/update", tags=["admin", "update"])

MAX_PACKAGE_BYTES = 100 * 1024 * 1024
_UPDATER_TASK = "PortalPedidosUpdater"


def _app_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _data_dir() -> Path:
    return Path(os.environ.get("APP_DATA_DIR") or (_app_dir() / "data"))


def updates_dir() -> Path:
    return _data_dir() / "updates"


def staging_dir() -> Path:
    return updates_dir() / "staging"


def _current_version() -> str:
    p = _data_dir() / "applied_update.json"
    if p.exists():
        try:
            import json
            return json.loads(p.read_text())["version"]
        except Exception:
            pass
    return "desconhecida"


def _start_updater_task() -> bool:
    """Dispara a task one-shot. Retorna False se ela não existe (não configurada)."""
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", _UPDATER_TASK],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


@router.get("/status")
def status(_=Depends(require_admin)):
    s = state.read_status(updates_dir())
    s.setdefault("current_version", _current_version())
    return s


@router.post("/upload")
async def upload(file: UploadFile = File(...), _=Depends(require_admin)):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "envie um arquivo .zip")
    if state.is_locked(updates_dir()):
        raise HTTPException(409, "há um update em andamento")
    fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_name)
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PACKAGE_BYTES:
                    raise HTTPException(413, "pacote excede o limite de 100MB")
                out.write(chunk)
        update_id = uuid.uuid4().hex[:12]
        sd = staging_dir()
        try:
            res = package.validate_and_stage(
                tmp, sd, _app_dir() / "pyproject.toml", update_id=update_id
            )
        except package.PackageError as e:
            raise HTTPException(422, e.reason) from None
        # só depois de validar com sucesso: limpa staging anterior (um staged
        # por vez), preservando o pacote recém-validado
        import shutil

        for child in sd.iterdir():
            if child.name != res.update_id:
                shutil.rmtree(child, ignore_errors=True)
        state.write_status(updates_dir(), status="staged", update_id=res.update_id,
                           version=res.version, deps_changed=res.deps_changed)
        return {
            "update_id": res.update_id, "version": res.version,
            "git_commit": res.git_commit, "built_at": res.built_at,
            "files_count": res.files_count, "deps_changed": res.deps_changed,
            "current_version": _current_version(),
        }
    finally:
        tmp.unlink(missing_ok=True)


class ApplyBody(BaseModel):
    update_id: str


@router.post("/apply", status_code=202)
def apply(body: ApplyBody, _=Depends(require_admin)):
    if state.is_locked(updates_dir()):
        raise HTTPException(409, "há um update em andamento")
    s = state.read_status(updates_dir())
    if s.get("status") != "staged" or s.get("update_id") != body.update_id:
        raise HTTPException(404, "update_id não corresponde ao pacote staged")
    state.write_status(updates_dir(), status="apply_requested", started_at=time.time())
    if not _start_updater_task():
        state.write_status(updates_dir(), status="staged")  # reverte
        raise HTTPException(409, "serviço de update não configurado — rode setup-service.bat no servidor")
    return {"update_id": body.update_id, "status": "apply_requested"}
