"""De-para de produto por cliente (`produto_depara`, db do ambiente).

A referência do varejista que não casa no Fire vira um vínculo persistente,
chaveado por CNPJ do cliente. `_norm_key` DEVE ser idêntica na gravação e na
leitura — chave divergente = vínculo fantasma.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "id",
    "cliente_cnpj",
    "chave_tipo",
    "chave_valor",
    "fire_produto_id",
    "fire_codigo",
    "fire_ean",
    "fire_nome",
    "criado_em",
    "criado_por",
)


def _norm_cnpj(cnpj: str | None) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _norm_key(tipo: str, valor: str) -> str:
    if tipo == "ean":
        return re.sub(r"\D", "", valor or "")
    return (valor or "").strip().upper()


def upsert(
    conn: sqlite3.Connection,
    *,
    cliente_cnpj: str,
    chave_tipo: str,
    chave_valor: str,
    fire_produto_id: str,
    fire_codigo: str,
    fire_ean: str | None,
    fire_nome: str,
    criado_em: str,
    criado_por: str | None,
) -> None:
    """Grava (ou substitui) um vínculo. Last-write-wins na chave única."""
    conn.execute(
        """
        INSERT INTO produto_depara
            (cliente_cnpj, chave_tipo, chave_valor,
             fire_produto_id, fire_codigo, fire_ean, fire_nome, criado_em, criado_por)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (cliente_cnpj, chave_tipo, chave_valor) DO UPDATE SET
            fire_produto_id = excluded.fire_produto_id,
            fire_codigo     = excluded.fire_codigo,
            fire_ean        = excluded.fire_ean,
            fire_nome       = excluded.fire_nome,
            criado_em       = excluded.criado_em,
            criado_por      = excluded.criado_por
        """,
        (
            _norm_cnpj(cliente_cnpj),
            chave_tipo,
            _norm_key(chave_tipo, chave_valor),
            fire_produto_id,
            fire_codigo,
            fire_ean,
            fire_nome,
            criado_em,
            criado_por,
        ),
    )
    conn.commit()


def lookup(
    conn: sqlite3.Connection,
    cliente_cnpj: str,
    *,
    codigos: list[str],
    eans: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve vínculos do cliente para as chaves dadas. Chave do dict:
    (chave_tipo, chave_valor_normalizada). Batelado (uma query)."""
    cnpj = _norm_cnpj(cliente_cnpj)
    wanted: list[tuple[str, str]] = []
    wanted += [("codigo", _norm_key("codigo", c)) for c in codigos if c]
    wanted += [("ean", _norm_key("ean", e)) for e in eans if e]
    wanted = list({w for w in wanted if w[1]})
    if not cnpj or not wanted:
        return {}

    out: dict[tuple[str, str], dict] = {}
    # (tipo, valor) pares via OR de igualdades — poucos itens por pedido.
    clause = " OR ".join(["(chave_tipo = ? AND chave_valor = ?)"] * len(wanted))
    params: list[str] = [cnpj]
    for tipo, val in wanted:
        params += [tipo, val]
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM produto_depara WHERE cliente_cnpj = ? AND ({clause})",
        params,
    ).fetchall()
    for r in rows:
        d = dict(zip(_COLS, r, strict=True))
        out[(d["chave_tipo"], d["chave_valor"])] = d
    return out


def delete(conn: sqlite3.Connection, id: int) -> None:
    conn.execute("DELETE FROM produto_depara WHERE id = ?", (id,))
    conn.commit()


def list_for_client(conn: sqlite3.Connection, cliente_cnpj: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM produto_depara WHERE cliente_cnpj = ? ORDER BY id",
        (_norm_cnpj(cliente_cnpj),),
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]
