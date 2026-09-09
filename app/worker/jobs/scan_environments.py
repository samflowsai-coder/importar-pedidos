"""Scan job multi-ambiente — varre watch_dir de cada ambiente.

Para cada ambiente ativo:
1. Lista arquivos no `watch_dir` (filtra extensões .pdf/.xls/.xlsx)
2. Para cada arquivo:
   a. Calcula sha256 — se já existe em `imports.file_sha256` de QUALQUER
      ambiente (ativo ou não — ver `_already_imported`), skip (move pra
      `Pedidos importados/`).
   b. Roda pipeline (parse → normalize → validate). Falha vira import
      com status='error' e o arquivo vai pra `Pedidos importados/com_erro/`.
   c. Roteamento (`app/routing/ambiente.py`, atrás do interruptor de
      `roteamento_repo.modo()`): em 'ligado', o ambiente que grava pode
      divergir do ambiente varrido — ver `_decidir_ambiente`. Sem humano
      pra perguntar, um pedido que cai em 'perguntar' NÃO é importado: fica
      retido na pasta e vira uma linha em `roteamento_pendencia`.
   d. Insere em `imports` via `repo.insert_import`, dentro de
      `env_context.active_env()` do ambiente DECIDIDO (que é o varrido em
      'desligado'/'observando') com status='success' e portal_status='parsed'
      — fica esperando o operador commitar.
   e. Move arquivo para `Pedidos importados/` do ambiente VARRIDO — é de lá
      que o arquivo veio, e é lá que fica o histórico de entrada.

Idempotência: chave = sha256, único no Portal inteiro (não por ambiente).
Mesmo arquivo recolocado, na mesma pasta ou em outra, não duplica.

Erro processando um arquivo NÃO interrompe o scan — continua para o próximo.
"""
from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.ingestion.file_loader import LoadedFile
from app.observability.trace import new_trace_id, with_trace_id
from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router
from app.pipeline import process as pipeline_process
from app.utils.logger import logger

if TYPE_CHECKING:
    from app.models.order import Order
    from app.routing.ambiente import Decisao

VALID_EXTS = (".pdf", ".xls", ".xlsx")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_imported(sha: str) -> bool:
    """O sha é único no Portal inteiro, não por ambiente: com roteamento, o
    arquivo pode ter entrado numa empresa diferente da pasta em que está —
    inclusive um ambiente já desativado (foi importado; reimportar seria
    duplicata). Por isso varre TODOS os ambientes, não só os ativos."""
    for env in environments_repo.list_all():
        with router.env_connect(env["slug"]) as conn:
            row = conn.execute(
                "SELECT 1 FROM imports WHERE file_sha256 = ? LIMIT 1", (sha,)
            ).fetchone()
        if row is not None:
            return True
    return False


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


def _decidir_ambiente(order: Order) -> tuple[str, Decisao | None]:
    """(modo, Decisao|None). Em 'desligado' o roteador nem é chamado.

    Mesma regra de `app/web/server.py::_decidir_ambiente`, adaptada pro
    watcher: exceção do roteador NUNCA propaga daqui pra fora — blindar é
    decisão de quem chama, não do módulo puro (`app/routing/ambiente.py`).

    - 'observando': vira log e `decisao=None` — o arquivo é importado
      normalmente na pasta varrida, sem sombra. Evidência é importante, o
      pedido é mais.
    - 'ligado': vira um degrau 'perguntar' com a falha explicada. Sem humano
      para responder, `_process_file` trata isso como retenção — nunca cai
      de volta pro ambiente varrido em silêncio.
    """
    from app.routing import ambiente as routing

    modo = roteamento_repo.modo()
    if modo == roteamento_repo.DESLIGADO:
        return modo, None
    try:
        return modo, routing.ambiente_para(order, routing.deps_padrao())
    except Exception as exc:  # noqa: BLE001 — roteador não pode derrubar o scan nem decidir errado em silêncio
        logger.warning("scan.roteamento_falhou modo={} erro={!r}", modo, exc)
        if modo == roteamento_repo.LIGADO:
            return modo, routing.Decisao(
                env_slug=None,
                degrau="perguntar",
                explicacao=f"Ambiente não resolvido: falha ao consultar o roteador ({exc})",
            )
        return modo, None


def _process_file(env: dict, p: Path) -> None:
    """Processa um arquivo: parse + roteamento + insert + move."""
    sha = _sha256(p)
    watch_dir = Path(env["watch_dir"])
    if _already_imported(sha):
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

        modo, decisao = _decidir_ambiente(order)
        env_alvo = env
        if modo == roteamento_repo.LIGADO:
            if decisao.resolveu:
                env_alvo = environments_repo.get_by_slug(decisao.env_slug) or env
            else:
                # Sem humano para perguntar: retém. O arquivo NÃO se move e
                # NÃO é importado — fica na pasta, onde o operador pode
                # abri-lo pelo preview e responder.
                roteamento_repo.registrar_pendencia(
                    sha256=sha,
                    source_path=str(p),
                    env_scan_slug=env["slug"],
                    order_number=order.header.order_number,
                    customer_cnpj=order.header.customer_cnpj,
                    customer_name=order.header.customer_name,
                )
                logger.info(
                    "scan.retido_sem_ambiente env={} file={} motivo={}",
                    env["slug"], p.name, decisao.explicacao,
                )
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
            "environment_id": env_alvo["id"],
        }
        # Armadilha: `imports` mora em `app_state_<slug>.db`, e `db.connect()`
        # resolve o arquivo pelo CONTEXTVAR de `env_context`, não por um campo
        # no dict — por isso o bloco inteiro roda dentro de
        # `active_env(env_alvo)`, o ambiente ROTEADO, não o `env` varrido.
        # `_move_to_imported` continua usando `watch_dir` da pasta varrida
        # (arquivo veio de lá) — só a linha em `imports` muda de ambiente.
        try:
            with env_context.active_env(env_alvo["id"], env_alvo["slug"]):
                repo.insert_import(entry)
                if modo == roteamento_repo.OBSERVANDO and decisao is not None:
                    roteamento_repo.registrar_sombra(
                        import_id=import_id,
                        degrau=decisao.degrau,
                        env_sugerido=decisao.env_slug,
                        env_escolhido=env["slug"],
                    )
                logger.info(
                    "scan.imported env={} file={} order={} import_id={}",
                    env_alvo["slug"], p.name, order.header.order_number, import_id,
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
