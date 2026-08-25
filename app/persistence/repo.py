"""Import history repository — parameterized queries only, no string concat."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.erp.fire_reconcile import Candidato
from app.observability.trace import current_trace_id
from app.persistence import context as env_context
from app.persistence import db
from app.state.events import _insert_event
from app.state.machine import EventSource, LifecycleEvent

_MAX_PAGE_SIZE = 500


def insert_import(entry: dict) -> None:
    """Upsert an import entry keyed by id. Idempotent for migration replays.

    State fields (`portal_status`, `production_status`, `state_version`,
    `sent_to_fire_at`, `released_at`, `released_by`) are owned by the
    state machine (`app.state.transition`). On INSERT they're seeded from
    the entry; on conflict they are NEVER clobbered — only the SM moves them.
    `trace_id` is preserved across upserts (COALESCE keeps the original).
    Cliente override fields (`cliente_override_*`) are owned by
    `set_client_override()` — also never clobbered on upsert.
    Sidecar do ack de itens sem preço (`sem_preco_ack_*`) é gerenciado
    por `set_sem_preco_ack()` — também nunca clobbado no upsert.
    """
    snapshot = entry.get("snapshot")
    check = entry.get("check")
    output_files = entry.get("output_files")
    db_result = entry.get("db_result")

    # Default portal_status for legacy rows = 'sent_to_fire' (what the old
    # pre-review flow did); new rows from /api/commit pass 'parsed'.
    portal_status = entry.get("portal_status")
    if not portal_status:
        portal_status = "sent_to_fire" if entry.get("fire_codigo") else "parsed"

    environment_id = entry.get("environment_id") or env_context.current_env_id()
    params = (
        entry["id"],
        environment_id,
        entry["source_filename"],
        entry["imported_at"],
        entry.get("order_number"),
        entry.get("customer_cnpj") or _derive_cnpj(snapshot),
        entry.get("customer") or entry.get("customer_name"),
        entry.get("fire_codigo"),
        json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None,
        json.dumps(check, ensure_ascii=False) if check is not None else None,
        json.dumps(output_files, ensure_ascii=False) if output_files else None,
        json.dumps(db_result, ensure_ascii=False) if db_result else None,
        entry.get("status", "success"),
        entry.get("error"),
        portal_status,
        entry.get("sent_to_fire_at"),
        entry.get("production_status", "none"),
        entry.get("released_at"),
        entry.get("released_by"),
        entry.get("trace_id"),
        int(entry.get("state_version", 1)),
        entry.get("file_sha256"),
        entry.get("original_path"),
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO imports (
                id, environment_id, source_filename, imported_at, order_number,
                customer_cnpj, customer_name, fire_codigo,
                snapshot_json, check_json, output_files_json, db_result_json,
                status, error,
                portal_status, sent_to_fire_at,
                production_status, released_at, released_by,
                trace_id, state_version,
                file_sha256, original_path
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                source_filename = excluded.source_filename,
                imported_at     = excluded.imported_at,
                order_number    = excluded.order_number,
                customer_cnpj   = excluded.customer_cnpj,
                customer_name   = excluded.customer_name,
                fire_codigo     = excluded.fire_codigo,
                snapshot_json   = excluded.snapshot_json,
                check_json      = excluded.check_json,
                output_files_json = excluded.output_files_json,
                db_result_json  = excluded.db_result_json,
                status          = excluded.status,
                error           = excluded.error,
                trace_id        = COALESCE(imports.trace_id, excluded.trace_id),
                file_sha256     = COALESCE(imports.file_sha256, excluded.file_sha256),
                original_path   = COALESCE(imports.original_path, excluded.original_path)
                -- portal_status, production_status, state_version,
                -- sent_to_fire_at, released_at, released_by,
                -- cliente_override_codigo, cliente_override_razao,
                -- cliente_override_at, cliente_override_by,
                -- sem_preco_ack_by, sem_preco_ack_at, sem_preco_ack_items
                -- are SM-owned or set via dedicated helpers — never clobbered here.
            """,
            params,
        )


def _derive_cnpj(snapshot: dict | None) -> str | None:
    if not snapshot:
        return None
    header = snapshot.get("header") or {}
    return header.get("customer_cnpj")


def _row_to_entry(row) -> dict:
    keys = row.keys()

    def _get(col):
        return row[col] if col in keys else None

    return {
        "id": row["id"],
        "source_filename": row["source_filename"],
        "imported_at": row["imported_at"],
        "order_number": row["order_number"],
        "customer_cnpj": row["customer_cnpj"],
        "customer": row["customer_name"],
        "fire_codigo": row["fire_codigo"],
        "status": row["status"],
        "error": row["error"],
        "portal_status": _get("portal_status")
        or ("sent_to_fire" if row["fire_codigo"] else "parsed"),
        "sent_to_fire_at": _get("sent_to_fire_at"),
        "production_status": row["production_status"],
        "released_at": row["released_at"],
        "released_by": row["released_by"],
        "trace_id": _get("trace_id"),
        "state_version": _get("state_version") or 1,
        "gestor_order_id": _get("gestor_order_id"),
        "apontae_order_id": _get("apontae_order_id"),
        "cliente_override_codigo": _get("cliente_override_codigo"),
        "cliente_override_razao": _get("cliente_override_razao"),
        "cliente_override_at": _get("cliente_override_at"),
        "cliente_override_by": _get("cliente_override_by"),
        "sem_preco_ack_by": _get("sem_preco_ack_by"),
        "sem_preco_ack_at": _get("sem_preco_ack_at"),
        "sem_preco_ack_items": (
            json.loads(_get("sem_preco_ack_items")) if _get("sem_preco_ack_items") else None
        ),
        "fire_status_last_seen": _get("fire_status_last_seen"),
        "fire_status_polled_at": _get("fire_status_polled_at"),
        "output_files": json.loads(row["output_files_json"]) if row["output_files_json"] else [],
        "db_result": json.loads(row["db_result_json"]) if row["db_result_json"] else None,
        "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] else None,
        "check": json.loads(row["check_json"]) if _get("check_json") else None,
    }


def _build_where(
    status: str | None,
    portal_status: str | list[str] | None,
    production_status: str | None,
    customer_search: str | None,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if portal_status:
        if isinstance(portal_status, list):
            placeholders = ", ".join("?" for _ in portal_status)
            where.append(f"portal_status IN ({placeholders})")
            params.extend(portal_status)
        else:
            where.append("portal_status = ?")
            params.append(portal_status)
    if production_status:
        where.append("production_status = ?")
        params.append(production_status)
    if customer_search:
        where.append("(customer_name LIKE ? OR customer_cnpj LIKE ? OR order_number LIKE ?)")
        needle = f"%{customer_search}%"
        params.extend([needle, needle, needle])
    if date_from:
        where.append("imported_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("imported_at <= ?")
        params.append(date_to)
    return (f"WHERE {' AND '.join(where)}" if where else ""), params


def list_imports(
    limit: int = 100,
    offset: int = 0,
    status: str | None = None,
    portal_status: str | list[str] | None = None,
    production_status: str | None = None,
    customer_search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict]:
    """Paginated list with optional filters. All params bound as ? placeholders."""
    limit = max(1, min(int(limit), _MAX_PAGE_SIZE))
    offset = max(0, int(offset))

    clause, params = _build_where(
        status, portal_status, production_status, customer_search, date_from, date_to
    )
    sql = f"""
        SELECT id, source_filename, imported_at, order_number,
               customer_cnpj, customer_name, fire_codigo,
               snapshot_json, check_json, output_files_json, db_result_json,
               status, error,
               portal_status, sent_to_fire_at,
               production_status, released_at, released_by,
               trace_id, state_version, gestor_order_id, apontae_order_id,
               cliente_override_codigo, cliente_override_razao,
               cliente_override_at, cliente_override_by,
               sem_preco_ack_by, sem_preco_ack_at, sem_preco_ack_items,
               fire_status_last_seen, fire_status_polled_at
        FROM imports
        {clause}
        ORDER BY imported_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_entry(r) for r in rows]


def count_imports(
    status: str | None = None,
    portal_status: str | list[str] | None = None,
    production_status: str | None = None,
    customer_search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    clause, params = _build_where(
        status, portal_status, production_status, customer_search, date_from, date_to
    )
    with db.connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM imports {clause}", params).fetchone()
    return int(row["n"])


def count_by_portal_status(
    status: str | None = None,
    production_status: str | None = None,
    customer_search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, int]:
    """Quantos pedidos existem em CADA `portal_status`, numa ida só ao banco.

    Quem consome é o seletor de status da lista ("Em revisão" / "No Fire" /
    "Cancelado"). Sem isto, a reconciliação move 296 dos 308 pedidos de uma vez
    e a tela não diz para onde eles foram — o contador do rodapé conta só o
    filtro ativo, então a lista simplesmente encolhe.

    `portal_status` fica DE FORA do WHERE de propósito: é o eixo que está sendo
    contado. Todos os OUTROS filtros entram, para que o número do chip seja
    exatamente o que aparece ao clicar nele — um contador que discordasse da
    lista seria pior que contador nenhum.

    A coluna é `NOT NULL DEFAULT 'sent_to_fire'` (`schema_env.py:27`), então
    toda linha cai em algum balde. Estados sem chip próprio (hoje `error`)
    voltam no dict mesmo assim: quem soma decide o que mostrar, e a rota não
    deve mentir por omissão.
    """
    clause, params = _build_where(
        status, None, production_status, customer_search, date_from, date_to
    )
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT portal_status AS ps, COUNT(*) AS n
            FROM imports
            {clause}
            GROUP BY portal_status
            """,
            params,
        ).fetchall()
    return {row["ps"]: int(row["n"]) for row in rows}


def get_import(import_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT id, source_filename, imported_at, order_number,
                   customer_cnpj, customer_name, fire_codigo,
                   snapshot_json, check_json, output_files_json, db_result_json,
                   status, error,
                   portal_status, sent_to_fire_at,
                   production_status, released_at, released_by,
                   trace_id, state_version, gestor_order_id, apontae_order_id,
                   cliente_override_codigo, cliente_override_razao,
                   cliente_override_at, cliente_override_by,
                   sem_preco_ack_by, sem_preco_ack_at, sem_preco_ack_items,
                   fire_status_last_seen, fire_status_polled_at
            FROM imports WHERE id = ?
            """,
            (import_id,),
        ).fetchone()
    return _row_to_entry(row) if row else None


def append_audit(import_id: str, event_type: str, detail: dict | None = None) -> None:
    environment_id = env_context.current_env_id()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (environment_id, import_id, event_type, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                environment_id,
                import_id,
                event_type,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def update_fire_metadata(
    import_id: str,
    *,
    fire_codigo: int | None = None,
    db_result: dict | None = None,
    output_files: list[dict] | None = None,
    sent_to_fire_at: str | None = None,
    check: dict | None = None,
) -> None:
    """Update Fire-related auxiliary columns. Does NOT touch portal_status /
    production_status — those mutations belong to `app.state.transition`.
    Pass only the fields you want to update; others stay as they are.

    `check`: quando passado, regrava `check_json` com o resultado fresco do
    `check_order` (ex.: após um vínculo de-para, para o re-open e o badge da
    lista refletirem o novo match em vez do check antigo armazenado).
    """
    sets: list[str] = []
    params: list[Any] = []
    if fire_codigo is not None:
        sets.append("fire_codigo = ?")
        params.append(fire_codigo)
    if db_result is not None:
        sets.append("db_result_json = ?")
        params.append(json.dumps(db_result, ensure_ascii=False))
    if output_files is not None:
        sets.append("output_files_json = ?")
        params.append(json.dumps(output_files, ensure_ascii=False))
    if sent_to_fire_at is not None:
        sets.append("sent_to_fire_at = ?")
        params.append(sent_to_fire_at)
    if check is not None:
        sets.append("check_json = ?")
        params.append(json.dumps(check, ensure_ascii=False))
    if not sets:
        return
    params.append(import_id)
    with db.connect() as conn:
        conn.execute(
            f"UPDATE imports SET {', '.join(sets)} WHERE id = ?",
            params,
        )


def set_gestor_order_id(import_id: str, gestor_order_id: str) -> None:
    """Stamp the external id returned by Gestor de Produção on the order."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE imports SET gestor_order_id = ? WHERE id = ?",
            (gestor_order_id, import_id),
        )


def set_apontae_order_id(import_id: str, apontae_order_id: str) -> None:
    """Stamp the Apontaê id (first webhook event includes it)."""
    with db.connect() as conn:
        conn.execute(
            "UPDATE imports SET apontae_order_id = ? WHERE id = ?",
            (apontae_order_id, import_id),
        )


def find_import_id_by_gestor(gestor_order_id: str) -> str | None:
    """Reverse-lookup for webhooks that omit our external_id."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM imports WHERE gestor_order_id = ? LIMIT 1",
            (gestor_order_id,),
        ).fetchone()
    return row["id"] if row else None


def set_client_override(
    import_id: str,
    *,
    codigo: int,
    razao: str,
    user: str | None = None,
) -> None:
    """Persist a manual cliente selection for a parsed pedido.

    Sidecar to the snapshot — never mutates `snapshot_json`. Read by
    `_send_one_to_fire` and passed as `override_client_id` to the
    FirebirdExporter. Last-write-wins; `audit_log` keeps every attempt.
    `user` is None until auth (v5) lands.
    """
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE imports
            SET cliente_override_codigo = ?,
                cliente_override_razao  = ?,
                cliente_override_at     = ?,
                cliente_override_by     = ?
            WHERE id = ?
            """,
            (
                int(codigo),
                razao,
                datetime.now().isoformat(timespec="seconds"),
                user,
                import_id,
            ),
        )


def set_sem_preco_ack(
    import_id: str,
    *,
    by_email: str,
    items: list[dict],
) -> None:
    """Persiste o ack do operador para itens sem preço cadastrado no Fire.

    Sidecar — não toca snapshot. Last-write-wins. `items` é lista de dicts
    {ean, product_code, fire_product_id}; serializado como JSON.
    """
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE imports
            SET sem_preco_ack_by    = ?,
                sem_preco_ack_at    = ?,
                sem_preco_ack_items = ?
            WHERE id = ?
            """,
            (
                by_email,
                datetime.now().isoformat(timespec="seconds"),
                json.dumps(items, ensure_ascii=False),
                import_id,
            ),
        )


def list_pending_for_fire_poll(window_days: int = 7) -> list[dict]:
    """Return imports eligible for Firebird status polling.

    Criteria: sent_to_fire OR found_in_fire (reconciliado à mão pode mudar de
    status no Fire tanto quanto o que o próprio portal inseriu), no production
    started, fire_codigo present, within the given time window. The window is
    measured from a FIXED anchor — `reconciled_at` (stamped once by
    `mark_found_in_fire`) falling back to `imported_at` for rows never
    reconciled by hand (plain `sent_to_fire`). Neither anchor ever moves after
    it's first set, which is the point: `fire_status_polled_at` used to be the
    anchor, but `update_fire_poll_result` recarimba esse campo a CADA poll —
    então toda linha que entrasse na janela uma vez nunca mais saía, ela
    renovava a própria âncora sozinha (regressão fechada; ver
    `tests/test_reconcile_repo.py::test_janela_do_poll_ancora_na_reconciliacao_nao_no_ultimo_poll`).
    Ordered so least-recently-polled entries come first (NULL treated as
    older than any timestamp).
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, fire_codigo, trace_id, snapshot_json,
                   fire_status_last_seen, fire_status_polled_at
            FROM imports
            WHERE portal_status IN ('sent_to_fire', 'found_in_fire')
              AND production_status = 'none'
              AND fire_codigo IS NOT NULL
              AND COALESCE(reconciled_at, imported_at)
                    >= datetime('now', '-' || ? || ' days')
            ORDER BY fire_status_polled_at ASC
            """,
            (window_days,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_fire_poll_result(import_id: str, fire_status: str, polled_at: str) -> None:
    """Stamp the latest Firebird status and poll timestamp. No state machine event.

    Nunca toca `reconciled_at` — essa coluna é a âncora fixa da janela do
    poll (ver `list_pending_for_fire_poll`) e só `mark_found_in_fire` a
    grava, uma única vez.
    """
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE imports
            SET fire_status_last_seen = ?, fire_status_polled_at = ?
            WHERE id = ?
            """,
            (fire_status, polled_at, import_id),
        )


def list_audit(import_id: str, limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), _MAX_PAGE_SIZE))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, import_id, event_type, detail_json, created_at
            FROM audit_log
            WHERE import_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (import_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "import_id": r["import_id"],
            "event_type": r["event_type"],
            "detail": json.loads(r["detail_json"]) if r["detail_json"] else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _delivery_cnpjs(snapshot_json: str | None) -> tuple[str, ...]:
    """CNPJs de entrega distintos, na ordem de primeira ocorrência.

    Itens sem `delivery_cnpj` são ignorados. Snapshot ausente/inválido vira
    tupla vazia — não é motivo para explodir a listagem de candidatos.
    """
    if not snapshot_json:
        return ()
    try:
        snapshot = json.loads(snapshot_json)
    except (TypeError, ValueError):
        return ()
    vistos: list[str] = []
    seen: set[str] = set()
    for item in snapshot.get("items") or []:
        cnpj = item.get("delivery_cnpj")
        if not cnpj or cnpj in seen:
            continue
        seen.add(cnpj)
        vistos.append(cnpj)
    return tuple(vistos)


def list_parsed_for_reconcile(limit: int = 500) -> list[Candidato]:
    """Pedidos `parsed` com pelo menos uma âncora de identidade de cliente
    (override de código, CNPJ do header, ou CNPJ de entrega dos itens) — os
    únicos que `fire_reconcile._decidir_candidato` consegue de fato casar.
    Mais antigos primeiro: quem espera há mais tempo é tentado primeiro.
    """
    limit = max(1, min(int(limit), _MAX_PAGE_SIZE))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, order_number, cliente_override_codigo, customer_cnpj,
                   snapshot_json, imported_at
            FROM imports
            WHERE portal_status = 'parsed'
            ORDER BY imported_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    candidatos: list[Candidato] = []
    for row in rows:
        cliente_codigo = row["cliente_override_codigo"]
        cnpj_header = row["customer_cnpj"]
        cnpjs_entrega = _delivery_cnpjs(row["snapshot_json"])
        if cliente_codigo is None and not cnpj_header and not cnpjs_entrega:
            continue  # sem nenhuma âncora de cliente — nunca casa, nem tenta
        candidatos.append(
            Candidato(
                import_id=row["id"],
                numero=row["order_number"] or "",
                cliente_codigo=cliente_codigo,
                cnpj_header=cnpj_header,
                cnpjs_entrega=cnpjs_entrega,
                data_pedido=row["imported_at"],
            )
        )
    return candidatos


def mark_found_in_fire(
    import_id: str,
    *,
    fire_codigo: int,
    fire_status: str,
    caminho: int,
    lojas_casadas: int,
    at: str,
) -> bool:
    """Marca o pedido como `found_in_fire` — compare-and-set de verdade.

    Web e worker rodam em processos distintos, e `transition()` (app.state.
    events) lê o estado com um SELECT FORA da transação de escrita — dois
    gatilhos concorrentes poderiam ambos ler 'parsed' e ambos gravarem o
    evento no log canônico. Por isso NÃO usamos `transition()` aqui: o UPDATE
    abaixo, com `portal_status = 'parsed'` no WHERE, é quem decide o vencedor
    — o SQLite serializa escritores, então só uma das UPDATEs concorrentes
    consegue mudar a linha (`rowcount == 1`); a outra chega depois e já não
    bate mais a condição (`rowcount == 0`). O evento de ciclo de vida só é
    gravado pelo vencedor, na MESMA transação/conexão do UPDATE (via
    `_insert_event`, o helper interno de `app.state.events` — reaproveitado
    para não duplicar a lógica de payload/environment_id/ingested_at; não dá
    para usar `append_event()` pública porque ela abre a própria conexão).

    A mesma UPDATE também bumpa `state_version` (`+1`, dentro do próprio
    compare-and-set — não numa instrução separada, senão deixaria de ser
    atômico). `state_version` é o campo de concorrência otimista que
    `transition()` valida via `expected_state_version` e que volta em
    respostas de webhook (`app/web/webhooks.py`) — sem o bump aqui, quem
    estivesse segurando a versão de antes da reconciliação não teria como
    detectar que o estado mudou por fora do `transition()`.

    Também grava `reconciled_at = at` — a âncora FIXA da janela do poll
    (`list_pending_for_fire_poll`). Só esta função escreve nessa coluna, e só
    aqui: `update_fire_poll_result` (chamado a cada tick do worker) nunca a
    toca, senão a janela nunca expiraria para uma linha reconciliada.

    Devolve `True` se este chamador ganhou a corrida, `False` se outro
    gatilho já tinha marcado o pedido (sem evento duplicado, sem log de
    erro — perder a corrida é o caminho feliz, não uma falha).
    """
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE imports
               SET portal_status = 'found_in_fire',
                   fire_codigo = ?,
                   fire_status_last_seen = ?,
                   fire_status_polled_at = ?,
                   reconciled_at = ?,
                   state_version = state_version + 1
             WHERE id = ? AND portal_status = 'parsed'
            """,
            (fire_codigo, fire_status, at, at, import_id),
        )
        if cur.rowcount != 1:
            return False  # outro gatilho ganhou a corrida; nada a gravar

        # `pedido_cliente` sai de uma leitura própria, não do parâmetro
        # `import_id` (esse é o id interno do portal, não o número do
        # pedido) — sem ele, auditar o evento no log exige join com
        # `imports` só pra saber a que pedido ele se refere.
        pedido_cliente = conn.execute(
            "SELECT order_number FROM imports WHERE id = ?", (import_id,)
        ).fetchone()["order_number"]

        _insert_event(
            conn,
            import_id=import_id,
            event_type=LifecycleEvent.FOUND_IN_FIRE,
            source=EventSource.FIRE,
            payload={
                "fire_codigo": fire_codigo,
                "fire_status": fire_status,
                "pedido_cliente": pedido_cliente,
                "caminho_match": caminho,
                "lojas_casadas": lojas_casadas,
            },
            trace_id=current_trace_id(),
            occurred_at=at,
        )
    return True
