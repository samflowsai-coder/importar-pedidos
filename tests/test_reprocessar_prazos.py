"""Ferramenta tools/reprocessar_prazos_flow.py — gera o patch de prazo_entrega
pros pedidos já no Flow (enviados com prazoSolicitado null antes do fix).

Testa a lógica pura (gerar_patch) com snapshots sintéticos, incluindo o caso da
Centauro (delivery_date em pontos), sem precisar do db do cliente.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from app.models.order import Order, OrderHeader, OrderItem

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "reprocessar_prazos_flow.py"
_spec = importlib.util.spec_from_file_location("reprocessar_prazos_flow", _TOOL)
mod = importlib.util.module_from_spec(_spec)
# @dataclass resolve tipos via sys.modules[cls.__module__] — registrar antes do exec.
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

TENANT = "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"


def _snapshot(delivery_date: str | None, n_items: int = 1) -> str:
    order = Order(
        header=OrderHeader(
            order_number="AW1", issue_date="25/11/2025",
            customer_name="SBF", customer_cnpj="06347409029651",
        ),
        items=[
            OrderItem(description=f"IT{i}", quantity=1.0, delivery_date=delivery_date)
            for i in range(n_items)
        ],
        source_file="x.pdf",
    )
    return json.dumps(order.model_dump(), ensure_ascii=False)


def _imp(id_, snapshot_json, order_number="AW1", cnpj="06347409029651"):
    return {"id": id_, "order_number": order_number, "customer_cnpj": cnpj, "snapshot_json": snapshot_json}


def test_gera_patch_com_prazo_corrigido_por_item():
    # Centauro: delivery_date em pontos → o fix normaliza → prazo ISO real, por item.
    res = mod.gerar_patch([_imp("imp1", _snapshot("01.03.2026", n_items=2))], tenant_id=TENANT)
    assert res.fixaveis == 1
    assert {r["source_id_externo"] for r in res.patch_rows} == {"imp1:0", "imp1:1"}
    assert all(r["prazo_entrega"] == "2026-03-01T00:00:00.000Z" for r in res.patch_rows)


def test_pula_import_sem_delivery_date():
    res = mod.gerar_patch([_imp("imp2", _snapshot(None))], tenant_id=TENANT)
    assert res.fixaveis == 0
    assert res.sem_prazo == 1
    assert res.patch_rows == []


def test_pula_import_sem_snapshot():
    res = mod.gerar_patch([_imp("imp3", None)], tenant_id=TENANT)
    assert res.sem_snapshot == 1
    assert res.patch_rows == []


def test_snapshot_corrompido_conta_erro_nao_derruba():
    res = mod.gerar_patch([_imp("imp4", "{lixo não-json")], tenant_id=TENANT)
    assert res.erros == 1
    assert res.patch_rows == []
