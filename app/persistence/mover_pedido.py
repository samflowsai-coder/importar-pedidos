"""Mover um pedido em revisão para a outra empresa (spec regra-faturamento §6.5, §10.4).

Mover = a linha de `imports` nasce no `app_state_<destino>.db` com o MESMO id
(o rastro não se parte) e some do `app_state_<origem>.db`. O arquivo original
não se mexe (`original_path` continua valendo). Nada derivado do Fire ou da
empresa de origem viaja: check de produto, check de preço, escolha pontual
(SEQ do Fire de origem), cliente escolhido, ack de sem preço — o destino
recalcula (quem chama, ver a rota `mover-empresa` em `app/web/server.py`).

Só move pedido que não saiu do Portal: em revisão, sem número do Fire, sem
XLSX, sem nada no outbox (Flow/Gestor) e sem revisão de preço que o Flow possa
conhecer. Rastro de saída é motivo de `PedidoNaoMovivel`, nunca de apagar.

Dois bancos não fecham numa transação só. A origem fica travada (BEGIN
IMMEDIATE) do começo ao fim: a guarda vale até o DELETE, e nada novo entra
nela no meio. Ordem: copiar no destino (commit) e só então apagar na origem.
Interrompido no meio, o pedido fica nos dois e a próxima chamada conclui — a
cópia é idempotente (INSERT OR IGNORE; audit e eventos sem repetir). Se na
janela a origem saiu do Portal, a cópia do destino é desfeita: nunca dois.

`trava_do_pedido` é a trava por pedido do processo (o web roda num processo
só), dividida entre o mover e tudo que leva o pedido para fora do Portal
(envio ao Fire, export XLSX — `app/web/server.py`): o envio a segura da
leitura do estado até gravar `fire_codigo`/`output_files`, e o mover da guarda
até apagar a origem. Sem ela, um mover na janela do envio (segundos no Fire)
passava pela guarda e o pedido ficava no Fire da origem e em revisão no destino.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal

from app.persistence import router
from app.utils.logger import logger

# Trava por import_id (padrão de `app/reconcile/runner.py::_lock_for`), com
# contagem de quem a usa para o dicionário não crescer sem fim.
_TRAVAS: dict[str, list[Any]] = {}  # import_id -> [Lock, usuários]
_TRAVAS_GUARD = threading.Lock()

# Quanto o mover espera um envio em andamento antes de desistir (409).
_ESPERA_S = 2.0

# Quanto o envio (Fire/XLSX) espera a trava do pedido: longo (um envio de
# outra aba leva segundos no Fire), mas finito — nunca um request pendurado.
_ESPERA_ENVIO_S = 120.0

# Um mover por vez na parte de banco: cada mover trava a própria origem e pede
# o banco do outro — dois em sentidos opostos (X mm→nasmar, Y nasmar→mm) se
# prenderiam até o timeout do SQLite ("database is locked") e parariam toda
# escrita nas duas empresas. Ordem fixa: trava do pedido → este lock → bancos.
# O envio nunca pega este lock, então não há ciclo.
_MOVER_LOCK = threading.Lock()

# Colunas de `imports` derivadas do Fire/empresa de origem: nascem NULL no destino.
_ZERADAS_NO_DESTINO = (
    "check_json",
    "check_preco_json",
    "produto_escolhido_json",  # R18: fire_seq do Fire de origem
    "db_result_json",
    "cliente_override_codigo",
    "cliente_override_razao",
    "cliente_override_at",
    "cliente_override_by",
    "cliente_override_cnpj",
    "sem_preco_ack_by",
    "sem_preco_ack_at",
    "sem_preco_ack_items",
    "sent_to_fire_at",
    "fire_status_last_seen",
    "fire_status_polled_at",
    "reconciled_at",
)

# Tabelas do banco da empresa com `import_id`. A guarda garante que as de
# rastro de saída estão vazias (ou só com revisão de preço que nunca saiu);
# apagar todas é o cinto de segurança contra o ON DELETE CASCADE desligado.
_APAGADAS_NA_ORIGEM = (
    "erp_item_vinculo",
    "erp_xlsx_linha",
    "preco_revisao",
    "preco_revisao_piso",
    "preco_decisao",
    "outbox",
    "audit_log",
    "order_lifecycle_events",
)

_AUDIT_COLS = ("event_type", "detail_json", "created_at")
_EVENTO_COLS = ("event_type", "source", "payload_json", "trace_id", "occurred_at", "ingested_at")


class PedidoOcupado(Exception):  # noqa: N818 — par de PedidoNaoMovivel
    """O pedido está sendo enviado agora (trava ocupada além da espera)."""


@contextmanager
def trava_do_pedido(import_id: str, *, espera: float | None = None) -> Iterator[None]:
    """Exclusão mútua por pedido entre mover e envio. `espera=None` espera o
    quanto for; com espera (o envio usa `_ESPERA_ENVIO_S`), `PedidoOcupado` ao
    estourar."""
    with _TRAVAS_GUARD:
        par = _TRAVAS.setdefault(import_id, [threading.Lock(), 0])
        par[1] += 1
    obteve = False
    try:
        obteve = par[0].acquire(timeout=-1 if espera is None else espera)
        if not obteve:
            raise PedidoOcupado(import_id)
        yield
    finally:
        if obteve:
            par[0].release()
        with _TRAVAS_GUARD:
            par[1] -= 1
            if par[1] == 0:
                _TRAVAS.pop(import_id, None)


class PedidoNaoMovivel(Exception):  # noqa: N818 — nome do contrato do plano
    """O pedido não pode mudar de empresa; `motivo` vai para o operador."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


def _tem(conn: sqlite3.Connection, tabela: str, import_id: str, extra: str = "") -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM {tabela} WHERE import_id = ?{extra} LIMIT 1",  # noqa: S608 — constante
            (import_id,),
        ).fetchone()
        is not None
    )


def _e_espelho_de_lote(import_id: str) -> bool:
    """O espelho mora na empresa do lote: movido, o reparo do fechamento o recriaria
    lá e o pedido existiria nas duas empresas, travando o envio."""
    from app.persistence import db, lotes_repo

    with db.connect_shared() as sconn:
        return lotes_repo.lote_do_espelho(sconn, import_id) is not None


def _motivo(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Por que este pedido não se move, ou None. Lê o banco de `conn` e, para o
    pedido espelho de lote, o banco compartilhado."""
    iid = row["id"]
    if _e_espelho_de_lote(iid):
        return "é o pedido espelho de um lote intercompany"
    if row["fire_codigo"] is not None or _tem(conn, "erp_item_vinculo", iid):
        return "já foi para o Fire"
    if (row["output_files_json"] or "").strip() not in ("", "[]") or _tem(
        conn, "erp_xlsx_linha", iid
    ):
        return "já foi exportado em XLSX"
    if row["portal_status"] != "parsed":
        return "não está em revisão"
    if row["gestor_order_id"] or row["apontae_order_id"] or _tem(conn, "outbox", iid):
        return "já foi enviado ao Flow ou ao Gestor"
    if (
        _tem(conn, "preco_revisao", iid, " AND (enviada_em IS NOT NULL OR tentada_em IS NOT NULL)")
        or _tem(conn, "preco_revisao_piso", iid)
        or _tem(conn, "preco_decisao", iid)
    ):
        return "o preço já foi para aprovação no Flow"
    return None


def motivo_nao_move(import_id: str) -> str | None:
    """A mesma guarda do mover (`_motivo`), lida na empresa ativa: o detalhe
    só oferece "Mover" quando o mover aceitaria. `None` = pode mover (ou o
    pedido não está aqui — aí o próprio mover responde)."""
    from app.persistence import db

    with db.connect() as conn:
        row = conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        return _motivo(conn, row) if row is not None else None


def _copiar_faltantes(
    dconn: sqlite3.Connection,
    tabela: str,
    cols: tuple[str, ...],
    linhas: list[sqlite3.Row],
    import_id: str,
    env_id: str,
) -> None:
    """Copia as linhas da origem que o destino ainda não tem (multiconjunto por
    conteúdo): na 1ª vez copia tudo; ao concluir um mover interrompido, só o
    que entrou na origem depois — nunca duplica."""
    lista = ", ".join(cols)
    ja = Counter(
        tuple(r)
        for r in dconn.execute(
            f"SELECT {lista} FROM {tabela} WHERE import_id = ?",  # noqa: S608 — constante
            (import_id,),
        ).fetchall()
    )
    marcas = ", ".join("?" * (len(cols) + 2))
    for r in linhas:
        valores = tuple(r[c] for c in cols)
        if ja[valores] > 0:
            ja[valores] -= 1
            continue
        dconn.execute(
            f"INSERT INTO {tabela} (environment_id, import_id, {lista}) VALUES ({marcas})",  # noqa: S608
            (env_id, import_id, *valores),
        )


def _copiar_no_destino(
    oconn: sqlite3.Connection, row: sqlite3.Row, *, origem: dict, destino: dict, por: str
) -> bool:
    """Copia o pedido no destino. True = mover novo; False = o destino já tinha
    este mesmo mover (origem→destino): é a conclusão de um interrompido."""
    iid = row["id"]
    linha = dict(row) | dict.fromkeys(_ZERADAS_NO_DESTINO) | {"environment_id": destino["id"]}
    audit = oconn.execute(
        f"SELECT {', '.join(_AUDIT_COLS)} FROM audit_log WHERE import_id = ? ORDER BY id",  # noqa: S608
        (iid,),
    ).fetchall()
    eventos = oconn.execute(
        f"SELECT {', '.join(_EVENTO_COLS)} FROM order_lifecycle_events"  # noqa: S608
        " WHERE import_id = ? ORDER BY id",
        (iid,),
    ).fetchall()
    with router.env_connect(destino["slug"]) as dconn:
        dconn.execute("BEGIN IMMEDIATE")
        dconn.execute(
            f"INSERT OR IGNORE INTO imports ({', '.join(linha)})"  # noqa: S608
            f" VALUES ({', '.join('?' * len(linha))})",
            tuple(linha.values()),
        )
        _copiar_faltantes(dconn, "audit_log", _AUDIT_COLS, audit, iid, destino["id"])
        _copiar_faltantes(
            dconn, "order_lifecycle_events", _EVENTO_COLS, eventos, iid, destino["id"]
        )
        # Na mesma transação da linha, e só se o último "movido" do destino não
        # for este mesmo mover: concluir um mover interrompido não repete, e
        # mandar de volta na janela (pedido nos dois) registra a volta.
        ultimo = dconn.execute(
            "SELECT detail_json FROM audit_log WHERE import_id = ?"
            " AND event_type = 'movido_de_empresa' ORDER BY id DESC LIMIT 1",
            (iid,),
        ).fetchone()
        ultimo_mover = json.loads(ultimo[0]) if ultimo and ultimo[0] else {}
        novo = (ultimo_mover.get("de"), ultimo_mover.get("para")) != (
            origem["slug"],
            destino["slug"],
        )
        if novo:
            dconn.execute(
                "INSERT INTO audit_log (environment_id, import_id, event_type, detail_json,"
                " created_at) VALUES (?, ?, 'movido_de_empresa', ?, ?)",
                (
                    destino["id"],
                    iid,
                    json.dumps(
                        {"de": origem["slug"], "para": destino["slug"], "por": por},
                        ensure_ascii=False,
                    ),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
    return novo


def _apagar_pedido(conn: sqlite3.Connection, import_id: str) -> None:
    for tabela in _APAGADAS_NA_ORIGEM:
        conn.execute(f"DELETE FROM {tabela} WHERE import_id = ?", (import_id,))  # noqa: S608
    conn.execute("DELETE FROM imports WHERE id = ?", (import_id,))


def _apagar_na_origem(conn: sqlite3.Connection, import_id: str) -> None:
    # Existe separado de `_apagar_pedido` só como ponto de monkeypatch dos testes.
    _apagar_pedido(conn, import_id)


def _desfazer_copia(import_id: str, destino: dict) -> None:
    """A origem não pode mais sair (foi ao Fire na janela de um mover
    interrompido): a cópia do destino, se ainda em revisão, some. Se ela também
    já saiu, fica — os dois lados são de verdade e só um humano resolve."""
    with router.env_connect(destino["slug"]) as dconn:
        dconn.execute("BEGIN IMMEDIATE")
        row = dconn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row is None:
            return
        if _motivo(dconn, row) is not None:
            logger.error(
                "mover.pedido_nos_dois_bancos import_id={} destino={}", import_id, destino["slug"]
            )
            return
        _apagar_pedido(dconn, import_id)
    logger.warning("mover.copia_desfeita import_id={} destino={}", import_id, destino["slug"])


EstadoMover = Literal["movido", "concluido", "ja_movido"]


def mover_import(
    import_id: str, *, origem: dict[str, Any], destino: dict[str, Any], por: str
) -> EstadoMover:
    """Move o pedido de `origem` para `destino`. "movido" = mover novo, feito
    agora; "concluido" = terminou um mover interrompido (o destino já tinha a
    cópia deste mesmo mover); "ja_movido" = já estava só no destino.
    `PedidoNaoMovivel` = nada mudou na origem. `PedidoOcupado` = um envio
    segura o pedido além de `_ESPERA_S`; nada mudou."""
    with (
        trava_do_pedido(import_id, espera=_ESPERA_S),
        _MOVER_LOCK,
        router.env_connect(origem["slug"]) as oconn,
    ):
        oconn.execute("BEGIN IMMEDIATE")
        row = oconn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        if row is None:
            with router.env_connect(destino["slug"]) as dconn:
                no_destino = dconn.execute(
                    "SELECT 1 FROM imports WHERE id = ?", (import_id,)
                ).fetchone()
            if no_destino is not None:
                return "ja_movido"
            raise PedidoNaoMovivel("não encontrado")
        motivo = _motivo(oconn, row)
        if motivo is not None:
            _desfazer_copia(import_id, destino)
            raise PedidoNaoMovivel(motivo)
        novo = _copiar_no_destino(oconn, row, origem=origem, destino=destino, por=por)
        _apagar_na_origem(oconn, import_id)
    estado: EstadoMover = "movido" if novo else "concluido"
    logger.info(
        "mover.pedido import_id={} de={} para={} por={} estado={}",
        import_id,
        origem["slug"],
        destino["slug"],
        por,
        estado,
    )
    return estado
