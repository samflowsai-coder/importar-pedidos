"""Scan job multi-ambiente — varre watch_dir de cada ambiente.

Para cada ambiente ativo:
1. Lista arquivos no `watch_dir` (filtra extensões .pdf/.xls/.xlsx)
2. Para cada arquivo:
   a. Calcula sha256 — se já existe em `imports.file_sha256` de QUALQUER
      ambiente (ativo ou não — ver `_already_imported`), skip (move pra
      `Pedidos importados/`).
   b. Roda pipeline (parse → normalize → validate). Falha vira import
      com status='error' e o arquivo vai pra `Pedidos importados/com_erro/`.
   c. Roteamento (`app/routing/ambiente.py::decidir`, atrás do interruptor
      de `roteamento_repo.modo()`): em 'ligado', o ambiente que grava pode
      divergir do ambiente varrido. Sem humano pra perguntar, um pedido que
      não resolve (degrau 'perguntar', ou ambiente resolvido que não
      existe mais) NÃO é importado: fica retido na pasta e vira uma linha
      em `roteamento_pendencia` — reavaliada no máximo 1x/hora (ver
      `_PENDENCIA_REAVALIACAO_S`), nunca a cada ciclo de 30s.
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.ingestion.file_loader import LoadedFile
from app.observability.trace import new_trace_id, with_trace_id
from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router
from app.pipeline import process as pipeline_process
from app.utils.logger import logger

VALID_EXTS = (".pdf", ".xls", ".xlsx")

# Um arquivo retido (roteamento em 'ligado' sem resposta) NÃO some do disco
# nem de `roteamento_pendencia` — a cada ciclo de scan (30s) ele reaparece
# na varredura. Sem este limite, ele seria reparseado (pipeline inteiro) e
# reroteado (uma query Firebird por ambiente ativo, no degrau 2) a cada
# ciclo: 2.880 vezes por dia, para sempre — e arquivo retido é o estado
# ESPERADO em produção (a spec já prevê Authentic Feet e NBA caindo aqui).
# Uma hora corta isso para 24 reavaliações/dia e ainda garante que o
# arquivo "sai sozinho" da fila se uma resposta do operador em OUTRO pedido
# do mesmo cliente fizer a memória passar a resolvê-lo.
_PENDENCIA_REAVALIACAO_S = 3600


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


def _pendencia_ainda_recente(visto_em: str) -> bool:
    """`visto_em` é ISO 8601 (UTC) gravado por `roteamento_repo._now()`.
    `True` = viu esse arquivo há menos de `_PENDENCIA_REAVALIACAO_S` — não
    reavalia de novo agora. Formato inesperado nunca trava o scan: conta
    como "não recente" (reavalia), o lado seguro."""
    try:
        quando = datetime.fromisoformat(visto_em)
    except (TypeError, ValueError):
        return False
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=UTC)
    return (datetime.now(UTC) - quando) < timedelta(seconds=_PENDENCIA_REAVALIACAO_S)


def _process_file(env: dict, p: Path) -> None:
    """Processa um arquivo: parse + roteamento + insert + move."""
    sha = _sha256(p)
    watch_dir = Path(env["watch_dir"])
    if _already_imported(sha):
        # Ciclo fecha pro arquivo: se ele tinha uma pendência (ex.: o
        # operador resolveu pelo preview em vez de esperar o watcher), ela
        # deixou de descrever a realidade — sem isto a fila só cresce.
        roteamento_repo.limpar_pendencia(sha)
        logger.info(
            "scan.skip_duplicate sha={} env={} file={}",
            sha[:12], env["slug"], p.name,
        )
        _move_to_imported(p, watch_dir)
        return

    pendencia = roteamento_repo.buscar_pendencia(sha)
    if pendencia is not None and _pendencia_ainda_recente(pendencia["visto_em"]):
        # Retido há menos de uma hora: só toca o carimbo (visto_em/visto_vezes)
        # — nada de reparsear nem requeimar Firebird a cada ciclo de 30s.
        roteamento_repo.registrar_pendencia(
            sha256=sha,
            source_path=str(p),
            env_scan_slug=env["slug"],
            order_number=pendencia["order_number"],
            customer_cnpj=pendencia["customer_cnpj"],
            customer_name=pendencia["customer_name"],
            motivo=pendencia["motivo"],
        )
        logger.info(
            "scan.pendencia_ainda_recente sha={} file={} visto_vezes={}",
            sha[:12], p.name, pendencia["visto_vezes"] + 1,
        )
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

        from app.routing import ambiente as routing

        modo, decisao = routing.decidir(order, origem="scan")
        env_alvo = env
        if modo == roteamento_repo.LIGADO:
            # Invariante de `decidir()`: em 'ligado' o retorno nunca é
            # `(modo, None)` — só varia entre Decisao resolvida e não
            # resolvida. Estreita o tipo explicitamente em vez de confiar
            # nisso por acaso.
            assert decisao is not None, "decidir() com modo=ligado nunca devolve Decisao=None"

            env_alvo = environments_repo.get_by_slug(decisao.env_slug) if decisao.resolveu else None
            if env_alvo is None:
                # Sem humano para perguntar: retém. O arquivo NÃO se move e
                # NÃO é importado — fica na pasta, onde o operador pode
                # abri-lo pelo preview e responder.
                #
                # `decisao.resolveu` e `env_alvo is None` juntos só acontecem
                # se o slug decidido não corresponder a ambiente nenhum —
                # inalcançável hoje (nenhum degrau devolve slug fantasma, e
                # não há hard-delete de `environments`), mas não é garantido
                # pelo TIPO: um `Deps` customizado, um rename de slug futuro
                # ou uma tool de exclusão quebrariam essa invariante em
                # silêncio se este branch não existisse.
                if decisao.resolveu:
                    motivo = (
                        f"Ambiente '{decisao.env_slug}' resolvido pelo roteador, mas "
                        f"não existe (ou foi removido) — retido em vez de importar na "
                        f"pasta varrida."
                    )
                else:
                    motivo = decisao.explicacao
                roteamento_repo.registrar_pendencia(
                    sha256=sha,
                    source_path=str(p),
                    env_scan_slug=env["slug"],
                    order_number=order.header.order_number,
                    customer_cnpj=order.header.customer_cnpj,
                    customer_name=order.header.customer_name,
                    motivo=motivo,
                )
                logger.info(
                    "scan.retido_sem_ambiente env={} file={} motivo={}",
                    env["slug"], p.name, motivo,
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
                # Fecha o ciclo também quando é a PRÓPRIA reavaliação do
                # watcher que resolve (ex.: a memória aprendeu por outro
                # pedido do mesmo cliente) — não só quando o operador
                # importa manualmente pelo preview (ramo skip_duplicate,
                # acima). Sem isto, um arquivo que já foi importado ficaria
                # "pendente" pra sempre na fila. No-op se não havia linha.
                roteamento_repo.limpar_pendencia(sha)
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
