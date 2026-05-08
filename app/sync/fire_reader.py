"""Read PRODUTOS + PRODUTOS_KIT from a Firebird ERP — read-only.

Uses the multi-environment FirebirdConnection (config dict, not env vars).
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.erp.connection import FirebirdConnection
from app.sync.models import ComponentRow, ProductRow
from app.utils.logger import logger

# Conservative COALESCE on flag columns so NULL → 'Nao'.
# We TRIM strings here so the test stubs see canonical values.
SQL_SELECT_PRODUTOS = """
    SELECT
        SEQ,
        TRIM(CODPROD_ALTERN),
        TRIM(DESCRICAO),
        TRIM(UNIDADE),
        TRIM(CODIGO_EAN13),
        COALESCE(TRIM(INATIVO), 'Nao'),
        COALESCE(TRIM(KIT_ATIVO), 'Nao')
    FROM PRODUTOS
"""

SQL_SELECT_PRODUTOS_KIT = """
    SELECT CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD
    FROM PRODUTOS_KIT
"""


def read_products_snapshot(fb_cfg: dict[str, Any]) -> list[ProductRow]:
    """Snapshot of PRODUTOS, classified as kit/non-kit.

    `fb_cfg` is the config dict returned by `environments_repo.to_fb_config(env)`.
    Reads PRODUTOS_KIT first to compute the set of pais, then PRODUTOS — a
    product is `is_kit=True` if KIT_ATIVO='Sim' OR its SEQ is a known pai.
    """
    fb = FirebirdConnection()
    pais: set[int] = set()
    raw_rows: list[tuple] = []

    with fb.connect_with_config(fb_cfg) as conn:
        cur = conn.cursor()
        cur.execute(SQL_SELECT_PRODUTOS_KIT)
        for _codigo, pai, _filho, _qtd in cur.fetchall():
            if pai is not None:
                pais.add(int(pai))

        cur.execute(SQL_SELECT_PRODUTOS)
        raw_rows = cur.fetchall()

    out: list[ProductRow] = []
    for row in raw_rows:
        seq, alt, descr, unid, ean, inativo, kit_ativo = row
        descr = (descr or "").strip()
        if not descr:
            logger.warning(f"sync.fire_reader: skipping SEQ={seq} with blank DESCRICAO")
            continue
        try:
            out.append(ProductRow(
                seq=int(seq),
                codprod_altern=(alt or None),
                descricao=descr,
                unidade=(unid or "un").lower(),
                codigo_ean13=(ean or None),
                inativo=(str(inativo).strip().lower() == "sim"),
                is_kit=(str(kit_ativo).strip().lower() == "sim") or (int(seq) in pais),
            ))
        except (ValidationError, ValueError, OverflowError) as exc:
            logger.warning(f"sync.fire_reader: SEQ={seq} skipped: {exc}")
    return out


def read_components_snapshot(fb_cfg: dict[str, Any]) -> list[ComponentRow]:
    """Snapshot of PRODUTOS_KIT, filtered to valid rows."""
    fb = FirebirdConnection()
    out: list[ComponentRow] = []
    with fb.connect_with_config(fb_cfg) as conn:
        cur = conn.cursor()
        cur.execute(SQL_SELECT_PRODUTOS_KIT)
        for codigo, pai, filho, qtd in cur.fetchall():
            if pai is None or filho is None:
                logger.warning(
                    f"sync.fire_reader: PRODUTOS_KIT.CODIGO={codigo} has NULL pai/filho — skipped"
                )
                continue
            try:
                qtd_f = float(qtd or 0)
                if qtd_f <= 0:
                    logger.warning(
                        f"sync.fire_reader: PRODUTOS_KIT.CODIGO={codigo} has qtd<=0 — skipped"
                    )
                    continue
                out.append(ComponentRow(
                    codigo=int(codigo),
                    codproduto_pai=int(pai),
                    codproduto=int(filho),
                    qtd=qtd_f,
                ))
            except (ValidationError, ValueError, OverflowError) as exc:
                logger.warning(f"sync.fire_reader: PRODUTOS_KIT.CODIGO={codigo} skipped: {exc}")
    return out
