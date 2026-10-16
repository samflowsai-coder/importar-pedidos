#!/usr/bin/env python3
"""Carga de material Fire->FlowPCP: extrai os materiais (fora do grupo 8) do Fire,
grava a cópia local e, com PORTAL_FLOWPCP_MATERIAIS ligado, empurra ao Flow em
DRY-RUN, imprimindo o relatório em JSON.

Rodar do root do projeto:
  .venv/bin/python tools/sync_materiais_fire.py mm            # dry-run
  .venv/bin/python tools/sync_materiais_fire.py mm --apply    # grava (exige materiais.v1)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description="Carga de material Fire->FlowPCP")
    parser.add_argument("slug")
    parser.add_argument("--apply", action="store_true", help="Manda dryRun=false (GRAVA no Flow).")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    load_dotenv(base / ".env")
    os.environ.setdefault("APP_DATA_DIR", str(base / "data"))

    from app.integrations.flowpcp.materiais_sync import MateriaisLocalResult, run_materiais_sync

    rep = run_materiais_sync(args.slug, dry_run=not args.apply, full_sync=True)
    if rep is None:
        print("ambiente sem FlowPCP habilitado.")
        return 2
    if isinstance(rep, MateriaisLocalResult):
        print(f"cópia local atualizada: {rep.itens} materiais (extraído em {rep.extraido_em}).")
        print("Envio ao Flow DESLIGADO: defina PORTAL_FLOWPCP_MATERIAIS=<slug> para enviar.")
        return 0
    print(json.dumps(rep.model_dump(by_alias=True), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
