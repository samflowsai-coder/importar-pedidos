from __future__ import annotations

import functools
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from app.observability import leituras
from app.observability.trace import current_trace_id, new_trace_id, with_trace_id
from app.persistence import context as env_context
from app.persistence import invites_repo, sessions_repo, users_repo
from app.reconcile.runner import Resultado, loop_periodico, reconciliar
from app.security import (
    PasswordTooLongError,
    WeakPasswordError,
    hash_needs_rehash,
    hash_password,
    verify_password,
)
from app.state import (
    EventSource,
    InvalidTransitionError,
    LifecycleEvent,
    transition,
)
from app.web.auth import (
    COOKIE_NAME,
    User,
    clear_env_cookie,
    clear_session_cookie,
    current_user,
    require_admin,
    require_user,
    set_session_cookie,
)
from app.web.dependencies.pedido import env_do_import_id, env_do_pedido
from app.web.middleware.rate_limit import check_and_consume

if TYPE_CHECKING:
    # Só para anotação de tipo — `app.routing.ambiente` fica fora do import
    # top-level do resto do módulo por convenção (imports pesados/roteamento
    # são locais nas funções que os usam).
    from app.routing.ambiente import Decisao


def _is_test_bypass() -> bool:
    return os.environ.get("TEST_AUTH_BYPASS", "").strip() == "1"


from app.web.preview_cache import (  # noqa: E402
    PreviewConsumedError,
    PreviewNotFoundError,
    get_cache,
)

STATIC_DIR = Path(__file__).parent / "static"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}
MAX_PAGE_SIZE = 500

app = FastAPI(title="Portal de Pedidos", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Inject UI-saved Firebird config into os.environ so connection.py picks it up
# without restart. Saved config takes precedence over .env (documented).
from app import firebird_config  # noqa: E402

firebird_config.apply_to_env()

# Multi-ambiente: middleware lê cookie `portal_env` e ativa env no contexto
# para todo o handler. Repos por-ambiente herdam o env via contextvar.
from app.web.middleware.environment import EnvironmentMiddleware  # noqa: E402

app.add_middleware(EnvironmentMiddleware)


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Impede o browser de cachear as PÁGINAS HTML. Os assets em /static seguem
    cacheáveis (o cache-bust deles é o ?v=N no <link>/<script>). Sem isso, após
    um deploy o browser servia o HTML antigo — que ainda apontava pro ?v velho —
    e as mudanças de UI só apareciam com hard-reload manual. `no-cache` faz o
    browser revalidar com o servidor (304 quando não mudou), então o HTML novo
    (com o ?v novo dos assets) é pego automaticamente."""
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Ponte FlowPCP (Modelo B/OVERLAY): push best-effort do pedido novo após o
# send-to-fire, só em ambientes com FlowPCP habilitado (MM).
from app.erp_eventos import vinculo as erp_vinculo  # noqa: E402
from app.integrations.flowpcp import produto_erp  # noqa: E402
from app.integrations.flowpcp.hook import push_new_order  # noqa: E402

# De-para de cliente intercompany: mostra no preview o cliente real que o
# Flow vai receber quando o pedido chegou no nome da revenda.
from app.integrations.flowpcp.intercompany import resolucao_para  # noqa: E402

# Multi-ambiente: traduz NoActiveEnvironmentError em 412 estruturado em vez
# de quebrar com 500. Esta é a mensagem que o operador realmente vê — com
# roteamento `ligado` não existe mais tela de seleção, então ela não manda
# "selecionar"; diz a verdade, que é a ação em si que exige uma empresa
# específica.
from app.persistence.context import NoActiveEnvironmentError  # noqa: E402


@app.exception_handler(NoActiveEnvironmentError)
async def _no_env_handler(_request, _exc):
    return JSONResponse(
        status_code=412,
        content={
            "detail": "Esta ação é de uma empresa específica — abra o pedido para agir nele.",
            "code": "no_active_env",
        },
    )


# Inbound webhooks (Fase 4 — Gestor de Produção status updates)
from app.web.webhooks import router as webhooks_router  # noqa: E402

app.include_router(webhooks_router)

# Multi-ambiente: rotas de seleção de ambiente.
from app.web.routes_env_select import router as env_select_router  # noqa: E402

app.include_router(env_select_router)

# Multi-ambiente: CRUD admin.
from app.web.routes_environments import router as environments_router  # noqa: E402

app.include_router(environments_router)

# Auto-update: rotas admin de upload/apply/status do pacote de atualização.
from app.web import routes_update  # noqa: E402

app.include_router(routes_update.router)

# Roteamento intercompany: interruptor de 3 estados + taxa de acerto.
from app.web.routes_roteamento import router as roteamento_router  # noqa: E402

app.include_router(roteamento_router)

# Lote semanal intercompany: config, fator com vigência, fechar agora.
from app.web.routes_lotes import router as lotes_router  # noqa: E402

app.include_router(lotes_router)


@app.on_event("startup")
def _iniciar_reconciliacao_periodica() -> None:
    """Reconciliação com o Fire, periódica (07h/12h/18h local) — registrada
    AQUI (processo web), não só no scheduler do worker.

    `scripts/setup-service.ps1` só registra `ui.py` como tarefa agendada no
    Windows do cliente — o worker (`python -m app.worker`) nunca roda lá. Um
    job registrado só no APScheduler do worker nunca dispararia em produção
    nesse deploy. `app/worker/scheduler.py` registra o MESMO runner com
    `CronTrigger` nos mesmos horários, para os deploys docker onde o worker
    existe; a trava de `app.reconcile.runner` evita trabalho dobrado quando
    os dois processos rodam ao mesmo tempo.

    Thread daemon: não impede o processo de encerrar, e nunca bloqueia o
    startup do FastAPI (só dorme e reconcilia, em loop, fora do event loop
    de request/response)."""
    threading.Thread(target=loop_periodico, name="reconcile-periodico", daemon=True).start()


@app.on_event("startup")
def _iniciar_erp_eventos() -> None:
    """SP1: vigia + drenagem + foto dos pedidos do ERP para o Flow, numa thread
    daemon do processo web (mesmo motivo da reconciliação acima). Só age em
    bases ligadas em /admin/ambientes; `PORTAL_ERP_VIGIA=0` desliga."""
    from app.erp_eventos import runner as erp_runner

    if erp_runner.habilitado():
        threading.Thread(target=erp_runner.loop, name="erp-eventos", daemon=True).start()


@app.on_event("startup")
def _iniciar_envio_flowpcp() -> None:
    """Fila do `/recebimento` e (com `PORTAL_FLOWPCP_DECISOES=1`) as decisões do
    Flow, numa thread daemon do processo web: no Windows do cliente o worker
    nunca roda (mesmo motivo da reconciliação acima). `PORTAL_FLOWPCP_ENVIO=0`
    desliga (deploy docker, onde o worker faz esse trabalho)."""
    from app.integrations.flowpcp import envio_portal

    if envio_portal.envio_habilitado():
        threading.Thread(target=envio_portal.loop, name="flowpcp-envio", daemon=True).start()


@app.on_event("startup")
def _iniciar_fechamento_lotes() -> None:
    """Fecha os lotes intercompany vencidos numa thread daemon do processo web
    (o worker não roda no Windows do cliente). `PORTAL_LOTES=0` desliga."""
    from app.consolidators import fechamento

    if fechamento.habilitado():
        threading.Thread(target=fechamento.loop, name="lotes-fechamento", daemon=True).start()


# ── Internal helpers ──────────────────────────────────────────────────────


def _get_cfg() -> dict:
    from app import config as app_config

    return app_config.load()


def _request_environment(request: Request) -> dict | None:
    return getattr(request.state, "environment", None)


def _trava_produto_ligada(env: dict | None) -> bool:
    """`produto_chave_modo` em `ligado` para esta ação (sem ambiente, o mais
    restrito — ver `preco_trava.env_trava_produto`)."""
    from app.erp import preco_trava, produto_trava

    return produto_trava.modo(preco_trava.env_trava_produto(env)) == "ligado"


def _produto_chave_modo(env: dict | None) -> str:
    """Modo da trava de produto para a TELA (mesma regra do portão: sem ambiente,
    o mais restrito). A tela só mostra o estado novo do item fora de `desligado`."""
    from app.erp import preco_trava, produto_trava

    return produto_trava.modo(preco_trava.env_trava_produto(env))


def _export_mode_atual() -> str:
    """`export_mode` que o envio vai usar (o portão de produto depende dele)."""
    return _get_cfg().get("export_mode", "xlsx")


def _produto_trava_preview(check: dict | None, env: dict | None) -> dict:
    """A decisão da trava de produto para o check guardado, com a MESMA regra do
    portão (`produto_trava.avaliar`, inclusive o export_mode): a tela espelha o
    backend em vez de reimplementar a regra. Só leitura."""
    from app.erp import preco_trava, produto_trava

    export_mode = "xlsx"
    try:
        export_mode = _export_mode_atual()
        env_trava = preco_trava.env_trava_produto(env)
        itens_check = (check or {}).get("items") or []
        if (
            produto_trava.modo(env_trava) != "desligado"
            and itens_check
            and not any(isinstance(it, dict) and "produto" in it for it in itens_check)
        ):
            # Check gravado antes do pacote da chave exata: nenhum item tem a
            # decisão de produto. A tela não trava (o operador confere de novo);
            # o portão do envio segue fechado, porque ele confere ao vivo.
            return {"bloqueado": False, "reason": "check_antigo", "export_mode": export_mode, "itens": []}
        tp = produto_trava.avaliar(check or {"items": []}, env=env_trava, export_mode=export_mode)
    except Exception as exc:  # noqa: BLE001 — a tela nunca quebra por isto, e trava
        from app.utils.logger import logger

        logger.warning(f"produto_trava do preview falhou ({type(exc).__name__}): {exc}")
        return {"bloqueado": True, "reason": "fire_indisponivel", "export_mode": export_mode, "itens": []}
    return {
        "bloqueado": tp.bloqueado,
        "reason": tp.reason,
        "export_mode": export_mode,
        "itens": tp.itens,
    }


def _cfg_para_env(env: dict) -> dict:
    """Cfg legado com as pastas DESTA empresa. Usado pelo lote agrupado, onde
    cada grupo escreve na pasta da sua empresa e não na do cookie."""
    out = dict(_get_cfg())
    out["watch_dir"] = env["watch_dir"]
    out["output_dir"] = env["output_dir"]
    return out


def _get_cfg_for_request(request: Request) -> dict:
    """Return legacy config, overridden by selected environment dirs when present."""
    env = _request_environment(request)
    return _get_cfg() if env is None else _cfg_para_env(env)


def _persist_ctx(env: dict[str, object] | None):
    """Context manager do bloco de persistência: ambiente DECLARADO quando
    há um (`env_slug` resolvido em `ligado` sem cookie), `nullcontext()`
    quando não há — o contexto já veio do middleware (cookie) ou nem existe
    (legado single-tenant). Uma fábrica, não uma instância guardada: um
    context manager baseado em `@contextmanager` só entra/sai UMA vez —
    reusar a mesma instância num segundo `with` (ex.: ramo de erro depois do
    ramo de sucesso) estoura."""
    from contextlib import nullcontext

    return env_context.active_env(env["id"], env["slug"]) if env is not None else nullcontext()


def _env_da_pasta(request: Request, env_slug: str | None) -> dict[str, object] | None:
    """De qual empresa é esta ação no fluxo da pasta de entrada
    (import/reimport/preview-pending).

    Aqui o identificador é diferente do resto do wiring de roteamento:
    `import_id` é único globalmente, **nome de arquivo não é** — dois
    arquivos podem ter o mesmo nome em pastas de empresas diferentes. Por
    isso a empresa não é derivada, é DECLARADA pelo `env_slug` do corpo.

    Fora de `'ligado'` o cookie manda e `env_slug` é ignorado — comportamento
    de hoje, intacto. Em `'ligado'` com cookie presente: `env_slug` AUSENTE
    ainda deixa o cookie mandar (idem); `env_slug` PRESENTE e DIVERGENTE do
    ambiente do cookie é conflito, não substituição silenciosa — nome de
    arquivo não é único entre empresas, então "ignorar a declaração" seria
    abrir o arquivo de outra empresa sem avisar. Em `'ligado'` sem cookie:
    ausente -> 400 nomeando o campo; não corresponde a empresa ATIVA -> 404.
    Nunca um default.
    """
    from app.persistence import environments_repo, roteamento_repo

    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        return None
    cookie_env = _request_environment(request)
    if cookie_env is not None:
        if env_slug and env_slug != cookie_env["slug"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"empresa declarada '{env_slug}', empresa selecionada "
                    f"'{cookie_env['slug']}' — recarregue a lista."
                ),
            )
        return None
    if not env_slug:
        raise HTTPException(
            status_code=400,
            detail=(
                "env_slug é obrigatório: roteamento ligado e nenhuma empresa "
                "selecionada (sem cookie)."
            ),
        )
    for env in environments_repo.list_active():
        if env["slug"] == env_slug:
            return env
    raise HTTPException(
        status_code=404, detail=f"Empresa '{env_slug}' não encontrada ou inativa."
    )


def _firebird_open_for_request(request: Request, conn_mgr):
    """Return a Firebird connection opener for selected env, or legacy fallback."""
    env = _request_environment(request)
    if env is not None:
        from app.persistence import environments_repo

        fb_cfg = environments_repo.to_fb_config(env)
        if not fb_cfg.get("path"):
            raise HTTPException(status_code=503, detail="FB_DATABASE não configurado")
        return lambda: conn_mgr.connect_with_config(fb_cfg)
    if not conn_mgr.is_configured():
        raise HTTPException(status_code=503, detail="FB_DATABASE não configurado")
    return conn_mgr.connect


def _append_log(cfg: dict, entry: dict) -> None:
    """Persist to SQLite. `cfg` kept for signature compatibility."""
    del cfg  # unused — repo resolves db path on its own
    from app.persistence import repo

    repo.insert_import(entry)


def _make_log_entry(
    source_filename: str,
    order_number: str | None,
    customer: str | None,
    output_files: list[dict],
    status: str,
    error: str | None = None,
    snapshot: dict | None = None,
    fire_codigo: int | None = None,
    db_result: dict | None = None,
    trace_id: str | None = None,
    file_sha256: str | None = None,
    original_path: str | None = None,
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
        "trace_id": trace_id or current_trace_id() or new_trace_id(),
        "file_sha256": file_sha256,
        "original_path": original_path,
    }


# ── Preview helpers ───────────────────────────────────────────────────────


def _decidir_ambiente(order) -> tuple[str, Decisao | None]:
    """(modo, Decisao|None) pro commit do preview. Contrato completo (modos,
    blindagem de exceção) mora em `app/routing/ambiente.py::decidir` — o
    scan_environments (watcher) usa a mesma função, com `origem="scan"`; só
    o prefixo do log muda. Este wrapper existe porque testes já importam
    `_decidir_ambiente` daqui (`tests/test_routing_wiring.py`)."""
    from app.routing import ambiente as routing

    return routing.decidir(order, origem="web")


def _roteamento_para_preview(order, *, entry=None) -> dict | None:
    """Bloco `roteamento` do payload do preview. `None` em 'desligado'.

    `entry` (o `PreviewEntry` do cache): guarda o modo e a decisão que o
    operador vai ver — o commit usa estes, não decide de novo.

    A UI nunca roteia em silêncio: mesmo quando a decisão é automática, o
    bloco viaja no preview para a faixa acima dos itens mostrar o ambiente
    de destino e o degrau que decidiu.

    `contraria`: o que o documento apontaria quando a regra decidiu outra
    empresa. `pode_trocar`: só em 'ligado' com decisão resolvida — a
    sugestão vem pré-selecionada e o operador pode trocar (o commit aceita
    `environment_slug`). Em 'observando' a sugestão é só informação.
    """
    from app.persistence import environments_repo, roteamento_repo

    modo, decisao = _decidir_ambiente(order)
    if entry is not None:
        entry.roteamento_modo = modo
        entry.roteamento_decisao = decisao
    if decisao is None:
        return None
    env = environments_repo.get_by_slug(decisao.env_slug) if decisao.env_slug else None
    contraria = environments_repo.get_by_slug(decisao.contraria) if decisao.contraria else None
    return {
        "modo": modo,
        "degrau": decisao.degrau,
        "env_slug": decisao.env_slug,
        "env_nome": env["name"] if env else None,
        "explicacao": decisao.explicacao,
        "divergiu_de": decisao.divergiu_de,
        "contraria": decisao.contraria,
        "contraria_nome": contraria["name"] if contraria else None,
        "precisa_escolher": modo == roteamento_repo.LIGADO and not decisao.resolveu,
        "pode_trocar": modo == roteamento_repo.LIGADO and decisao.resolveu,
        "opcoes": [{"slug": e["slug"], "name": e["name"]} for e in environments_repo.list_active()],
    }


def _sem_preco_dispensado_no_preview(env: dict | None) -> bool:
    """A tela não pede o ack de "sem preço no Fire" onde a planilha o dispensa.
    O MESMO predicado do portão (`preco_trava.sem_preco_dispensado`): o botão
    individual e o lote não podem divergir. `planilha=True`: a ação primária do
    preview é a planilha sempre que a integração direta está desligada, que é a
    única situação em que o predicado vale."""
    from app.erp import preco_trava

    return preco_trava.sem_preco_dispensado(env, planilha=True)


def _build_preview_payload(
    preview_id: str, source_filename: str, order, check: dict | None = None
) -> dict:
    """Shape an Order for the preview modal: items, per-store groups, totals, product check."""
    items = []
    for it in order.items:
        items.append(
            {
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
                "qtd_por_embalagem": it.qtd_por_embalagem,
            }
        )

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
        g = groups.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "cnpj": it.delivery_cnpj,
                "items_count": 0,
                "total_qty": 0.0,
                "total_value": 0.0,
            },
        )
        g["items_count"] += 1
        g["total_qty"] += float(it.quantity or 0)
        g["total_value"] += float(it.total_price or (it.quantity or 0) * (it.unit_price or 0))

    totals = {
        "items_count": len(order.items),
        "total_qty": sum(float(it.quantity or 0) for it in order.items),
        "total_value": sum(
            float(it.total_price or (it.quantity or 0) * (it.unit_price or 0)) for it in order.items
        ),
    }

    return {
        "preview_id": preview_id,
        "source_filename": source_filename,
        "header": {
            "order_number": order.header.order_number,
            "order_number_gerado": order.header.order_number_gerado,
            "issue_date": order.header.issue_date,
            "customer_name": order.header.customer_name,
            "customer_cnpj": order.header.customer_cnpj,
        },
        "items": items,
        "groups": sorted(groups.values(), key=lambda g: g["label"] or ""),
        "totals": totals,
        "check": check,
    }


def _run_exporters(order, output_path: Path, *, env: dict | None = None) -> dict:
    """Execute XLSX + Firebird exporters per export_mode; return summary dict.

    `env`: ambiente ativo (dict de environments_repo). Se passado, o
    FirebirdExporter usa as creds do ambiente em vez das env vars FB_*.
    """
    from app import config as app_config
    from app.exporters.erp_exporter import ERPExporter

    cfg = app_config.load()
    export_mode = cfg.get("export_mode", "xlsx")

    output_files: list[dict] = []
    db_result_dict: dict | None = None
    fire_codigo: int | None = None

    # Trava de preço ativa (observando/ligado): o XLSX não nasce na importação.
    # O pedido fica em revisão e só sai pelo preview → exportar/enviar, que
    # passa por `preco_trava.decidir_envio`.
    from app.erp import preco_trava

    # Trava de produto `ligado` vem antes da de preço: o XLSX só nasce na
    # revisão, depois que todo item tem produto exato no Fire.
    adiado: str | None = None
    if _trava_produto_ligada(env):
        adiado = "produto_a_identificar"
    elif preco_trava.trava_ativa(env):
        adiado = "trava_de_preco"
    if export_mode in ("xlsx", "both") and not adiado:
        exporter = ERPExporter()
        paths = exporter.export(order, str(output_path))
        output_files = [{"name": p.name, "path": str(p)} for p in paths]

    if export_mode in ("db", "both"):
        # Gravação no Fire só pelo preview → enviar (preço, de-para e cliente
        # checados em `_send_one_to_fire`). Este caminho legado nunca grava.
        db_result_dict = {"skipped": True, "skip_reason": "USE_PREVIEW"}

    out = {
        "output_files": output_files,
        "db_result": db_result_dict,
        "fire_codigo": fire_codigo,
    }
    if adiado and export_mode in ("xlsx", "both"):
        out["xlsx_adiado"] = adiado
    return out


def _marca_xlsx_adiado(result: dict) -> dict:
    """`{"xlsx_adiado": ...}` só quando a trava adiou o XLSX; senão nada (a
    resposta de hoje fica igual)."""
    return {"xlsx_adiado": result["xlsx_adiado"]} if result.get("xlsx_adiado") else {}


def _guardar_original(raw: bytes, nome: str):
    """Cópia imutável do arquivo recebido, ANTES do parse (ver
    `app/ingestion/arquivo_recebido.py`). Falha vira HTTP 500 e nada segue:
    a garantia é 100% dos arquivos, não best-effort."""
    from app.ingestion import arquivo_recebido
    from app.utils.logger import logger

    env = env_context.current()
    try:
        return arquivo_recebido.guardar(
            raw,
            nome,
            raiz=arquivo_recebido.raiz_recebidos(),
            ambiente=env["slug"] if env else None,
        )
    except OSError as exc:
        logger.error(f"Falha ao guardar o arquivo original {nome!r}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=f"Não foi possível guardar o arquivo original ({exc}). Nada foi importado.",
        ) from exc


def _arquivo_original_info(entry: dict) -> dict:
    original = entry.get("original_path")
    disponivel = bool(original) and Path(original).is_file()
    return {"disponivel": disponivel, "nome": entry.get("source_filename") if disponivel else None}


def _process_file(
    file_path: Path,
    output_path: Path,
    *,
    env: dict | None = None,
    origem: str = "lote",
    usuario: str | None = None,
    copia=None,
) -> dict:
    from app.ingestion.file_loader import LoadedFile

    ext = file_path.suffix.lower()
    raw = file_path.read_bytes()
    loaded = LoadedFile(path=file_path, extension=ext, raw=raw)
    with leituras.registrar(
        origem=origem,
        nome=file_path.name,
        raw=raw,
        usuario=usuario,
        ambiente=env.get("slug") if env else None,
        copia=copia,
    ) as L:
        order = L.processar(loaded)
        if not order:
            raise ValueError(L.diag.recusa or "Formato não reconhecido ou pedido sem itens")
        exported = _run_exporters(order, output_path, env=env)
        L.resultado = "importado"

    return {
        "order_number": order.header.order_number,
        "customer": order.header.customer_name,
        "snapshot": order.model_dump(),
        **exported,
    }


# ── Routes ────────────────────────────────────────────────────────────────


@app.get("/")
def index(request: Request):
    """Dashboard. Redireciona para login se não autenticado, e para
    seleção de ambiente se logado mas sem env ativo (cookie portal_env
    ausente ou inválido — middleware não hidrata request.state.environment).

    Com `roteamento_modo='ligado'` o ambiente é propriedade do pedido, não da
    sessão: não há o que escolher, e o passo de seleção some. Nos outros
    modos o comportamento é o de sempre.
    """
    from app.persistence import roteamento_repo

    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        if getattr(request.state, "environment", None) is None and not _is_test_bypass():
            return RedirectResponse(url="/selecionar-ambiente")
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/login")
def login_page() -> FileResponse:
    """Static login form. Talks to /api/auth/login via fetch + sets cookie."""
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/selecionar-ambiente")
def select_environment_page(request: Request):
    """Página estática para escolher o ambiente ativo. Auth é exigida via fetch."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "selecionar-ambiente.html"))


@app.get("/admin/ambientes")
def admin_envs_page(request: Request):
    """Lista CRUD de ambientes (admin-only). API enforce o role."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-ambientes.html"))


@app.get("/admin/ambientes/novo")
def admin_env_new_page(request: Request):
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-ambiente-edit.html"))


@app.get("/admin/ambientes/{env_id}")
def admin_env_edit_page(env_id: str, request: Request):  # noqa: ARG001 — env_id consumed in client
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-ambiente-edit.html"))


@app.get("/admin/atualizacao")
def admin_atualizacao_page(request: Request):
    """Tela de auto-update do portal (admin-only). API enforce o role."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-atualizacao.html"))


@app.get("/admin/roteamento")
def admin_roteamento_page(request: Request):
    """Interruptor do roteamento + taxa de acerto. Exige login (não admin —
    leitura é `require_user`, só a troca do modo exige admin). API enforce o role."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-roteamento.html"))


@app.get("/admin/usuarios")
def admin_users_legacy() -> RedirectResponse:
    """Legacy URL — moved under /configuracoes/usuarios with the redesign."""
    return RedirectResponse(url="/configuracoes/usuarios", status_code=301)


@app.get("/configuracoes/usuarios")
def admin_users_page() -> FileResponse:
    """User-management UI. Auth is enforced by the API endpoints it calls;
    the page itself is static and redirects to /login if /api/auth/me returns null."""
    return FileResponse(str(STATIC_DIR / "admin-usuarios.html"))


@app.get("/configuracoes/banco")
def config_banco_page() -> FileResponse:
    """Firebird connection settings (admin-only — gated client-side by the shell)."""
    return FileResponse(str(STATIC_DIR / "config-banco.html"))


@app.get("/configuracoes/diretorios")
def config_diretorios_page() -> FileResponse:
    """Watch/output directories + export mode (replaces the old in-page modal)."""
    return FileResponse(str(STATIC_DIR / "config-diretorios.html"))


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "importar-pedidos"})


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Prometheus scrape endpoint. Restrict to internal network in production."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Auth (Fase 4b) ────────────────────────────────────────────────────────


def _login_rate_limit(request: Request) -> None:
    """FastAPI dependency: token-bucket per-IP rate limit on login attempts."""
    ip = (request.client.host if request.client else None) or "unknown"
    if not check_and_consume(key=f"login:{ip}", capacity=10, refill_rate=10 / 900):
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas de login. Tente novamente em 15 minutos.",
            headers={"Retry-After": "900"},
        )


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login", dependencies=[Depends(_login_rate_limit)])
def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate by email + password. Sets HttpOnly session cookie.

    Always responds with the same generic message on failure to avoid
    enumeration of registered emails.
    """
    user = users_repo.find_by_email(body.email)
    # Run verify_password unconditionally (with a dummy hash if user is None)
    # to keep timing identical between "no such user" and "wrong password".
    _DUMMY_HASH = "$2b$12$ChXp.TS9Wq8pTNNGxN3lT.LYsVVJrAvZBwR9NLfKMKzVsC9ATcz9G"
    is_valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)

    if user is None or not user.active or not is_valid:
        raise HTTPException(status_code=401, detail="email ou senha inválidos")

    # Opportunistic rehash if rounds changed
    if hash_needs_rehash(user.password_hash):
        try:
            users_repo.update_password_hash(user.id, hash_password(body.password))
        except (WeakPasswordError, PasswordTooLongError):
            # User has a legacy weak/long password we no longer accept;
            # leave the existing hash and let them keep logging in.
            pass

    users_repo.update_last_login(user.id)
    sess = sessions_repo.create_session(
        user_id=user.id,
        ip=(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    response = JSONResponse(
        {
            "user": {"id": user.id, "email": user.email, "role": user.role},
            "session_expires_at": sess.expires_at,
        }
    )
    set_session_cookie(response, sess.token)
    return response


@app.post("/api/auth/logout")
def logout(
    request: Request,
    user: User = Depends(require_user),  # noqa: ARG001 — enforce auth
) -> JSONResponse:
    """Delete the current session. Cookies (sessão e ambiente) cleared on response."""
    token = request.cookies.get("portal_session")
    if token:
        sessions_repo.delete(token)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    clear_env_cookie(response)
    return response


@app.get("/api/auth/me")
def auth_me(
    request: Request,
    user: User | None = Depends(current_user),
) -> JSONResponse:
    """Whoami — usuário + ambiente atual. SPA usa pra renderizar shell."""
    if user is None:
        return JSONResponse({"user": None, "environment": None})
    env = getattr(request.state, "environment", None)
    env_payload = {"id": env["id"], "slug": env["slug"], "name": env["name"]} if env else None
    return JSONResponse(
        {
            "user": {"id": user.id, "email": user.email, "role": user.role},
            "environment": env_payload,
        }
    )


# ── Bootstrap (first-admin signup; closes after first user) ──────────────


@app.get("/api/auth/bootstrap-status")
def bootstrap_status() -> JSONResponse:
    """Tells the login page whether to show the 'create first admin' form.

    Open as long as the `users` table has zero ACTIVE rows. Once the first
    admin exists, this returns `required: false` and `/api/auth/bootstrap`
    starts rejecting with 403.
    """
    return JSONResponse({"required": users_repo.count_active_users() == 0})


class BootstrapRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/bootstrap")
def bootstrap_admin(body: BootstrapRequest, request: Request) -> JSONResponse:
    """Create the first admin. ONLY works if no active user exists yet.

    Re-checks the users count under the same connection lifetime to avoid
    a TOCTOU where two simultaneous bootstrap calls each create an admin.
    The UNIQUE(email) constraint helps too if the same email is used.
    """
    if users_repo.count_active_users() > 0:
        raise HTTPException(
            status_code=403,
            detail="bootstrap fechado — já existe administrador ativo",
        )
    try:
        user = users_repo.create_user(
            email=body.email,
            password=body.password,
            role="admin",
        )
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except users_repo.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    users_repo.update_last_login(user.id)
    sess = sessions_repo.create_session(
        user_id=user.id,
        ip=(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    response = JSONResponse(
        {
            "user": {"id": user.id, "email": user.email, "role": user.role},
            "session_expires_at": sess.expires_at,
        }
    )
    set_session_cookie(response, sess.token)
    return response


# ── Admin user management (admin-only) ───────────────────────────────────


def _user_dto(u: users_repo.User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "role": u.role,
        "active": u.active,
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    }


@app.get("/api/admin/users")
def admin_list_users(
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    return JSONResponse(
        {
            "users": [_user_dto(u) for u in users_repo.list_users(limit=500)],
        }
    )


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "operator"


@app.post("/api/admin/users")
def admin_create_user(
    body: AdminCreateUserRequest,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    try:
        user = users_repo.create_user(
            email=body.email,
            password=body.password,
            role=body.role,
        )
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except users_repo.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except users_repo.InvalidRoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"user": _user_dto(user)}, status_code=201)


class AdminResetPasswordRequest(BaseModel):
    password: str


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    body: AdminResetPasswordRequest,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    target = users_repo.find_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="usuário não encontrado")
    try:
        new_hash = hash_password(body.password)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    users_repo.update_password_hash(target.id, new_hash)
    # Sign target out everywhere — they must log in with the new password.
    sessions_repo.delete_all_for_user(target.id)
    return JSONResponse({"ok": True})


@app.post("/api/admin/users/{user_id}/deactivate")
def admin_deactivate(
    user_id: int,
    admin_user: User = Depends(require_admin),
) -> JSONResponse:
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=409,
            detail="você não pode desativar a si mesmo",
        )
    target = users_repo.find_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="usuário não encontrado")
    users_repo.deactivate(user_id)
    sessions_repo.delete_all_for_user(user_id)
    return JSONResponse({"ok": True})


@app.post("/api/admin/users/{user_id}/reactivate")
def admin_reactivate(
    user_id: int,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    target = users_repo.find_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="usuário não encontrado")
    users_repo.reactivate(user_id)
    return JSONResponse({"ok": True})


# ── Invitations (admin issues, invitee accepts) ──────────────────────────


def _invite_dto(inv: invites_repo.Invite, request: Request | None = None) -> dict:
    """Public-safe view of an invite. Builds the absolute URL when a request
    is in scope, so the admin UI can show a copy-paste link.
    """
    base = ""
    if request is not None:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return {
        "token": inv.token,
        "email": inv.email,
        "role": inv.role,
        "invited_by_user_id": inv.invited_by_user_id,
        "created_at": inv.created_at,
        "expires_at": inv.expires_at,
        "expired": inv.is_expired(),
        "accepted_at": inv.accepted_at,
        "revoked_at": inv.revoked_at,
        "accept_url": f"{base}/invite/{inv.token}",
    }


class CreateInviteRequest(BaseModel):
    email: str
    role: str = "operator"
    ttl_hours: int | None = None  # None → repo default (7d)


@app.post("/api/admin/invites")
def admin_create_invite(
    body: CreateInviteRequest,
    request: Request,
    admin_user: User = Depends(require_admin),
) -> JSONResponse:
    # Refuse if there's already an active user with this email (vs creating
    # a confusing duplicate-then-fail-on-accept).
    existing_user = users_repo.find_by_email(body.email)
    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail=f"já existe usuário com este e-mail (id={existing_user.id})",
        )
    try:
        ttl = (
            body.ttl_hours
            if body.ttl_hours and body.ttl_hours > 0
            else invites_repo.DEFAULT_TTL_HOURS
        )
        inv = invites_repo.create(
            email=body.email,
            role=body.role,
            invited_by_user_id=admin_user.id,
            ttl_hours=ttl,
        )
    except users_repo.InvalidRoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except invites_repo.OpenInviteExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"invite": _invite_dto(inv, request)}, status_code=201)


@app.get("/api/admin/invites")
def admin_list_invites(
    request: Request,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    return JSONResponse(
        {
            "invites": [_invite_dto(inv, request) for inv in invites_repo.list_pending()],
        }
    )


@app.delete("/api/admin/invites/{token}")
def admin_revoke_invite(
    token: str,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    inv = invites_repo.get_by_token(token)
    if inv is None:
        raise HTTPException(status_code=404, detail="convite não encontrado")
    changed = invites_repo.revoke(token)
    if not changed:
        # already accepted or already revoked — return idempotent OK
        return JSONResponse({"ok": True, "noop": True})
    return JSONResponse({"ok": True})


# Public endpoints — invitee uses these. NO auth dependency. They are
# guarded entirely by the secret token in the URL.


@app.get("/api/invites/{token}")
def public_get_invite(token: str) -> JSONResponse:
    """Returns minimal info to render the accept page. 404 if invalid."""
    inv = invites_repo.get_by_token(token)
    if inv is None or inv.is_revoked or inv.is_accepted:
        raise HTTPException(status_code=404, detail="convite inválido ou já utilizado")
    if inv.is_expired():
        raise HTTPException(status_code=410, detail="convite expirado")
    return JSONResponse(
        {
            "email": inv.email,
            "role": inv.role,
            "expires_at": inv.expires_at,
        }
    )


class AcceptInviteRequest(BaseModel):
    password: str


@app.post("/api/invites/{token}/accept")
def public_accept_invite(
    token: str,
    body: AcceptInviteRequest,
    request: Request,
) -> JSONResponse:
    """Accept the invite: create user with the chosen password, mark invite
    used, log the new user in.
    """
    inv = invites_repo.get_by_token(token)
    if inv is None or inv.is_revoked or inv.is_accepted:
        raise HTTPException(status_code=404, detail="convite inválido ou já utilizado")
    if inv.is_expired():
        raise HTTPException(status_code=410, detail="convite expirado")

    # Edge case: someone (admin manually) created a user with this email
    # between issue and accept — refuse rather than collide.
    if users_repo.find_by_email(inv.email) is not None:
        raise HTTPException(
            status_code=409,
            detail="usuário com este e-mail já existe; revogue o convite",
        )

    try:
        user = users_repo.create_user(
            email=inv.email,
            password=body.password,
            role=inv.role,
        )
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except users_repo.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        invites_repo.accept_for_user(token, accepted_user_id=user.id)
    except invites_repo.InviteUnusableError as exc:
        # Lost the race: someone consumed the same token concurrently.
        users_repo.deactivate(user.id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    users_repo.update_last_login(user.id)
    sess = sessions_repo.create_session(
        user_id=user.id,
        ip=(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    response = JSONResponse(
        {
            "user": {"id": user.id, "email": user.email, "role": user.role},
            "session_expires_at": sess.expires_at,
        },
        status_code=201,
    )
    set_session_cookie(response, sess.token)
    return response


@app.get("/invite/{token}")
def invite_accept_page(token: str) -> FileResponse:  # noqa: ARG001 — token used by frontend
    """Public page where invitee sets password. JS reads token from URL."""
    return FileResponse(str(STATIC_DIR / "invite.html"))


@app.get("/api/config")
def get_config(request: Request) -> JSONResponse:
    cfg = _get_cfg_for_request(request)
    env = _request_environment(request)
    if env is not None:
        # Multi-ambiente: o status do banco é o do ambiente ATIVO, não do
        # singleton legado. Sem isso o pill mostrava "SEM BANCO" mesmo com a
        # Fire do ambiente funcionando (lia só o firebird.json/FB_DATABASE).
        from app.persistence import environments_repo

        fb_cfg = environments_repo.to_fb_config(env)
        firebird_configured = bool(fb_cfg.get("path"))
        environment = {"name": env.get("name"), "slug": env.get("slug")}
    else:
        firebird_configured = firebird_config.is_configured() or bool(
            os.environ.get("FB_DATABASE")
        )
        environment = None
    return JSONResponse(
        {
            "watchDir": cfg["watch_dir"],
            "outputDir": cfg["output_dir"],
            "exportMode": cfg.get("export_mode", "xlsx"),
            "firebirdConfigured": firebird_configured,
            "environment": environment,
        }
    )


class ConfigUpdate(BaseModel):
    watchDir: str | None = None
    outputDir: str | None = None
    exportMode: str | None = None


@app.post("/api/config")
def update_config(
    body: ConfigUpdate,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app import config as app_config

    watch_dir = str(Path(body.watchDir).expanduser().resolve()) if body.watchDir else None
    output_dir = str(Path(body.outputDir).expanduser().resolve()) if body.outputDir else None
    cfg = app_config.save(watch_dir=watch_dir, output_dir=output_dir, export_mode=body.exportMode)
    return JSONResponse(
        {
            "watchDir": cfg["watch_dir"],
            "outputDir": cfg["output_dir"],
            "exportMode": cfg.get("export_mode", "xlsx"),
        }
    )


# ── Firebird connection config (admin-managed via UI) ────────────────────


class FirebirdConfigUpdate(BaseModel):
    path: str | None = None
    host: str | None = None
    port: str | None = None
    user: str | None = None
    charset: str | None = None
    # Omit `password` to keep current; empty string clears it.
    password: str | None = None


@app.get("/api/firebird/config")
def get_firebird_config(
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Public view — never returns the password (encrypted or otherwise)."""
    cfg = firebird_config.public_view()
    cfg["configured"] = firebird_config.is_configured()
    cfg["passwordSet"] = bool(firebird_config.load()["password_enc"])
    return JSONResponse(cfg)


@app.post("/api/firebird/config")
def save_firebird_config(
    body: FirebirdConfigUpdate,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    payload = {
        "path": body.path or "",
        "host": body.host or "",
        "port": body.port or "",
        "user": body.user or "",
        "charset": body.charset or "",
    }
    firebird_config.save(payload, password=body.password)
    firebird_config.apply_to_env()
    out = firebird_config.public_view()
    out["configured"] = firebird_config.is_configured()
    out["passwordSet"] = bool(firebird_config.load()["password_enc"])
    return JSONResponse(out)


@app.post("/api/firebird/test")
def test_firebird_connection(
    body: FirebirdConfigUpdate,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    """Try to open a Firebird connection with either the saved config or the
    payload (if any field is provided). Returns ok/error + trace_id for debug.

    The payload's password, when present, is used as-is and not persisted.
    """
    from app.erp.connection import FirebirdConnection
    from app.erp.exceptions import FirebirdConnectionError

    saved = firebird_config.load()
    has_payload_field = any(
        getattr(body, k) not in (None, "")
        for k in ("path", "host", "port", "user", "charset", "password")
    )

    # Resolve effective values (payload field overrides saved when provided).
    def pick(field: str) -> str:
        v = getattr(body, field)
        return v if (v is not None and v != "") else saved.get(field, "") or ""

    eff_path = pick("path")
    eff_host = pick("host")
    eff_port = pick("port") or "3050"
    eff_user = pick("user") or "SYSDBA"
    eff_charset = pick("charset") or "WIN1252"
    if body.password is not None:
        eff_password = body.password
    else:
        eff_password = firebird_config.get_password() or os.environ.get("FB_PASSWORD", "masterkey")

    if not eff_path:
        return JSONResponse(
            {
                "ok": False,
                "error": "Caminho do banco (path) é obrigatório.",
                "traceId": current_trace_id(),
            },
            status_code=400,
        )

    # Temporarily mutate os.environ for the duration of the test only when
    # the request supplied overrides — otherwise just use the saved/env state.
    saved_env: dict[str, str | None] = {}
    env_keys = {
        "FB_DATABASE": eff_path,
        "FB_HOST": eff_host,
        "FB_PORT": eff_port,
        "FB_USER": eff_user,
        "FB_CHARSET": eff_charset,
        "FB_PASSWORD": eff_password,
    }
    if has_payload_field:
        for k, v in env_keys.items():
            saved_env[k] = os.environ.get(k)
            if v:
                os.environ[k] = v
            elif k in os.environ:
                del os.environ[k]

    try:
        with FirebirdConnection().connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
        return JSONResponse({"ok": True, "traceId": current_trace_id()})
    except FirebirdConnectionError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "traceId": current_trace_id()},
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001 — surface any driver error
        return JSONResponse(
            {"ok": False, "error": f"Erro inesperado: {exc}", "traceId": current_trace_id()},
            status_code=500,
        )
    finally:
        if has_payload_field:
            for k, prev in saved_env.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev


def _listar_pasta_pendente(watch_dir: str) -> tuple[list[dict], bool]:
    """Arquivos elegíveis de UMA pasta de entrada, ou `([], False)` se ela não
    existir. Extraído de `list_pending` pra ser reusado tanto no caso de uma
    empresa só quanto na soma entre empresas (`ligado` sem cookie)."""
    watch = Path(watch_dir)
    if not watch.exists():
        return [], False

    files = []
    for f in sorted(
        watch.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True
    ):
        if not f.is_file():
            continue
        if f.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        # Exclude anything inside "Pedidos importados" (safety, iterdir is not recursive)
        try:
            stat = f.stat()
            files.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    "ext": f.suffix.lower().lstrip("."),
                }
            )
        except Exception:
            pass

    return files, True


@app.get("/api/pending")
def list_pending(request: Request, _user: User = Depends(require_user)) -> JSONResponse:
    from app.persistence import environments_repo, roteamento_repo

    if roteamento_repo.modo() == roteamento_repo.LIGADO and _request_environment(request) is None:
        # Ambiente é propriedade do PEDIDO — a caixa soma as pastas de todas
        # as empresas ativas. Nome de arquivo não é único entre elas, então
        # cada item carrega `env_slug`/`env_name` (mesmo `_com_env` do lote
        # somado) pra quem for agir saber qual pasta é qual.
        files: list[dict] = []
        any_exists = False
        missing_envs: list[dict] = []
        for env in environments_repo.list_active():
            env_files, exists = _listar_pasta_pendente(env["watch_dir"])
            any_exists = any_exists or exists
            if not exists:
                missing_envs.append({"slug": env["slug"], "name": env["name"]})
            files.extend(_com_env(f, env) for f in env_files)
        files.sort(key=lambda f: f["mtime"], reverse=True)
        # `watchDir` não tem valor único no caso somado — a UI usa o selo por
        # item em vez do topo (ver Step 6). `exists` reflete "pelo menos uma
        # pasta existe", pra não acender o aviso de "pasta não encontrada" à
        # toa quando só uma das empresas ainda não tem a pasta criada.
        # `missingEnvs` é o sinal que `exists` sozinho mascara: uma pasta
        # saudável não pode esconder a ausência da outra (share de rede
        # desmontado em uma das empresas continua invisível sem isto).
        return JSONResponse(
            {"files": files, "watchDir": None, "exists": any_exists, "missingEnvs": missing_envs}
        )

    cfg = _get_cfg_for_request(request)
    files, exists = _listar_pasta_pendente(cfg["watch_dir"])
    return JSONResponse({"files": files, "watchDir": cfg["watch_dir"], "exists": exists})


class ImportRequest(BaseModel):
    files: list[str]
    outputDir: str | None = None
    # Empresa DECLARADA pelo cliente — nome de arquivo não é único entre
    # empresas, então não dá pra derivar. Só obrigatório em `ligado` sem
    # cookie; ver `_env_da_pasta`.
    env_slug: str | None = None


@app.post("/api/import")
def import_files(
    body: ImportRequest,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app import config as app_config

    env = _env_da_pasta(request, body.env_slug)
    cfg = _cfg_para_env(env) if env is not None else _get_cfg_for_request(request)
    request_env = env if env is not None else _request_environment(request)
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

    # `env` só é não-None em 'ligado' sem cookie: aí o cfg acima já é o DESTA
    # empresa (via `_cfg_para_env`), e `_guardar_original`/`_append_log`
    # também precisam do contextvar ativo — eles resolvem a DB por ele, não
    # por `cfg`. Nos outros casos o contexto já veio do middleware (cookie)
    # ou nem existe (legado single-tenant) — `_persist_ctx` devolve
    # `nullcontext()` e não muda nada.
    with _persist_ctx(env):
        for filename in body.files:
            with with_trace_id():
                name = Path(filename).name  # strip any path component — security
                src = watch / name

                slug = request_env.get("slug") if request_env else None
                if not src.exists() or not src.is_file():
                    leituras.rejeitado(
                        "lote", name, "nao_encontrado", usuario=_user.email, ambiente=slug
                    )
                    errors.append(
                        {"source": name, "error": "Arquivo não encontrado na pasta de entrada"}
                    )
                    continue
                if src.suffix.lower() not in ALLOWED_EXTENSIONS:
                    leituras.rejeitado(
                        "lote",
                        name,
                        "extensao",
                        src.suffix.lower(),
                        usuario=_user.email,
                        ambiente=slug,
                    )
                    errors.append({"source": name, "error": "Extensão não permitida"})
                    continue

                try:
                    raw_guarda = src.read_bytes()
                    try:
                        recebido = _guardar_original(raw_guarda, name)
                    except HTTPException as exc_guarda:
                        leituras.rejeitado(
                            "lote",
                            name,
                            "guarda_falhou",
                            str(exc_guarda.detail),
                            tamanho=len(raw_guarda),
                            usuario=_user.email,
                            ambiente=slug,
                        )
                        raise
                    result = _process_file(
                        src,
                        output_path,
                        env=request_env,
                        usuario=_user.email,
                        copia=recebido.path,
                    )

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
                        file_sha256=recebido.sha256,
                        original_path=str(recebido.path),
                    )
                    _append_log(cfg, entry)

                    results.append(
                        {
                            "source": name,
                            "order": result["order_number"] or "—",
                            "customer": result["customer"] or "—",
                            "files": result["output_files"],
                            "fire_codigo": result.get("fire_codigo"),
                            "entry_id": entry["id"],
                            **_marca_xlsx_adiado(result),
                        }
                    )

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
    request: Request,
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
    portal_status: list[str] | None = Query(None),
    production_status: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """`?portal_status=` aceita o parâmetro repetido (`?portal_status=a&portal_status=b`)
    para filtrar por múltiplos estados numa chamada só (ex.: chip "No Fire" da UI,
    que precisa casar `sent_to_fire` e `found_in_fire` de uma vez). Um único valor
    continua funcionando exatamente como antes — vira uma lista de 1 item, e
    `repo._build_where` trata `IN (?)` como equivalente a `= ?`."""
    from app.persistence import repo, roteamento_repo

    # Sem empresa no cookie E com o roteamento valendo, a caixa de entrada é
    # de todas as empresas — o ambiente virou propriedade do pedido, não da
    # sessão. Com empresa escolhida (cookie presente), o cookie volta a ser
    # um filtro: a lista é só daquela empresa, como sempre foi. As três
    # funções trocam JUNTAS — lista, total e chips têm que somar o mesmo
    # conjunto, ou a tela mostra números que não batem com o que renderizou.
    cross = (
        roteamento_repo.modo() == roteamento_repo.LIGADO
        and getattr(request.state, "environment", None) is None
    )
    listar = repo.list_imports_all_envs if cross else repo.list_imports
    contar = repo.count_imports_all_envs if cross else repo.count_imports
    contar_chips = (
        repo.count_by_portal_status_all_envs if cross else repo.count_by_portal_status
    )

    entries = listar(
        limit=limit,
        offset=offset,
        status=status,
        portal_status=portal_status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    total = contar(
        status=status,
        portal_status=portal_status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    # `counts` alimenta os contadores dos chips de status. Vai junto da lista,
    # e não numa rota própria, para que os números NUNCA divirjam do que a
    # tela acabou de renderizar — duas chamadas separadas podem cair em lados
    # opostos de uma reconciliação em background e mostrar 12 na lista com 308
    # no chip.
    counts = contar_chips(
        status=status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    return JSONResponse(
        {
            "entries": entries,
            "total": total,
            "counts": counts,
            "limit": limit,
            "offset": offset,
        }
    )


@app.get("/api/imported/{import_id}")
def get_imported(
    import_id: str,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Importação não encontrada")
    audit = repo.list_audit(import_id)
    return JSONResponse({"entry": entry, "audit": audit})


@app.get("/api/imported/{import_id}/arquivo-original")
def baixar_arquivo_original(
    import_id: str,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> FileResponse:
    """Cópia exata do arquivo recebido, antes do parse. Só serve de dentro de
    `recebidos/`: `original_path` vem do banco, mas defesa em profundidade
    custa duas linhas."""
    import mimetypes

    from app.ingestion.arquivo_recebido import raiz_recebidos
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    original = entry.get("original_path")
    if not original:
        raise HTTPException(
            status_code=404,
            detail="Pedido anterior à guarda de arquivos: não existe cópia do original",
        )
    caminho = Path(original).resolve()
    if raiz_recebidos().resolve() not in caminho.parents:
        raise HTTPException(status_code=403, detail="Caminho fora da pasta de arquivos recebidos")
    if not caminho.is_file():
        raise HTTPException(status_code=404, detail="Cópia do original não está mais no disco")
    nome = entry.get("source_filename") or caminho.name
    return FileResponse(
        str(caminho),
        filename=nome,
        media_type=mimetypes.guess_type(nome)[0] or "application/octet-stream",
    )


@app.post("/api/imported/reconciliar-fire")
def reconciliar_fire_agora(
    user: User = Depends(require_user),
) -> Resultado:
    """Botão manual: reconcilia o ambiente ativo agora, ignorando a trava —
    a operadora pediu explicitamente, não é o loop periódico esbarrando nele
    mesmo (a exclusão mútua por ambiente continua valendo: duplo clique não
    dispara dois scans, devolve `status="em_execucao"` na hora — ver
    `app.reconcile.runner._lock_for` e `Resultado.status`). `status`
    distingue "rodou e não achou nada" (`"ok"`) de "não consegui consultar o
    Fire" (`"erro_conexao"`) de "já tem uma reconciliação deste ambiente em
    andamento" (`"em_execucao"`) — sem essa distinção a operadora vê
    '0 casaram' pros três casos e conclui, errado, que a feature quebrou."""
    from app.utils.logger import logger

    slug = env_context.current_env_slug()
    logger.info(f"reconcile.web: botão manual acionado por {user.email} em '{slug}'")
    resultado = reconciliar(slug, respeitar_trava=False)
    logger.info(
        f"reconcile.web: botão manual '{slug}' por {user.email} -> "
        f"verificados={resultado.verificados} casaram={resultado.casaram} "
        f"status={resultado.status}"
    )
    return resultado


class ReimportRequest(BaseModel):
    filename: str
    outputDir: str | None = None
    # Empresa DECLARADA — ver `ImportRequest.env_slug` / `_env_da_pasta`.
    env_slug: str | None = None


def _retirar_preco_do_reimportado(src: Path, novo_id: str) -> None:
    """O reimport cria `import_id` novo: as importações anteriores dos MESMOS
    bytes (`imports.file_sha256`; o nome não serve, homônimo é outro arquivo e
    o nome muda com o carimbo), ainda fora do Fire, saem da fila de aprovação
    de preço do Flow (contrato precos v1 §4.3). A entrada de um reimport não
    grava sha (o sinal de duplicata de `leituras` depende disso): ela casa por
    `file_sha256 IS NULL` + o nome do arquivo reimportado. Sem sha do
    arquivo, nada é retirado. Sem revisão é no-op. Nunca derruba o reimport."""
    from app.utils.logger import logger

    try:
        import hashlib
        from datetime import UTC

        from app.persistence import db as _db
        from app.persistence import preco_revisao_repo

        try:
            sha = hashlib.sha256(src.read_bytes()).hexdigest()
        except OSError as exc:
            logger.warning(f"reimport: sem sha256 do arquivo ({type(exc).__name__}); nada retirado")
            return
        with _db.connect() as conn:
            antigos = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM imports"
                    " WHERE (file_sha256 = ? OR (file_sha256 IS NULL AND source_filename = ?))"
                    " AND id != ?"
                    " AND COALESCE(portal_status, '') NOT IN ('sent_to_fire', 'found_in_fire')",
                    (sha, src.name, novo_id),
                )
            ]
            for antigo in antigos:
                preco_revisao_repo.retirar(
                    conn, antigo, "pedido reimportado no Portal", em=datetime.now(UTC)
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"retirar revisão de preço do reimport falhou ({type(exc).__name__}): {exc}")


@app.post("/api/reimport")
def reimport_file(
    body: ReimportRequest,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app import config as app_config

    env = _env_da_pasta(request, body.env_slug)
    cfg = _cfg_para_env(env) if env is not None else _get_cfg_for_request(request)
    request_env = env if env is not None else _request_environment(request)
    imp = app_config.imported_dir(cfg)

    name = Path(body.filename).name
    src = imp / name

    slug = request_env.get("slug") if request_env else None
    if not src.exists() or not src.is_file():
        leituras.rejeitado("reimport", name, "nao_encontrado", usuario=_user.email, ambiente=slug)
        raise HTTPException(
            status_code=404, detail="Arquivo não encontrado em 'Pedidos importados'"
        )
    if src.suffix.lower() not in ALLOWED_EXTENSIONS:
        leituras.rejeitado(
            "reimport", name, "extensao", src.suffix.lower(), usuario=_user.email, ambiente=slug
        )
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

    with with_trace_id():
        try:
            with _persist_ctx(env):
                result = _process_file(
                    src, output_path, env=request_env, origem="reimport", usuario=_user.email
                )
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
                _retirar_preco_do_reimportado(src, entry["id"])
                return JSONResponse(
                    {
                        "source": name,
                        "order": result["order_number"] or "—",
                        "customer": result["customer"] or "—",
                        "files": result["output_files"],
                        "fire_codigo": result.get("fire_codigo"),
                        "entry_id": entry["id"],
                        **_marca_xlsx_adiado(result),
                    }
                )
        except Exception as exc:
            with _persist_ctx(env):
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
def download_file(path: str, _user: User = Depends(require_user)) -> FileResponse:
    """XLSX gerado pelo exportador. Confinado às raízes de onde
    `ERPExporter().export(...)` de fato grava — ver `_run_exporters` e
    `_send_one_to_fire`/`_export_one_xlsx`: `OUTPUT_DIR` legado (quando não há
    ambiente selecionado) e `output_dir` de cada ambiente ATIVO (multi-empresa).
    Mesma defesa em profundidade que `baixar_arquivo_original` já usa pra
    `recebidos/`: resolve o caminho, confere a raiz ANTES de checar existência
    (não vaza "existe/não existe" fora das pastas de saída), só então serve.
    """
    from app import config as app_config
    from app.persistence import environments_repo

    file_path = Path(path).expanduser().resolve()
    if file_path.suffix.lower() != ".xlsx":
        raise HTTPException(status_code=403, detail="Apenas arquivos .xlsx podem ser baixados")

    raizes = [Path(app_config.load()["output_dir"]).expanduser().resolve()]
    raizes += [
        Path(env["output_dir"]).expanduser().resolve() for env in environments_repo.list_active()
    ]
    if not any(raiz in file_path.parents for raiz in raizes):
        raise HTTPException(status_code=403, detail="Caminho fora das pastas de saída permitidas")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(
        str(file_path),
        filename=file_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/fs")
def browse_filesystem(
    path: str = "~",
    file_ext: str = "",
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    """Lista subpastas. Quando `file_ext` (ex: '.fdb') é passado, também lista
    arquivos com essa extensão na pasta atual — útil para pickers de arquivo.
    Sem `file_ext`, comportamento histórico (apenas pastas).
    """
    try:
        p = Path(path).expanduser().resolve()
        while not p.exists() or not p.is_dir():
            parent = p.parent
            if parent == p:
                p = Path.home()
                break
            p = parent
        dirs = sorted(
            [
                {"name": e.name, "path": str(e), "type": "dir"}
                for e in p.iterdir()
                if e.is_dir() and not e.name.startswith(".")
            ],
            key=lambda x: x["name"].lower(),
        )
        files: list[dict] = []
        if file_ext:
            ext = file_ext.lower().strip()
            if not ext.startswith("."):
                ext = "." + ext
            files = sorted(
                [
                    {"name": e.name, "path": str(e), "type": "file"}
                    for e in p.iterdir()
                    if e.is_file() and e.suffix.lower() == ext and not e.name.startswith(".")
                ],
                key=lambda x: x["name"].lower(),
            )
        parent = str(p.parent) if p != p.parent else None
        return JSONResponse(
            {
                "current": str(p),
                "parent": parent,
                "entries": dirs,  # mantém shape antigo (só dirs) p/ chamadas legadas
                "files": files,  # adicional, vazio quando file_ext não foi passado
            }
        )
    except PermissionError:
        return JSONResponse({"error": "Sem permissão para acessar este diretório"}, status_code=403)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Preview → Commit flow ─────────────────────────────────────────────────


@app.post("/api/preview")
async def preview_file(
    request: Request,
    file: UploadFile = File(...),
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app.ingestion.file_loader import LoadedFile

    filename = file.filename or "arquivo"
    ext = Path(filename).suffix.lower()
    request_env = _request_environment(request)
    slug = request_env.get("slug") if request_env else None

    if ext not in ALLOWED_EXTENSIONS:
        leituras.rejeitado(
            "upload", filename, "extensao", ext, usuario=_user.email, ambiente=slug
        )
        raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {ext}")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        leituras.rejeitado(
            "upload", filename, "tamanho", tamanho=len(raw), usuario=_user.email, ambiente=slug
        )
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )

    try:
        recebido = _guardar_original(raw, filename)
    except HTTPException as exc:
        leituras.rejeitado(
            "upload",
            filename,
            "guarda_falhou",
            str(exc.detail),
            tamanho=len(raw),
            usuario=_user.email,
            ambiente=slug,
        )
        raise

    # Trace mintado ANTES do parse: a linha de leitura e o `imports` do commit
    # compartilham o mesmo trace_id.
    with (
        with_trace_id() as trace_id,
        leituras.registrar(
            origem="upload",
            nome=filename,
            raw=raw,
            usuario=_user.email,
            ambiente=slug,
            copia=recebido.path,
        ) as L,
    ):
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)

            loaded = LoadedFile(path=tmp_path, extension=ext, raw=raw)
            order = L.processar(loaded)
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

        if not order:
            raise HTTPException(
                status_code=422,
                detail=L.diag.recusa or "Formato não reconhecido ou pedido sem itens",
            )

        from app.erp.product_check import check_order

        check = check_order(order, env=request_env)
        L.check = check
        L.resultado = "preview"
        entry = get_cache().put(
            order=order,
            source_filename=filename,
            source_bytes=raw,
            source_ext=ext,
            check=check,
            original_path=str(recebido.path),
            file_sha256=recebido.sha256,
            trace_id=trace_id,
            check_env_slug=slug,
        )
    payload = _build_preview_payload(entry.preview_id, filename, order, check)
    # Bloco `roteamento`: só nos dois montadores de preview FRESCO (aqui e em
    # `preview_pending`) — não em `_build_preview_payload`, que também
    # alimenta `rehydrate_preview` (pedido já commitado, ambiente definitivo,
    # nada a decidir ali — ver Achado 4 do fix round 1).
    payload["roteamento"] = _roteamento_para_preview(order, entry=entry)
    payload["produto_chave_modo"] = _produto_chave_modo(request_env)
    payload["produto_trava"] = _produto_trava_preview(check, request_env)
    payload["sem_preco_dispensado"] = _sem_preco_dispensado_no_preview(request_env)
    return JSONResponse(payload)


class PreviewPendingRequest(BaseModel):
    filename: str
    # Empresa DECLARADA — ver `ImportRequest.env_slug` / `_env_da_pasta`.
    env_slug: str | None = None


@app.post("/api/preview-pending")
def preview_pending(
    body: PreviewPendingRequest,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Preview a file already in the watch folder (no upload)."""
    from app.ingestion.file_loader import LoadedFile

    env = _env_da_pasta(request, body.env_slug)
    cfg = _cfg_para_env(env) if env is not None else _get_cfg_for_request(request)
    request_env = env if env is not None else _request_environment(request)
    slug = request_env.get("slug") if request_env else None
    watch = Path(cfg["watch_dir"])

    name = Path(body.filename).name  # strip path components
    src = watch / name
    if not src.exists() or not src.is_file():
        leituras.rejeitado(
            "preview-pending", name, "nao_encontrado", usuario=_user.email, ambiente=slug
        )
        raise HTTPException(status_code=404, detail="Arquivo não encontrado na pasta de entrada")
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        leituras.rejeitado(
            "preview-pending", name, "extensao", ext, usuario=_user.email, ambiente=slug
        )
        raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {ext}")
    if src.stat().st_size > MAX_UPLOAD_BYTES:
        leituras.rejeitado(
            "preview-pending",
            name,
            "tamanho",
            tamanho=src.stat().st_size,
            usuario=_user.email,
            ambiente=slug,
        )
        raise HTTPException(status_code=413, detail="Arquivo excede o limite")

    with _persist_ctx(env):
        raw = src.read_bytes()
        try:
            recebido = _guardar_original(raw, name)
        except HTTPException as exc:
            leituras.rejeitado(
                "preview-pending",
                name,
                "guarda_falhou",
                str(exc.detail),
                tamanho=len(raw),
                usuario=_user.email,
                ambiente=slug,
            )
            raise
        loaded = LoadedFile(path=src, extension=ext, raw=raw)

        with (
            with_trace_id() as trace_id,
            leituras.registrar(
                origem="preview-pending",
                nome=name,
                raw=raw,
                usuario=_user.email,
                ambiente=slug,
                copia=recebido.path,
            ) as L,
        ):
            order = L.processar(loaded)

            if not order:
                raise HTTPException(
                    status_code=422,
                    detail=L.diag.recusa or "Formato não reconhecido ou pedido sem itens",
                )

            from app.erp.product_check import check_order

            check = check_order(order, env=request_env)
            L.check = check
            L.resultado = "preview"
            entry = get_cache().put(
                order=order,
                source_filename=name,
                source_bytes=raw,
                source_ext=ext,
                source_path=str(src),
                check=check,
                original_path=str(recebido.path),
                file_sha256=recebido.sha256,
                trace_id=trace_id,
                check_env_slug=slug,
            )
        payload = _build_preview_payload(entry.preview_id, name, order, check)
        # Ver comentário equivalente em `preview_upload` (Achado 4, fix round 1).
        payload["roteamento"] = _roteamento_para_preview(order, entry=entry)
    payload["produto_chave_modo"] = _produto_chave_modo(request_env)
    payload["produto_trava"] = _produto_trava_preview(check, request_env)
    payload["sem_preco_dispensado"] = _sem_preco_dispensado_no_preview(request_env)
    return JSONResponse(payload)


class CommitRequest(BaseModel):
    preview_id: str
    # Escolha do operador em 'ligado': a resposta quando o roteamento não
    # soube decidir sozinho, ou a troca da empresa sugerida. Ignorado nos
    # outros modos — lá o ambiente é sempre o do cookie, como sempre foi.
    environment_slug: str | None = None


def _resolver_env_alvo(
    request: Request, body: CommitRequest, modo: str, decisao: Decisao | None
) -> dict | None:
    """O ambiente onde ESTE commit vai gravar. Nunca chuta em 'ligado'.

    Fora de `'ligado'` é sempre o ambiente do cookie (`None` se não houver
    nenhum) — idêntico ao que já era antes desta feature. Em `'ligado'`:
    escolha explícita do operador (`body.environment_slug`) > decisão
    resolvida (a sugestão do preview) > pergunta (409). A escolha vence
    porque o operador pode trocar qualquer sugestão (spec §10.1); o rastro
    (`roteamento_decisao`) registra a troca. Os 409/412 daqui rodam ANTES
    de `get_cache().consume()` no chamador — não queimam o preview (ver
    docstring de `commit_preview`, ponto 3).
    """
    from app.persistence import environments_repo, roteamento_repo

    env_alvo = _request_environment(request)
    if modo != roteamento_repo.LIGADO:
        return env_alvo

    if body.environment_slug:
        env_alvo = environments_repo.get_by_slug(body.environment_slug)
    elif decisao.resolveu:
        env_alvo = environments_repo.get_by_slug(decisao.env_slug)
    else:
        # Sem resposta NÃO vira ambiente default. Pergunta.
        raise HTTPException(
            status_code=409,
            detail={
                "precisa_escolher": True,
                "explicacao": decisao.explicacao,
                "degrau": decisao.degrau,
                "opcoes": [
                    {"slug": e["slug"], "name": e["name"]} for e in environments_repo.list_active()
                ],
            },
        )
    if env_alvo is None or not env_alvo.get("is_active"):
        raise HTTPException(
            status_code=412,
            detail="Esta ação é de uma empresa específica — abra o pedido para agir nele.",
        )
    return env_alvo


@app.post("/api/commit")
def commit_preview(
    body: CommitRequest,
    request: Request,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Salva o pedido no portal como 'em revisão'. NÃO grava no Fire.
    O usuário revisa o match na aba Pedidos e só depois clica em 'Cadastrar no Fire'.

    Roteamento intercompany (`app/routing/ambiente.py`, modos 'observando' e
    'ligado'): o ambiente onde o pedido é gravado pode divergir do ambiente
    do cookie (`env_alvo`, resolvido por `_resolver_env_alvo`). Três cuidados
    resolvidos neste handler:

    1. `imports` mora em `app_state_<slug>.db`, e `db.connect()` resolve o
       arquivo pelo CONTEXTVAR de `env_context`, não por um campo no dict —
       por isso todo o bloco de persistência roda dentro de
       `env_context.active_env(env_alvo["id"], env_alvo["slug"])`.
    2. O arquivo original segue o pedido: o `shutil.move` do fim usa as
       pastas do ambiente ROTEADO (via `cfg` sobrescrito abaixo), não as do
       cookie — senão o arquivo da Nasmar iria parar em `Pedidos importados`
       da MM.
    3. O preview só é CONSUMIDO depois que o ambiente já foi decidido sem
       erro. Em `'ligado'`, o roteador pode ter resolvido no preview e
       falhar bem no commit (ex.: Firebird caiu no meio) — se `consume()`
       rodasse antes da decisão, o 409 que pede escolha de novo queimaria o
       preview, e a segunda tentativa do operador bateria em "Preview já foi
       importado" sem nada ter sido importado (as duas DBs continuariam
       vazias). `consume()` continua atômico sob lock e roda antes de
       qualquer escrita, então a proteção contra clique duplo não se perde —
       só um request vence a corrida por `consume()`.

    A decisão de empresa é a do preview (`entry.roteamento_decisao`, a que o
    operador viu), não uma nova; se o modo do roteamento mudou desde o
    preview, 409 pedindo nova conferência (antes de consumir).

    O check de produto gravado é sempre o da empresa onde o pedido entra: o
    do preview foi conferido no Fire de `entry.check_env_slug`; em 'ligado'
    com `env_alvo` diferente (roteado ou trocado pelo operador), é refeito no
    Fire do destino — SEQs de um Fire não valem no outro.

    Fora de 'desligado', cada commit deixa uma linha em `roteamento_decisao`
    (sugestão, escolha, quem decidiu e a ação).

    Em modo 'desligado' (default) `env_alvo` é sempre o ambiente do cookie —
    idêntico ao comportamento anterior a esta feature — e nenhuma sombra,
    memória ou rastro é gravado.
    """
    cfg = _get_cfg_for_request(request)

    entry = get_cache().get(body.preview_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Preview expirado ou inexistente")
    if entry.consumed:
        raise HTTPException(status_code=409, detail="Preview já foi importado")

    order = entry.order

    from contextlib import nullcontext

    from app.persistence import decisao_ambiente_repo as memoria
    from app.persistence import roteamento_decisao_repo, roteamento_repo
    from app.routing.ambiente import cnpjs_do_pedido

    # A decisão que o operador viu no preview vale (sem I/O de novo no Fire).
    # Modo mudou desde então: a decisão de agora ele não viu — confere de novo.
    # Sem decisão guardada (preview que não passou pelos montadores), decide.
    if entry.roteamento_modo is None:
        modo, decisao = _decidir_ambiente(order)
    else:
        modo, decisao = roteamento_repo.modo(), entry.roteamento_decisao
        if modo != entry.roteamento_modo:
            raise HTTPException(
                status_code=409,
                detail=(
                    "O modo do roteamento mudou desde a conferência deste pedido."
                    " Confira o arquivo de novo antes de gravar."
                ),
            )
    env_alvo = _resolver_env_alvo(request, body, modo, decisao)

    cnpj_cliente = (cnpjs_do_pedido(order) or [""])[0]

    # Ação do rastro (`roteamento_decisao`). `None` em 'desligado' e sem
    # ambiente resolvido — nada a registrar.
    acao: str | None = None
    if decisao is not None and env_alvo is not None:
        if modo == roteamento_repo.OBSERVANDO:
            acao = "observando"
        elif not decisao.resolveu:
            acao = "escolha_sem_sugestao"
        elif env_alvo["slug"] == decisao.env_slug:
            acao = "sugestao_aceita"
        else:
            acao = "troca_no_preview"

    # Armadilha 2: as pastas seguem o pedido roteado, não o cookie. Quando
    # não há ambiente nenhum resolvido (deploy single-tenant legado, sem
    # cookie de ambiente) `cfg` fica como `_get_cfg_for_request` já devolveu.
    if env_alvo is not None:
        cfg = dict(cfg)
        cfg["watch_dir"] = env_alvo["watch_dir"]
        cfg["output_dir"] = env_alvo["output_dir"]

    # Armadilha 1: o bind do ambiente é o contextvar, não `log_entry["environment_id"]`
    # sozinho. Sem ambiente resolvido, o bloco roda sob o contexto que já
    # estava ativo (cookie via middleware, ou nenhum — igual sempre foi).
    persist_ctx = (
        env_context.active_env(env_alvo["id"], env_alvo["slug"])
        if env_alvo is not None
        else nullcontext()
    )

    # Só consome depois que `_resolver_env_alvo` não levantou — ver ponto 3
    # do docstring acima. `consume()` segue atômico sob lock: só um request
    # vence em caso de clique duplo / corrida.
    try:
        get_cache().consume(body.preview_id)
    except PreviewNotFoundError:
        raise HTTPException(status_code=404, detail="Preview expirado ou inexistente")
    except PreviewConsumedError:
        raise HTTPException(status_code=409, detail="Preview já foi importado")

    with persist_ctx:
        # O trace_id nasce no preview (que abre a linha de leitura) e viaja com o
        # pedido para sempre; o commit só o reaproveita.
        with with_trace_id(entry.trace_id) as trace_id:
            log_entry = _make_log_entry(
                source_filename=entry.source_filename,
                order_number=order.header.order_number,
                customer=order.header.customer_name,
                output_files=[],
                status="success",
                snapshot=order.model_dump(),
                trace_id=trace_id,
                file_sha256=entry.file_sha256,
                original_path=entry.original_path,
            )
            log_entry["portal_status"] = "parsed"
            # Pedido em empresa diferente da conferida no preview: refaz o check
            # no Fire do destino, sob o contexto dele (o de-para lê o SQLite do
            # destino). Só em 'ligado' — nos outros modos `env_alvo` é o cookie.
            # O preview já foi consumido: falha aqui grava SEM check (o
            # "Conferir de novo" refaz), nunca com o da outra empresa.
            check = entry.check
            if (
                modo == roteamento_repo.LIGADO
                and env_alvo is not None
                and env_alvo["slug"] != entry.check_env_slug
            ):
                from app.erp import product_check

                try:
                    check = product_check.check_order(order, env=env_alvo)
                except Exception as exc:  # noqa: BLE001 — o pedido entra; o check é refeito depois
                    from app.utils.logger import logger

                    logger.warning(
                        "commit.check_destino_falhou env={} erro={!r}", env_alvo["slug"], exc
                    )
                    check = None
            log_entry["check"] = check
            if env_alvo is not None:
                log_entry["environment_id"] = env_alvo["id"]
            # Trava de preço com ambiente fonte (Nasmar → MM): o check do Fire da
            # fonte fica salvo para o preview. Desligado/sem fonte: None, sem I/O.
            from app.erp import preco_trava

            log_entry["check_preco"] = preco_trava.check_preco_para_gravar(order, env_alvo)

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
                    "check": check.get("summary") if check else None,
                    # ACHADO 3 da revisão final: sem isto, um pedido roteado
                    # em 'ligado' não deixava registro de POR QUE foi pra
                    # esta empresa — só `environment_id` (o resultado), nunca
                    # a razão. `None` em 'desligado' (decisao is None) — sinal
                    # de "roteador nem rodou", não "não sabemos por quê".
                    #
                    # Achado (minor) da re-review: `env_slug` sozinho é
                    # ambíguo em 'observando' — é a SUGESTÃO do roteador, não
                    # o destino (que é a escolha do operador/cookie); só em
                    # 'ligado' os dois coincidem. `modo` viaja junto pra quem
                    # ler este registro daqui a meses não concluir o oposto
                    # do que aconteceu.
                    "roteamento": (
                        {
                            "modo": modo,
                            "degrau": decisao.degrau,
                            "env_slug": decisao.env_slug,
                            "explicacao": decisao.explicacao,
                            "contraria": decisao.contraria,
                            "decidido_por": user.email,
                        }
                        if decisao is not None
                        else None
                    ),
                },
            )
            transition(
                log_entry["id"],
                LifecycleEvent.IMPORTED,
                source=EventSource.PORTAL,
                payload={
                    "items": len(order.items),
                    "from_watch": entry.source_path is not None,
                    "check_summary": check.get("summary") if check else None,
                },
            )

            if decisao is not None:
                if modo == roteamento_repo.OBSERVANDO and env_alvo is not None:
                    roteamento_repo.registrar_sombra(
                        import_id=log_entry["id"],
                        degrau=decisao.degrau,
                        env_sugerido=decisao.env_slug,
                        env_escolhido=env_alvo["slug"],
                    )
                if decisao.divergiu_de and cnpj_cliente:
                    memoria.marcar_divergencia(cnpj_cliente=cnpj_cliente, de=decisao.divergiu_de)
                # A memória aprende com o operador: quando o roteador não
                # soube, e quando ele trocou a sugestão.
                if acao in ("escolha_sem_sugestao", "troca_no_preview") and cnpj_cliente:
                    memoria.lembrar(
                        cnpj_cliente=cnpj_cliente,
                        env_slug=env_alvo["slug"],
                        por=user.email,
                    )
                if acao is not None:
                    roteamento_decisao_repo.registrar(
                        import_id=log_entry["id"],
                        modo=modo,
                        degrau=decisao.degrau,
                        env_sugerido=decisao.env_slug,
                        env_escolhido=env_alvo["slug"],
                        decidido_por=user.email,
                        motivo=decisao.explicacao,
                        acao=acao,
                    )

            # Only move the source after persistence succeeded. `cfg` já
            # aponta pras pastas do ambiente roteado (armadilha 2, acima).
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

            return JSONResponse(
                {
                    "entry_id": log_entry["id"],
                    "order": order.header.order_number or "—",
                    "customer": order.header.customer_name or "—",
                    "portal_status": "parsed",
                    "trace_id": trace_id,
                }
            )


# ── Per-order actions ───────────────────────────────────────────────────


class _FireSendOutcome:
    """Internal result of _send_one_to_fire. HTTP layer translates to status."""

    __slots__ = ("ok", "reason", "http_status", "fire_codigo", "items_inserted", "detail")

    def __init__(
        self,
        ok: bool,
        reason: str | None = None,
        http_status: int = 200,
        fire_codigo: int | None = None,
        items_inserted: int = 0,
        detail: str | None = None,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.http_status = http_status
        self.fire_codigo = fire_codigo
        self.items_inserted = items_inserted
        self.detail = detail


def _vinculos_de_para(
    order, caminho: str, entry: dict | None = None
) -> tuple[list[dict], dict[int, int]]:
    """Vínculos de-para do pedido, consultados UMA vez antes do portão de preço.

    O portão avalia o SEQ do vínculo (`produtos_por_item`), o exporter grava
    esse mesmo SEQ e o XLSX leva o código dele (`depara_apply.aplicar`). Falha
    aqui não derruba o envio: sem vínculo, portão e exporter caem juntos na
    busca normal por EAN/código (e, sem match, o exporter recusa o pedido).

    `entry` (import salvo): a escolha pontual de cada linha (R20) vai por cima do
    v1, em todo modo — sem ela a escolha do operador seria ignorada fora de `ligado`."""
    from app.erp import depara_apply
    from app.persistence import db

    escolhidos = (entry or {}).get("produto_escolhido")
    try:
        with db.connect() as sconn:
            vinculos = depara_apply.resolver(order, conn=sconn)
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.warning(f"de-para pulado no {caminho}: {type(exc).__name__}: {exc}")
        vinculos = []
    vinculos = depara_apply.sobrepor_escolhas(order, vinculos, escolhidos)
    return vinculos, depara_apply.produtos_por_item(vinculos)


def _vinculos_antes_do_portao(
    order, caminho: str, env: dict | None, entry: dict | None = None
) -> tuple[list[dict] | None, dict[int, int] | None]:
    """De-para v1 (com as escolhas pontuais do import por cima) para o portão,
    só fora de `produto_chave_modo` `ligado`. Em `ligado` o v1 nem é lido
    (None, None): a saída é o produto decidido, que já traz a escolha pontual."""
    if _trava_produto_ligada(env):
        return None, None
    return _vinculos_de_para(order, caminho, entry)


def _saida_do_envio(
    decisao,
    order,
    caminho: str,
    vinculos: list[dict] | None,
    produtos_por_item: dict[int, int] | None,
    entry: dict | None = None,
) -> tuple[list[dict], dict[int, int]]:
    """O que vai ao Fire depois do portão. Em `produto_chave_modo` `ligado`
    (`decisao.check` preenchido), o produto decidido de todo item `ok` do MESMO
    check que a trava aprovou: o insert entra pelo SEQ e a planilha leva o
    `CODPROD_ALTERN` e o EAN dele, com o código do cliente na OBS. Fora de
    `ligado`, os vínculos de-para de hoje (`_vinculos_de_para`); se o modo mudou
    entre o portão e aqui e o v1 não foi lido, lê agora."""
    if decisao.check is not None:
        from app.erp import produto_trava

        return produto_trava.produtos_decididos(decisao.check)
    if vinculos is None or produtos_por_item is None:
        return _vinculos_de_para(order, caminho, entry)
    return vinculos, produtos_por_item


def _recusa_planilha_pontual(
    import_id: str, vinculos: list[dict], decisao, audit: str, *, planilha: bool = True
) -> tuple[str, str] | None:
    """Escolha pontual que não pode sair, em nenhum modo de `produto_chave_modo`.

    M1 (toda saída, planilha e insert): o check ao vivo marca a linha
    `vinculo_invalido` — o SEQ escolhido sumiu ou foi bloqueado no Fire.
    R21 (só com `planilha`): o produto não tem `CODPROD_ALTERN` (o SEQ em texto
    seria casado por PREFIXO pelo importador de Excel do Fire); muta `vinculos`
    com o código lido do Fire agora (`decisao.check_vivo`).
    Só a escolha pontual: o v1 de hoje não muda. Devolve (reason, detalhe) e
    audita `audit`, ou None."""
    from app.erp import depara_apply
    from app.persistence import repo

    check_vivo = getattr(decisao, "check_vivo", None)
    invalidas = depara_apply.pontuais_invalidas(vinculos, check_vivo)
    if invalidas:
        itens = [
            {
                "index": v["item_index"],
                "estado": "vinculo_invalido",
                "motivo": depara_apply.motivo_pontual_invalida(v),
            }
            for v in invalidas
        ]
        repo.append_audit(import_id, audit, {"produto": itens})
        return "vinculo_invalido", itens[0]["motivo"]
    if not planilha:
        return None
    vinculos[:] = depara_apply.codigo_vivo_das_escolhas(vinculos, check_vivo)
    sem_codigo = depara_apply.pontuais_sem_codigo(vinculos)
    if not sem_codigo:
        return None
    itens = [
        {
            "index": v["item_index"],
            "estado": "sem_codigo_fire",
            "motivo": depara_apply.motivo_pontual_sem_codigo(v),
        }
        for v in sem_codigo
    ]
    repo.append_audit(import_id, audit, {"produto": itens})
    return "sem_codigo_fire", itens[0]["motivo"]


def _pedido_para_o_flow(order, decisao, produtos_por_item=None, entry=None):
    """O pedido que vai ao Flow (I2). Em `produto_chave_modo` `ligado`
    (`decisao.check` preenchido), cópia ANTES do `depara_apply.aplicar`: o Flow
    recebe o código/EAN do cliente, não os do produto decidido no Fire. Fora de
    `ligado`, o próprio pedido, como hoje.

    Em qualquer modo cada item leva o SEQ do Fire que o Portal já decidiu
    (`erpProdutoId`, vínculo forte); a ordem da fonte está em `produto_erp.seqs_por_item`."""
    flow = order.model_copy(deep=True) if getattr(decisao, "check", None) is not None else order
    check = getattr(decisao, "check", None) or (entry or {}).get("check")
    produto_erp.anexar_produto_erp(
        flow,
        produto_erp.seqs_por_item(
            len(flow.items), produtos_por_item=produtos_por_item, check=check
        ),
    )
    return flow


def _registrar_aplicados(import_id: str, decisao) -> None:
    """Audit `preco_aplicado` depois que o pedido saiu. Nunca derruba um envio
    que já deu certo (o Fire já tem o pedido / o XLSX já existe)."""
    from app.erp import preco_trava

    try:
        preco_trava.registrar_aplicados(import_id, decisao)
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.warning(f"audit preco_aplicado falhou ({type(exc).__name__}): {exc}")


_MOVER_INACABADO = "Mover inacabado: conclua o mover do pedido antes de enviar."


def _mover_inacabado(import_id: str) -> bool:
    """O pedido também está em OUTRA empresa ativa (mover interrompido): as
    duas cópias estão em revisão e qualquer uma poderia ir ao Fire. Vale em
    todos os modos; custa uma busca por empresa por envio."""
    from app.persistence import environments_repo

    atual = env_context.current()
    if atual is None:
        return False
    outras = [e for e in environments_repo.list_active() if e["slug"] != atual["slug"]]
    return env_do_import_id(import_id, envs=outras) is not None


_MOVER_NAO_CONFERIDO = "Não deu para conferir as outras empresas agora; tente de novo."


def _recusa_mover(import_id: str, outcome_cls):
    """Recusa (outcome 409) quando o pedido não pode sair agora: mover
    inacabado, ou não deu para conferir as outras empresas (SQLite de outra
    empresa com erro — recusa só este pedido, sem ir ao Fire). None = pode."""
    try:
        inacabado = _mover_inacabado(import_id)
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.warning(f"mover inacabado: conferência falhou ({type(exc).__name__})")
        return outcome_cls(
            False, reason="mover_inacabado", http_status=409, detail=_MOVER_NAO_CONFERIDO
        )
    if inacabado:
        return outcome_cls(
            False, reason="mover_inacabado", http_status=409, detail=_MOVER_INACABADO
        )
    return None


def _com_trava_do_pedido(outcome_cls):
    """Envio que leva o pedido para fora do Portal segura a trava do pedido da
    leitura do estado até gravar o resultado: o mover de empresa não corre no
    meio (`mover_pedido.trava_do_pedido`). Pedido movido antes de a trava sair
    some desta empresa: a leitura devolve `not_found` sem ir ao Fire. Vale em
    todos os modos (só serializa o mesmo pedido). A espera tem teto
    (`_ESPERA_ENVIO_S`): estourou, `outcome_cls` 409 `pedido_ocupado`."""

    def _decorar(fn):
        @functools.wraps(fn)
        def _com_trava(import_id: str, *args, **kwargs):
            from app.persistence import mover_pedido

            try:
                with mover_pedido.trava_do_pedido(
                    import_id, espera=mover_pedido._ESPERA_ENVIO_S
                ):
                    return fn(import_id, *args, **kwargs)
            except mover_pedido.PedidoOcupado:
                return outcome_cls(
                    False,
                    reason="pedido_ocupado",
                    http_status=409,
                    detail=(
                        "Outro envio ou mover deste pedido está em andamento há mais de"
                        " 2 minutos; tente de novo em instantes."
                    ),
                )

        return _com_trava

    return _decorar


@_com_trava_do_pedido(_FireSendOutcome)
def _send_one_to_fire(
    import_id: str, cfg: dict, *, request_env: dict | None = None
) -> _FireSendOutcome:
    """Insert a parsed order into Fire. Returns structured outcome (no HTTP exceptions)
    so batch callers can aggregate per-item results.

    `request_env`: ambiente atual hidratado pelo middleware. Quando presente,
    o FirebirdExporter conecta com as creds do env (multi-ambiente). Quando
    None, cai no fallback de env vars FB_* (legado).

    State mutation contract:
        - On Fire failure: log SEND_TO_FIRE_FAILED (state stays PARSED).
        - On Fire success: update aux fields then transition SEND_TO_FIRE_SUCCEEDED.
    """
    from app.consolidators import integracao
    from app.exporters.firebird_exporter import FirebirdExporter
    from app.models.order import Order
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        return _FireSendOutcome(
            False, reason="not_found", http_status=404, detail="Pedido não encontrado"
        )
    recusa = _recusa_mover(import_id, _FireSendOutcome)
    if recusa is not None:
        return recusa
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

    # Portão único de preço (regra de hoje em `desligado`; trava por rede em
    # `ligado`). Preço aprovado no Flow entra antes do de-para e do exporter.
    from datetime import UTC

    from app.erp import preco_trava
    from app.observability import metrics

    export_mode = cfg.get("export_mode", "xlsx")
    vinculos, produtos_por_item = _vinculos_antes_do_portao(order, "envio", request_env, entry)
    decisao = preco_trava.decidir_envio(
        import_id,
        order,
        entry,
        env=request_env,
        agora=datetime.now(UTC),
        produtos_por_item=produtos_por_item,
        export_mode=export_mode,
    )
    if decisao.bloqueado:
        repo.append_audit(import_id, "send_to_fire_blocked", decisao.block_detail)
        for label in decisao.metric_labels:
            metrics.price_check_blocks_total.labels(reason=label).inc()
        return _FireSendOutcome(
            False, reason=decisao.reason, http_status=409, detail=decisao.detalhe
        )
    preco_trava.aplicar_precos(order, decisao.precos_aplicar)
    vinculos, produtos_por_item = _saida_do_envio(
        decisao, order, "envio", vinculos, produtos_por_item, entry
    )
    recusa = _recusa_planilha_pontual(
        import_id,
        vinculos,
        decisao,
        "send_to_fire_blocked",
        planilha=export_mode in ("xlsx", "both"),
    )
    if recusa is not None:
        return _FireSendOutcome(False, reason=recusa[0], http_status=409, detail=recusa[1])

    # Lote intercompany: o espelho passou pela trava acima como qualquer pedido;
    # aqui as guardas do lote e a reserva do lote_fiscal do SP1.
    espelho = integracao.preparar_envio_espelho(import_id, order, request_env)
    if isinstance(espelho, integracao.BloqueioEspelho):
        repo.append_audit(
            import_id, "send_to_fire_blocked", {"motivo": espelho.motivo, **espelho.detalhe}
        )
        return _FireSendOutcome(
            False, reason=espelho.motivo, http_status=409, detail=espelho.mensagem
        )

    # Reuse the trace_id minted on commit so logs across commit→send-to-fire
    # are correlated for the same pedido.
    with (
        with_trace_id(entry.get("trace_id")),
        integracao.guardar_reserva(espelho, request_env),
    ):
        # De-para antes do XLS (modo both) e do insert, com os MESMOS vínculos
        # que o portão avaliou. Item vinculado entra no Fire pelo SEQ do vínculo:
        # o `fire_codigo` é str(SEQ) e relido como CODPROD_ALTERN casaria outro
        # produto (BACKLOG §2.1).
        from app.erp import depara_apply

        order_flow = _pedido_para_o_flow(order, decisao, produtos_por_item, entry)
        dep_changed = depara_apply.aplicar(order, vinculos)
        if dep_changed:
            repo.append_audit(import_id, "depara_aplicado_no_fire", {"itens": dep_changed})

        output_path = Path(cfg["output_dir"]).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        output_files: list[dict] = []
        # Espelho de lote vai ao Fire só pelo insert direto (rastro do lote_fiscal).
        if export_mode in ("xlsx", "both") and not espelho:
            from app.exporters.erp_exporter import ERPExporter

            paths = ERPExporter().export(order, str(output_path))
            output_files = [{"name": p.name, "path": str(p)} for p in paths]
            erp_vinculo.registrar_planilhas(import_id, order, paths)

        override = entry.get("cliente_override_codigo")
        # Multi-ambiente: usa creds do env atual em vez de env vars FB_*.
        result = FirebirdExporter(env=request_env).export(
            order,
            override_client_id=override,
            produtos_por_item=produtos_por_item,
            **({"obs": espelho.obs} if espelho else {}),
        )
        db_result = result.to_dict()

        if result.skipped or result.fire_codigo is None:
            if espelho:
                integracao.abortar_envio_espelho(espelho, request_env)
            repo.append_audit(
                import_id,
                "send_to_fire_failed",
                {
                    "skip_reason": result.skip_reason,
                    "items_inserted": result.items_inserted,
                    "cliente_override_codigo": override,
                },
            )
            try:
                transition(
                    import_id,
                    LifecycleEvent.SEND_TO_FIRE_FAILED,
                    source=EventSource.PORTAL,
                    payload={
                        "skip_reason": result.skip_reason,
                        "items_inserted": result.items_inserted,
                        "cliente_override_codigo": override,
                    },
                )
            except InvalidTransitionError:
                # State already moved; lifecycle log captured by audit_log.
                pass
            return _FireSendOutcome(
                False,
                reason=result.skip_reason or "no_fire_codigo",
                http_status=409,
                detail=(
                    "Fire rejeitou o pedido: produto de um item não existe no Fire. "
                    "Confira o vínculo do item e envie de novo."
                    if result.skip_reason == "PRODUCT_NOT_FOUND"
                    else f"Fire rejeitou o pedido: {result.skip_reason or 'sem fire_codigo'}"
                ),
            )

        # Fire aceitou: o rastro do lote vem ANTES de tudo que ainda pode levantar.
        if espelho:
            integracao.concluir_envio_espelho(espelho, result, request_env)

        now = datetime.now().isoformat(timespec="seconds")
        repo.update_fire_metadata(
            import_id,
            fire_codigo=result.fire_codigo,
            db_result=db_result,
            output_files=output_files or entry.get("output_files") or [],
            sent_to_fire_at=now,
        )
        repo.append_audit(
            import_id,
            "sent_to_fire",
            {
                "fire_codigo": result.fire_codigo,
                "items_inserted": result.items_inserted,
                "cliente_override_codigo": override,
            },
        )
        transition(
            import_id,
            LifecycleEvent.SEND_TO_FIRE_SUCCEEDED,
            source=EventSource.PORTAL,
            payload={
                "fire_codigo": result.fire_codigo,
                "items_inserted": result.items_inserted,
                "cliente_override_codigo": override,
            },
        )
        _registrar_aplicados(import_id, decisao)
        integracao.anexar_perna_de_origem(import_id, entry, order, result, request_env)

        # SP1: vínculo exato item do ERP ↔ posição em order.items (best-effort).
        if request_env:
            erp_vinculo.registrar_insert(
                import_id,
                environment_id=request_env["id"],
                erp_codigo=result.fire_codigo,
                itens=result.itens,
            )

        # Ponte FlowPCP (Modelo B/OVERLAY): notifica o pedido novo em paralelo
        # ao Fire — só em ambientes com FlowPCP habilitado (MM). Best-effort:
        # nunca derruba o send-to-fire que já teve sucesso.
        # Espelho de lote não vai ao /recebimento: no Flow segue tudo por pedido de
        # origem; o vínculo vai pelo lote_fiscal do SP1.
        slug = (request_env or {}).get("slug")
        if slug and espelho is None:
            push_new_order(order_flow, import_id=import_id, slug=slug)

        return _FireSendOutcome(
            True,
            fire_codigo=result.fire_codigo,
            items_inserted=result.items_inserted,
        )


@app.post("/api/imported/{import_id}/send-to-fire")
def send_to_fire(
    import_id: str,
    request: Request,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    cfg = _get_cfg_for_request(request)
    request_env = getattr(request.state, "environment", None)
    outcome = _send_one_to_fire(import_id, cfg, request_env=request_env)
    if not outcome.ok:
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    return JSONResponse(
        {
            "entry_id": import_id,
            "fire_codigo": outcome.fire_codigo,
            "items_inserted": outcome.items_inserted,
            "portal_status": "sent_to_fire",
        }
    )


class _XlsxExportOutcome:
    """Internal result of _export_one_xlsx. HTTP layer translates to status."""

    __slots__ = ("ok", "reason", "http_status", "output_files", "detail")

    def __init__(
        self,
        ok: bool,
        reason: str | None = None,
        http_status: int = 200,
        output_files: list[dict] | None = None,
        detail: str | None = None,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.http_status = http_status
        self.output_files = output_files or []
        self.detail = detail


@_com_trava_do_pedido(_XlsxExportOutcome)
def _export_one_xlsx(
    import_id: str, cfg: dict, *, request_env: dict | None = None
) -> _XlsxExportOutcome:
    """Generate XLSX for a parsed order WITHOUT touching Firebird.

    Used when EXPORT_MODE='xlsx'. Audit-only side effect; portal_status stays
    'parsed' so the operator can still cancel or re-export.
    """
    from app.exporters.erp_exporter import ERPExporter
    from app.models.order import Order
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        return _XlsxExportOutcome(
            False, reason="not_found", http_status=404, detail="Pedido não encontrado"
        )
    recusa = _recusa_mover(import_id, _XlsxExportOutcome)
    if recusa is not None:
        return recusa
    if entry.get("portal_status") == "found_in_fire":
        return _XlsxExportOutcome(
            False,
            reason="found_in_fire",
            http_status=409,
            detail=(
                "Pedido já consta no Fire — não é possível exportar novamente "
                "(reexportar cadastraria duplicata no ERP)"
            ),
        )
    if entry.get("portal_status") != "parsed":
        return _XlsxExportOutcome(
            False,
            reason="wrong_status",
            http_status=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    from app.consolidators import integracao

    if integracao.e_espelho(import_id):
        return _XlsxExportOutcome(
            False,
            reason="lote_espelho",
            http_status=409,
            detail="Pedido de lote vai ao Fire só pelo cadastro direto (EXPORT_MODE=db)",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        return _XlsxExportOutcome(
            False, reason="no_snapshot", http_status=422, detail="Snapshot do pedido indisponível"
        )

    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        return _XlsxExportOutcome(
            False, reason="invalid_snapshot", http_status=422, detail=f"Snapshot inválido: {exc}"
        )

    # Portão único de preço — o mesmo de _send_one_to_fire. Usa o ambiente da
    # request (multi-ambiente); cai pra env vars FB_* só quando request_env é
    # None (deploy single-DB legado).
    from datetime import UTC

    from app.erp import preco_trava
    from app.observability import metrics

    vinculos, produtos_por_item = _vinculos_antes_do_portao(order, "export", request_env, entry)
    decisao = preco_trava.decidir_envio(
        import_id,
        order,
        entry,
        env=request_env,
        agora=datetime.now(UTC),
        produtos_por_item=produtos_por_item,
        # Esta rota gera planilha em qualquer EXPORT_MODE: valem as regras dela.
        export_mode="xlsx",
        planilha=True,
    )
    if decisao.bloqueado:
        repo.append_audit(import_id, "xlsx_export_blocked", decisao.block_detail)
        for label in decisao.metric_labels:
            metrics.price_check_blocks_total.labels(reason=label).inc()
        return _XlsxExportOutcome(
            False, reason=decisao.reason, http_status=409, detail=decisao.detalhe
        )
    preco_trava.aplicar_precos(order, decisao.precos_aplicar)
    vinculos, produtos_por_item = _saida_do_envio(
        decisao, order, "export", vinculos, produtos_por_item, entry
    )
    recusa = _recusa_planilha_pontual(import_id, vinculos, decisao, "xlsx_export_blocked")
    if recusa is not None:
        return _XlsxExportOutcome(False, reason=recusa[0], http_status=409, detail=recusa[1])

    with with_trace_id(entry.get("trace_id")):
        # Identidade do Fire onde há vínculo de-para (ou, em `ligado`, o produto
        # decidido): os MESMOS vínculos que o portão avaliou (o XLSX leva o código
        # do SEQ avaliado).
        from app.erp import depara_apply

        order_flow = _pedido_para_o_flow(order, decisao, produtos_por_item, entry)
        dep_changed = depara_apply.aplicar(order, vinculos)
        if dep_changed:
            repo.append_audit(import_id, "depara_aplicado_no_xls", {"itens": dep_changed})

        output_path = Path(cfg["output_dir"]).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        paths = ERPExporter().export(order, str(output_path))
        output_files = [{"name": p.name, "path": str(p)} for p in paths]
        erp_vinculo.registrar_planilhas(import_id, order, paths)

        repo.update_fire_metadata(import_id, output_files=output_files)
        repo.append_audit(import_id, "xlsx_exported", {"files": output_files})

        # Ponte FlowPCP (IDA): mesmo com o Fire manual (XLS), o pedido já vai
        # pro Flow. Gated por flowpcp_enabled dentro do hook; o Flow deduplica
        # por externalId (re-export não cria duplicata). Best-effort: falha
        # vira outbox/log e nunca derruba a geração do XLS.
        slug = (request_env or {}).get("slug")
        if slug:
            pushed = push_new_order(order_flow, import_id=import_id, slug=slug)
            repo.append_audit(import_id, "flowpcp_push", {"ok": pushed})
        _registrar_aplicados(import_id, decisao)

        return _XlsxExportOutcome(True, output_files=output_files)


@app.post("/api/imported/{import_id}/export-xlsx")
def export_xlsx(
    import_id: str,
    request: Request,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    cfg = _get_cfg_for_request(request)
    request_env = getattr(request.state, "environment", None)
    outcome = _export_one_xlsx(import_id, cfg, request_env=request_env)
    if not outcome.ok:
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    return JSONResponse(
        {
            "entry_id": import_id,
            "output_files": outcome.output_files,
            "portal_status": "parsed",
        }
    )


def _com_env(item: dict, env: dict | None) -> dict:
    """Anota `env_slug`/`env_name` no item de lote quando `env` é dado.

    `env=None` é o ramo legado (uma empresa só, a do cookie) — item sai sem
    selo de empresa, exatamente como antes desta task.
    """
    if env is not None:
        item["env_slug"] = env["slug"]
        item["env_name"] = env["name"]
    return item


def _item_fail(
    import_id: str, *, reason: str | None, detail: str | None, env: dict | None = None
) -> dict:
    """Item de resultado de lote para falha — usado nos dois ramos (legado e
    agrupado) e nas duas rotas de lote (`_send_one_to_fire`/`_export_one_xlsx`
    têm o mesmo `reason`/`detail`)."""
    return _com_env({"id": import_id, "ok": False, "reason": reason, "detail": detail}, env)


def _item_ok_fire(import_id: str, outcome: _FireSendOutcome, *, env: dict | None = None) -> dict:
    return _com_env(
        {
            "id": import_id,
            "ok": True,
            "fire_codigo": outcome.fire_codigo,
            "items_inserted": outcome.items_inserted,
        },
        env,
    )


def _item_ok_xlsx(import_id: str, outcome: _XlsxExportOutcome, *, env: dict | None = None) -> dict:
    return _com_env({"id": import_id, "ok": True, "output_files": outcome.output_files}, env)


class BatchSendRequest(BaseModel):
    ids: list[str]


@app.post("/api/batch/send-to-fire")
def batch_send_to_fire(
    body: BatchSendRequest,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Send multiple parsed orders to Fire. Tolerates partial failures — each id is
    attempted independently; response lists per-id outcome."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="Lista de ids vazia")
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 pedidos por lote")

    from app.persistence import roteamento_repo

    results: list[dict] = []
    ok_count = 0
    fail_count = 0

    env_do_cookie = _request_environment(request)
    # Caixa somada: em 'ligado' sem cookie a seleção pode ter pedidos de
    # empresas diferentes — cada id resolve a SUA empresa. Fora de 'ligado',
    # ou com cookie presente, é o comportamento de sempre: uma empresa só, a
    # do cookie (sem cookie, o handler falha como sempre falhou — 412).
    agrupar_por_pedido = (
        roteamento_repo.modo() == roteamento_repo.LIGADO and env_do_cookie is None
    )

    if not agrupar_por_pedido:
        cfg = _get_cfg_for_request(request)
        for import_id in body.ids:
            outcome = _send_one_to_fire(import_id, cfg, request_env=env_do_cookie)
            if outcome.ok:
                ok_count += 1
                results.append(_item_ok_fire(import_id, outcome))
            else:
                fail_count += 1
                results.append(
                    _item_fail(import_id, reason=outcome.reason, detail=outcome.detail)
                )
        return JSONResponse(
            {"total": len(body.ids), "ok": ok_count, "failed": fail_count, "results": results}
        )

    from collections import defaultdict

    from app.persistence import environments_repo

    # Hoisted pra fora do loop: `env_do_import_id` sem isto rodaria
    # `list_active()` (query no app_shared.db + abertura de SQLite por
    # ambiente) uma vez PRA CADA id do lote — 100 ids = 100 buscas na mesma
    # lista. Não fecha nenhuma janela de corrida (nem antes nem depois disto
    # a escrita re-checa `is_active`) — é só query a menos.
    envs_ativos = environments_repo.list_active()
    grupos: dict[str, list[str]] = defaultdict(list)
    envs_por_slug: dict[str, dict] = {}
    for import_id in body.ids:
        achado = env_do_import_id(import_id, envs=envs_ativos)
        if achado is None:
            fail_count += 1
            results.append(
                _item_fail(
                    import_id,
                    reason="nao_encontrado",
                    detail="Pedido não encontrado em nenhuma empresa ativa",
                )
            )
        else:
            grupos[achado["slug"]].append(import_id)
            envs_por_slug.setdefault(achado["slug"], achado)

    for slug, ids in grupos.items():
        env = envs_por_slug[slug]
        cfg_do_grupo = _cfg_para_env(env)
        with env_context.active_env(env["id"], env["slug"]):
            for import_id in ids:
                outcome = _send_one_to_fire(import_id, cfg_do_grupo, request_env=env)
                if outcome.ok:
                    ok_count += 1
                    results.append(_item_ok_fire(import_id, outcome, env=env))
                else:
                    fail_count += 1
                    results.append(
                        _item_fail(
                            import_id, reason=outcome.reason, detail=outcome.detail, env=env
                        )
                    )

    return JSONResponse(
        {
            "total": len(body.ids),
            "ok": ok_count,
            "failed": fail_count,
            "results": results,
        }
    )


@app.post("/api/batch/export-xlsx")
def batch_export_xlsx(
    body: BatchSendRequest,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Generate XLSX for multiple parsed orders WITHOUT touching Firebird."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="Lista de ids vazia")
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 pedidos por lote")

    from app.persistence import roteamento_repo

    results: list[dict] = []
    ok_count = 0
    fail_count = 0

    env_do_cookie = _request_environment(request)
    # Caixa somada: em 'ligado' sem cookie a seleção pode ter pedidos de
    # empresas diferentes — cada id resolve a SUA empresa. Fora de 'ligado',
    # ou com cookie presente, é o comportamento de sempre: uma empresa só, a
    # do cookie (sem cookie, o handler falha como sempre falhou — 412).
    agrupar_por_pedido = (
        roteamento_repo.modo() == roteamento_repo.LIGADO and env_do_cookie is None
    )

    if not agrupar_por_pedido:
        cfg = _get_cfg_for_request(request)
        for import_id in body.ids:
            outcome = _export_one_xlsx(import_id, cfg, request_env=env_do_cookie)
            if outcome.ok:
                ok_count += 1
                results.append(_item_ok_xlsx(import_id, outcome))
            else:
                fail_count += 1
                results.append(
                    _item_fail(import_id, reason=outcome.reason, detail=outcome.detail)
                )
        return JSONResponse(
            {"total": len(body.ids), "ok": ok_count, "failed": fail_count, "results": results}
        )

    from collections import defaultdict

    from app.persistence import environments_repo

    # Hoisted pra fora do loop: `env_do_import_id` sem isto rodaria
    # `list_active()` (query no app_shared.db + abertura de SQLite por
    # ambiente) uma vez PRA CADA id do lote — 100 ids = 100 buscas na mesma
    # lista. Não fecha nenhuma janela de corrida (nem antes nem depois disto
    # a escrita re-checa `is_active`) — é só query a menos.
    envs_ativos = environments_repo.list_active()
    grupos: dict[str, list[str]] = defaultdict(list)
    envs_por_slug: dict[str, dict] = {}
    for import_id in body.ids:
        achado = env_do_import_id(import_id, envs=envs_ativos)
        if achado is None:
            fail_count += 1
            results.append(
                _item_fail(
                    import_id,
                    reason="nao_encontrado",
                    detail="Pedido não encontrado em nenhuma empresa ativa",
                )
            )
        else:
            grupos[achado["slug"]].append(import_id)
            envs_por_slug.setdefault(achado["slug"], achado)

    for slug, ids in grupos.items():
        env = envs_por_slug[slug]
        cfg_do_grupo = _cfg_para_env(env)
        with env_context.active_env(env["id"], env["slug"]):
            for import_id in ids:
                outcome = _export_one_xlsx(import_id, cfg_do_grupo, request_env=env)
                if outcome.ok:
                    ok_count += 1
                    results.append(_item_ok_xlsx(import_id, outcome, env=env))
                else:
                    fail_count += 1
                    results.append(
                        _item_fail(
                            import_id, reason=outcome.reason, detail=outcome.detail, env=env
                        )
                    )

    return JSONResponse(
        {
            "total": len(body.ids),
            "ok": ok_count,
            "failed": fail_count,
            "results": results,
        }
    )


class CancelRequest(BaseModel):
    reason: str | None = None


# ── Gestor de Produção (Fase 3, gatilho manual + drain inline) ──────────


@app.post("/api/imported/{import_id}/post-to-gestor")
def post_to_gestor(
    import_id: str,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    """Envia pedido (já em Fire) para o Gestor de Produção.

    Phase 3 wiring: enqueue outbox → transition REQUESTED → drain inline.
    Phase 5 substitutes inline drain by background worker.
    """
    from app.integrations.gestor import (
        GESTOR_TARGET_NAME,
        GestorClient,
        GestorClientError,
        build_gestor_payload,
    )
    from app.models.order import Order
    from app.persistence import outbox_repo, repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "sent_to_fire":
        raise HTTPException(
            status_code=409,
            detail=(
                "Pedido precisa estar em Fire antes do Gestor "
                f"(status atual: {entry.get('portal_status')})"
            ),
        )
    if entry.get("production_status") != "none":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pedido já enviado ao Gestor (production_status: {entry.get('production_status')})"
            ),
        )

    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc

    with with_trace_id(entry.get("trace_id")) as trace_id:
        payload_request = build_gestor_payload(
            import_id=import_id,
            order=order,
            metadata={
                "fire_codigo": entry.get("fire_codigo"),
                "trace_id": trace_id,
            },
        )
        idempotency_key = str(uuid.uuid4())

        # 1) Enqueue durably FIRST. If something blows up next, the row is
        #    in DB and a retry is straightforward.
        try:
            row = outbox_repo.enqueue(
                import_id=import_id,
                target=GESTOR_TARGET_NAME,
                endpoint="/v1/orders",
                payload=payload_request.model_dump(),
                idempotency_key=idempotency_key,
            )
        except outbox_repo.OutboxDuplicateError as exc:
            raise HTTPException(
                status_code=409, detail=f"Idempotency key collision: {exc}"
            ) from exc

        # 2) State transition: this is the audit-grade record of the request.
        try:
            transition(
                import_id,
                LifecycleEvent.POST_TO_GESTOR_REQUESTED,
                source=EventSource.PORTAL,
                payload={"outbox_id": row.id, "idempotency_key": idempotency_key},
            )
        except InvalidTransitionError as exc:
            outbox_repo.mark_failed(row.id, error=f"transition_invalid: {exc}", dead=True)
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 3) Drain inline. Phase 5 worker will own this.
        try:
            client = GestorClient()
        except GestorClientError as exc:
            outbox_repo.mark_failed(row.id, error=str(exc))
            transition(
                import_id,
                LifecycleEvent.POST_TO_GESTOR_FAILED,
                source=EventSource.PORTAL,
                payload={"reason": str(exc)},
            )
            raise HTTPException(status_code=503, detail=f"Gestor não configurado: {exc}") from exc

        try:
            try:
                response = client.create_order(
                    payload_request,
                    idempotency_key=idempotency_key,
                )
            except GestorClientError as exc:
                outbox_repo.mark_failed(row.id, error=str(exc))
                transition(
                    import_id,
                    LifecycleEvent.POST_TO_GESTOR_FAILED,
                    source=EventSource.PORTAL,
                    payload={"reason": str(exc), "status_code": exc.status_code},
                )
                raise HTTPException(status_code=502, detail=f"Gestor rejeitou: {exc}") from exc

            # 4) Success: persist correlation, mark outbox, transition SENT.
            outbox_repo.mark_sent(row.id, response=response.model_dump())
            repo.set_gestor_order_id(import_id, response.id)
            result = transition(
                import_id,
                LifecycleEvent.POST_TO_GESTOR_SENT,
                source=EventSource.PORTAL,
                payload={
                    "gestor_order_id": response.id,
                    "outbox_id": row.id,
                },
            )
        finally:
            client.close()

        return JSONResponse(
            {
                "entry_id": import_id,
                "gestor_order_id": response.id,
                "production_status": result.production_status.value,
                "outbox_id": row.id,
                "trace_id": trace_id,
            }
        )


@app.post("/api/imported/{import_id}/cancel")
def cancel_import(
    import_id: str,
    body: CancelRequest | None = None,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") == "sent_to_fire":
        raise HTTPException(
            status_code=409,
            detail="Pedido já foi enviado ao Fire — não pode ser cancelado pelo portal",
        )
    if entry.get("portal_status") == "found_in_fire":
        raise HTTPException(
            status_code=409,
            detail="Pedido já consta no Fire — não pode ser cancelado pelo portal",
        )

    reason = body.reason if body else None
    with with_trace_id(entry.get("trace_id")):
        try:
            result = transition(
                import_id,
                LifecycleEvent.CANCELLED,
                source=EventSource.PORTAL,
                payload={"reason": reason},
            )
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        repo.append_audit(import_id, "cancelled", {"reason": reason})
    try:
        from datetime import UTC

        from app.persistence import db as _db
        from app.persistence import preco_revisao_repo

        motivo = ((reason or "").strip() or "pedido cancelado no Portal")[:500]
        with _db.connect() as _pconn:
            preco_revisao_repo.retirar(_pconn, import_id, motivo, em=datetime.now(UTC))
    except Exception as exc:  # cancelar nunca falha por causa da trava de preço
        from app.utils.logger import logger

        logger.warning(f"retirar revisão de preço falhou ({type(exc).__name__}): {exc}")
    return JSONResponse(
        {
            "entry_id": import_id,
            "portal_status": result.portal_status.value,
        }
    )


# ── Manual cliente override (CLIENT_NOT_FOUND recovery) ─────────────────
#
# When `FirebirdExporter` fails with skip_reason=CLIENT_NOT_FOUND, the user
# can search CADASTRO and pick the right cliente manually. The selection is
# stored as a sidecar on `imports` (não muta snapshot) and consumed by
# `_send_one_to_fire` on the next attempt. Identificação do usuário que
# aplicou o override fica em `audit_log` e em `imports.cliente_override_by`.


@app.get("/api/clientes/search")
def search_clientes(
    q: str,
    request: Request,
    limit: int = 20,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Busca clientes ativos em CADASTRO por razão social ou CNPJ.

    `q`: ao menos 2 caracteres (após strip).
    `limit`: clamp em [1, 50].
    503 se Firebird não configurado.
    """
    from app.erp import queries
    from app.erp.connection import FirebirdConnection

    needle = (q or "").strip()
    if len(needle) < 2:
        raise HTTPException(status_code=400, detail="Informe ao menos 2 caracteres")
    limit = max(1, min(int(limit), 50))

    conn_mgr = FirebirdConnection()
    razao_pattern = f"%{needle.upper()}%"
    cnpj_digits = re.sub(r"\D", "", needle)
    # Sentinel pattern that LIKE never matches: avoids OR-injection of
    # rows when the query has no digits at all.
    cnpj_pattern = f"%{cnpj_digits}%" if cnpj_digits else "%__never_matches__%"
    open_conn = _firebird_open_for_request(request, conn_mgr)

    try:
        with open_conn() as conn:
            cur = conn.cursor()
            cur.execute(queries.SEARCH_CLIENTS, (razao_pattern, cnpj_pattern))
            rows = cur.fetchall()
            cur.close()
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.warning(f"clientes/search falhou ({type(exc).__name__}): {exc}")
        raise HTTPException(status_code=502, detail="Falha consultando o Fire") from exc

    results = [
        {
            "codigo": int(r[0]) if r[0] is not None else None,
            "razao_social": (r[1] or "").strip() if r[1] else None,
            "cpf_cnpj": (r[2] or "").strip() if r[2] else None,
        }
        for r in rows[:limit]
    ]
    return JSONResponse({"results": results, "total_returned": len(results)})


class ClienteOverrideRequest(BaseModel):
    cliente_codigo: int
    reason: str | None = None


@app.post("/api/imported/{import_id}/override-cliente")
def override_cliente(
    import_id: str,
    body: ClienteOverrideRequest,
    request: Request,
    user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    """Aplica seleção manual de cliente a um pedido em revisão.

    Não muda portal_status (override é metadado sidecar, não evento de SM).
    Registra em `audit_log` (`cliente_override_selected`) e em
    `imports.cliente_override_by` o email do usuário autenticado.
    """
    from app.erp import queries
    from app.erp.connection import FirebirdConnection
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(
            status_code=409,
            detail=(
                "Override só é permitido em pedidos em revisão "
                f"(status atual: {entry.get('portal_status')})"
            ),
        )

    conn_mgr = FirebirdConnection()
    open_conn = _firebird_open_for_request(request, conn_mgr)
    try:
        with open_conn() as conn:
            cur = conn.cursor()
            cur.execute(queries.FIND_CLIENT_BY_CODIGO, (body.cliente_codigo,))
            row = cur.fetchone()
            cur.close()
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.warning(f"override-cliente falhou ({type(exc).__name__}): {exc}")
        raise HTTPException(status_code=502, detail="Falha consultando o Fire") from exc

    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"Cliente CODIGO={body.cliente_codigo} não encontrado em CADASTRO ou inativo",
        )

    razao = (row[1] or "").strip() if row[1] else ""
    actor = user.email

    with with_trace_id(entry.get("trace_id")):
        repo.set_client_override(
            import_id,
            codigo=int(row[0]),
            razao=razao,
            user=actor,
            cnpj=row[2] if len(row) > 2 else None,
        )
        repo.append_audit(
            import_id,
            "cliente_override_selected",
            {
                "cliente_codigo": int(row[0]),
                "cliente_razao": razao,
                "previous_cnpj": entry.get("customer_cnpj"),
                "reason": body.reason,
                "user_id": user.id,
                "user_email": actor,
            },
        )

    return JSONResponse(
        {
            "entry_id": import_id,
            "cliente_override_codigo": int(row[0]),
            "cliente_override_razao": razao,
        }
    )


@app.get("/api/produtos/search")
def search_produtos(
    request: Request,
    limit: int = 20,
    q: str = "",
    desc: str = "",
    code: str | None = None,
    ean_item: str | None = None,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Busca produtos na cópia local do catálogo do Fire (catalogo_fire).

    Instantâneo, sem Firebird — funciona com a Fire offline. `q`: ao menos
    2 caracteres (após strip) para popular `results`. `limit`: clamp em
    [1, 50].

    `desc`/`code`/`ean_item` são dicas opcionais do item do pedido (não do
    texto de busca `q`): quando ao menos uma vier preenchida, a resposta
    inclui `suggestions` — top 5 candidatos ranqueados pelo
    `product_ranking` sobre o catálogo completo. É sugestão, nunca vínculo
    automático; o operador confirma clicando.

    `q` é opcional: o picker de de-para abre em cima de um item sem match e
    busca sugestões só com as dicas, sem texto de busca — nesse caso
    `results` vem vazio e só `suggestions` é preenchido. 400 só quando não
    há nada pra fazer: `q` curto (ou ausente) e nenhuma dica.
    """
    from app.erp import product_ranking
    from app.persistence import catalogo_fire_repo, db

    needle = (q or "").strip()
    hints_present = bool(desc.strip() or (code or "").strip() or (ean_item or "").strip())
    if len(needle) < 2 and not hints_present:
        raise HTTPException(
            status_code=400, detail="Informe ao menos 2 caracteres ou um item para sugestões"
        )
    limit = max(1, min(int(limit), 50))
    up = needle.upper()
    digits = re.sub(r"\D", "", needle)

    with db.connect() as conn:
        rows = catalogo_fire_repo.list_all(conn)

    def _hit(r: dict) -> bool:
        if up in (r.get("nome") or "").upper():
            return True
        if digits and (digits in (r.get("codigo") or "") or digits in (r.get("ean") or "")):
            return True
        return False

    results: list[dict] = []
    if len(needle) >= 2:
        results = [
            {
                "fire_produto_id": r["fire_produto_id"],
                "fire_codigo": r["codigo"],
                "fire_ean": r.get("ean"),
                "fire_codprod_altern": r.get("codprod_altern"),
                "fire_nome": r["nome"],
            }
            for r in rows
            if _hit(r)
        ][:limit]

    suggestions: list[dict] = []
    if hints_present:
        ranked = product_ranking.rank_candidates(
            description=desc, product_code=code, ean=ean_item, catalog=rows, limit=5
        )
        suggestions = [
            {
                "fire_produto_id": r["fire_produto_id"],
                "fire_codigo": r["codigo"],
                "fire_ean": r.get("ean"),
                "fire_codprod_altern": r.get("codprod_altern"),
                "fire_nome": r["nome"],
                "score": r["score"],
            }
            for r in ranked
        ]

    return JSONResponse(
        {"results": results, "suggestions": suggestions, "total_returned": len(results)}
    )


class VincularProdutoRequest(BaseModel):
    item_index: int
    fire_produto_id: str
    substituir: bool = False


MSG_VINCULO_SEM_CNPJ = "Este pedido não tem CNPJ do cliente; o vínculo não pode ser reaproveitado."
MSG_VINCULO_CLIENTE_REAL = (
    "Não consegui confirmar o cliente real deste pedido agora; tente de novo."
)
MSG_VINCULO_REVENDA = (
    "Não sei qual é o cliente real deste pedido (pedido da revenda); "
    "o vínculo não pode ser salvo."
)
# Pedidos em revisão lidos (página máxima do repo) e rechecados (R12) por vínculo.
_RECHECK_MAX_PEDIDOS = 500
_RECHECK_MAX_POR_VINCULO = 50


def _chaves_do_item(item) -> list[tuple[str, str]]:
    """(tipo, valor normalizado) do item, código antes do EAN. Mesma
    normalização da gravação e da leitura (`produto_vinculo_repo.norm_chave`)."""
    from app.persistence import produto_vinculo_repo

    out = []
    for tipo, valor in (("codigo", item.product_code), ("ean", item.ean)):
        norm = produto_vinculo_repo.norm_chave(tipo, valor)
        if norm:
            out.append((tipo, norm))
    return out


def _cnpj_do_escopo(order, indice: int, env: dict | None) -> tuple[str | None, bool]:
    """CNPJ que define o escopo do item na GRAVAÇÃO do vínculo (R11).

    Pedido intercompany (`resolucao_para` não é None) só grava com o cliente
    real resolvido: sem ele, o CNPJ do cabeçalho é o da revenda e o vínculo
    cairia no escopo errado. Falha de conexão → 503 (tente de novo); qualquer
    outro motivo (sem_chave, nao_encontrado, ambiguo...) → 422. A leitura do
    check (`product_check._cnpj_real`) segue como está.

    Devolve (cnpj, intercompany?)."""
    from app.erp import produto_escopo

    cnpj_real = None
    slug = (env or {}).get("slug")
    if slug:
        try:
            r = resolucao_para(order, slug=slug)
        except Exception as exc:  # noqa: BLE001 — resolucao_para não deveria levantar
            raise HTTPException(status_code=503, detail=MSG_VINCULO_CLIENTE_REAL) from exc
        if r is not None:
            if not r.resolvido or not r.cnpj:
                if r.motivo == "erro_conexao":
                    raise HTTPException(status_code=503, detail=MSG_VINCULO_CLIENTE_REAL)
                raise HTTPException(status_code=422, detail=MSG_VINCULO_REVENDA)
            cnpj_real = r.cnpj
    return produto_escopo.cnpj_do_item(order, indice, cnpj_real=cnpj_real), cnpj_real is not None


def _conflito_json(v) -> dict:
    return {
        "conflito": {
            "chave_tipo": v.chave_tipo,
            "chave_valor": v.chave_valor,
            "atual": {
                "fire_seq": v.fire_seq,
                "fire_descricao": v.fire_descricao,
                "criado_por": v.criado_por,
                "criado_em": v.criado_em,
            },
        },
        "detail": (
            f"Esta chave já está vinculada ao produto {v.fire_seq} ({v.fire_descricao}). "
            "Confirme para trocar."
        ),
    }


def _item_em_chave_repetida(entry: dict, order, indice: int) -> bool:
    """A escolha desta linha tem de ser pontual (R18)? Sim quando o check guardado
    diz `chave_repetida` ou a linha já tem escolha pontual (trocar também é
    pontual), e SEMPRE que a chave do item se repete no pedido com descrições
    diferentes, qualquer que seja o estado (`a_identificar`, `ambiguo`…): um
    vínculo dessa chave valeria para toda linha, e todo pedido futuro, dela."""
    from app.erp.produto_chave import chaves_repetidas
    from app.persistence import produto_vinculo_repo

    itens_check = (entry.get("check") or {}).get("items") or []
    salvo = (itens_check[indice] or {}).get("produto") if indice < len(itens_check) else None
    if isinstance(salvo, dict) and (
        salvo.get("estado") == "chave_repetida" or salvo.get("fonte") == "escolha_pontual"
    ):
        return True

    def chaves(it):
        return (
            produto_vinculo_repo.norm_chave("codigo", it.product_code) or None,
            produto_vinculo_repo.norm_chave("ean", it.ean) or None,
        )

    repetidas = chaves_repetidas([(*chaves(it), it.description) for it in order.items])
    codigo, ean = chaves(order.items[indice])
    return ("codigo", codigo) in repetidas or ("ean", ean) in repetidas


def _escolher_pontual(
    entry: dict, order, indice: int, prod: dict, fire_seq: int, user: User, env: dict | None
) -> JSONResponse:
    """Grava a escolha pontual da linha (só neste import), audita
    `produto_escolha_pontual` e devolve o check novo com as escolhas do import.
    Nenhum outro pedido é rechecado: a escolha não vale para eles."""
    from app.erp import product_check
    from app.persistence import repo

    import_id = entry["id"]
    antigo = product_check.escolhas_do_import(entry).get(indice)
    repo.set_produto_escolha(
        import_id,
        indice,
        fire_seq=fire_seq,
        por=user.email,
        descricao=prod["nome"] or "",
        codigo=(prod.get("codprod_altern") or "").strip() or None,
        ean=(prod.get("ean") or "").strip() or None,
    )
    with with_trace_id(entry.get("trace_id")):
        repo.append_audit(
            import_id,
            "produto_escolha_pontual",
            {
                "item_index": indice,
                "fire_produto_id": fire_seq,
                "fire_seq_antigo": antigo,
                "fire_nome": prod["nome"],
                "pedido": order.header.order_number,
                "user_id": user.id,
                "user_email": user.email,
            },
        )
    # Relê: a escolha de outra linha gravada ao mesmo tempo entra também.
    atual = repo.get_import(import_id)
    escolhas = product_check.escolhas_do_import(atual)
    check = product_check.check_order(order, env=env, escolhas=escolhas)
    if check.get("available"):
        repo.update_fire_metadata(import_id, check=check)
    # Trava de preço com ambiente fonte: a fonte casa a linha pelo código da
    # escolha; o check dela é refeito. Desligado/sem fonte: None, sem I/O.
    from app.erp import preco_trava

    check_preco = preco_trava.check_preco_para_gravar(order, env, atual, check)
    if check_preco is not None:
        repo.update_fire_metadata(import_id, check_preco=check_preco)
    return JSONResponse(
        {
            "entry_id": import_id,
            "pontual": True,
            "vinculos": [],
            "check": check,
            "produto_trava": _produto_trava_preview(check, env),
            "rechecados": [],
        }
    )


def _rechecar_pedidos_do_escopo(
    import_id: str, escopo: str, chaves: list[tuple[str, str]], env: dict | None
) -> list[str]:
    """Recheca os pedidos em revisão do ambiente com item na mesma chave, fora
    de `ok`, cujo escopo de busca inclui `escopo`. Lê o check guardado (sem
    Fire) para escolher; só os escolhidos passam pelo `check_order`, no máximo
    `_RECHECK_MAX_POR_VINCULO` por chamada, mais antigos primeiro (R12)."""
    from app.erp import product_check, produto_escopo
    from app.models.order import Order
    from app.persistence import db, repo

    alvo = set(chaves)
    escolhidos: list[tuple[str, Order, dict[int, int]]] = []
    entries = repo.list_imports(portal_status="parsed", limit=_RECHECK_MAX_PEDIDOS)
    with db.connect() as conn:
        for entry in sorted(entries, key=lambda e: (e["imported_at"] or "", e["id"])):
            if len(escolhidos) >= _RECHECK_MAX_POR_VINCULO:
                break
            if entry["id"] == import_id or not entry.get("snapshot"):
                continue
            itens_check = (entry.get("check") or {}).get("items") or []
            try:
                order = Order.model_validate(entry["snapshot"])
            except Exception:  # noqa: BLE001 — snapshot podre não para o recheck dos outros
                continue
            indices = [
                i
                for i, it in enumerate(order.items)
                if i < len(itens_check)
                and ((itens_check[i] or {}).get("produto") or {}).get("estado") != "ok"
                and alvo.intersection(_chaves_do_item(it))
            ]
            if not indices:
                continue
            cnpj_real, conferiu = product_check._cnpj_real(order, env)
            # Cliente real incerto: rechecar é inofensivo, então entra.
            if not conferiu or any(
                escopo
                in produto_escopo.escopos_para(
                    conn, produto_escopo.cnpj_do_item(order, i, cnpj_real=cnpj_real)
                )
                for i in indices
            ):
                escolhidos.append((entry["id"], order, product_check.escolhas_do_import(entry)))

    rechecados: list[str] = []
    for outro_id, order, escolhas in escolhidos:
        check = product_check.check_order(order, env=env, escolhas=escolhas)
        if check.get("available"):
            repo.update_fire_metadata(outro_id, check=check)
            rechecados.append(outro_id)
    return rechecados


@app.post("/api/imported/{import_id}/vincular-produto")
def vincular_produto(
    import_id: str,
    body: VincularProdutoRequest,
    request: Request,
    user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    """Vincula a chave exata do item (código e/ou EAN) a um produto do Fire,
    no escopo do cliente (`rede:` ou `raiz:`), para reaproveitar em todo pedido
    dele. Chave já vinculada a outro produto: 409 com o vínculo atual, e só
    troca com `substituir`. Enquanto o modo do ambiente não é `ligado`, grava
    também o de-para v1 do check legado. Audita, recheca este pedido e os
    pedidos em revisão do mesmo escopo. Só em pedidos em revisão.

    Item em `chave_repetida` (R18) ou já com escolha pontual: não grava v1 nem
    v2; grava a escolha só desta linha deste import (`_escolher_pontual`) e a
    resposta traz `"pontual": true`."""
    from app.erp import product_check, produto_escopo
    from app.models.order import Order
    from app.persistence import (
        catalogo_fire_repo,
        db,
        produto_depara_repo,
        produto_vinculo_repo,
        repo,
    )

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(status_code=409, detail="Vínculo só é permitido em pedidos em revisão")
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot do pedido indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc
    if not (0 <= body.item_index < len(order.items)):
        raise HTTPException(status_code=422, detail="item_index fora do intervalo")
    item = order.items[body.item_index]
    env = _request_environment(request)

    chaves = _chaves_do_item(item)
    if not chaves:
        raise HTTPException(status_code=422, detail="Item sem código nem EAN para vincular")

    with db.connect() as conn:
        prod = next(
            (
                p
                for p in catalogo_fire_repo.list_all(conn)
                if p["fire_produto_id"] == body.fire_produto_id
            ),
            None,
        )
    if prod is None or not str(prod["fire_produto_id"]).isdigit():
        raise HTTPException(status_code=422, detail="Produto não encontrado no catálogo local")
    fire_seq = int(prod["fire_produto_id"])

    # R18: chave repetida no pedido com descrições diferentes não separa as
    # linhas; um vínculo dela valeria para todas. A escolha fica só nesta linha
    # deste pedido (sem v1, sem v2, sem conflito).
    if _item_em_chave_repetida(entry, order, body.item_index):
        return _escolher_pontual(entry, order, body.item_index, prod, fire_seq, user, env)

    cnpj, intercompany = _cnpj_do_escopo(order, body.item_index, env)
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect() as conn:
        escopo = produto_escopo.escopo_de_gravacao(conn, cnpj)
        if escopo is None:
            raise HTTPException(status_code=422, detail=MSG_VINCULO_SEM_CNPJ)
        # Confere as duas chaves antes de gravar: conflito numa não deixa a
        # outra gravada pela metade.
        atuais = {
            tipo: next(iter(produto_vinculo_repo.lookup(
                conn,
                [escopo],
                codigos=[valor] if tipo == "codigo" else [],
                eans=[valor] if tipo == "ean" else [],
            ).values()), None)
            for tipo, valor in chaves
        }
        if not body.substituir:
            for tipo, _valor in chaves:
                atual = atuais[tipo]
                if atual is not None and atual.fire_seq != fire_seq:
                    return JSONResponse(status_code=409, content=_conflito_json(atual))
        gravados = []
        try:
            for tipo, valor in chaves:
                v = produto_vinculo_repo.vincular(
                    conn,
                    escopo=escopo,
                    chave_tipo=tipo,
                    chave_valor=valor,
                    fire_seq=fire_seq,
                    fire_codigo=prod.get("codprod_altern"),
                    fire_descricao=prod["nome"] or "",
                    por=user.email,
                    pedido_id=import_id,
                    substituir=body.substituir,
                    agora=now,
                )
                gravados.append(v)
        except produto_vinculo_repo.VinculoConflito as exc:
            # corrida com outro usuário entre a conferência e a gravação
            return JSONResponse(status_code=409, content=_conflito_json(exc.atual))

    # Check legado (v1) segue coerente enquanto a decisão nova não manda.
    # R13: pedido intercompany não grava v1 — o v1 é chaveado pelo cliente do
    # cabeçalho (a revenda) e valeria para todo cliente dela no caminho legado.
    grava_v1 = not intercompany and not _trava_produto_ligada(env)
    if grava_v1:
        ckey = produto_depara_repo.client_key(
            order.header.customer_cnpj, order.header.customer_name
        )
        with db.connect() as conn:
            for tipo, valor in (("ean", item.ean), ("codigo", item.product_code)):
                if not valor:
                    continue
                produto_depara_repo.upsert(
                    conn,
                    client_key=ckey,
                    chave_tipo=tipo,
                    chave_valor=valor,
                    fire_produto_id=prod["fire_produto_id"],
                    fire_codigo=prod["codigo"],
                    fire_ean=prod.get("ean"),
                    fire_nome=prod["nome"],
                    criado_em=now,
                    criado_por=user.email,
                )

    trocas = [
        {
            "chave_tipo": tipo,
            "chave_valor": valor,
            "fire_seq_antigo": atuais[tipo].fire_seq if atuais[tipo] is not None else None,
            "fire_seq_novo": fire_seq,
        }
        for tipo, valor in chaves
    ]
    trocou = any(t["fire_seq_antigo"] not in (None, fire_seq) for t in trocas)
    with with_trace_id(entry.get("trace_id")):
        repo.append_audit(
            import_id,
            "produto_vinculo_trocado" if trocou else "produto_vinculo_criado",
            {
                "item_index": body.item_index,
                "escopo": escopo,
                "fire_produto_id": prod["fire_produto_id"],
                "fire_nome": prod["nome"],
                "chaves": trocas,
                "vinculo_ids": [v.id for v in gravados],
                "v1": grava_v1,
                "pedido": order.header.order_number,
                "user_id": user.id,
                "user_email": user.email,
            },
        )

    # As escolhas pontuais de outras linhas deste import ficam no check regravado.
    check = product_check.check_order(
        order, env=env, escolhas=product_check.escolhas_do_import(repo.get_import(import_id))
    )
    # Persiste o check fresco para o re-open do pedido e o badge da lista
    # refletirem o vínculo (senão rehydrate_preview leria o check antigo e o
    # item apareceria como ✗ de novo). Só grava quando o check é confiável —
    # se a Fire estava fora, mantém o check anterior em vez de sobrescrever
    # com um "indisponível".
    if check.get("available"):
        repo.update_fire_metadata(import_id, check=check)
    # Trava de preço com ambiente fonte: o check da fonte é refeito também
    # (o vínculo pode ter mudado o produto). Desligado/sem fonte: None, sem I/O.
    from app.erp import preco_trava

    check_preco = preco_trava.check_preco_para_gravar(order, env, entry, check)
    if check_preco is not None:
        repo.update_fire_metadata(import_id, check_preco=check_preco)
    # R12: Fire fora agora → os outros também dariam indisponível; não recheca.
    rechecados = (
        _rechecar_pedidos_do_escopo(import_id, escopo, chaves, env)
        if check.get("available")
        else []
    )
    return JSONResponse(
        {
            "entry_id": import_id,
            "vinculos": [
                {"id": v.id, "env": (env or {}).get("slug") or env_context.current_env_slug(),
                 "escopo": v.escopo, "chave_tipo": v.chave_tipo, "chave_valor": v.chave_valor}
                for v in gravados
            ],
            "check": check,
            # A tela lê a trava do produto daqui (bloqueio, estados de planilha):
            # sem isto ela ficaria com a decisão de antes do vínculo.
            "produto_trava": _produto_trava_preview(check, _request_environment(request)),
            "rechecados": rechecados,
        }
    )


def _v1_do_vinculo_revogado(conn, v, ckey_criador: str | None) -> list[dict]:
    """Linhas do de-para v1 gravadas junto com o vínculo v2 revogado: mesma
    chave (normalizada), mesmo SEQ e cliente do pedido que criou o vínculo ou
    CNPJ do mesmo escopo. v1 apontando para outro SEQ fica intocado."""
    from app.erp import produto_escopo
    from app.persistence import produto_vinculo_repo

    rows = conn.execute(
        "SELECT id, client_key, chave_valor FROM produto_depara "
        "WHERE chave_tipo = ? AND fire_produto_id = ?",
        (v.chave_tipo, str(v.fire_seq)),
    ).fetchall()
    out = []
    for row_id, ckey, valor in rows:
        if produto_vinculo_repo.norm_chave(v.chave_tipo, valor) != v.chave_valor:
            continue
        mesmo_cliente = ckey_criador is not None and ckey == ckey_criador
        mesmo_escopo = (
            len(ckey) == 14
            and ckey.isdigit()
            and v.escopo in produto_escopo.escopos_para(conn, ckey)
        )
        if mesmo_cliente or mesmo_escopo:
            out.append({"id": row_id, "client_key": ckey})
    return out


@app.delete("/api/produtos/vinculo/{vinculo_id}")
def revogar_vinculo(
    vinculo_id: int,
    env: str = Query(..., min_length=1, max_length=64, description="slug da empresa do vínculo"),
    user: User = Depends(require_user),
) -> JSONResponse:
    """Revoga um vínculo v2 (a linha fica, com revogado_em/revogado_por) no
    banco da empresa `env` — o id do vínculo é por empresa, então a empresa
    vem explícita e nunca do cookie. Empresa inexistente ou inativa: 403 (o
    mesmo portão do middleware e do `env_do_pedido`: só empresa ativa).

    Enquanto o `produto_chave_modo` da empresa não é `ligado`, apaga também o
    de-para v1 gravado junto (senão o check legado seguiria casando). Audita no
    pedido que criou o vínculo, quando ele ainda existe (o `audit_log` exige um
    pedido); senão, log direto."""
    from app.models.order import Order
    from app.persistence import (
        db,
        environments_repo,
        produto_depara_repo,
        produto_vinculo_repo,
        repo,
    )
    from app.utils.logger import logger

    alvo = environments_repo.get_by_slug(env)
    if alvo is None or not alvo.get("is_active"):
        raise HTTPException(status_code=403, detail="Empresa indisponível")

    now = datetime.now().isoformat(timespec="seconds")
    with env_context.active_env(alvo["id"], alvo["slug"]):
        with db.connect() as conn:
            try:
                v = produto_vinculo_repo.revogar(conn, vinculo_id, por=user.email, agora=now)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="Vínculo não encontrado") from exc
            row = conn.execute(
                "SELECT pedido_id FROM produto_vinculo_v2 WHERE id = ?", (vinculo_id,)
            ).fetchone()
        pedido_id = row[0] if row else None
        pedido = repo.get_import(pedido_id) if pedido_id else None

        v1_apagados: list[dict] = []
        if not _trava_produto_ligada(alvo):
            ckey = None
            if pedido and pedido.get("snapshot"):
                try:
                    h = Order.model_validate(pedido["snapshot"]).header
                    ckey = produto_depara_repo.client_key(h.customer_cnpj, h.customer_name)
                except Exception:  # noqa: BLE001 — sem snapshot legível: só o escopo decide
                    ckey = None
            with db.connect() as conn:
                v1_apagados = _v1_do_vinculo_revogado(conn, v, ckey)
                for linha in v1_apagados:
                    conn.execute("DELETE FROM produto_depara WHERE id = ?", (linha["id"],))

        detalhe = {
            "vinculo_id": v.id,
            "env": alvo["slug"],
            "escopo": v.escopo,
            "chave_tipo": v.chave_tipo,
            "chave_valor": v.chave_valor,
            "fire_seq": v.fire_seq,
            "v1_apagados": v1_apagados,
            "user_id": user.id,
            "user_email": user.email,
        }
        if pedido is not None:
            repo.append_audit(pedido_id, "produto_vinculo_revogado", detalhe)
        else:
            logger.info(f"produto_vinculo_revogado: {detalhe}")
    return JSONResponse({"revogado": v.id})


@app.get("/api/produtos/a-identificar")
def produtos_a_identificar(
    request: Request,
    user: User = Depends(require_user),  # noqa: ARG001
) -> JSONResponse:
    """Itens a identificar dos pedidos em revisão do ambiente ativo: lê o check
    guardado (`produto.estado` diferente de `ok`), sem ir ao Fire. Mais antigos
    primeiro."""
    from app.erp import produto_trava
    from app.persistence import repo

    # Com planilha, o item `ok` que a planilha não aponta (sem código) ou que o
    # importador do Fire trocaria também para o envio: entra na fila, com o
    # mesmo estado e motivo do portão.
    planilha = _export_mode_atual() in ("xlsx", "both")
    entries = repo.list_imports(portal_status="parsed", limit=_RECHECK_MAX_PEDIDOS)
    linhas = []
    for entry in sorted(entries, key=lambda e: (e["imported_at"] or "", e["id"])):
        check = entry.get("check") or {}
        itens_check = check.get("items") or []
        itens = ((entry.get("snapshot") or {}).get("items")) or []
        da_planilha = {}
        if planilha:
            sem_codigo, trocas = produto_trava.itens_planilha(check)
            da_planilha = {x["index"]: x for x in sem_codigo + trocas}
        for i, ic in enumerate(itens_check):
            produto = (ic or {}).get("produto") if isinstance(ic, dict) else None
            if not isinstance(produto, dict):
                continue
            estado, motivo = produto.get("estado"), produto.get("motivo")
            if estado == "ok":
                if i not in da_planilha:
                    continue
                estado, motivo = da_planilha[i]["estado"], da_planilha[i]["motivo"]
            it = itens[i] if i < len(itens) and isinstance(itens[i], dict) else {}
            linhas.append(
                {
                    "import_id": entry["id"],
                    "pedido": entry.get("order_number"),
                    "cliente": entry.get("customer"),
                    "item_index": i,
                    "codigo": it.get("product_code"),
                    "ean": it.get("ean"),
                    "descricao": it.get("description"),
                    "estado": estado,
                    "motivo": motivo,
                    "importado_em": entry.get("imported_at"),
                }
            )
    modo = _produto_chave_modo(_request_environment(request))
    return JSONResponse({"modo": modo, "itens": linhas})


@app.delete("/api/produtos/depara/{depara_id}")
def delete_depara(
    depara_id: int,
    request: Request,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Desfaz um vínculo de-para v1. Log direto (não audit_log — sem import_id
    associado, e `audit_log.import_id` é NOT NULL com FK pra `imports`).

    M4: revoga também o vínculo v2 gêmeo — vigente, mesma chave (normalizada),
    mesmo SEQ, num escopo do CNPJ do `client_key` do v1 — e audita a revogação no
    pedido que criou o v2 (senão log). `client_key` sem CNPJ (nome do cliente): o
    escopo não sai dele; o v2 fica e a resposta traz `aviso`."""
    from app.erp import produto_escopo
    from app.persistence import db, produto_depara_repo, produto_vinculo_repo, repo
    from app.utils.logger import logger

    now = datetime.now().isoformat(timespec="seconds")
    revogados = []
    aviso = None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT client_key, chave_tipo, chave_valor, fire_produto_id "
            "FROM produto_depara WHERE id = ?",
            (depara_id,),
        ).fetchone()
        produto_depara_repo.delete(conn, depara_id)
        if row is not None:
            ckey, tipo, valor, seq = row
            escopos = produto_escopo.escopos_para(conn, ckey)
            if not escopos:
                aviso = (
                    "O vínculo novo deste cliente não foi revogado (o de-para antigo não "
                    "tem CNPJ). Revogue pela fila de produtos, se precisar."
                )
            else:
                chave = produto_vinculo_repo.norm_chave(tipo, valor)
                gemeos = [
                    v
                    for e in escopos
                    for v in produto_vinculo_repo.listar(conn, escopo=e)
                    if v.chave_tipo == tipo and v.chave_valor == chave and str(v.fire_seq) == str(seq)
                ]
                for v in gemeos:
                    pedido = conn.execute(
                        "SELECT pedido_id FROM produto_vinculo_v2 WHERE id = ?", (v.id,)
                    ).fetchone()
                    produto_vinculo_repo.revogar(conn, v.id, por=user.email, agora=now)
                    revogados.append((v, pedido[0] if pedido else None))
    for v, pedido_id in revogados:
        detalhe = {
            "vinculo_id": v.id,
            "env": env_context.current_env_slug(),
            "escopo": v.escopo,
            "chave_tipo": v.chave_tipo,
            "chave_valor": v.chave_valor,
            "fire_seq": v.fire_seq,
            "origem": "depara_v1_removido",
            "depara_id": depara_id,
            "user_id": user.id,
            "user_email": user.email,
        }
        if pedido_id and repo.get_import(pedido_id) is not None:
            repo.append_audit(pedido_id, "produto_vinculo_revogado", detalhe)
        else:
            logger.info(f"produto_vinculo_revogado: {detalhe}")
    logger.info(
        f"produto_vinculo_removido: depara_id={depara_id} user={user.email} "
        f"v2_revogados={[v.id for v, _ in revogados]}"
    )
    return JSONResponse(
        {"deleted": depara_id, "v2_revogados": [v.id for v, _ in revogados], "aviso": aviso}
    )


@app.post("/api/imported/{import_id}/ack-sem-preco")
def ack_sem_preco(
    import_id: str,
    request: Request,
    user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    """Registra ack do operador para itens sem preço cadastrado no Fire.

    Re-roda o check_order para coletar a lista atual de itens com
    price_status='no_price_in_fire' e persiste em imports.sem_preco_ack_*.
    Audit log grava a ação. Itens com mismatch ou no_order_price NÃO podem
    ser ack-ados — devolveriam 409 implicitamente porque o guard server-side
    no envio bloqueia mesmo com ack.
    """
    from app.erp.product_check import check_order
    from app.models.order import Order
    from app.observability import metrics
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(
            status_code=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc

    request_env = getattr(request.state, "environment", None)
    from app.erp.product_check import escolhas_do_import

    with with_trace_id(entry.get("trace_id")):
        check = check_order(order, env=request_env, escolhas=escolhas_do_import(entry))
        if not check.get("available"):
            raise HTTPException(status_code=503, detail="Fire indisponível para validar ack")

        items_acked = [
            {
                "ean": it.get("ean"),
                "product_code": it.get("product_code"),
                "fire_product_id": it.get("fire_product_id"),
            }
            for it in check.get("items", [])
            if it.get("price_status") == "no_price_in_fire"
        ]

        repo.set_sem_preco_ack(import_id, by_email=user.email, items=items_acked)
        repo.append_audit(
            import_id,
            "sem_preco_acknowledged",
            {"user_email": user.email, "user_id": user.id, "items": items_acked},
        )
        metrics.price_check_acks_total.inc()

    fresh = repo.get_import(import_id)
    return JSONResponse(
        {
            "entry_id": import_id,
            "ack_by": fresh["sem_preco_ack_by"],
            "ack_at": fresh["sem_preco_ack_at"],
            "items_acked": items_acked,
        }
    )


@app.post("/api/imported/{import_id}/reconferir")
def reconferir_pedido(
    import_id: str,
    request: Request,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    """Confere de novo no Fire o pedido em revisão (com as escolhas pontuais do
    import), grava o check e devolve o check e a trava da tela. Sai do beco do
    check salvo antes do pacote da chave exata (itens sem `produto`). Fire fora:
    devolve o check indisponível e mantém o check salvo."""
    from app.erp import preco_trava, product_check
    from app.models.order import Order
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(
            status_code=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc

    env = _request_environment(request)
    with with_trace_id(entry.get("trace_id")):
        check = product_check.check_order(
            order, env=env, escolhas=product_check.escolhas_do_import(entry)
        )
        if check.get("available"):
            repo.update_fire_metadata(import_id, check=check)
            check_preco = preco_trava.check_preco_para_gravar(order, env, entry, check)
            if check_preco is not None:
                repo.update_fire_metadata(import_id, check_preco=check_preco)
    return JSONResponse(
        {
            "entry_id": import_id,
            "check": check,
            "produto_chave_modo": _produto_chave_modo(env),
            "produto_trava": _produto_trava_preview(check, env),
        }
    )


class MoverEmpresaRequest(BaseModel):
    destino: str


@app.post("/api/imported/{import_id}/mover-empresa")
def mover_pedido_de_empresa(
    import_id: str,
    body: MoverEmpresaRequest,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Move o pedido em revisão para a outra empresa (`mover_pedido.mover_import`)
    e confere de novo no destino, como o "Conferir de novo": check de produto
    no Fire do destino (o de-para v2 lê o SQLite dele) e check de preço. Falha
    da conferência não desfaz o mover — o pedido fica sem check e o detalhe
    refaz. Só fora de `desligado` — lá, só CONCLUI um mover interrompido
    (pedido já no destino e na origem), nunca começa um."""
    import sqlite3

    from app.erp import preco_trava, product_check
    from app.models.order import Order
    from app.persistence import decisao_ambiente_repo as memoria
    from app.persistence import environments_repo, repo, roteamento_decisao_repo, roteamento_repo
    from app.persistence.mover_pedido import PedidoNaoMovivel, PedidoOcupado, mover_import
    from app.routing.ambiente import cnpjs_do_pedido
    from app.utils.logger import logger

    modo = roteamento_repo.modo()
    envs = environments_repo.list_active()
    destino = next((e for e in envs if e["slug"] == body.destino), None)
    # Empresas que têm o pedido: uma, ou duas num mover interrompido.
    onde = [e for e in envs if env_do_import_id(import_id, envs=[e]) is not None]
    concluir = destino in onde and len(onde) > 1
    if modo == roteamento_repo.DESLIGADO and not concluir:
        raise HTTPException(
            status_code=409,
            detail="Mover pedido entre empresas exige o roteamento em observando ou ligado",
        )
    if not onde:
        raise HTTPException(
            status_code=404, detail="Pedido não encontrado em nenhuma empresa ativa"
        )
    if destino is None:
        raise HTTPException(status_code=422, detail="Empresa de destino inexistente ou inativa")
    # A origem é a empresa que tem o pedido e NÃO é o destino: num mover
    # interrompido ele está nos dois, e é assim que a segunda chamada conclui.
    origens = [e for e in onde if e["id"] != destino["id"]]
    if not origens:
        raise HTTPException(status_code=422, detail="O pedido já está nessa empresa")
    if len(origens) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "O pedido está em duas empresas; conclua o mover anterior antes de mover de novo."
            ),
        )
    (origem,) = origens

    try:
        estado = mover_import(import_id, origem=origem, destino=destino, por=user.email)
    except PedidoNaoMovivel as exc:
        return JSONResponse(
            {"detail": f"O pedido não pode mudar de empresa: {exc.motivo}.", "motivo": exc.motivo},
            status_code=409,
        )
    except PedidoOcupado:
        return JSONResponse(
            {"detail": "O pedido está sendo enviado agora; tente de novo em instantes."},
            status_code=409,
        )
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc):
            raise
        # Banco de uma das empresas ocupado além do timeout: nada foi apagado
        # na origem (a cópia, se houve, a próxima chamada conclui).
        return JSONResponse(
            {"detail": "O banco da empresa está ocupado; tente de novo em instantes."},
            status_code=409,
        )

    resposta = {"import_id": import_id, "de": origem["slug"], "para": destino["slug"]}
    if estado == "ja_movido":  # outro clique já moveu: nada a refazer nem registrar
        return JSONResponse(resposta)

    order = None
    with env_context.active_env(destino["id"], destino["slug"]):
        try:
            entry = repo.get_import(import_id)
            order = Order.model_validate(entry["snapshot"])
        except Exception as exc:  # noqa: BLE001 — sem o pedido legível: sem check e sem memória
            logger.warning(
                "mover.pedido_ilegivel_no_destino import_id={} env={} erro={}",
                import_id,
                destino["slug"],
                type(exc).__name__,
            )
        if order is not None:
            try:
                with with_trace_id(entry.get("trace_id")):
                    check = product_check.check_order(
                        order, env=destino, escolhas=product_check.escolhas_do_import(entry)
                    )
                    if check.get("available"):
                        repo.update_fire_metadata(import_id, check=check)
                    check_preco = preco_trava.check_preco_para_gravar(
                        order, destino, entry, check
                    )
                    if check_preco is not None:
                        repo.update_fire_metadata(import_id, check_preco=check_preco)
            except Exception as exc:  # noqa: BLE001 — o pedido já mudou; o check é refeito depois
                logger.warning(
                    "mover.check_destino_falhou import_id={} env={} erro={}",
                    import_id,
                    destino["slug"],
                    type(exc).__name__,
                )

    try:
        ultima = roteamento_decisao_repo.ultima(import_id)
    except Exception as exc:  # noqa: BLE001 — o mover já foi gravado; o rastro sai sem sugestão
        logger.warning(
            "mover.ultima_decisao_falhou import_id={} erro={}", import_id, type(exc).__name__
        )
        ultima = None
    # Rastro e memória uma vez por mover: concluir um interrompido só registra
    # se o rastro ainda não tem este mover (o interrompido não chegou a gravar).
    if (
        estado == "concluido"
        and ultima is not None
        and ultima.get("acao") == "movido_apos_importar"
        and ultima.get("env_escolhido") == destino["slug"]
    ):
        return JSONResponse(resposta)
    roteamento_decisao_repo.registrar(
        import_id=import_id,
        modo=modo,
        degrau=None,
        env_sugerido=(ultima or {}).get("env_sugerido"),
        env_escolhido=destino["slug"],
        decidido_por=user.email,
        motivo=f"movido de {origem['slug']} para {destino['slug']}",
        acao="movido_apos_importar",
    )
    # A memória aprende com o operador, como na troca do preview (só em
    # 'ligado'; em 'observando' nada aprende, para a taxa medir o roteador).
    if modo == roteamento_repo.LIGADO and order is not None:
        try:
            cnpj_cliente = (cnpjs_do_pedido(order) or [""])[0]
            if cnpj_cliente:
                memoria.lembrar(cnpj_cliente=cnpj_cliente, env_slug=destino["slug"], por=user.email)
        except Exception as exc:  # noqa: BLE001 — memória não derruba um mover já gravado
            logger.warning(
                "mover.memoria_falhou import_id={} erro={}", import_id, type(exc).__name__
            )
    return JSONResponse(resposta)


@app.get("/api/imported/{import_id}/preview")
def rehydrate_preview(
    import_id: str,
    request: Request,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    """Rebuild the preview payload for a stored order (for the review modal)."""
    from app.models.order import Order
    from app.persistence import repo

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
    payload["fire_status_last_seen"] = entry.get("fire_status_last_seen")
    payload["arquivo_original"] = _arquivo_original_info(entry)
    payload["produto_chave_modo"] = _produto_chave_modo(_request_environment(request))
    payload["produto_trava"] = _produto_trava_preview(check, _request_environment(request))
    payload["sem_preco_dispensado"] = _sem_preco_dispensado_no_preview(
        _request_environment(request)
    )
    # Situação de preço por item (trava). Só leitura; ausente em `desligado` e
    # em qualquer falha — o preview sempre renderiza.
    try:
        from datetime import UTC, datetime

        from app.erp import preco_trava

        request_env = _request_environment(request)
        if _trava_produto_ligada(request_env):
            # Mesmo SEQ que o envio avalia em `ligado`: o decidido do check salvo.
            from app.erp import produto_trava

            _, produtos_por_item = produto_trava.produtos_decididos(entry.get("check"))
        else:
            _, produtos_por_item = _vinculos_de_para(order, "preview", entry)
        preco = preco_trava.preview_preco(
            import_id,
            entry,
            order,
            env=_request_environment(request),
            agora=datetime.now(UTC),
            produtos_por_item=produtos_por_item,
        )
        if preco is not None:
            payload["preco"] = preco
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger

        logger.warning(f"preview: avaliação de preço indisponível ({type(exc).__name__}): {exc}")
    # Só existe quando a empresa foi RESOLVIDA pelo pedido (roteamento
    # 'ligado', via `env_do_pedido`) — fora dali nada muda: o selo já existe
    # na linha da caixa somada, mas some ao abrir o modal, que é a tela onde
    # o operador decide gravar. Ver Menor 1 do lote 2.
    payload["env_name"] = _env.get("name") if _env is not None else None
    # Empresa onde o pedido está (a do pedido em 'ligado', senão a do cookie)
    # e se o "Mover para" pode ser oferecido: mesma guarda do mover, para a
    # tela não prometer o que a rota recusa (ex.: XLSX já exportado). Em
    # 'desligado' nada disso roda (o mover não existe); `roteamento_modo`
    # diz à tela que não há rastro a buscar. Erro aqui nunca impede abrir o
    # pedido: sem mover.
    from app.persistence import mover_pedido, roteamento_repo

    _onde = _env if _env is not None else _request_environment(request)
    payload["env_slug"] = _onde.get("slug") if _onde is not None else None
    payload["roteamento_modo"] = None
    payload["motivo_nao_move"] = None
    payload["pode_mover"] = False
    try:
        payload["roteamento_modo"] = roteamento_repo.modo()
        if payload["roteamento_modo"] != roteamento_repo.DESLIGADO:
            motivo = mover_pedido.motivo_nao_move(import_id)
            payload["motivo_nao_move"] = motivo
            payload["pode_mover"] = motivo is None
    except Exception as exc:  # noqa: BLE001 — o detalhe abre sem o "Mover para"
        from app.utils.logger import logger

        logger.warning(
            "preview.guarda_mover_falhou import_id={} erro={}", import_id, type(exc).__name__
        )

    # Surface the manual cliente override into the check banner so the UI shows
    # it in green instead of the original "✗ Cliente não encontrado" red flag.
    override_codigo = entry.get("cliente_override_codigo")
    if override_codigo and payload.get("check"):
        payload["check"]["client"] = {
            "match": True,
            "fire_id": override_codigo,
            "razao_social": entry.get("cliente_override_razao"),
            "cnpj": entry.get("customer_cnpj"),
            "override": True,
        }
        if "summary" in payload["check"]:
            payload["check"]["summary"]["client_matched"] = True
    payload["cliente_override"] = (
        {
            "codigo": override_codigo,
            "razao_social": entry.get("cliente_override_razao"),
            "at": entry.get("cliente_override_at"),
            "by": entry.get("cliente_override_by"),
        }
        if override_codigo
        else None
    )
    payload["sem_preco_ack"] = (
        {
            "by": entry.get("sem_preco_ack_by"),
            "at": entry.get("sem_preco_ack_at"),
            "items": entry.get("sem_preco_ack_items") or [],
        }
        if entry.get("sem_preco_ack_by")
        else None
    )

    # De-para de cliente intercompany: mostra no preview o cliente que o Flow
    # vai receber. Leitura barata (só dispara quando o cliente é a revenda) e
    # best-effort — preview nunca pode quebrar por causa do Firebird da revenda.
    payload["depara_cliente"] = None
    slug = (_request_environment(request) or {}).get("slug")
    if slug:
        try:
            res = resolucao_para(order, slug=slug)
        except Exception:  # noqa: BLE001
            res = None
        if res is not None:
            payload["depara_cliente"] = {
                "resolvido": res.resolvido,
                "cnpj": res.cnpj,
                "nome": res.nome,
                "motivo": res.motivo,
            }
    return JSONResponse(payload)


# Keep /api/process for backward compat (drag-drop upload flow)
@app.post("/api/process")
async def process_files(
    request: Request,
    _user: User = Depends(require_user),
    files: list[UploadFile] = File(...),
    output_dir: str = Form("output"),
) -> JSONResponse:
    from app.erp import preco_trava
    from app.exporters.erp_exporter import ERPExporter
    from app.ingestion.file_loader import LoadedFile

    # Caminho legado: gera XLSX sem o portão de preço nem o de produto. Com uma
    # trava ativa, a planilha só sai pela revisão (preview → exportar/enviar).
    request_env = _request_environment(request)
    if _trava_produto_ligada(request_env):
        raise HTTPException(
            status_code=409,
            detail="Trava de produto ligada: use a revisão de pedidos.",
        )
    if preco_trava.trava_ativa(request_env):
        raise HTTPException(
            status_code=409,
            detail="Com a trava de preço ativa, use a revisão do pedido para exportar.",
        )

    output_path = Path(output_dir).expanduser().resolve()
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return JSONResponse(
            {
                "results": [],
                "errors": [{"source": "—", "error": f"Pasta inválida: {exc}"}],
            }
        )

    exporter = ERPExporter()
    results = []
    errors = []

    for upload in files:
        filename = upload.filename or "arquivo"
        ext = Path(filename).suffix.lower()

        if ext not in ALLOWED_EXTENSIONS:
            leituras.rejeitado("process-legado", filename, "extensao", ext, usuario=_user.email)
            errors.append({"source": filename, "error": f"Tipo de arquivo não suportado: {ext}"})
            continue

        raw = await upload.read()

        if len(raw) > MAX_UPLOAD_BYTES:
            leituras.rejeitado(
                "process-legado", filename, "tamanho", tamanho=len(raw), usuario=_user.email
            )
            errors.append(
                {
                    "source": filename,
                    "error": f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
                }
            )
            continue

        try:
            _guardar_original(raw, filename)
        except HTTPException as exc:
            leituras.rejeitado(
                "process-legado",
                filename,
                "guarda_falhou",
                str(exc.detail),
                tamanho=len(raw),
                usuario=_user.email,
            )
            errors.append({"source": filename, "error": exc.detail})
            continue
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)

            loaded = LoadedFile(path=tmp_path, extension=ext, raw=raw)
            paths = None
            with (
                with_trace_id(),
                leituras.registrar(
                    origem="process-legado", nome=filename, raw=raw, usuario=_user.email
                ) as L,
            ):
                order = L.processar(loaded)
                if order:
                    paths = exporter.export(order, str(output_path))
                L.resultado = "importado" if order else "erro"

            if order:
                results.append(
                    {
                        "source": filename,
                        "order": order.header.order_number or "—",
                        "files": [{"name": p.name, "path": str(p)} for p in paths],
                    }
                )
            else:
                errors.append(
                    {
                        "source": filename,
                        "error": L.diag.recusa or "Formato não reconhecido ou pedido sem itens",
                    }
                )
        except Exception as exc:
            errors.append({"source": filename, "error": str(exc)})
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

    return JSONResponse({"results": results, "errors": errors})
