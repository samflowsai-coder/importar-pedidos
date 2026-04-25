#!/usr/bin/env python3
"""
Firebird schema explorer — read-only dump de tabelas, colunas, triggers, FKs e generators.

Uso:
    python tools/explore_firebird.py --database empresa_COPIA.fdb > schema_report.txt
    python tools/explore_firebird.py --host 192.168.1.10 --database C:\\Fire\\empresa.fdb

IMPORTANTE: sempre rodar em CÓPIA do banco, nunca na produção.
"""

from __future__ import annotations

import argparse
import os
import sys

FIELD_TYPE_MAP = {
    7: "SMALLINT",
    8: "INTEGER",
    10: "FLOAT",
    12: "DATE",
    13: "TIME",
    14: "CHAR",
    16: "BIGINT",
    23: "BOOLEAN",
    27: "DOUBLE PRECISION",
    35: "TIMESTAMP",
    37: "VARCHAR",
    40: "CSTRING",
    261: "BLOB",
}

TRIGGER_TYPE_MAP = {
    1: "BEFORE INSERT",
    2: "AFTER INSERT",
    3: "BEFORE UPDATE",
    4: "AFTER UPDATE",
    5: "BEFORE DELETE",
    6: "AFTER DELETE",
    17: "BEFORE INSERT OR UPDATE",
    18: "AFTER INSERT OR UPDATE",
    113: "BEFORE INSERT OR UPDATE OR DELETE",
    114: "AFTER INSERT OR UPDATE OR DELETE",
}


def _decode_field_type(ftype: int, flen: int, sub_type: int) -> str:
    if ftype == 261:
        return "BLOB TEXT" if sub_type == 1 else "BLOB BIN"
    name = FIELD_TYPE_MAP.get(ftype, f"TYPE({ftype})")
    if ftype in (14, 37) and flen:
        return f"{name}({flen})"
    return name


def _decode_trigger_type(ttype: int) -> str:
    return TRIGGER_TYPE_MAP.get(ttype, f"TRIGGER_TYPE({ttype})")


def _connect(args: argparse.Namespace):
    try:
        from firebird.driver import connect, driver_config  # type: ignore[import]
    except ImportError:
        print("ERRO: firebird-driver não instalado. Rode: pip install firebird-driver",
              file=sys.stderr)
        sys.exit(1)

    # Optional: custom Firebird client library (for extracted/non-installed Firebird)
    client_lib = args.client_library or os.environ.get("FB_CLIENT_LIBRARY", "")
    if client_lib:
        driver_config.fb_client_library.value = client_lib

    database = args.database or os.environ.get("FB_DATABASE", "")
    host = args.host or os.environ.get("FB_HOST", "")
    port = int(args.port or os.environ.get("FB_PORT", "3050"))
    user = args.user or os.environ.get("FB_USER", "SYSDBA")
    password = args.password or os.environ.get("FB_PASSWORD", "masterkey")

    if not database:
        print("ERRO: --database é obrigatório (ou defina FB_DATABASE)", file=sys.stderr)
        sys.exit(1)

    try:
        if host:
            return connect(host=host, port=port, database=database, user=user, password=password)
        return connect(database=database, user=user, password=password)
    except Exception as exc:
        print(f"ERRO ao conectar: {exc}", file=sys.stderr)
        sys.exit(1)


def explore(conn) -> None:
    cur = conn.cursor()

    # ── Tabelas ──────────────────────────────────────────────────────────────
    cur.execute("""
        SELECT TRIM(RDB$RELATION_NAME)
        FROM RDB$RELATIONS
        WHERE RDB$SYSTEM_FLAG = 0 AND RDB$VIEW_BLR IS NULL
        ORDER BY RDB$RELATION_NAME
    """)
    tables = [row[0] for row in cur.fetchall()]

    print(f"{'═' * 70}")
    print(f"TABELAS DO USUÁRIO ({len(tables)})")
    print(f"{'═' * 70}\n")

    for table in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            row_count = cur.fetchone()[0]
        except Exception:
            row_count = "N/A"

        print(f"┌─ {table}  ({row_count} linha(s))")

        cur.execute("""
            SELECT
                TRIM(RF.RDB$FIELD_NAME),
                F.RDB$FIELD_TYPE,
                F.RDB$FIELD_LENGTH,
                F.RDB$FIELD_SUB_TYPE,
                RF.RDB$NULL_FLAG,
                RF.RDB$DEFAULT_VALUE
            FROM RDB$RELATION_FIELDS RF
            JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME = RF.RDB$FIELD_SOURCE
            WHERE TRIM(RF.RDB$RELATION_NAME) = ?
            ORDER BY RF.RDB$FIELD_POSITION
        """, (table,))

        for col_name, ftype, flen, sub_type, null_flag, has_default in cur.fetchall():
            type_str = _decode_field_type(ftype or 0, flen or 0, sub_type or 0)
            nullable = "NOT NULL" if null_flag else "NULL    "
            default = " DEFAULT" if has_default else ""
            print(f"│  {col_name:<35} {type_str:<22} {nullable}{default}")

        cur.execute("""
            SELECT TRIM(RDB$TRIGGER_NAME), RDB$TRIGGER_TYPE, RDB$TRIGGER_INACTIVE
            FROM RDB$TRIGGERS
            WHERE RDB$SYSTEM_FLAG = 0 AND TRIM(RDB$RELATION_NAME) = ?
            ORDER BY RDB$TRIGGER_SEQUENCE
        """, (table,))
        triggers = cur.fetchall()
        if triggers:
            print("│  ── triggers ──")
            for tname, ttype, inactive in triggers:
                ttype_str = _decode_trigger_type(ttype or 0)
                status = "INATIVO" if inactive else "ativo"
                print(f"│  ⚡ {tname:<40} [{ttype_str}] ({status})")

        print("│")

    # ── Generators/Sequences ─────────────────────────────────────────────────
    cur.execute("""
        SELECT TRIM(RDB$GENERATOR_NAME)
        FROM RDB$GENERATORS
        WHERE RDB$SYSTEM_FLAG = 0
        ORDER BY RDB$GENERATOR_NAME
    """)
    generators = [row[0] for row in cur.fetchall()]

    print(f"\n{'═' * 70}")
    print(f"GENERATORS / SEQUENCES ({len(generators)})")
    print(f"{'═' * 70}\n")

    for gen in generators:
        try:
            cur.execute(f"SELECT GEN_ID({gen}, 0) FROM RDB$DATABASE")
            current = cur.fetchone()[0]
        except Exception:
            current = "ERR"
        print(f"  {gen:<50} atual = {current}")

    # ── Foreign Keys ─────────────────────────────────────────────────────────
    cur.execute("""
        SELECT
            TRIM(RC.RDB$RELATION_NAME),
            TRIM(ISEG.RDB$FIELD_NAME),
            TRIM(RC2.RDB$RELATION_NAME)
        FROM RDB$REF_CONSTRAINTS REFC
        JOIN RDB$RELATION_CONSTRAINTS RC
            ON RC.RDB$CONSTRAINT_NAME = REFC.RDB$CONSTRAINT_NAME
        JOIN RDB$INDEX_SEGMENTS ISEG
            ON ISEG.RDB$INDEX_NAME = RC.RDB$INDEX_NAME
        JOIN RDB$RELATION_CONSTRAINTS RC2
            ON RC2.RDB$CONSTRAINT_NAME = REFC.RDB$CONST_NAME_UQ
        ORDER BY RC.RDB$RELATION_NAME, ISEG.RDB$FIELD_NAME
    """)
    fks = cur.fetchall()

    print(f"\n{'═' * 70}")
    print(f"FOREIGN KEYS ({len(fks)})")
    print(f"{'═' * 70}\n")

    for table, col, ref_table in fks:
        print(f"  {table}.{col}  →  {ref_table}")

    # ── Índices ──────────────────────────────────────────────────────────────
    cur.execute("""
        SELECT TRIM(RDB$INDEX_NAME), TRIM(RDB$RELATION_NAME), RDB$UNIQUE_FLAG
        FROM RDB$INDICES
        WHERE RDB$SYSTEM_FLAG = 0
        ORDER BY RDB$RELATION_NAME, RDB$INDEX_NAME
    """)
    indexes = cur.fetchall()

    print(f"\n{'═' * 70}")
    print(f"ÍNDICES ({len(indexes)})")
    print(f"{'═' * 70}\n")

    for idx_name, table, unique in indexes:
        unique_str = "UNIQUE" if unique else "      "
        print(f"  {unique_str}  {table:<30} {idx_name}")

    cur.close()
    print(f"\n{'═' * 70}")
    print("Exploração concluída (read-only — nenhum dado foi modificado).")
    print(f"{'═' * 70}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explora o schema de um banco Firebird (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python tools/explore_firebird.py --database empresa_COPIA.fdb > schema.txt\n"
            "  python tools/explore_firebird.py --host 192.168.1.10 --database /opt/fire/emp.fdb\n"
            "\nTodas as opções também podem ser definidas via variáveis de ambiente:\n"
            "  FB_DATABASE, FB_HOST, FB_PORT, FB_USER, FB_PASSWORD"
        ),
    )
    parser.add_argument("--database", "-d", metavar="PATH",
                        help="Arquivo .fdb (embedded) ou path no servidor (TCP)")
    parser.add_argument("--host", metavar="HOST",
                        help="Host TCP (omitir para conexão embedded/local)")
    parser.add_argument("--port", metavar="PORT", default="3050",
                        help="Porta TCP (padrão: 3050)")
    parser.add_argument("--user", metavar="USER",
                        help="Usuário Firebird (padrão: SYSDBA)")
    parser.add_argument("--password", metavar="PASS",
                        help="Senha (padrão: FB_PASSWORD ou masterkey)")
    parser.add_argument("--client-library", metavar="PATH",
                        help="Caminho do libfbclient.dylib (padrão: busca automática)")
    args = parser.parse_args()

    conn = _connect(args)
    try:
        explore(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
