"""De-para de produto por cliente (`produto_depara`, db do ambiente).

A referência do varejista que não casa no Fire vira um vínculo persistente,
chaveado por `client_key` — CNPJ (só dígitos) quando o header do pedido tem
CNPJ, ou o nome do cliente normalizado quando não tem (varejistas como
Riachuelo: o CNPJ real é por loja, não aparece no header). Use `client_key()`
para computar a chave antes de chamar `upsert`/`lookup`/`list_for_client` —
eles gravam/consultam ela verbatim, sem normalizar de novo. `_norm_key` DEVE
ser idêntica na gravação e na leitura — chave divergente = vínculo fantasma.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "id",
    "client_key",
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


def client_key(cnpj: str | None, name: str | None) -> str:
    """Chave de cliente do de-para: CNPJ (só dígitos) quando existe; senão o
    nome normalizado. Varejistas como Riachuelo vêm sem CNPJ no header (o CNPJ
    real é por loja) — o nome é a granularidade certa: uma referência → um
    produto Fire, através de todas as lojas do cliente."""
    d = _norm_cnpj(cnpj)
    if d:
        return d
    return re.sub(r"\s+", " ", (name or "").strip()).upper()


def upsert(
    conn: sqlite3.Connection,
    *,
    client_key: str,
    chave_tipo: str,
    chave_valor: str,
    fire_produto_id: str,
    fire_codigo: str,
    fire_ean: str | None,
    fire_nome: str,
    criado_em: str,
    criado_por: str | None,
) -> None:
    """Grava (ou substitui) um vínculo. Last-write-wins na chave única.

    `client_key` deve vir pré-computado via `client_key()` (helper acima) —
    gravado verbatim, sem normalização adicional aqui."""
    conn.execute(
        """
        INSERT INTO produto_depara
            (client_key, chave_tipo, chave_valor,
             fire_produto_id, fire_codigo, fire_ean, fire_nome, criado_em, criado_por)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (client_key, chave_tipo, chave_valor) DO UPDATE SET
            fire_produto_id = excluded.fire_produto_id,
            fire_codigo     = excluded.fire_codigo,
            fire_ean        = excluded.fire_ean,
            fire_nome       = excluded.fire_nome,
            criado_em       = excluded.criado_em,
            criado_por      = excluded.criado_por
        """,
        (
            client_key,
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
    client_key: str,
    *,
    codigos: list[str],
    eans: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve vínculos do cliente para as chaves dadas. Chave do dict:
    (chave_tipo, chave_valor_normalizada). Batelado (uma query).

    `client_key` deve vir pré-computado via `client_key()` — não é
    normalizado aqui."""
    wanted: list[tuple[str, str]] = []
    wanted += [("codigo", _norm_key("codigo", c)) for c in codigos if c]
    wanted += [("ean", _norm_key("ean", e)) for e in eans if e]
    wanted = list({w for w in wanted if w[1]})
    if not client_key or not wanted:
        return {}

    out: dict[tuple[str, str], dict] = {}
    # (tipo, valor) pares via OR de igualdades — poucos itens por pedido.
    clause = " OR ".join(["(chave_tipo = ? AND chave_valor = ?)"] * len(wanted))
    params: list[str] = [client_key]
    for tipo, val in wanted:
        params += [tipo, val]
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM produto_depara WHERE client_key = ? AND ({clause})",
        params,
    ).fetchall()
    for r in rows:
        d = dict(zip(_COLS, r, strict=True))
        out[(d["chave_tipo"], d["chave_valor"])] = d
    return out


def delete(conn: sqlite3.Connection, id: int) -> None:
    conn.execute("DELETE FROM produto_depara WHERE id = ?", (id,))
    conn.commit()


def list_for_client(conn: sqlite3.Connection, client_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM produto_depara WHERE client_key = ? ORDER BY id",
        (client_key,),
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]
