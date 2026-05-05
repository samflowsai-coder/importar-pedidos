"""Import history repository — parameterized queries only, no string concat."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from app.persistence import context as env_context
from app.persistence import db

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
                trace_id, state_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                trace_id        = COALESCE(imports.trace_id, excluded.trace_id)
                -- portal_status, production_status, state_version,
                -- sent_to_fire_at, released_at, released_by,
                -- cliente_override_codigo, cliente_override_razao,
                -- cliente_override_at, cliente_override_by are SM-owned
                -- or set via dedicated helpers — never clobbered here.
            """,
            params,
        )


def _derive_cnpj(snapshot: Optional[dict]) -> Optional[str]:
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
        "portal_status": _get("portal_status") or ("sent_to_fire" if row["fire_codigo"] else "parsed"),
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
        "output_files": json.loads(row["output_files_json"]) if row["output_files_json"] else [],
        "db_result": json.loads(row["db_result_json"]) if row["db_result_json"] else None,
        "snapshot": json.loads(row["snapshot_json"]) if row["snapshot_json"] else None,
        "check": json.loads(row["check_json"]) if _get("check_json") else None,
    }


def _build_where(
    status: Optional[str],
    portal_status: Optional[str],
    production_status: Optional[str],
    customer_search: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if portal_status:
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
    status: Optional[str] = None,
    portal_status: Optional[str] = None,
    production_status: Optional[str] = None,
    customer_search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
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
               cliente_override_at, cliente_override_by
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
    status: Optional[str] = None,
    portal_status: Optional[str] = None,
    production_status: Optional[str] = None,
    customer_search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> int:
    clause, params = _build_where(
        status, portal_status, production_status, customer_search, date_from, date_to
    )
    with db.connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM imports {clause}", params).fetchone()
    return int(row["n"])


def get_import(import_id: str) -> Optional[dict]:
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
                   cliente_override_at, cliente_override_by
            FROM imports WHERE id = ?
            """,
            (import_id,),
        ).fetchone()
    return _row_to_entry(row) if row else None


def append_audit(import_id: str, event_type: str, detail: Optional[dict] = None) -> None:
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
    fire_codigo: Optional[int] = None,
    db_result: Optional[dict] = None,
    output_files: Optional[list[dict]] = None,
    sent_to_fire_at: Optional[str] = None,
) -> None:
    """Update Fire-related auxiliary columns. Does NOT touch portal_status /
    production_status — those mutations belong to `app.state.transition`.
    Pass only the fields you want to update; others stay as they are.
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


def find_import_id_by_gestor(gestor_order_id: str) -> Optional[str]:
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
    user: Optional[str] = None,
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


def list_pending_for_fire_poll(window_days: int = 7) -> list[dict]:
    """Return imports eligible for Firebird status polling.

    Criteria: sent_to_fire, no production started, fire_codigo present,
    within the given time window. Ordered so least-recently-polled entries
    come first (NULL treated as older than any timestamp).
    """
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, fire_codigo, trace_id, snapshot_json,
                   fire_status_last_seen, fire_status_polled_at
            FROM imports
            WHERE portal_status = 'sent_to_fire'
              AND production_status = 'none'
              AND fire_codigo IS NOT NULL
              AND imported_at >= datetime('now', '-' || ? || ' days')
            ORDER BY fire_status_polled_at ASC
            """,
            (window_days,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_fire_poll_result(import_id: str, fire_status: str, polled_at: str) -> None:
    """Stamp the latest Firebird status and poll timestamp. No state machine event."""
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
