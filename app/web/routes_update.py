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


_RUNNING_STATUSES = {"apply_requested", "in_progress"}
_TERMINAL_STATUSES = {"succeeded", "rolled_back", "rollback_failed"}
# Um updater VIVO segura o update.lock durante toda a aplicação. Logo, status
# "rodando" SEM lock e com started_at antigo = o updater morreu sem escrever
# status terminal (ex.: o watchdog já removeu o lock órfão). Bem acima da janela
# de criação do lock (~1-3s) e de qualquer apply real (~1-5min holds the lock).
_STALE_RUNNING_SECONDS = 180


def _applied_info() -> dict:
    p = _data_dir() / "applied_update.json"
    if p.exists():
        try:
            import json
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _current_version() -> str:
    return _applied_info().get("version", "desconhecida")


def _reject_if_update_running(s: dict | None = None) -> None:
    """Guarda de ESTADO (além do `is_locked`).

    `update.lock` só existe depois que o processo updater arranca — o que
    acontece de forma ASSÍNCRONA, ~1-3s após `/apply` disparar
    `schtasks /run`. Nessa janela, `status.json` já está `apply_requested`
    mas ainda não há lock no disco. Sem esta checagem, um 2º `/upload` (ou
    `/apply`) nessa janela passaria pelo `is_locked` e corromperia o staging
    do pacote que está sendo aplicado."""
    if s is None:
        s = state.read_status(updates_dir())
    if s.get("status") in _RUNNING_STATUSES:
        raise HTTPException(409, "há um update em andamento")


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
    info = _applied_info()
    s.setdefault("current_version", info.get("version", "desconhecida"))
    applied_at = info.get("applied_at")
    if applied_at is not None:
        s.setdefault("applied_at", applied_at)
    return s


@router.post("/upload")
async def upload(file: UploadFile = File(...), _=Depends(require_admin)):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "envie um arquivo .zip")
    if state.is_locked(updates_dir()):
        raise HTTPException(409, "há um update em andamento")
    _reject_if_update_running()
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
    _reject_if_update_running(s)
    if s.get("status") != "staged" or s.get("update_id") != body.update_id:
        raise HTTPException(404, "update_id não corresponde ao pacote staged")
    state.write_status(updates_dir(), status="apply_requested", started_at=time.time())
    if not _start_updater_task():
        state.write_status(updates_dir(), status="staged")  # reverte
        raise HTTPException(409, "serviço de update não configurado — rode setup-service.bat no servidor")
    return {"update_id": body.update_id, "status": "apply_requested"}


def _running_but_dead(s: dict) -> bool:
    """status diz 'rodando' mas — chamado DEPOIS do guard de is_locked, então sem
    lock — o started_at é antigo → o updater morreu sem escrever status terminal.
    Só nesse caso /dismiss pode destravar um status 'rodando' (senão respeita o
    update em andamento)."""
    if s.get("status") not in _RUNNING_STATUSES:
        return False
    started = s.get("started_at")
    if not isinstance(started, (int, float)):
        return False
    return (time.time() - started) > _STALE_RUNNING_SECONDS


@router.post("/dismiss")
def dismiss(_=Depends(require_admin)):
    """Dispensa um status TERMINAL (succeeded/rolled_back/rollback_failed) — ou um
    'rodando' comprovadamente MORTO (sem lock + started_at antigo) — de volta pra
    idle, liberando a tela de upload sem apagar o status.json na mão. Recusa (409)
    se há um update de fato em andamento; em idle/staged é no-op (não mexe no
    pacote staged)."""
    if state.is_locked(updates_dir()):
        raise HTTPException(409, "há um update em andamento")
    s = state.read_status(updates_dir())
    dead = _running_but_dead(s)
    if s.get("status") in _RUNNING_STATUSES and not dead:
        raise HTTPException(409, "há um update em andamento")
    if s.get("status") in _TERMINAL_STATUSES or dead:
        state.clear_status(updates_dir())
    # devolve o estado REAL resultante (idle se limpou; senão o atual inalterado,
    # ex.: staged não é mexido) — a resposta nunca mente sobre o disco.
    out = state.read_status(updates_dir())
    out.setdefault("current_version", _current_version())
    return out
