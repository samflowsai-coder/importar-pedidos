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

    from app.integrations.flowpcp.catalogo_sync import CatalogoLocalResult, run_catalogo_sync

    dry_run = not args.apply
    print(f"== Catálogo Fire->FlowPCP | slug={args.slug} | dry_run={dry_run} ==")
    rep = run_catalogo_sync(args.slug, dry_run=dry_run, full_sync=True)
    if rep is None:
        print("✗ ambiente sem FlowPCP habilitado.")
        return 2
    if isinstance(rep, CatalogoLocalResult):
        print(f"✓ cópia local atualizada: {rep.itens} produtos (extraído em {rep.extraido_em}).")
        print("  Envio ao Flow DESLIGADO — ligue 'Enviar catálogo ao Flow' no ambiente pra enviar.")
        return 0

    print("\n== RELATÓRIO DE RECONCILIAÇÃO ==")
    print(f"  dry_run          {rep.dry_run}")
    print(f"  full_sync        {rep.full_sync}")
    print(f"  fire_pk_presente {rep.fire_pk_presente}")
    print("  contagens:")
    c = rep.contagens
    for campo in ("fire_total", "flow_total", "match_limpo", "ambiguo", "fire_only", "flow_only"):
        print(f"    {campo:<12} {getattr(c, campo)}")
    a = rep.amostras
    print(
        f"  amostras (qtd): ambiguo={len(a.ambiguo)} "
        f"fire_only={len(a.fire_only)} flow_only={len(a.flow_only)}"
    )
    extras = rep.model_dump(
        exclude={"dry_run", "full_sync", "fire_pk_presente", "contagens", "amostras"}
    )
    if extras:
        print("\n  extras do Flow:", extras)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
