"""Cópia local da tabela de preço do Flow (contrato precos v1, rota a; db do ambiente).

O Portal nunca edita preço: só aplica páginas do Flow (`aplicar_pagina`,
`reiniciar`) e lê (`resolver_rede`, `preco_vigente`, `estado_sync`). Rede é o
estado inteiro da entidade; preço é append-only por `precoId`. Um cursor só.
A resolução é a regra pura do §5 (`app.erp.preco_regra`); quem chama passa o
CNPJ já normalizado (`preco_regra.normalizar_cnpj`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from app.erp.preco_regra import PrecoLinha, cnpj_valido, resolver_preco

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class Rede:
    id: str
    nome: str
    ativa: bool
    travada: bool


@dataclass(frozen=True)
class EstadoSync:
    empresa_cnpj: str | None
    cursor: str | None
    completo_em: datetime | None


def _instante(texto: str) -> datetime:
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"instante sem fuso: {texto!r}")
    return dt


def aplicar_pagina(conn: sqlite3.Connection, pagina: dict[str, Any], *, empresa_cnpj: str) -> None:
    """Aplica uma página da rota (a) já validada pelo schema, numa transação:
    upsert das redes (chaves substituídas inteiras) e dos preços, e só então o
    `cursor`. `temMais: false` grava `completo_em = geradoEm`; com `true` o
    `completo_em` anterior fica. `reiniciar` é do chamador (`reiniciar()`)."""
    completo = None if pagina["temMais"] else _instante(pagina["geradoEm"]).isoformat()
    with conn:
        for m in pagina["mudancas"]:
            if m["tipo"] == "rede":
                rede_id = m["redeId"]
                conn.execute(
                    "INSERT INTO preco_rede_v1 (rede_id, nome, ativa, travada) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(rede_id) DO UPDATE SET nome=excluded.nome,"
                    " ativa=excluded.ativa, travada=excluded.travada",
                    (rede_id, m["nome"], 1 if m["ativa"] else 0, 1 if m["travada"] else 0),
                )
                conn.execute("DELETE FROM preco_rede_chave_v1 WHERE rede_id = ?", (rede_id,))
                chaves = [("raiz", r) for r in m["raizes"]] + [("cnpj", c) for c in m["cnpjs"]]
                conn.executemany(
                    "INSERT OR IGNORE INTO preco_rede_chave_v1 (rede_id, tipo, chave)"
                    " VALUES (?, ?, ?)",
                    [(rede_id, tipo, chave) for tipo, chave in chaves],
                )
            elif m["tipo"] == "preco":
                conn.execute(
                    "INSERT INTO preco_tabela_v1 (preco_id, rede_id, empresa_cnpj,"
                    " erp_produto_id, unidade_erp, preco_centavos, vigencia_inicio,"
                    " registrado_em, cancelado) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(preco_id) DO UPDATE SET rede_id=excluded.rede_id,"
                    " empresa_cnpj=excluded.empresa_cnpj,"
                    " erp_produto_id=excluded.erp_produto_id,"
                    " unidade_erp=excluded.unidade_erp,"
                    " preco_centavos=excluded.preco_centavos,"
                    " vigencia_inicio=excluded.vigencia_inicio,"
                    " registrado_em=excluded.registrado_em, cancelado=excluded.cancelado",
                    (
                        m["precoId"],
                        m["redeId"],
                        m["empresaFaturadoraCnpj"],
                        m["erpProdutoId"],
                        m["unidadeErp"],
                        int(m["precoCentavos"]),
                        m["vigenciaInicio"],
                        m["registradoEm"],
                        1 if m["cancelado"] else 0,
                    ),
                )
            else:
                raise ValueError(f"mudança de tipo desconhecido: {m.get('tipo')!r}")
        conn.execute(
            "INSERT INTO preco_sync_v1 (id, empresa_cnpj, cursor, completo_em)"
            " VALUES (1, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET empresa_cnpj=excluded.empresa_cnpj,"
            " cursor=excluded.cursor,"
            " completo_em=COALESCE(excluded.completo_em, preco_sync_v1.completo_em)",
            (empresa_cnpj, pagina["cursor"], completo),
        )


def reiniciar(conn: sqlite3.Connection, *, empresa_cnpj: str) -> None:
    """Descarta a cópia inteira (redes, chaves, preços) e o cursor."""
    with conn:
        conn.execute("DELETE FROM preco_rede_v1")
        conn.execute("DELETE FROM preco_rede_chave_v1")
        conn.execute("DELETE FROM preco_tabela_v1")
        conn.execute(
            "INSERT INTO preco_sync_v1 (id, empresa_cnpj, cursor, completo_em)"
            " VALUES (1, ?, NULL, NULL)"
            " ON CONFLICT(id) DO UPDATE SET empresa_cnpj=excluded.empresa_cnpj,"
            " cursor=NULL, completo_em=NULL",
            (empresa_cnpj,),
        )


def estado_sync(conn: sqlite3.Connection) -> EstadoSync:
    """Empresa, cursor e `completo_em` da cópia. Cursor de outra empresa que
    não a do ambiente: o chamador trata como sem cursor e reinicia."""
    row = conn.execute(
        "SELECT empresa_cnpj, cursor, completo_em FROM preco_sync_v1 WHERE id = 1"
    ).fetchone()
    if row is None:
        return EstadoSync(None, None, None)
    return EstadoSync(row[0], row[1], _instante(row[2]) if row[2] else None)


def ultima_sync_ok(conn: sqlite3.Connection) -> datetime | None:
    """`completo_em`: `geradoEm` da última página com `temMais: false`."""
    return estado_sync(conn).completo_em


def resolver_rede(conn: sqlite3.Connection, cnpj_normalizado: str) -> Rede | None:
    """§5: CNPJ completo em rede ativa → raiz `[:8]` em rede ativa → None.
    Entrada fora do formato do contrato → None (sem normalizar)."""
    if not cnpj_valido(cnpj_normalizado):
        return None
    for tipo, chave in (("cnpj", cnpj_normalizado), ("raiz", cnpj_normalizado[:8])):
        row = conn.execute(
            "SELECT r.rede_id, r.nome, r.ativa, r.travada FROM preco_rede_chave_v1 k"
            " JOIN preco_rede_v1 r ON r.rede_id = k.rede_id"
            " WHERE k.tipo = ? AND k.chave = ? AND r.ativa = 1"
            " ORDER BY r.rede_id LIMIT 1",
            (tipo, chave),
        ).fetchone()
        if row is not None:
            return Rede(row[0], row[1], bool(row[2]), bool(row[3]))
    return None


def preco_vigente(
    conn: sqlite3.Connection,
    *,
    empresa_cnpj: str,
    rede_id: str | None,
    erp_produto_id: str,
    data: date,
) -> PrecoLinha | None:
    """Preço vigente pela regra do §5 (`preco_regra.resolver_preco`) sobre os
    preços da cópia daquela empresa e produto."""
    rows = conn.execute(
        "SELECT preco_id, rede_id, empresa_cnpj, erp_produto_id, unidade_erp,"
        " preco_centavos, vigencia_inicio, registrado_em, cancelado"
        " FROM preco_tabela_v1 WHERE empresa_cnpj = ? AND erp_produto_id = ?",
        (empresa_cnpj, erp_produto_id),
    ).fetchall()
    candidatos = [
        PrecoLinha(
            preco_id=r[0],
            rede_id=r[1],
            empresa_cnpj=r[2],
            erp_produto_id=r[3],
            unidade_erp=r[4],
            preco_centavos=int(r[5]),
            vigencia_inicio=date.fromisoformat(r[6]),
            registrado_em=r[7],
            cancelado=bool(r[8]),
        )
        for r in rows
    ]
    return resolver_preco(
        candidatos,
        empresa_cnpj=empresa_cnpj,
        rede_id=rede_id,
        erp_produto_id=erp_produto_id,
        data=data,
    )
