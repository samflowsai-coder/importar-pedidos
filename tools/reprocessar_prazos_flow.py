#!/usr/bin/env python3
"""Gera o patch de prazo_entrega pros pedidos que já foram enviados ao Flow com
`prazoSolicitado` null — o bug de canonicalização de delivery_date (corrigido em
`OrderNormalizer`). O `/recebimento` do Flow é insert-only (ignora re-envio), então
não dá pra consertar re-enviando: este script produz o mapa
`source_id_externo → prazo_entrega (ISO)` pro time do Flow rodar um UPDATE.

READ-ONLY: só faz SELECT no `app_state_<slug>.db` do cliente e escreve os arquivos
de saída. NUNCA toca o Flow nem o banco.

Como funciona: pra cada import com snapshot, deserializa o Order salvo, re-normaliza
(o fix corrige delivery_date DD.MM.YYYY → DD/MM/YYYY), recomputa o payload de
recebimento e pega o `prazoSolicitado` — o mesmo valor que um envio corrigido produziria.

Uso:
  .venv/bin/python tools/reprocessar_prazos_flow.py --db /caminho/app_state_mm.db --tenant <uuid>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order
from app.normalizers.order_normalizer import OrderNormalizer

# Tenant do ambiente MM no Flow (mesmo default de tools/configurar_flowpcp.py).
_TENANT_MM_PROD = "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"
_normalizer = OrderNormalizer()


@dataclass
class ReprocResult:
    patch_rows: list[dict] = field(default_factory=list)
    total: int = 0
    fixaveis: int = 0
    sem_prazo: int = 0
    sem_snapshot: int = 0
    erros: int = 0
    amostra: list[dict] = field(default_factory=list)


def gerar_patch(imports: list[dict], *, tenant_id: str) -> ReprocResult:
    """Lógica pura: recebe os imports (dicts com id/order_number/customer_cnpj/
    snapshot_json) e devolve as linhas de patch + estatísticas."""
    res = ReprocResult()
    for imp in imports:
        res.total += 1
        snap = imp.get("snapshot_json")
        if not snap:
            res.sem_snapshot += 1
            continue
        try:
            order = Order.model_validate(json.loads(snap))
        except Exception:  # noqa: BLE001 — snapshot corrompido não derruba o lote
            res.erros += 1
            continue
        if not order.items:
            res.sem_prazo += 1
            continue
        order = _normalizer.normalize(order)
        req = build_recebimento_payload(import_id=imp["id"], order=order, tenant_id=tenant_id)
        if not req.prazoSolicitado:
            res.sem_prazo += 1
            continue
        res.fixaveis += 1
        # O Flow grava o prazoSolicitado (order-level) em TODOS os itens do pedido;
        # o patch espelha isso — uma linha por item (source_id_externo = id:idx).
        for idx in range(len(order.items)):
            res.patch_rows.append(
                {
                    "source_id_externo": f"{imp['id']}:{idx}",
                    "prazo_entrega": req.prazoSolicitado,
                    "order_number": imp.get("order_number"),
                    "cnpj": imp.get("customer_cnpj"),
                }
            )
        if len(res.amostra) < 15:
            res.amostra.append(
                {
                    "order_number": imp.get("order_number"),
                    "emitidoEm": req.emitidoEm,
                    "prazo_novo": req.prazoSolicitado,
                    "itens": len(order.items),
                }
            )
    return res


def _ler_imports(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, order_number, customer_cnpj, snapshot_json "
            "FROM imports WHERE snapshot_json IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _sql_str(v: str) -> str:
    return v.replace("'", "''")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gera patch de prazo_entrega dos pedidos já no Flow.")
    ap.add_argument("--db", required=True, help="caminho do app_state_<slug>.db do cliente")
    ap.add_argument("--tenant", default=_TENANT_MM_PROD, help="tenant_id do ambiente no Flow")
    ap.add_argument("--out", default="reprocessar_prazos_patch", help="prefixo dos arquivos de saída")
    args = ap.parse_args(argv)

    res = gerar_patch(_ler_imports(args.db), tenant_id=args.tenant)

    json_path, sql_path = f"{args.out}.json", f"{args.out}.sql"
    Path(json_path).write_text(
        json.dumps(res.patch_rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    linhas = [
        f"UPDATE pedido_items SET prazo_entrega = '{_sql_str(r['prazo_entrega'])}' "
        f"WHERE tenant_id = '{_sql_str(args.tenant)}' "
        f"AND source_id_externo = '{_sql_str(r['source_id_externo'])}';"
        for r in res.patch_rows
    ]
    Path(sql_path).write_text("\n".join(linhas) + ("\n" if linhas else ""), encoding="utf-8")

    print(f"imports com snapshot:        {res.total}")
    print(f"  fixáveis (prazo real):     {res.fixaveis}  →  {len(res.patch_rows)} linhas (por item)")
    print(f"  sem prazo no documento:    {res.sem_prazo}")
    print(f"  sem snapshot:              {res.sem_snapshot}")
    print(f"  snapshot corrompido:       {res.erros}")
    print(f"\nSaída: {json_path} (canônico) + {sql_path} (o Flow revisa e roda)")
    if res.amostra:
        print("\nAmostra (order_number: emitidoEm → prazo novo · itens):")
        for a in res.amostra:
            print(f"  {a['order_number']}: {a['emitidoEm']} → {a['prazo_novo']}  ({a['itens']} itens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
