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

# Estados em que o lease pode ser reaproveitado. `liberado` só vale sem SEQ
# reservado: com SEQ o INSERT pode ter comitado no ERP (ver `reservar`).
ESTADOS_DESCARTAVEIS = ("simulado", "erro_definitivo", "transitorio", "liberado")
_PENDENTES = ("gravado_sem_volta", "erro_a_enviar")
# O produto existe no ERP: nenhuma transição daqui pode levar a gravar de novo.
_JA_NO_ERP = ("gravado_sem_volta", "confirmado", "orfao", "orfao_fechado")
ESTADOS = frozenset(
    {
        "simulado",
        "erro_definitivo",
        "em_andamento",
        "gravado_sem_volta",
        "erro_a_enviar",
        "confirmado",
        "erro_registrado",
        "transitorio",
        "liberado",
        "orfao",
        "superado",
        "incerto",
        "orfao_fechado",
    }
)
# Lista branca de `marcar_desfecho`: destino -> origens permitidas. `liberado`,
# `erro_a_enviar` e `gravado_sem_volta` têm porta própria; `orfao_fechado` também.
TRANSICOES: dict[str, frozenset[str]] = {
    "confirmado": frozenset({"gravado_sem_volta"}),
    "orfao": frozenset({"gravado_sem_volta"}),
    "erro_registrado": frozenset({"erro_a_enviar"}),
    "transitorio": frozenset({"erro_a_enviar"}),
    "superado": frozenset({"erro_a_enviar"}),
    "incerto": frozenset({"em_andamento"}),
}
_RESPOSTA_MAX = 64
_NOTA_MAX = 500
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
    Linha descartável (simulação, transitório, liberada) é reaproveitada, mas
    `transitorio`/`liberado` com SEQ não: o INSERT pode ter comitado no ERP."""
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
        "erro_mensagem = NULL, corpo_json = NULL, erp_produto_id = NULL, tentativas = 0, "
        "proxima_tentativa_em = NULL, alerta = NULL, resposta_flow = NULL, "
        "iniciado_em = datetime('now'), atualizado_em = datetime('now') "
        f"WHERE cadastro_id = ? AND versao = ? AND estado IN ({_DESCARTAVEIS_SQL}) "
        "AND NOT (estado IN ('liberado', 'transitorio') AND seq_reservado IS NOT NULL)",
        (acao, tipo, produto_id, modo, plano_json, cadastro_id, versao),
    )
    return cur.rowcount == 1


def marcar_seq(conn: sqlite3.Connection, cadastro_id: str, versao: int, seq: int) -> bool:
    """Só age em `em_andamento`. True se gravou."""
    cur = conn.execute(
        "UPDATE cadastro_volta SET seq_reservado = ?, atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND estado = 'em_andamento'",
        (seq, cadastro_id, versao),
    )
    return cur.rowcount == 1


def marcar_gravado(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    *,
    erp_produto_id: str,
    corpo_json: str,
) -> bool:
    """Só age em `em_andamento` (uma vez). True se gravou."""
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = 'gravado_sem_volta', erp_produto_id = ?, "
        "corpo_json = ?, proxima_tentativa_em = NULL, atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND estado = 'em_andamento'",
        (erp_produto_id, corpo_json, cadastro_id, versao),
    )
    return cur.rowcount == 1


def marcar_erro_a_enviar(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    *,
    codigo: str,
    mensagem: str,
    corpo_json: str,
    rollback_comprovado: bool,
) -> bool:
    """Só age em `em_andamento` (nunca em linha que já está no ERP). True se mudou.
    Um `em_andamento` com SEQ pode ter comitado no ERP, então o SEQ só é zerado
    quando quem chama afirma `rollback_comprovado=True` (o Fire deu rollback e nada
    comitou); com False o SEQ fica e a linha nunca volta a ser reservável."""
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = 'erro_a_enviar', erro_codigo = ?, "
        "erro_mensagem = ?, corpo_json = ?, proxima_tentativa_em = NULL, "
        "seq_reservado = CASE WHEN ? THEN NULL ELSE seq_reservado END, "
        "atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND estado = 'em_andamento'",
        (codigo, mensagem, corpo_json, int(rollback_comprovado), cadastro_id, versao),
    )
    return cur.rowcount == 1


def marcar_desfecho(
    conn: sqlite3.Connection,
    cadastro_id: str,
    versao: int,
    estado: str,
    *,
    alerta: str | None = None,
    resposta: str | None = None,
) -> bool:
    """True se mudou. Só vale a lista branca `TRANSICOES` (destino -> origens):
    o que pode ter comitado no ERP (`incerto`, `liberado` com SEQ, `gravado_sem_volta`,
    `confirmado`, `orfao`) nunca volta a um estado reaproveitável por aqui.
    `em_andamento` com SEQ só chega a `transitorio` (reaproveitável) se o SEQ foi
    zerado antes por `marcar_erro_a_enviar(rollback_comprovado=True)`; sem isso,
    `reservar` recusa `transitorio` com SEQ.
    Destino fora da tabela: ValueError."""
    origens = TRANSICOES.get(estado)
    if origens is None:
        raise ValueError("destino não permitido em marcar_desfecho")
    resposta = resposta[:_RESPOSTA_MAX] if resposta is not None else None
    marcas = ", ".join("?" for _ in origens)
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = ?, alerta = ?, resposta_flow = ?, "
        "proxima_tentativa_em = NULL, atualizado_em = datetime('now') "
        f"WHERE cadastro_id = ? AND versao = ? AND estado IN ({marcas})",
        (estado, alerta, resposta, cadastro_id, versao, *sorted(origens)),
    )
    return cur.rowcount == 1


def liberar_comprovado(conn: sqlite3.Connection, cadastro_id: str, versao: int) -> bool:
    """`em_andamento` -> `liberado`, zerando o SEQ na mesma instrução. Única porta
    para `liberado`: só quem comprovou a ausência do SEQ no ERP chama."""
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = 'liberado', seq_reservado = NULL, "
        "atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND estado = 'em_andamento'",
        (cadastro_id, versao),
    )
    return cur.rowcount == 1


def fechar_orfao(
    conn: sqlite3.Connection, cadastro_id: str, versao: int, *, operador: str, nota: str
) -> bool:
    """`orfao` -> `orfao_fechado` (final). Não vira `superado`: um estado reaproveitável
    apagaria a prova de que o produto existe no ERP. A nota fica em `nota_operador` (500)."""
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = 'orfao_fechado', alerta = ?, nota_operador = ?, "
        "atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ? AND estado = 'orfao'",
        (f"fechado_manual:{operador}"[:_RESPOSTA_MAX], nota[:_NOTA_MAX], cadastro_id, versao),
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
) -> bool:
    """Só age em linha pendente de envio. True se adiou."""
    resposta = resposta[:_RESPOSTA_MAX] if resposta is not None else None
    cur = conn.execute(
        "UPDATE cadastro_volta SET tentativas = tentativas + 1, proxima_tentativa_em = ?, "
        "alerta = COALESCE(?, alerta), resposta_flow = COALESCE(?, resposta_flow), "
        "atualizado_em = datetime('now') "
        f"WHERE cadastro_id = ? AND versao = ? AND estado IN ({_PENDENTES_SQL})",
        (ate, alerta, resposta, cadastro_id, versao),
    )
    return cur.rowcount == 1


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
    Preferência: gravado_sem_volta, em_andamento, incerto, liberado/transitorio com SEQ,
    orfao/orfao_fechado, confirmado; dentro do estado, a mais recente."""
    linhas = _dicts(
        conn.execute(
            "SELECT * FROM cadastro_volta WHERE produto_id = ? AND acao = 'criar' AND ("
            "estado IN ('em_andamento', 'gravado_sem_volta', 'incerto', 'confirmado', 'orfao', "
            "'orfao_fechado') "
            "OR (estado IN ('liberado', 'transitorio') AND seq_reservado IS NOT NULL)) "
            "ORDER BY CASE estado WHEN 'gravado_sem_volta' THEN 0 WHEN 'em_andamento' THEN 1 "
            "WHEN 'incerto' THEN 2 WHEN 'liberado' THEN 3 WHEN 'transitorio' THEN 3 WHEN 'orfao' THEN 4 WHEN 'orfao_fechado' THEN 4 "
            "ELSE 5 END, "
            "iniciado_em DESC, rowid DESC LIMIT 1",
            (produto_id,),
        )
    )
    return linhas[0] if linhas else None


def linha_do_produto_com_seq(
    conn: sqlite3.Connection, produto_id: str, seq: int
) -> dict[str, Any] | None:
    """Linha deste produto que reservou ou gravou `seq` (qualquer estado): o nome
    achado no ERP pode ser o do próprio produto, não um homônimo."""
    linhas = _dicts(
        conn.execute(
            "SELECT * FROM cadastro_volta WHERE produto_id = ? "
            "AND (seq_reservado = ? OR erp_produto_id = ?) "
            "ORDER BY iniciado_em DESC, rowid DESC LIMIT 1",
            (produto_id, int(seq), str(seq)),
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
        "OR (estado IN ('liberado', 'transitorio') AND seq_reservado IS NOT NULL)"
    ).fetchall()
    return {str(v) for r in rows for v in r if v is not None}
