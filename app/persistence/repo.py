"""Import history repository — parameterized queries only, no string concat."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from app.persistence import db

_MAX_PAGE_SIZE = 500


def insert_import(entry: dict) -> None:
    """Upsert an import entry keyed by id. Idempotent for migration replays."""
    snapshot = entry.get("snapshot")
    check = entry.get("check")
    output_files = entry.get("output_files")
    db_result = entry.get("db_result")

    # Default portal_status for legacy rows = 'sent_to_fire' (what the old
    # pre-review flow did); new rows from /api/commit pass 'parsed'.
    portal_status = entry.get("portal_status")
    if not portal_status:
        portal_status = "sent_to_fire" if entry.get("fire_codigo") else "parsed"

    params = (
        entry["id"],
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
    )
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO imports (
                id, source_filename, imported_at, order_number,
                customer_cnpj, customer_name, fire_codigo,
                snapshot_json, check_json, output_files_json, db_result_json,
                status, error,
                portal_status, sent_to_fire_at,
                production_status, released_at, released_by
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                portal_status   = excluded.portal_status,
                sent_to_fire_at = excluded.sent_to_fire_at,
                production_status = excluded.production_status,
                released_at     = excluded.released_at,
                released_by     = excluded.released_by
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
               production_status, released_at, released_by
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
                   production_status, released_at, released_by
            FROM imports WHERE id = ?
            """,
            (import_id,),
        ).fetchone()
    return _row_to_entry(row) if row else None


def append_audit(import_id: str, event_type: str, detail: Optional[dict] = None) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (import_id, event_type, detail_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                import_id,
                event_type,
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                datetime.now().isoformat(timespec="seconds"),
            ),
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
