"""Lote semanal intercompany (Nasmar -> MM): config, fator com vigência, lote e pernas.

Recebe `conn` e não dá commit (mesma regra de `erp_vinculo_repo`).
Datas em ISO `YYYY-MM-DD`, instantes em ISO com segundos, fator como TEXT
(`str(Decimal)`): quem lê converte com `Decimal(...)`.

`get` devolve a composição já parseada em `composicao` (o `composicao_json`
cru não é exposto).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

from app.consolidators.janela import chave_para, instante_fechamento

_HORA_PADRAO = 8
# "Ainda não fechou": 'erro' entra porque o próximo ciclo tenta de novo (dado
# corrigido fecha; mesmo problema grava o mesmo motivo). Fechado nunca reabre.
_ABERTOS = ("aberto", "aguardando_fator", "erro")


class LoteFechadoError(Exception):
    """Operação que só vale em lote ainda não fechado (aberto/aguardando_fator/erro)."""


class ChaveDeLoteEmUsoError(Exception):
    """A chave do lote (3 letras do slug) já pertence a outra empresa de origem."""


class JanelaComLoteAbertoError(Exception):
    """Trocar a janela com lote aberto que já tem pernas sobreporia intervalos."""


def _ts(agora: datetime) -> str:
    return agora.isoformat(timespec="seconds")


def _dict(row: sqlite3.Row | tuple, cols: list[str]) -> dict:
    return dict(zip(cols, row, strict=True))


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return _dict(row, [d[0] for d in cur.description])


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [_dict(r, cols) for r in cur.fetchall()]


# --- config -----------------------------------------------------------------


def _config_out(d: dict) -> dict:
    d["ativo"] = bool(d["ativo"])
    return d


def get_config(conn: sqlite3.Connection, env_origem_slug: str) -> dict | None:
    d = _fetch_one(conn, "SELECT * FROM lote_config WHERE env_origem_slug = ?", (env_origem_slug,))
    return _config_out(d) if d else None


def listar_configs(conn: sqlite3.Connection, *, so_ativas: bool) -> list[dict]:
    sql = "SELECT * FROM lote_config"
    if so_ativas:
        sql += " WHERE ativo = 1"
    return [_config_out(d) for d in _fetch_all(conn, sql + " ORDER BY env_origem_slug")]


def save_config(
    conn: sqlite3.Connection,
    *,
    env_origem_slug: str,
    env_espelho_slug: str,
    cnpj_revenda: str,
    nome_revenda: str,
    janela: str,
    dia_inicio: int,
    hora_fechamento: int,
    ativo: bool,
    por: str,
    agora: datetime,
) -> dict:
    atual = get_config(conn, env_origem_slug)
    if atual is not None and atual["janela"] != janela:
        com_perna = conn.execute(
            """
            SELECT 1 FROM intercompany_lote l
            WHERE l.env_origem_slug = ? AND l.status IN ('aberto','aguardando_fator','erro')
              AND EXISTS (SELECT 1 FROM intercompany_lote_item i WHERE i.lote_id = l.id)
            LIMIT 1
            """,
            (env_origem_slug,),
        ).fetchone()
        if com_perna:
            raise JanelaComLoteAbertoError(env_origem_slug)
    ts = _ts(agora)
    # Só o religamento (ou a 1ª ativação) carimba; segue ativo mantém; desligar não apaga.
    if atual is not None and atual["ativo"]:
        ativo_desde = atual["ativo_desde"]
    else:
        ativo_desde = ts if ativo else (atual["ativo_desde"] if atual else None)
    conn.execute(
        """
        INSERT INTO lote_config (env_origem_slug, env_espelho_slug, cnpj_revenda, nome_revenda,
                                 janela, dia_inicio, hora_fechamento, ativo, ativo_desde,
                                 alterado_por, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(env_origem_slug) DO UPDATE SET
            env_espelho_slug = excluded.env_espelho_slug,
            cnpj_revenda     = excluded.cnpj_revenda,
            nome_revenda     = excluded.nome_revenda,
            janela           = excluded.janela,
            dia_inicio       = excluded.dia_inicio,
            hora_fechamento  = excluded.hora_fechamento,
            ativo            = excluded.ativo,
            ativo_desde      = excluded.ativo_desde,
            alterado_por     = excluded.alterado_por,
            updated_at       = excluded.updated_at
        """,
        (
            env_origem_slug,
            env_espelho_slug,
            cnpj_revenda,
            nome_revenda,
            janela,
            dia_inicio,
            hora_fechamento,
            1 if ativo else 0,
            ativo_desde,
            por,
            ts,
            ts,
        ),
    )
    return get_config(conn, env_origem_slug)  # type: ignore[return-value]


# --- fator ------------------------------------------------------------------


def registrar_fator(
    conn: sqlite3.Connection,
    *,
    env_origem_slug: str,
    fator: Decimal,
    modo: str,
    vigente_desde: date,
    por: str,
    agora: datetime,
) -> dict:
    cur = conn.execute(
        """
        INSERT INTO lote_fator (env_origem_slug, modo_preco, fator_preco, vigente_desde,
                                informado_por, informado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (env_origem_slug, modo, str(fator), vigente_desde.isoformat(), por, _ts(agora)),
    )
    return _fetch_one(conn, "SELECT * FROM lote_fator WHERE id = ?", (cur.lastrowid,))  # type: ignore[return-value]


def fator_vigente(
    conn: sqlite3.Connection, env_origem_slug: str, em: date
) -> tuple[Decimal, str] | None:
    """Maior `vigente_desde <= em`; empate = o último salvo (maior id)."""
    row = conn.execute(
        """
        SELECT fator_preco, modo_preco FROM lote_fator
        WHERE env_origem_slug = ? AND vigente_desde <= ?
        ORDER BY vigente_desde DESC, id DESC LIMIT 1
        """,
        (env_origem_slug, em.isoformat()),
    ).fetchone()
    return (Decimal(row[0]), row[1]) if row else None


# --- lote -------------------------------------------------------------------


def _lote_out(d: dict | None) -> dict | None:
    if d is None:
        return None
    raw = d.pop("composicao_json", None)
    d["composicao"] = json.loads(raw) if raw else None
    if d.get("lote_fiscal_ok") is not None:
        d["lote_fiscal_ok"] = bool(d["lote_fiscal_ok"])
    return d


def lote_para(
    conn: sqlite3.Connection,
    *,
    env_origem_slug: str,
    env_espelho_slug: str,
    inicio: date,
    fim: date,
    modo_janela: str,
    agora: datetime,
) -> dict:
    """Cria o lote da janela se não existe, congelando o fator vigente em `inicio`."""
    chave = chave_para(env_origem_slug, inicio, modo_janela)
    vig = fator_vigente(conn, env_origem_slug, inicio)
    conn.execute(
        """
        INSERT INTO intercompany_lote (chave_lote, env_origem_slug, env_espelho_slug, janela,
                                       janela_inicio, janela_fim, status, modo_preco_aplicado,
                                       fator_preco_aplicado, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'aberto', ?, ?, ?)
        ON CONFLICT(chave_lote) DO NOTHING
        """,
        (
            chave,
            env_origem_slug,
            env_espelho_slug,
            modo_janela,
            inicio.isoformat(),
            fim.isoformat(),
            vig[1] if vig else None,
            str(vig[0]) if vig else None,
            _ts(agora),
        ),
    )
    lote = _lote_out(
        _fetch_one(
            conn,
            "SELECT * FROM intercompany_lote WHERE chave_lote = ? AND env_origem_slug = ?",
            (chave, env_origem_slug),
        )
    )
    if lote is None:
        dono = conn.execute(
            "SELECT env_origem_slug FROM intercompany_lote WHERE chave_lote = ?", (chave,)
        ).fetchone()
        raise ChaveDeLoteEmUsoError(
            f"chave {chave} de {env_origem_slug!r} já é de {dono[0] if dono else '?'!r}"
        )
    return lote


def buscar_lote(conn: sqlite3.Connection, env_origem_slug: str, inicio: date) -> dict | None:
    return _lote_out(
        _fetch_one(
            conn,
            "SELECT * FROM intercompany_lote WHERE env_origem_slug = ? AND janela_inicio = ?",
            (env_origem_slug, inicio.isoformat()),
        )
    )


def get(conn: sqlite3.Connection, lote_id: int) -> dict | None:
    return _lote_out(_fetch_one(conn, "SELECT * FROM intercompany_lote WHERE id = ?", (lote_id,)))


def lote_do_espelho(conn: sqlite3.Connection, import_id: str) -> dict | None:
    return _lote_out(
        _fetch_one(
            conn, "SELECT * FROM intercompany_lote WHERE import_id_espelho = ?", (import_id,)
        )
    )


def lotes_vencidos(conn: sqlite3.Connection, agora: datetime) -> list[dict]:
    """Não fechados (aberto/aguardando_fator/erro) cujo instante de fechamento já passou."""
    rows = _fetch_all(
        conn,
        """
        SELECT l.*, c.hora_fechamento AS _hora FROM intercompany_lote l
        LEFT JOIN lote_config c ON c.env_origem_slug = l.env_origem_slug
        WHERE l.status IN ('aberto','aguardando_fator','erro')
        ORDER BY l.janela_inicio, l.id
        """,
    )
    out: list[dict] = []
    for d in rows:
        hora = d.pop("_hora")
        hora = _HORA_PADRAO if hora is None else hora
        fim = date.fromisoformat(d["janela_fim"])
        if instante_fechamento(fim, hora=hora) <= agora:
            out.append(_lote_out(d))  # type: ignore[arg-type]
    return out


def listar(conn: sqlite3.Connection, *, limite: int = 20) -> list[dict]:
    rows = _fetch_all(
        conn,
        "SELECT * FROM intercompany_lote ORDER BY janela_inicio DESC, id DESC LIMIT ?",
        (limite,),
    )
    return [_lote_out(d) for d in rows]  # type: ignore[misc]


# --- pernas -----------------------------------------------------------------


def anexar_perna(
    conn: sqlite3.Connection,
    *,
    lote_id: int,
    import_id: str,
    env_origem_slug: str,
    fire_codigo_origem: int,
    itens_fire: list[tuple[int, int]],
    pedido_cliente: str | None,
    cnpj_cliente_final: str | None,
    razao_cliente_final: str | None,
    agora: datetime,
) -> None:
    """Mesma perna no mesmo lote = no-op (mesmo se o lote já fechou); em outro lote,
    IntegrityError propaga. Checagem de status e INSERT são um só statement, para
    que um `fechar` concorrente não deixe entrar perna em lote fechado."""
    existente = conn.execute(
        "SELECT lote_id FROM intercompany_lote_item WHERE env_origem_slug = ? AND import_id = ?",
        (env_origem_slug, import_id),
    ).fetchone()
    if existente is not None and existente[0] == lote_id:
        return
    cur = conn.execute(
        """
        INSERT INTO intercompany_lote_item (lote_id, import_id, env_origem_slug,
            fire_codigo_origem, itens_fire_json, pedido_cliente, cnpj_cliente_final,
            razao_cliente_final, created_at)
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
        WHERE EXISTS (SELECT 1 FROM intercompany_lote
                      WHERE id = ? AND status IN ('aberto','aguardando_fator','erro'))
        """,
        (
            lote_id,
            import_id,
            env_origem_slug,
            fire_codigo_origem,
            json.dumps([list(p) for p in itens_fire]),
            pedido_cliente,
            cnpj_cliente_final,
            razao_cliente_final,
            _ts(agora),
            lote_id,
        ),
    )
    if cur.rowcount == 0:
        raise LoteFechadoError(str(lote_id))


def pernas_do_lote(conn: sqlite3.Connection, lote_id: int) -> list[dict]:
    rows = _fetch_all(
        conn, "SELECT * FROM intercompany_lote_item WHERE lote_id = ? ORDER BY id", (lote_id,)
    )
    for d in rows:
        d["itens_fire"] = [tuple(p) for p in json.loads(d.pop("itens_fire_json"))]
    return rows


# --- transições -------------------------------------------------------------


def marcar_aguardando_fator(conn: sqlite3.Connection, lote_id: int) -> None:
    conn.execute(
        "UPDATE intercompany_lote SET status = 'aguardando_fator' "
        "WHERE id = ? AND status IN ('aberto','erro')",
        (lote_id,),
    )


def informar_fator(
    conn: sqlite3.Connection,
    *,
    lote_id: int,
    fator: Decimal,
    modo: str,
    por: str,
    agora: datetime,
) -> dict:
    cur = conn.execute(
        """
        UPDATE intercompany_lote
        SET fator_preco_aplicado = ?, modo_preco_aplicado = ?, fator_informado_por = ?,
            fator_informado_em = ?, status = 'aberto'
        WHERE id = ? AND status IN ('aberto','aguardando_fator','erro')
        """,
        (str(fator), modo, por, _ts(agora), lote_id),
    )
    if cur.rowcount != 1:
        raise LoteFechadoError(str(lote_id))
    return get(conn, lote_id)  # type: ignore[return-value]


def fechar(
    conn: sqlite3.Connection,
    *,
    lote_id: int,
    import_id_espelho: str,
    composicao: list[list[dict]],
    pernas_esperadas: int,
    por: str,
    agora: datetime,
) -> bool:
    """False = outro fechamento chegou antes, o lote não tem fator, ou chegou perna
    depois que o chamador leu as pernas (`pernas_esperadas` diverge da contagem)."""
    cur = conn.execute(
        """
        UPDATE intercompany_lote
        SET status = 'fechado', import_id_espelho = ?, composicao_json = ?,
            closed_at = ?, closed_by = ?, erro_motivo = NULL
        WHERE id = ? AND status IN ('aberto','aguardando_fator','erro')
          AND fator_preco_aplicado IS NOT NULL
          AND (SELECT COUNT(*) FROM intercompany_lote_item
               WHERE lote_id = intercompany_lote.id) = ?
        """,
        (import_id_espelho, json.dumps(composicao), _ts(agora), por, lote_id, pernas_esperadas),
    )
    return cur.rowcount == 1


def marcar_erro(conn: sqlite3.Connection, *, lote_id: int, motivo: str, agora: datetime) -> None:
    conn.execute(
        "UPDATE intercompany_lote SET status = 'erro', erro_motivo = ? WHERE id = ?",
        (motivo, lote_id),
    )


def marcar_enviado(
    conn: sqlite3.Connection,
    *,
    lote_id: int,
    fire_codigo_espelho: int,
    lote_fiscal_ok: bool,
    agora: datetime,
) -> bool:
    """Só transita de 'fechado'; segunda chamada devolve False e não sobrescreve."""
    cur = conn.execute(
        """
        UPDATE intercompany_lote
        SET status = 'enviado', fire_codigo_espelho = ?, lote_fiscal_ok = ?, sent_at = ?,
            erro_motivo = NULL
        WHERE id = ? AND status = 'fechado' AND import_id_espelho IS NOT NULL
        """,
        (fire_codigo_espelho, 1 if lote_fiscal_ok else 0, _ts(agora), lote_id),
    )
    return cur.rowcount == 1
