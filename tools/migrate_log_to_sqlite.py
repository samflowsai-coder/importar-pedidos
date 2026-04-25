"""One-shot migration: import_log.json → SQLite (app_state.db).

Idempotent — safe to re-run. Existing ids are updated (UPSERT in repo).

Usage:
    python tools/migrate_log_to_sqlite.py                 # uses configured paths
    python tools/migrate_log_to_sqlite.py --log path.json # explicit source
    python tools/migrate_log_to_sqlite.py --dry-run       # show counts only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app import config as app_config
from app.persistence import db, repo


def default_log_path() -> Path:
    cfg = app_config.load()
    return app_config.imported_dir(cfg) / "import_log.json"


def load_entries(path: Path) -> list[dict]:
    if not path.exists():
        print(f"! log não encontrado: {path}", file=sys.stderr)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"! JSON inválido em {path}: {exc}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"! formato inesperado em {path} (esperava lista)", file=sys.stderr)
        return []
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=None, help="caminho do import_log.json")
    parser.add_argument("--dry-run", action="store_true", help="não escreve no DB")
    args = parser.parse_args()

    log_path = args.log or default_log_path()
    entries = load_entries(log_path)

    print(f"Log: {log_path}")
    print(f"Entradas: {len(entries)}")
    print(f"DB:  {db.db_path()}")

    if args.dry_run:
        print("(dry-run — nenhuma escrita)")
        return 0

    db.init()
    inserted = 0
    skipped = 0
    for entry in entries:
        if not entry.get("id"):
            skipped += 1
            continue
        try:
            repo.insert_import(entry)
            inserted += 1
        except (KeyError, TypeError, ValueError) as exc:
            print(f"! pulando {entry.get('id')}: {exc}", file=sys.stderr)
            skipped += 1

    print(f"OK — {inserted} inserido(s)/atualizado(s), {skipped} pulado(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
