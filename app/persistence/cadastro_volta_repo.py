"""Livro da volta Flow → Fire (`cadastro_volta`, db do ambiente).

(cadastro_id, versao) é a chave: registrar duas vezes o mesmo item não duplica.
Recebe a conexão aberta, como `catalogo_fire_repo`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "cadastro_id",
    "versao",
    "estado",
    "acao",
    "tipo",
    "seq_reservado",
    "erro_codigo",
    "erro_mensagem",
    "plano_json",
    "iniciado_em",
    "atualizado_em",
)

# Estados em que o lease pode ser reaproveitado: nada foi gravado no Fire.
ESTADOS_DESCARTAVEIS = ("simulado", "erro_definitivo", "transitorio", "liberado")
_PENDENTES = ("gravado_sem_volta", "erro_a_enviar")
_DESCARTAVEIS_SQL = ", ".join(f"'{e}'" for e in ESTADOS_DESCARTAVEIS)
_PENDENTES_SQL = ", ".join(f"'{e}'" for e in _PENDENTES)


def registrar(
    conn: sqlite3.Connection,
    *,
    cadastro_id: str,
    versao: int,
    estado: str,
    acao: str | None,
    tipo: str | None,
    erro_codigo: str | None,
    erro_mensagem: str | None,
    plano_json: str | None,
) -> bool:
    """False se `(cadastro_id, versao)` já está no livro (nada é alterado)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO cadastro_volta "
        "(cadastro_id, versao, estado, acao, tipo, erro_codigo, erro_mensagem, plano_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cadastro_id, versao, estado, acao, tipo, erro_codigo, erro_mensagem, plano_json),
    )
    return cur.rowcount == 1


def existe(conn: sqlite3.Connection, cadastro_id: str, versao: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM cadastro_volta WHERE cadastro_id = ? AND versao = ?",
        (cadastro_id, versao),
    ).fetchone()
    return row is not None


def listar(conn: sqlite3.Connection, limite: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM cadastro_volta "
        "ORDER BY iniciado_em DESC, rowid DESC LIMIT ?",
        (limite,),
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]


def contar_por_estado(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT estado, COUNT(*) FROM cadastro_volta GROUP BY estado").fetchall()
    return {r[0]: r[1] for r in rows}


def reservar(
    conn: sqlite3.Connection,
    *,
    cadastro_id: str,
    versao: int,
    produto_id: str,
    acao: str,
    tipo: str,
    modo: str,
    plano_json: str,
) -> bool:
    """Lease de `(cadastro_id, versao)`. True = este ciclo é dono do item.
    Linha descartável (simulação, transitório, liberada) é reaproveitada."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO cadastro_volta "
        "(cadastro_id, versao, estado, acao, tipo, produto_id, modo, plano_json) "
        "VALUES (?, ?, 'em_andamento', ?, ?, ?, ?, ?)",
        (cadastro_id, versao, acao, tipo, produto_id, modo, plano_json),
    )
    if cur.rowcount == 1:
        return True
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = 'em_andamento', acao = ?, tipo = ?, produto_id = ?, "
        "modo = ?, plano_json = ?, seq_reservado = NULL, erro_codigo = NULL, "
        "erro_mensagem = NULL, corpo_json = NULL, iniciado_em = datetime('now'), "
        "atualizado_em = datetime('now') "
        f"WHERE cadastro_id = ? AND versao = ? AND estado IN ({_DESCARTAVEIS_SQL})",
        (acao, tipo, produto_id, modo, plano_json, cadastro_id, versao),
    )
    return cur.rowcount == 1


def marcar_seq(conn: sqlite3.Connection, cadastro_id: str, versao: int, seq: int) -> None:
    conn.execute(
        "UPDATE cadastro_volta SET seq_reservado = ?, atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ?",
        (seq, cadastro_id, versao),
    )


def marcar_gravado(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    *,
    erp_produto_id: str,
    corpo_json: str,
) -> None:
    conn.execute(
        "UPDATE cadastro_volta SET estado = 'gravado_sem_volta', erp_produto_id = ?, "
        "corpo_json = ?, proxima_tentativa_em = NULL, atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ?",
        (erp_produto_id, corpo_json, cadastro_id, versao),
    )


def marcar_erro_a_enviar(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    *,
    codigo: str,
    mensagem: str,
    corpo_json: str,
) -> None:
    """Nunca toca linha `gravado_sem_volta`: o produto já está no ERP."""
    conn.execute(
        "UPDATE cadastro_volta SET estado = 'erro_a_enviar', erro_codigo = ?, "
        "erro_mensagem = ?, corpo_json = ?, proxima_tentativa_em = NULL, "
        "atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND estado <> 'gravado_sem_volta'",
        (codigo, mensagem, corpo_json, cadastro_id, versao),
    )


def marcar_desfecho(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    estado: str,
    *,
    alerta: str | None = None,
    resposta: str | None = None,
) -> bool:
    """True se mudou. `gravado_sem_volta` só sai para `confirmado` ou `orfao`:
    qualquer outro estado faria o Portal gravar o produto de novo no ERP."""
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = ?, alerta = ?, resposta_flow = ?, "
        "proxima_tentativa_em = NULL, atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? "
        "AND (estado <> 'gravado_sem_volta' OR ? IN ('confirmado', 'orfao'))",
        (estado, alerta, resposta, cadastro_id, versao, estado),
    )
    return cur.rowcount == 1


def adiar(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    *,
    ate: str,
    alerta: str | None = None,
    resposta: str | None = None,
) -> None:
    conn.execute(
        "UPDATE cadastro_volta SET tentativas = tentativas + 1, proxima_tentativa_em = ?, "
        "alerta = COALESCE(?, alerta), resposta_flow = COALESCE(?, resposta_flow), "
        "atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ?",
        (ate, alerta, resposta, cadastro_id, versao),
    )


def _dicts(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    nomes = [d[0] for d in cur.description]
    return [dict(zip(nomes, r, strict=True)) for r in cur.fetchall()]


def pendencias_de_envio(
    conn: sqlite3.Connection, agora_iso: str, limite: int = 20
) -> list[dict[str, Any]]:
    """`proxima_tentativa_em` é texto ISO UTC (`YYYY-MM-DDTHH:MM:SS`)."""
    return _dicts(
        conn.execute(
            f"SELECT * FROM cadastro_volta WHERE estado IN ({_PENDENTES_SQL}) "
            "AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= ?) "
            "ORDER BY atualizado_em LIMIT ?",
            (agora_iso, limite),
        )
    )


def tem_pendencia(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM cadastro_volta WHERE estado IN ({_PENDENTES_SQL}) LIMIT 1"
    ).fetchone()
    return row is not None


def buscar(conn: sqlite3.Connection, cadastro_id: str, versao: int) -> dict[str, Any] | None:
    linhas = _dicts(
        conn.execute(
            "SELECT * FROM cadastro_volta WHERE cadastro_id = ? AND versao = ?",
            (cadastro_id, versao),
        )
    )
    return linhas[0] if linhas else None


def criar_do_produto(conn: sqlite3.Connection, produto_id: str) -> dict[str, Any] | None:
    """Linha de `criar` deste produto que pode ter (ou já ter) comitado no ERP.
    Preferência: gravado_sem_volta, em_andamento, incerto, liberado com SEQ,
    orfao, confirmado; dentro do estado, a mais recente."""
    linhas = _dicts(
        conn.execute(
            "SELECT * FROM cadastro_volta WHERE produto_id = ? AND acao = 'criar' AND ("
            "estado IN ('em_andamento', 'gravado_sem_volta', 'incerto', 'confirmado', 'orfao') "
            "OR (estado = 'liberado' AND seq_reservado IS NOT NULL)) "
            "ORDER BY CASE estado WHEN 'gravado_sem_volta' THEN 0 WHEN 'em_andamento' THEN 1 "
            "WHEN 'incerto' THEN 2 WHEN 'liberado' THEN 3 WHEN 'orfao' THEN 4 ELSE 5 END, "
            "iniciado_em DESC, rowid DESC LIMIT 1",
            (produto_id,),
        )
    )
    return linhas[0] if linhas else None


def seq_criado_pelo_portal(conn: sqlite3.Connection, seq: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM cadastro_volta WHERE erp_produto_id = ? "
        "AND estado IN ('gravado_sem_volta', 'confirmado') LIMIT 1",
        (seq,),
    ).fetchone()
    return row is not None


def em_andamento(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _dicts(conn.execute("SELECT * FROM cadastro_volta WHERE estado = 'em_andamento'"))


def seqs_em_aberto(conn: sqlite3.Connection) -> set[str]:
    """SEQs que o Portal tem em trânsito (ida não deve oferecê-los ao Flow)."""
    rows = conn.execute(
        "SELECT erp_produto_id, seq_reservado FROM cadastro_volta WHERE "
        "estado IN ('em_andamento', 'gravado_sem_volta', 'incerto') "
        "OR (estado = 'liberado' AND seq_reservado IS NOT NULL)"
    ).fetchall()
    return {str(v) for r in rows for v in r if v is not None}
