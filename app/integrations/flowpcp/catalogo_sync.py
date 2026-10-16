from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

from app.erp.catalog_extract import extract_composicao, extract_produtos
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp import catalogo_composicao
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import catalogo_fire_repo, environments_repo, router
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"


class ConfirmacaoSemApplyError(ValueError):
    """Confirmação de trava pedida junto com dry-run."""


class ForaDoEnvioInexistenteError(ValueError):
    """SEQ pedido em `fora_do_envio` que não está no catálogo extraído. Falha
    fechada: a exclusão confirmada na resposta tem que ter acontecido."""

    def __init__(
        self, seqs: list[str], guardados: list[str] | None = None, variavel: str | None = None
    ):
        self.seqs = seqs
        self.variavel = variavel
        # Os que vieram da exclusão guardada (variável de ambiente), não da rota.
        self.guardados = guardados or []
        super().__init__(f"fora do envio, mas não estão no catálogo extraído: {', '.join(seqs)}")


@dataclass(frozen=True)
class CatalogoLocalResult:
    """Resultado do sync quando o envio ao Flow está DESLIGADO
    (flowpcp_catalogo_push=0): a extração foi gravada só na cópia local."""

    itens: int
    extraido_em: str


def _build_client(cfg) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def run_catalogo_sync(
    slug: str,
    *,
    dry_run: bool = True,
    full_sync: bool = True,
    now_iso: str | None = None,
    fora_do_envio: frozenset[str] = frozenset(),
    confirmar_remocao: bool = False,
    confirmar_substituicao: bool = False,
    _client=None,
    _fire_conn=None,
    _env_conn=None,
) -> CatalogoReconciliacaoResponse | CatalogoLocalResult | None:
    """Extrai o catálogo do Fire do ambiente `slug`, SEMPRE grava a cópia local
    (`catalogo_fire` no db do ambiente) e — só se `flowpcp_catalogo_push` estiver
    ligado — empurra ao FlowPCP.

    Retorna:
    - `CatalogoReconciliacaoResponse` — enviado ao Flow (gate ON)
    - `CatalogoLocalResult` — só cópia local atualizada (gate OFF)
    - `None` — ambiente sem FlowPCP habilitado
    `fora_do_envio`: SEQs que não vão ao Flow nesta rodada (a cópia local continua
    com o Fire inteiro). Existe para segurar cadastro com problema no Fire sem
    travar o resto: em 15/10/2026, 10 pares do listrado Sam's tinham 2 cadastros
    e virariam par duplicado no Flow. Vale só para a rodada (rota `excluir=`); a exclusão
    guardada `PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>` vale em toda rodada, manual e
    diária. SEQ pedido que não está no catálogo extraído levanta `ForaDoEnvioInexistenteError`
    antes de qualquer envio.
    Composição (`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO` com o slug): lê PRODUTOS_KIT
    e manda `componentes`/`codigoMes` pelas regras de `catalogo_composicao`; o
    relatório ganha `composicao_portal`. `PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>`
    soma-se a `fora_do_envio` em toda rodada, manual ou diária. As confirmações
    liberam as travas do Flow e só valem com `dry_run=False`.
    `_client`/`_fire_conn`/`_env_conn` são injeção de teste (default constrói os reais)."""
    if dry_run and (confirmar_remocao or confirmar_substituicao):
        raise ConfirmacaoSemApplyError(
            "confirmar trava de composição só vale com dryRun=false (apply)"
        )
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"catalogo sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        fire_ctx = FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))

    with fire_ctx as fire_conn:
        dtos = extract_produtos(
            fire_conn, apenas_meias=getattr(cfg, "catalogo_apenas_meias", False)
        )
        ligado = getattr(cfg, "catalogo_push", False) and catalogo_composicao.envio_ligado(slug)
        lida = None
        if ligado:
            try:
                lida = extract_composicao(fire_conn)
            except Exception as exc:  # noqa: BLE001 — falha vira "ausente em todos", nunca "vazio"
                logger.error(f"catalogo sync env={slug}: leitura de PRODUTOS_KIT falhou: {exc!r}")

    # Cópia local SEMPRE — "manter no importador" independe do envio ao Flow.
    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        catalogo_fire_repo.replace_all(env_conn, dtos, extraido_em=extraido_em)
    logger.info(f"catalogo sync env={slug} itens={len(dtos)} — cópia local atualizada")

    if not getattr(cfg, "catalogo_push", False):
        logger.info(f"catalogo sync env={slug}: envio ao Flow DESLIGADO (catalogo_push=0)")
        return CatalogoLocalResult(itens=len(dtos), extraido_em=extraido_em)

    guardado = catalogo_composicao.excluir_guardado(slug)
    fora = fora_do_envio | guardado
    if fora:
        nao_achados = sorted(fora - {d.fire_produto_id for d in dtos}, key=int)
        if nao_achados:
            raise ForaDoEnvioInexistenteError(
                nao_achados,
                guardados=[s for s in nao_achados if s in guardado],
                variavel=catalogo_composicao._var_excluir(slug),
            )
        logger.info(
            f"catalogo sync env={slug}: {len(fora)} itens fora do envio "
            f"(solicitados: {sorted(fora_do_envio, key=int)}, guardados: {sorted(guardado, key=int)})"
        )
    enviados = [d for d in dtos if d.fire_produto_id not in fora]

    composicao = codigos = resumo = None
    if ligado:
        composicao = catalogo_composicao.montar_composicao(enviados, lida)
        codigos, repetidos = catalogo_composicao.codigos_mes(enviados)
        resumo = catalogo_composicao.resumo(composicao, codigos, repetidos)
        _logar_composicao(slug, resumo)
    client = _client or _build_client(cfg)
    try:
        request = build_catalogo_request(
            enviados,
            dry_run=dry_run,
            full_sync=full_sync,
            importador_versao=_IMPORTADOR_VERSAO,
            extraido_em=extraido_em,
            composicao=composicao,
            codigos_mes=codigos,
            confirmar_remocao=confirmar_remocao,
            confirmar_substituicao=confirmar_substituicao,
        )
        logger.info(
            f"catalogo sync env={slug} itens={len(enviados)} dry_run={dry_run} full_sync={full_sync}"
        )
        try:
            rep = client.send_catalogo(request)
        except FlowPCPClientError as exc:
            if ligado and exc.status_code == 400:
                # o caminho do Zod (campo recusado) só aparece no corpo; sem segredo nele
                logger.error(
                    f"catalogo sync env={slug}: Flow recusou status=400 body={(exc.body or '')[:500]}"
                )
                raise FlowPCPClientError(
                    f"{exc} — o Flow recusou o catálogo com composição. Se a Fatia A não "
                    f"estiver no ar, tire '{slug}' de PORTAL_FLOWPCP_CATALOGO_COMPOSICAO",
                    status_code=exc.status_code,
                    body=exc.body,
                ) from exc
            raise
        if resumo is not None:
            rep.composicao_portal = resumo
        if guardado:
            rep.fora_do_envio_guardado = sorted(guardado, key=int)
        return rep
    finally:
        if _client is None:
            client.close()


def _logar_composicao(slug: str, resumo: dict) -> None:
    logger.info(
        f"catalogo sync env={slug} composicao: leitura_ok={resumo['leitura_ok']} "
        f"com_lista={resumo['com_lista']} pares_vazios={resumo['pares_vazios']} "
        f"kits_sem_composicao={resumo['kits_sem_composicao']} omitidos={len(resumo['omitidos'])} "
        f"codigo_mes={resumo['codigo_mes']}"
    )
    if not resumo["leitura_ok"]:
        logger.warning(f"catalogo sync env={slug} composicao não enviada: {resumo['motivo_falha']}")
    for seq, motivo in resumo["omitidos"].items():
        logger.warning(f"catalogo sync env={slug} composicao omitida SEQ {seq}: {motivo}")
    for seq, aviso in resumo["divergencia_nome"].items():
        logger.warning(f"catalogo sync env={slug} quantidade diverge do nome SEQ {seq}: {aviso}")
    for codigo, seqs in resumo["codigo_mes_repetido"].items():
        logger.warning(f"catalogo sync env={slug} codigoMes {codigo} repetido em {seqs}: vai null")
