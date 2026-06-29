#!/usr/bin/env python3
"""Fase 0 do sync de catálogo Fire->FlowPCP: extrai PRODUTOS do Fire e empurra
em DRY-RUN, imprimindo o relatório de reconciliação que o Flow devolve.

Pré-requisitos: engine Firebird configurada (FB_CLIENT_LIBRARY no .env) + o
endpoint POST /api/portal-pedidos/catalogo no pcp-app no ar.

Rodar do root do projeto:
  .venv/bin/python tools/sync_catalogo_fire.py            # dry-run (Fase 0)
  .venv/bin/python tools/sync_catalogo_fire.py --slug mm
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync catálogo Fire->FlowPCP (Fase 0 dry-run)")
    parser.add_argument("--slug", default="mm")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Manda dryRun=false (PROMOVE no Flow). NÃO usar na Fase 0.",
    )
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    load_dotenv(base / ".env")
    os.environ.setdefault("APP_DATA_DIR", str(base / "data"))

    from app.integrations.flowpcp.catalogo_sync import run_catalogo_sync

    dry_run = not args.apply
    print(f"== Catálogo Fire->FlowPCP | slug={args.slug} | dry_run={dry_run} ==")
    rep = run_catalogo_sync(args.slug, dry_run=dry_run, full_sync=True)
    if rep is None:
        print("✗ ambiente sem FlowPCP habilitado.")
        return 2

    print("\n== RELATÓRIO DE RECONCILIAÇÃO ==")
    for campo in (
        "match_limpo",
        "ambiguo",
        "flow_only",
        "fire_only",
        "criados",
        "atualizados",
        "inalterados",
        "desativados",
        "erros",
        "fire_pk_presente",
    ):
        print(f"  {campo:<18} {getattr(rep, campo, None)}")
    extras = rep.model_dump(
        exclude={
            "match_limpo",
            "ambiguo",
            "flow_only",
            "fire_only",
            "criados",
            "atualizados",
            "inalterados",
            "desativados",
            "erros",
            "fire_pk_presente",
        }
    )
    if extras:
        print("\n  extras do Flow:", extras)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
