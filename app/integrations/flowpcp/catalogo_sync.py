from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

from app.erp.catalog_extract import (
    extract_com_nota,
    extract_composicao,
    extract_marcas,
    extract_produtos,
)
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp import (
    capacidades,
    catalogo_composicao,
    catalogo_fiscal,
    catalogo_marcas,
)
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import cadastro_volta_repo, catalogo_fire_repo, environments_repo, router
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
    Fiscal (`PORTAL_FLOWPCP_CATALOGO_FISCAL` com o slug): manda NCM, tipo SPED, grupo,
    código NF-e só quando válidos; o relatório ganha `fiscal_portal`, e sem
    `fiscal.v1` nas `capacidades` da resposta (o Flow ignorou os campos) vira
    `capacidade_ok=false` e erro no log.
    Marcas (`PORTAL_FLOWPCP_CATALOGO_MARCAS`) e nota (`PORTAL_FLOWPCP_CATALOGO_NOTA`): mandam
    `marcasErp` (filtradas pelas regras do Flow) e `temNotaFiscal` por produto; o relatório
    ganha `marcas_portal`/`nota_portal` com o que o Flow devolveu, e `erro: true` na resposta
    vira ERROR no log e `reenviar: true` no bloco. Em toda rodada saem do lote os SEQs que
    o livro da volta tem em aberto (`cadastro_volta_repo.seqs_em_aberto`), reportados em
    `fora_do_envio_volta`: o Flow criaria outro produto para um SEQ que o Portal acabou de gravar.
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
        marcas_lidas = nota_lida = None
        if getattr(cfg, "catalogo_push", False):
            if catalogo_marcas.envio_ligado(slug):
                try:
                    marcas_lidas = extract_marcas(fire_conn)
                except Exception as exc:  # noqa: BLE001 — falha vira "marcasErp ausente"
                    logger.error(
                        f"catalogo sync env={slug}: leitura de PRODUTOS_MARCA falhou: {exc!r}"
                    )
            if catalogo_marcas.nota_ligada(slug):
                try:
                    nota_lida = extract_com_nota(fire_conn)
                except Exception as exc:  # noqa: BLE001 — falha vira "temNotaFiscal ausente", nunca False
                    logger.error(f"catalogo sync env={slug}: leitura de NOTAPROD falhou: {exc!r}")

    # Cópia local SEMPRE — "manter no importador" independe do envio ao Flow.
    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        catalogo_fire_repo.replace_all(env_conn, dtos, extraido_em=extraido_em)
    logger.info(f"catalogo sync env={slug} itens={len(dtos)} — cópia local atualizada")

    if not getattr(cfg, "catalogo_push", False):
        logger.info(f"catalogo sync env={slug}: envio ao Flow DESLIGADO (catalogo_push=0)")
        return CatalogoLocalResult(itens=len(dtos), extraido_em=extraido_em)

    # Leitura própria, depois do commit da cópia local: se falhar, a cópia fica e o
    # envio aborta (falha fechada — sem o livro não dá para saber o que está em trânsito).
    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        em_aberto = cadastro_volta_repo.seqs_em_aberto(env_conn)

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
    # SEQ em trânsito na volta: some do lote sem erro se não está no extract.
    volta = em_aberto & {d.fire_produto_id for d in dtos}
    if volta:
        logger.info(
            f"catalogo sync env={slug}: {len(volta)} itens fora do envio por estarem em aberto "
            f"na volta: {sorted(volta, key=int)}"
        )
    enviados = [d for d in dtos if d.fire_produto_id not in fora | volta]

    composicao = codigos = resumo = None
    if ligado:
        composicao = catalogo_composicao.montar_composicao(enviados, lida)
        codigos, repetidos = catalogo_composicao.codigos_mes(enviados)
        resumo = catalogo_composicao.resumo(composicao, codigos, repetidos)
        _logar_composicao(slug, resumo)
    fiscal = fiscal_resumo = None
    if catalogo_fiscal.envio_ligado(slug):
        fiscal, pendencias = {}, {}
        for d in enviados:
            campos, motivos = catalogo_fiscal.campos_fiscais(d)
            fiscal[d.fire_produto_id] = campos
            if motivos:
                pendencias[d.fire_produto_id] = motivos
        fiscal_resumo = catalogo_fiscal.resumo(pendencias, enviados=len(enviados))
    marcas = marcas_resumo = com_nota = nota_resumo = None
    if catalogo_marcas.envio_ligado(slug):
        if marcas_lidas is None:
            marcas_resumo = {"enviadas": 0, "filtradas": [], "leitura_ok": False}
        else:
            marcas, filtradas = catalogo_marcas.filtrar(marcas_lidas)
            marcas_resumo = {"enviadas": len(marcas), "filtradas": filtradas, "leitura_ok": True}
    if catalogo_marcas.nota_ligada(slug):
        com_nota_no_lote = sum(1 for d in enviados if d.fire_produto_id in (nota_lida or ()))
        # Vazia, ou sem nenhum SEQ do lote, é anomalia (banco errado, base restaurada):
        # mandar False em todos tiraria a nota de todo produto no Flow.
        if nota_lida is None:
            nota_resumo = {"com_nota": 0, "enviados": 0, "leitura_ok": False}
        elif not nota_lida or (enviados and not com_nota_no_lote):
            logger.error(
                f"catalogo sync env={slug}: NOTAPROD veio {'vazia' if not nota_lida else 'sem nenhum SEQ do lote'} "
                "— temNotaFiscal não enviado"
            )
            nota_resumo = {"com_nota": 0, "enviados": 0, "leitura_ok": False}
        else:
            com_nota = nota_lida
            nota_resumo = {
                "com_nota": com_nota_no_lote,
                "enviados": len(enviados),
                "leitura_ok": True,
            }
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
            fiscal=fiscal,
            marcas=marcas,
            com_nota=com_nota,
        )
        logger.info(
            f"catalogo sync env={slug} itens={len(enviados)} dry_run={dry_run} full_sync={full_sync}"
        )
        try:
            rep = client.send_catalogo(request)
        except FlowPCPClientError as exc:
            vars_ligadas = [
                v
                for v, on in (
                    ("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", ligado),
                    ("PORTAL_FLOWPCP_CATALOGO_MARCAS", marcas is not None),
                    ("PORTAL_FLOWPCP_CATALOGO_NOTA", com_nota is not None),
                )
                if on
            ]
            if vars_ligadas and exc.status_code == 400:
                # o caminho do Zod (campo recusado) só aparece no corpo; sem segredo nele
                logger.error(
                    f"catalogo sync env={slug}: Flow recusou status=400 body={(exc.body or '')[:500]}"
                )
                raise FlowPCPClientError(
                    f"{exc} — o Flow recusou o catálogo com campos novos. Se o lado dele "
                    f"não estiver no ar, tire '{slug}' de {' / '.join(vars_ligadas)}",
                    status_code=exc.status_code,
                    body=exc.body,
                ) from exc
            raise
        if resumo is not None:
            rep.composicao_portal = resumo
        if fiscal_resumo is not None:
            ok = capacidades.FISCAL in capacidades.de(rep)
            fiscal_resumo["capacidade_ok"] = ok
            rep.fiscal_portal = fiscal_resumo
            if not ok:
                logger.error(
                    f"catalogo sync env={slug}: o Flow ignorou os campos fiscais (sem "
                    f"'{capacidades.FISCAL}' na resposta) — tire '{slug}' de PORTAL_FLOWPCP_CATALOGO_FISCAL "
                    "até o Flow aceitar"
                )
            elif fiscal_resumo["com_pendencia"]:
                logger.warning(
                    f"catalogo sync env={slug} fiscal: {fiscal_resumo['com_pendencia']} com pendência "
                    f"{fiscal_resumo['por_motivo']}"
                )
        if marcas_resumo is not None:
            rep.marcas_portal = _ler_bloco(
                slug,
                rep,
                "marcas",
                capacidades.MARCAS,
                marcas_resumo,
                ("vinculadas", "jaVinculadas", "semPar", "conflitos", "recebidas"),
                "PORTAL_FLOWPCP_CATALOGO_MARCAS",
                not dry_run,
            )
        if nota_resumo is not None:
            rep.nota_portal = _ler_bloco(
                slug,
                rep,
                "notaFiscal",
                capacidades.NOTA,
                nota_resumo,
                ("atualizados",),
                "PORTAL_FLOWPCP_CATALOGO_NOTA",
                not dry_run,
            )
        if guardado:
            rep.fora_do_envio_guardado = sorted(guardado, key=int)
        if volta:
            rep.fora_do_envio_volta = sorted(volta, key=int)
        return rep
    finally:
        if _client is None:
            client.close()


def _ler_bloco(
    slug: str,
    rep,
    chave: str,
    capacidade: str,
    resumo: dict,
    campos: tuple[str, ...],
    variavel: str,
    apply: bool,
) -> dict:
    """Junta ao resumo do Portal o que o Flow devolveu em `chave` (`marcas`/`notaFiscal`).
    Sem a capacidade, o Flow ignorou o campo: `capacidade_ok=False` e ERROR. `erro: true`
    no bloco: ERROR e `reenviar: true` (o diário tenta de novo; a rota manual mostra).
    `semPar`/`conflitos` não vazios viram WARNING com os nomes."""
    ok = capacidade in capacidades.de(rep)
    resumo["capacidade_ok"] = ok
    if not ok:
        logger.error(
            f"catalogo sync env={slug}: o Flow ignorou {chave} (sem '{capacidade}' na resposta) "
            f"— tire '{slug}' de {variavel} até o Flow aceitar"
        )
        return resumo
    bloco = (getattr(rep, "model_extra", None) or {}).get(chave)
    if not isinstance(bloco, dict):
        if apply:
            logger.warning(
                f"catalogo sync env={slug}: o Flow aceitou {chave} mas não devolveu o bloco"
            )
        return resumo
    if bloco.get("erro") is True:
        resumo["reenviar"] = True
        logger.error(f"catalogo sync env={slug}: o Flow falhou ao processar {chave} — reenviar")
    for campo in campos:
        if campo in bloco:
            resumo[campo] = bloco[campo]
    for campo in ("semPar", "conflitos"):
        if bloco.get(campo):
            logger.warning(f"catalogo sync env={slug} {chave} {campo}: {bloco[campo]}")
    return resumo


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
        logger.warning(f"catalogo sync env={slug} composicao congelada SEQ {seq} (quantidade diverge do nome): {aviso}")
    for codigo, seqs in resumo["codigo_mes_repetido"].items():
        logger.warning(f"catalogo sync env={slug} codigoMes {codigo} repetido em {seqs}: vai null")
