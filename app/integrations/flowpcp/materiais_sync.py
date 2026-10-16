"""Carga de material Fire -> cópia local -> Flow (spec 2026-10-16-flow-fonte-do-cadastro §6.2).

Tudo que não é CONFECCIONADO (grupo 8). Sempre grava a cópia local `materiais_fire`;
só envia ao Flow com o interruptor `PORTAL_FLOWPCP_MATERIAIS=<slug>` ligado."""

from __future__ import annotations

import os
import re
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.erp.catalog_extract import MaterialFireDTO, extract_materiais, extract_ultima_entrada
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp import capacidades
from app.integrations.flowpcp.catalogo_fiscal import normalizar_ncm, normalizar_sped, texto
from app.integrations.flowpcp.catalogo_schema import CatalogoGrupoErp, CatalogoOrigem
from app.integrations.flowpcp.catalogo_sync import _build_client
from app.integrations.flowpcp.client import FlowPCPClientError
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.integrations.flowpcp.materiais_schema import (
    MateriaisRequest,
    MateriaisResposta,
    MaterialItem,
)
from app.persistence import environments_repo, materiais_fire_repo, router
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"
_GRUPOS_PRODUCAO_PADRAO = frozenset({1, 4, 5, 6, 11, 13, 19})
_SPED_PRODUCAO = frozenset({*range(0, 7), 10})
_LIM_NOME, _LIM_UNIDADE, _LIM_EAN = 255, 16, 32


class ApplySemCapacidadeError(ValueError):
    """`apply` pedido, mas o Flow não declarou `materiais.v1`: nada foi gravado."""


class MateriaisDesligadoError(ValueError):
    """`apply` pedido com o envio de materiais desligado para o ambiente."""


@dataclass(frozen=True)
class MateriaisLocalResult:
    """Envio ao Flow DESLIGADO: a extração foi gravada só na cópia local."""

    itens: int
    extraido_em: str


def _var_grupos(slug: str) -> str:
    return "PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_" + re.sub(r"[^A-Z0-9]", "_", slug.upper())


def envio_ligado(slug: str) -> bool:
    """`PORTAL_FLOWPCP_MATERIAIS=mm` liga por empresa."""
    bruto = os.environ.get("PORTAL_FLOWPCP_MATERIAIS", "")
    return slug.lower() in {s.strip().lower() for s in bruto.split(",") if s.strip()}


def grupos_producao(slug: str) -> frozenset[int]:
    nome = _var_grupos(slug)
    bruto = os.environ.get(nome, "").strip()
    if not bruto:
        return _GRUPOS_PRODUCAO_PADRAO
    try:
        return frozenset(int(p.strip()) for p in bruto.split(",") if p.strip())
    except ValueError:
        logger.error(f"{nome}='{bruto}' inválido (esperado lista de inteiros): usando o padrão")
        return _GRUPOS_PRODUCAO_PADRAO


def exige_aprovacao(grupo: int | None, sped: int | None, grupos: frozenset[int]) -> bool:
    return (grupo is not None and grupo in grupos) or (sped is not None and sped in _SPED_PRODUCAO)


def _data_iso(valor) -> str | None:
    """Só um `date`/`datetime` real vira data civil; qualquer outra coisa é omitida."""
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    return None


def _montar(
    dtos: list[MaterialFireDTO],
    entradas: dict[str, tuple[str, date]],
    grupos: frozenset[int],
) -> tuple[list[MaterialItem], dict[str, str], dict[str, list[str]], dict[str, bool]]:
    """-> (itens a enviar, fora {seq: motivo}, pendências {seq: [motivos]}, exige {seq: bool})."""
    itens: list[MaterialItem] = []
    fora: dict[str, str] = {}
    pendencias: dict[str, list[str]] = {}
    exige: dict[str, bool] = {}
    ids_vistos: set[str] = set()
    codigos_vistos: set[str] = set()
    for d in dtos:
        seq = d.erp_produto_id
        ncm, m_ncm = normalizar_ncm(d.ncm_bruto)
        sped, m_sped = normalizar_sped(d.sped_bruto)
        exige[seq] = exige_aprovacao(d.grupo_codigo, sped, grupos)
        if d.grupo_codigo is None:
            fora[seq] = "sem_grupo"
            continue
        nome, nome_longo = texto(d.nome, _LIM_NOME)
        unidade, uni_longa = texto(d.unidade, _LIM_UNIDADE)
        ean, ean_longo = texto(d.ean, _LIM_EAN)
        if nome_longo or uni_longa or ean_longo:
            fora[seq] = "campo_excede"
            continue
        if nome is None:
            fora[seq] = "sem_nome"
            continue
        cod_chave = d.codigo.strip().lower()
        if seq in ids_vistos or cod_chave in codigos_vistos:
            fora[seq] = "duplicado"
            continue
        ids_vistos.add(seq)
        codigos_vistos.add(cod_chave)
        motivos: list[str] = []
        if m_ncm:
            motivos.append(m_ncm)
        if m_sped == "sped_invalido":  # sped_vazio é comum em material antigo: não é pendência
            motivos.append(m_sped)
        nome_grupo, _ = texto(d.grupo_nome, 80)
        if nome_grupo is None:
            nome_grupo = f"GRUPO {d.grupo_codigo}"
            motivos.append("grupo_sem_nome")
        ncm_ent = data_ent = None
        if seq in entradas:
            ncm_ent, _ = normalizar_ncm(entradas[seq][0])
            if ncm_ent:
                data_ent = _data_iso(entradas[seq][1])
        itens.append(
            MaterialItem(
                erpProdutoId=seq,
                codigo=d.codigo,
                nome=nome,
                unidadeErp=unidade,
                ncm=ncm,
                tipoItemSped=sped,
                grupoErp=CatalogoGrupoErp(codigo=str(d.grupo_codigo), nome=nome_grupo),
                ean=ean,
                ativo=d.ativo,
                exigeAprovacao=exige[seq],
                ncmUltimaEntrada=ncm_ent,
                ultimaEntradaEm=data_ent,
            )
        )
        if motivos:
            pendencias[seq] = motivos
    return itens, fora, pendencias, exige


def run_materiais_sync(
    slug: str,
    *,
    dry_run: bool = True,
    full_sync: bool = True,
    now_iso: str | None = None,
    _client=None,
    _fire_conn=None,
    _env_conn=None,
) -> MateriaisResposta | MateriaisLocalResult | None:
    """Extrai os materiais do Fire do ambiente `slug`, SEMPRE grava `materiais_fire` e,
    com o interruptor ligado, manda ao Flow.

    - `MateriaisResposta` — enviado (relatório do Flow + `materiais_portal`)
    - `MateriaisLocalResult` — interruptor desligado, só cópia local
    - `None` — ambiente sem FlowPCP habilitado
    `dry_run=False` faz antes um dryRun de sondagem e exige `materiais.v1` nas
    `capacidades` (senão `ApplySemCapacidadeError`, sem enviar o apply).
    Itens fora do envio (`materiais_portal.fora`): `sem_grupo`, `campo_excede`,
    `sem_nome`, `duplicado` (o Flow responde 400 a erpProdutoId/codigo repetido)."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"materiais sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None
    ligado = envio_ligado(slug)

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        # Fluxo só de SELECT: transação READ ONLY, o próprio Firebird recusa escrita.
        fire_ctx = FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env))
    with fire_ctx as fire_conn:
        dtos = extract_materiais(fire_conn)
        entradas = extract_ultima_entrada(fire_conn)

    grupos = grupos_producao(slug)
    itens, fora, pendencias, exige = _montar(dtos, entradas, grupos)

    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        materiais_fire_repo.replace_all(
            env_conn,
            [
                {
                    "erp_produto_id": d.erp_produto_id,
                    "nome": d.nome,
                    "unidade": d.unidade,
                    "ean": d.ean,
                    "ativo": 1 if d.ativo else 0,
                    "grupo_codigo": d.grupo_codigo,
                    "ncm": normalizar_ncm(d.ncm_bruto)[0],
                    "tipo_item_sped": normalizar_sped(d.sped_bruto)[0],
                    "exige_aprovacao": 1 if exige[d.erp_produto_id] else 0,
                }
                for d in dtos
            ],
            extraido_em=extraido_em,
        )
    logger.info(f"materiais sync env={slug} itens={len(dtos)} — cópia local atualizada")

    if not dry_run and not ligado:
        raise MateriaisDesligadoError(
            f"envio de materiais desligado para '{slug}' (PORTAL_FLOWPCP_MATERIAIS); "
            "a cópia local foi atualizada"
        )
    if not ligado:
        logger.info(
            f"materiais sync env={slug}: envio ao Flow DESLIGADO (PORTAL_FLOWPCP_MATERIAIS)"
        )
        return MateriaisLocalResult(itens=len(dtos), extraido_em=extraido_em)
    if not itens:
        raise ValueError("nenhum material elegível para enviar ao Flow")

    def _pedido(dry: bool) -> MateriaisRequest:
        return MateriaisRequest(
            dryRun=dry,
            fullSync=full_sync,
            itens=itens,
            origem=CatalogoOrigem(importadorVersao=_IMPORTADOR_VERSAO, extraidoEm=extraido_em),
        )

    client = _client or _build_client(cfg)
    try:
        logger.info(
            f"materiais sync env={slug} itens={len(itens)} fora={len(fora)} "
            f"dry_run={dry_run} full_sync={full_sync}"
        )
        try:
            sonda = client.send_materiais(_pedido(True))
            if not dry_run:
                if capacidades.MATERIAIS not in capacidades.de(sonda):
                    raise ApplySemCapacidadeError(
                        f"o Flow não declarou '{capacidades.MATERIAIS}': o apply de materiais "
                        "não foi enviado (nada gravado)"
                    )
                rep = client.send_materiais(_pedido(False))
            else:
                rep = sonda
        except FlowPCPClientError as exc:
            if exc.status_code == 403:
                raise FlowPCPClientError(
                    f"{exc} — o Flow recusou (403): a flag do tenant `materiaisErpAtivo` "
                    "está desligada no Flow",
                    status_code=403,
                    body=exc.body,
                ) from exc
            raise
        ok = capacidades.MATERIAIS_DRYRUN in capacidades.de(sonda)
        if not ok:
            logger.error(
                f"materiais sync env={slug}: o Flow não declarou '{capacidades.MATERIAIS_DRYRUN}' "
                f"— tire '{slug}' de PORTAL_FLOWPCP_MATERIAIS até o Flow aceitar"
            )
        rep.materiais_portal = {
            "enviados": len(itens),
            "fora": fora,
            "pendencias": pendencias,
            "por_motivo_fora": dict(Counter(fora.values())),
            "por_motivo_pendencia": dict(Counter(m for ms in pendencias.values() for m in ms)),
            "capacidade_ok": ok,
        }
        if fora:
            logger.warning(
                f"materiais sync env={slug}: {len(fora)} fora do envio {dict(Counter(fora.values()))}"
            )
        return rep
    finally:
        if _client is None:
            client.close()
