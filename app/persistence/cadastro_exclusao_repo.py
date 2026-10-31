"""Backup das linhas que o `excluir` apaga do Fire (`cadastro_exclusao_backup`, db do ambiente).

A linha completa de PRODUTOS e as de PRODUTOS_KIT do pai ficam aqui, em JSON que
preserva tipo (data, Decimal, bytes), antes de qualquer DELETE. `(cadastro_id, versao)`
é único: a reentrega do mesmo item não duplica nem sobrescreve o backup. Para restaurar,
`restaurar(linha)` devolve os dicts originais e a pessoa reinsere no Fire (ver
docs/ai/modules/flowpcp.md, "Excluir").
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3


def _codifica(valor: Any) -> Any:
    # datetime antes de date (é subclasse): senão a hora se perde.
    if isinstance(valor, datetime):
        return {"t": "datetime", "v": valor.isoformat()}
    if isinstance(valor, date):
        return {"t": "date", "v": valor.isoformat()}
    if isinstance(valor, time):
        return {"t": "time", "v": valor.isoformat()}
    if isinstance(valor, Decimal):
        return {"t": "decimal", "v": str(valor)}
    if isinstance(valor, (bytes, bytearray, memoryview)):
        return {"t": "bytes", "v": base64.b64encode(bytes(valor)).decode("ascii")}
    if valor is None or isinstance(valor, (bool, int, float, str)):
        return valor
    raise TypeError(f"backup: tipo {type(valor).__name__} sem serialização")


def _decodifica(valor: Any) -> Any:
    if isinstance(valor, dict) and set(valor) == {"t", "v"}:
        t, v = valor["t"], valor["v"]
        if t == "datetime":
            return datetime.fromisoformat(v)
        if t == "date":
            return date.fromisoformat(v)
        if t == "time":
            return time.fromisoformat(v)
        if t == "decimal":
            return Decimal(v)
        if t == "bytes":
            return base64.b64decode(v)
    return valor


def _json(linhas: Any) -> str:
    if isinstance(linhas, dict):
        return json.dumps({k: _codifica(v) for k, v in linhas.items()}, ensure_ascii=False)
    return json.dumps(
        [{k: _codifica(v) for k, v in linha.items()} for linha in linhas], ensure_ascii=False
    )


def guardar(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    seq: int,
    produto: dict,
    kit: list[dict],
) -> bool:
    """Uma linha por `(cadastro_id, versao)`, que NUNCA é sobrescrita: se uma tentativa anterior
    já guardou o backup, ele continua sendo o do produto que existia na época (se o código foi
    reaproveitado por outro produto, o backup original não pode se perder). True se gravou,
    False se já existia. Não faz commit: quem chama commita e confere antes do DELETE."""
    produto_json, kit_json = _json(produto), _json(kit)  # levanta antes de tocar no banco
    cur = conn.execute(
        "INSERT OR IGNORE INTO cadastro_exclusao_backup "
        "(cadastro_id, versao, seq, produto_json, kit_json) VALUES (?, ?, ?, ?, ?)",
        (cadastro_id, versao, int(seq), produto_json, kit_json),
    )
    return cur.rowcount == 1


def buscar(conn: sqlite3.Connection, cadastro_id: str, versao: int) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT cadastro_id, versao, seq, produto_json, kit_json, criado_em, aplicado_em "
        "FROM cadastro_exclusao_backup WHERE cadastro_id = ? AND versao = ?",
        (cadastro_id, versao),
    )
    row = cur.fetchone()
    if row is None:
        return None
    nomes = [d[0] for d in cur.description]
    return dict(zip(nomes, row, strict=True))


def marcar_aplicado(conn: sqlite3.Connection, cadastro_id: str, versao: int) -> bool:
    """Registra que o DELETE comitou no Fire. True só na primeira vez."""
    cur = conn.execute(
        "UPDATE cadastro_exclusao_backup SET aplicado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND aplicado_em IS NULL",
        (cadastro_id, versao),
    )
    return cur.rowcount == 1


def restaurar(linha: dict[str, Any]) -> tuple[dict, list[dict]]:
    """(linha de PRODUTOS, linhas de PRODUTOS_KIT) com os tipos originais."""
    produto = {k: _decodifica(v) for k, v in json.loads(linha["produto_json"]).items()}
    kit = [{k: _decodifica(v) for k, v in r.items()} for r in json.loads(linha["kit_json"])]
    return produto, kit
