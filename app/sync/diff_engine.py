"""Compute SyncDelta between Firebird snapshot and local SQLite state.

Rules (products):
- Product not in state and active → upsert.
- Product in state with different hash and active → upsert.
- Product in state with same hash and active → skip.
- Product `inativo=True` → tombstone (always; idempotent on server side).
- Product in state but missing from snapshot → tombstone.

Rules (components):
- Component not in state → upsert.
- Component in state with different hash → upsert.
- Component in state with same hash → skip.
- Component in state but missing from snapshot → component_tombstone (server
  removes via "componentes do pai são autoritativos" rule).
"""
from __future__ import annotations

from app.sync.canonical import canonical_hash
from app.sync.models import (
    ComponentDeltaItem,
    ComponentRow,
    ProductDeltaItem,
    ProductRow,
    SyncDelta,
)


def build_product_payload(p: ProductRow) -> dict:
    """Canonical FlowPCP-shaped payload for a product."""
    return {
        "codigo": str(p.seq),
        "codigo_alternativo": p.codprod_altern,
        "nome": p.descricao,
        "unidade": p.unidade or "un",
        "ean": p.codigo_ean13,
        "tipo": "kit" if p.is_kit else "simples",
        "ativo": not p.inativo,
    }


def build_component_payload(c: ComponentRow) -> dict:
    """Canonical FlowPCP-shaped payload for a kit component."""
    return {
        "produto_pai_codigo": str(c.codproduto_pai),
        "produto_filho_codigo": str(c.codproduto),
        "quantidade": float(c.qtd),
        "posicao": 0,
    }


def compute_delta(
    *,
    product_snapshot: list[ProductRow],
    component_snapshot: list[ComponentRow],
    product_state: dict[int, str],
    component_state: dict[int, str],
) -> SyncDelta:
    delta = SyncDelta()

    seen_products: set[int] = set()
    for p in product_snapshot:
        seen_products.add(p.seq)
        if p.inativo:
            delta.tombstones.append(p.seq)
            continue
        payload = build_product_payload(p)
        new_hash = canonical_hash(payload)
        if product_state.get(p.seq) == new_hash:
            continue
        delta.products.append(ProductDeltaItem(seq=p.seq, payload=payload))

    # Disappeared from snapshot → tombstone
    for seq in product_state:
        if seq not in seen_products:
            delta.tombstones.append(seq)

    seen_components: set[int] = set()
    for c in component_snapshot:
        seen_components.add(c.codigo)
        payload = build_component_payload(c)
        new_hash = canonical_hash(payload)
        if component_state.get(c.codigo) == new_hash:
            continue
        delta.components.append(ComponentDeltaItem(codigo=c.codigo, payload=payload))

    for codigo in component_state:
        if codigo not in seen_components:
            delta.component_tombstones.append(codigo)

    return delta
