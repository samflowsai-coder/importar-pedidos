"""Cópia local da tabela de preço do Flow (`preco_*`, db do ambiente).

O Portal nunca edita preço: só aplica lotes do Flow (`aplicar_lote`) e lê
(`resolver_rede`, `preco_vigente`). Preço em centavos inteiros; datas ISO.
Rede = raiz do CNPJ (8 dígitos); um CNPJ completo (14) cadastrado vence a raiz.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from app.erp.cnpj import cnpj_digits

if TYPE_CHECKING:
    import sqlite3

RECURSO_PRECOS = "precos"
RECURSO_REDES = "redes"
# Frescor exige AMBOS: recurso esperado sem linha = nunca sincronizou (bloqueia).
RECURSOS_ESPERADOS = (RECURSO_PRECOS, RECURSO_REDES)


@dataclass(frozen=True)
class Rede:
    id: str
    nome: str
    travada: bool
    travada_motivo: str | None


def aplicar_lote(
    conn: sqlite3.Connection,
    *,
    redes: list[dict[str, Any]],
    cnpjs: list[dict[str, Any]],
    precos: list[dict[str, Any]],
    cursor: str,
    agora: datetime,
    recursos: tuple[str, ...] | None = None,
) -> None:
    """Upsert idempotente numa única transação (tudo ou nada).

    `removido=True` apaga a linha. `agora` deve ter timezone. O cursor/hora é
    gravado em `preco_sync` para cada recurso que o lote carrega (redes ou
    cnpjs -> "redes"; preços -> "precos"); lote vazio conta como sync dos
    dois. `recursos` força o conjunto explicitamente."""
    if agora.tzinfo is None or agora.utcoffset() is None:
        raise ValueError("agora deve ter timezone (datetime aware)")
    if recursos is None:
        marcados = []
        if redes or cnpjs:
            marcados.append(RECURSO_REDES)
        if precos:
            marcados.append(RECURSO_PRECOS)
        recursos = tuple(marcados) or RECURSOS_ESPERADOS
    agora_iso = agora.isoformat()
    with conn:
        for r in redes:
            conn.execute(
                "INSERT INTO preco_rede (id, nome, travada, travada_motivo, atualizado_em)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET nome=excluded.nome,"
                " travada=excluded.travada, travada_motivo=excluded.travada_motivo,"
                " atualizado_em=excluded.atualizado_em",
                (
                    str(r["id"]),
                    r["nome"],
                    1 if r["travada"] else 0,
                    r.get("travada_motivo"),
                    agora_iso,
                ),
            )
        for c in cnpjs:
            digitos = cnpj_digits(c["cnpj"])
            if c["removido"]:
                conn.execute("DELETE FROM preco_rede_cnpj WHERE cnpj = ?", (digitos,))
            else:
                conn.execute(
                    "INSERT INTO preco_rede_cnpj (cnpj, rede_id) VALUES (?, ?)"
                    " ON CONFLICT(cnpj) DO UPDATE SET rede_id=excluded.rede_id",
                    (digitos, str(c["rede_id"])),
                )
        for p in precos:
            chave = (
                str(p["rede_id"]),
                str(p["fire_produto_id"]),
                p["vigente_desde"].isoformat(),
            )
            if p["removido"]:
                conn.execute(
                    "DELETE FROM preco_tabela WHERE rede_id = ? AND fire_produto_id = ?"
                    " AND vigente_desde = ?",
                    chave,
                )
            else:
                ate = p["vigente_ate"]
                conn.execute(
                    "INSERT INTO preco_tabela (rede_id, fire_produto_id, vigente_desde,"
                    " preco_centavos, vigente_ate) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(rede_id, fire_produto_id, vigente_desde) DO UPDATE SET"
                    " preco_centavos=excluded.preco_centavos,"
                    " vigente_ate=excluded.vigente_ate",
                    (*chave, int(p["preco_centavos"]), ate.isoformat() if ate else None),
                )
        for recurso in recursos:
            conn.execute(
                "INSERT INTO preco_sync (recurso, cursor, ultima_ok_em) VALUES (?, ?, ?)"
                " ON CONFLICT(recurso) DO UPDATE SET cursor=excluded.cursor,"
                " ultima_ok_em=excluded.ultima_ok_em",
                (recurso, cursor, agora_iso),
            )


def _rede(conn: sqlite3.Connection, rede_id: str) -> Rede | None:
    row = conn.execute(
        "SELECT id, nome, travada, travada_motivo FROM preco_rede WHERE id = ?",
        (rede_id,),
    ).fetchone()
    if row is None:
        return None
    return Rede(row[0], row[1], bool(row[2]), row[3])


def resolver_rede(conn: sqlite3.Connection, cnpj: str) -> Rede | None:
    """CNPJ de 14 dígitos: linha exata primeiro, depois a raiz. 8 dígitos: raiz.
    Qualquer outro tamanho (após normalizar) → None."""
    d = cnpj_digits(cnpj)
    if len(d) not in (8, 14):
        return None
    candidatos = [d, d[:8]] if len(d) == 14 else [d]
    for chave in candidatos:
        row = conn.execute(
            "SELECT rede_id FROM preco_rede_cnpj WHERE cnpj = ?", (chave,)
        ).fetchone()
        if row is not None:
            return _rede(conn, row[0])
    return None


def preco_vigente(
    conn: sqlite3.Connection, rede_id: str, fire_produto_id: str, data: date
) -> int | None:
    """Preço em centavos. Só conta a linha com maior `vigente_desde <= data`;
    se ela já expirou (`vigente_ate <= data`) → None (nunca cai numa linha
    mais antiga: preço velho é fail-open)."""
    iso = data.isoformat()
    row = conn.execute(
        "SELECT preco_centavos, vigente_ate FROM preco_tabela"
        " WHERE rede_id = ? AND fire_produto_id = ? AND vigente_desde <= ?"
        " ORDER BY vigente_desde DESC LIMIT 1",
        (str(rede_id), str(fire_produto_id), iso),
    ).fetchone()
    if row is None or (row[1] is not None and row[1] <= iso):
        return None
    return int(row[0])


def estado_sync(conn: sqlite3.Connection, recurso: str) -> tuple[str | None, datetime | None]:
    row = conn.execute(
        "SELECT cursor, ultima_ok_em FROM preco_sync WHERE recurso = ?", (recurso,)
    ).fetchone()
    if row is None:
        return (None, None)
    return (row[0], datetime.fromisoformat(row[1]) if row[1] else None)


def ultima_sync_ok(
    conn: sqlite3.Connection, recursos: tuple[str, ...] | None = None
) -> datetime | None:
    """A `ultima_ok_em` MAIS ANTIGA entre os recursos esperados (decide as 24 h).

    Default `RECURSOS_ESPERADOS`: se algum nunca sincronizou → None."""
    datas = [estado_sync(conn, r)[1] for r in (recursos or RECURSOS_ESPERADOS)]
    if any(d is None for d in datas):
        return None
    return min(d for d in datas if d is not None)
