"""Campos fiscais do produto no catálogo enviado ao Flow (spec
2026-10-16-flow-fonte-do-cadastro, §6.1). Puro: sem Fire, sem rede.

O Portal só manda valor válido: o que não passa na regra vai AUSENTE ("não
mexer" no Flow) e vira pendência no relatório. Em 16/10/2026, das 771 meias da
MM, 51 tinham NCM com pontos no lugar errado, 3 lixo e 19 vazio."""

from __future__ import annotations

import os
import re
from collections import Counter

from app.erp.catalog_extract import ProdutoFireDTO

SPED_VALIDOS = frozenset({*range(0, 11), 99})
LIMITE_CODIGO_NFE = 60
LIMITE_COR = 120
LIMITE_GRUPO_NOME = 80
_NCM = re.compile(r"[0-9]{8}")  # só ASCII, a mesma regra do Zod do Flow
_SEPARADORES = re.compile(r"[\s.\-]")


def envio_ligado(slug: str) -> bool:
    """`PORTAL_FLOWPCP_CATALOGO_FISCAL=mm` liga por empresa. Só ligar depois de
    o Flow devolver `fiscal.v1` num dryRun."""
    bruto = os.environ.get("PORTAL_FLOWPCP_CATALOGO_FISCAL", "")
    return slug.lower() in {s.strip().lower() for s in bruto.split(",") if s.strip()}


def normalizar_ncm(bruto: str | None) -> tuple[str | None, str | None]:
    s = _SEPARADORES.sub("", str(bruto or ""))
    if not s:
        return None, "ncm_vazio"
    if _NCM.fullmatch(s):
        return s, None
    return None, "ncm_invalido"


def normalizar_sped(bruto) -> tuple[int | None, str | None]:
    if bruto is None or (isinstance(bruto, str) and not bruto.strip()):
        return None, "sped_vazio"
    try:
        v = int(str(bruto).strip())
    except ValueError:
        return None, "sped_invalido"
    return (v, None) if v in SPED_VALIDOS else (None, "sped_invalido")


def texto(bruto, limite: int) -> tuple[str | None, bool]:
    s = str(bruto).strip() if bruto is not None else ""
    if not s:
        return None, False
    if len(s) > limite:
        return None, True
    return s, False


def campos_fiscais(d: ProdutoFireDTO) -> tuple[dict, list[str]]:
    campos: dict = {}
    motivos: list[str] = []
    ncm, m = normalizar_ncm(d.ncm_bruto)
    if ncm:
        campos["ncm"] = ncm
    elif m:
        motivos.append(m)
    sped, m = normalizar_sped(d.sped_bruto)
    if sped is not None:
        campos["tipoItemSped"] = sped
    elif m == "sped_invalido":
        # sped_vazio não é pendência: comum em material antigo, não é erro de meia
        motivos.append(m)
    nome_grupo, _ = texto(d.grupo_nome, LIMITE_GRUPO_NOME)
    if d.grupo_codigo is not None and nome_grupo:
        campos["grupoErp"] = {"codigo": str(d.grupo_codigo), "nome": nome_grupo}
    nfe, _ = texto(d.codigo_nfe, LIMITE_CODIGO_NFE)
    if nfe:
        campos["codigoNfe"] = nfe
    cor, longa = texto(d.cor, LIMITE_COR)
    if cor:
        campos["cor"] = cor
    elif longa:
        motivos.append("cor_longa")
    return campos, motivos


def resumo(pendencias: dict[str, list[str]], enviados: int) -> dict:
    por_motivo = Counter(m for ms in pendencias.values() for m in ms)
    return {
        "enviados": enviados,
        "com_pendencia": len(pendencias),
        "por_motivo": dict(por_motivo),
        "pendencias": pendencias,
        "capacidade_ok": None,
    }
