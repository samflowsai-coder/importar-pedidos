"""De-para v2 (`produto_vinculo_v2`, db do ambiente).

Vínculo da chave EXATA do varejista (código ou EAN) a um produto do Fire (SEQ),
por escopo: `rede:<rede_id>` ou `raiz:<8 dígitos do CNPJ>`. Nunca casa por
descrição ou semelhança. Vínculo não é apagado: `revogar` grava
`revogado_em`/`revogado_por`, e só um vínculo vigente existe por
(escopo, chave_tipo, chave_valor).

`norm_chave` DEVE ser a mesma na gravação e na leitura — chave divergente é
vínculo fantasma. Timestamps (`agora`) vêm do chamador, o repo não lê relógio.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

_COLS = (
    "id",
    "escopo",
    "chave_tipo",
    "chave_valor",
    "fire_seq",
    "fire_codigo",
    "fire_descricao",
    "origem",
    "criado_em",
    "criado_por",
)
_SELECT = f"SELECT {', '.join(_COLS)} FROM produto_vinculo_v2"
_TIPOS = ("codigo", "ean")
_ORIGENS = ("manual", "migrado_v1")


@dataclass(frozen=True)
class Vinculo:
    id: int
    escopo: str
    chave_tipo: str
    chave_valor: str
    fire_seq: int
    fire_codigo: str | None
    fire_descricao: str
    origem: str
    criado_em: str
    criado_por: str | None


class VinculoConflito(Exception):  # noqa: N818 - nome fixado pelo contrato do plano
    """Já existe vínculo vigente para a chave, apontando para outro produto."""

    def __init__(self, atual: Vinculo) -> None:
        super().__init__(
            f"{atual.chave_tipo} {atual.chave_valor} já está vinculado ao produto {atual.fire_seq}"
        )
        self.atual = atual


def norm_chave(tipo: str, valor: str | None) -> str:
    if tipo == "ean":
        return re.sub(r"\D", "", valor or "")
    return (valor or "").strip().upper()


def _vinculo(row: sqlite3.Row | tuple) -> Vinculo:
    return Vinculo(*tuple(row))


def _vigente(conn: sqlite3.Connection, escopo: str, tipo: str, valor: str) -> Vinculo | None:
    row = conn.execute(
        f"{_SELECT} WHERE escopo = ? AND chave_tipo = ? AND chave_valor = ? "
        "AND revogado_em IS NULL",
        (escopo, tipo, valor),
    ).fetchone()
    return _vinculo(row) if row else None


def lookup(
    conn: sqlite3.Connection,
    escopos: list[str],
    *,
    codigos: list[str],
    eans: list[str],
) -> dict[tuple[str, str], Vinculo]:
    """Vínculos vigentes para as chaves dadas. Chave do dict:
    (chave_tipo, chave_valor_normalizada). Para cada chave vence o primeiro
    escopo da lista que a tem."""
    wanted: list[tuple[str, str]] = [("codigo", norm_chave("codigo", c)) for c in codigos if c]
    wanted += [("ean", norm_chave("ean", e)) for e in eans if e]
    wanted = list(dict.fromkeys(w for w in wanted if w[1]))
    escopos = [e for e in escopos if e]
    if not escopos or not wanted:
        return {}

    clause = " OR ".join(["(chave_tipo = ? AND chave_valor = ?)"] * len(wanted))
    marks = ", ".join("?" * len(escopos))
    params: list[str] = list(escopos)
    for tipo, valor in wanted:
        params += [tipo, valor]
    rows = conn.execute(
        f"{_SELECT} WHERE escopo IN ({marks}) AND revogado_em IS NULL AND ({clause})",
        params,
    ).fetchall()

    ordem = {e: i for i, e in enumerate(escopos)}
    out: dict[tuple[str, str], Vinculo] = {}
    for v in sorted((_vinculo(r) for r in rows), key=lambda v: ordem[v.escopo]):
        out.setdefault((v.chave_tipo, v.chave_valor), v)
    return out


def _abrir_transacao(conn: sqlite3.Connection) -> None:
    """O repo é dono da transação de `vincular`/`revogar`: com uma transação do
    chamador aberta, commit/rollback aqui efetivaria ou descartaria escritas que
    não são dele. Falha alto em vez de adivinhar."""
    if conn.in_transaction:
        raise RuntimeError(
            "produto_vinculo_repo precisa da própria transação: "
            "faça commit ou rollback antes de chamar"
        )
    conn.execute("BEGIN IMMEDIATE")


def _eh_unico_do_vinculo(exc: sqlite3.IntegrityError) -> bool:
    return "UNIQUE constraint failed: produto_vinculo_v2" in str(exc)


def vincular(
    conn: sqlite3.Connection,
    *,
    escopo: str,
    chave_tipo: str,
    chave_valor: str,
    fire_seq: int,
    fire_codigo: str | None,
    fire_descricao: str,
    por: str,
    pedido_id: str | None,
    substituir: bool = False,
    origem: str = "manual",
    agora: str,
) -> Vinculo:
    """Grava o vínculo vigente da chave, numa transação só.

    Contrato de transação: o repo abre e fecha a própria transação. Com uma
    transação já aberta na conexão (`conn.in_transaction`), levanta
    `RuntimeError` sem tocar em nada.

    Mesmo SEQ já vigente: devolve o vigente. Outro SEQ: `VinculoConflito`, a
    menos que `substituir` — aí o antigo é revogado e o novo entra. Violação do
    índice único (corrida com outro escritor) também vira `VinculoConflito`;
    qualquer outro erro de integridade sobe como está, com rollback."""
    if chave_tipo not in _TIPOS:
        raise ValueError(f"chave_tipo inválido: {chave_tipo!r}")
    if origem not in _ORIGENS:
        raise ValueError(f"origem inválida: {origem!r}")
    valor = norm_chave(chave_tipo, chave_valor)
    if not valor:
        raise ValueError("chave vazia")

    _abrir_transacao(conn)
    try:
        atual = _vigente(conn, escopo, chave_tipo, valor)
        if atual is not None:
            if atual.fire_seq == fire_seq:
                conn.commit()
                return atual
            if not substituir:
                raise VinculoConflito(atual)
            conn.execute(
                "UPDATE produto_vinculo_v2 SET revogado_em = ?, revogado_por = ? WHERE id = ?",
                (agora, por, atual.id),
            )
        cur = conn.execute(
            """
            INSERT INTO produto_vinculo_v2
                (escopo, chave_tipo, chave_valor, fire_seq, fire_codigo, fire_descricao,
                 origem, pedido_id, criado_em, criado_por)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                escopo,
                chave_tipo,
                valor,
                fire_seq,
                fire_codigo,
                fire_descricao,
                origem,
                pedido_id,
                agora,
                por,
            ),
        )
        novo_id = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if not _eh_unico_do_vinculo(exc):
            raise
        # corrida: outro escritor gravou o vigente entre a leitura e o insert
        atual = _vigente(conn, escopo, chave_tipo, valor)
        if atual is None:
            raise
        raise VinculoConflito(atual) from None
    except BaseException:
        conn.rollback()
        raise
    row = conn.execute(f"{_SELECT} WHERE id = ?", (novo_id,)).fetchone()
    return _vinculo(row)


def revogar(conn: sqlite3.Connection, vinculo_id: int, *, por: str, agora: str) -> Vinculo:
    """Revoga o vínculo (preserva a linha). Já revogado: devolve sem alterar.
    Inexistente: `LookupError`.

    Mesmo contrato de transação de `vincular`: o repo é dono da transação e
    levanta `RuntimeError` se já houver uma aberta na conexão."""
    _abrir_transacao(conn)
    try:
        row = conn.execute(f"{_SELECT} WHERE id = ?", (vinculo_id,)).fetchone()
        if row is None:
            raise LookupError(f"vínculo {vinculo_id} não existe")
        conn.execute(
            "UPDATE produto_vinculo_v2 SET revogado_em = ?, revogado_por = ? "
            "WHERE id = ? AND revogado_em IS NULL",
            (agora, por, vinculo_id),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return _vinculo(row)


def listar(
    conn: sqlite3.Connection,
    *,
    escopo: str | None = None,
    incluir_revogados: bool = False,
) -> list[Vinculo]:
    where: list[str] = []
    params: list[str] = []
    if escopo is not None:
        where.append("escopo = ?")
        params.append(escopo)
    if not incluir_revogados:
        where.append("revogado_em IS NULL")
    sql = _SELECT + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id"
    return [_vinculo(r) for r in conn.execute(sql, params).fetchall()]
