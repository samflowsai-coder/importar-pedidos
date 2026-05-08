from __future__ import annotations

from app.sync.canonical import canonical_hash
from app.sync.diff_engine import (
    build_component_payload,
    build_product_payload,
    compute_delta,
)
from app.sync.models import ComponentRow, ProductRow


def _p(seq, descr="X", inativo=False, is_kit=False, alt=None, ean=None, unid="un"):
    return ProductRow(
        seq=seq, codprod_altern=alt, descricao=descr, unidade=unid,
        codigo_ean13=ean, inativo=inativo, is_kit=is_kit,
    )


def _c(codigo, pai, filho, qtd=1.0):
    return ComponentRow(codigo=codigo, codproduto_pai=pai, codproduto=filho, qtd=qtd)


def test_build_product_payload_shape():
    p = _p(1, descr="Tenis", is_kit=True, alt="ABC", ean="789")
    payload = build_product_payload(p)
    assert payload == {
        "codigo": "1",
        "codigo_alternativo": "ABC",
        "nome": "Tenis",
        "unidade": "un",
        "ean": "789",
        "tipo": "kit",
        "ativo": True,
    }


def test_build_product_payload_simples_when_not_kit():
    p = _p(1, is_kit=False)
    assert build_product_payload(p)["tipo"] == "simples"


def test_build_product_payload_inativo_makes_ativo_false():
    p = _p(1, inativo=True)
    assert build_product_payload(p)["ativo"] is False


def test_build_component_payload_shape():
    c = _c(1, pai=10, filho=20, qtd=2.5)
    assert build_component_payload(c) == {
        "produto_pai_codigo": "10",
        "produto_filho_codigo": "20",
        "quantidade": 2.5,
        "posicao": 0,
    }


def test_empty_state_treats_all_as_inserts():
    snapshot_p = [_p(1, "A"), _p(2, "B")]
    delta = compute_delta(
        product_snapshot=snapshot_p,
        component_snapshot=[],
        product_state={},
        component_state={},
    )
    assert {x.seq for x in delta.products} == {1, 2}
    assert delta.tombstones == []
    assert delta.component_tombstones == []


def test_unchanged_hash_produces_no_delta():
    p = _p(1, "Same")
    h = canonical_hash(build_product_payload(p))
    delta = compute_delta(
        product_snapshot=[p],
        component_snapshot=[],
        product_state={1: h},
        component_state={},
    )
    assert delta.is_empty()


def test_changed_descricao_produces_update():
    old = _p(1, "Old")
    new = _p(1, "New")
    h_old = canonical_hash(build_product_payload(old))
    delta = compute_delta(
        product_snapshot=[new],
        component_snapshot=[],
        product_state={1: h_old},
        component_state={},
    )
    assert len(delta.products) == 1
    assert delta.products[0].seq == 1
    assert delta.products[0].payload["nome"] == "New"


def test_inativo_yields_tombstone_not_upsert():
    p = _p(1, "X", inativo=True)
    delta = compute_delta(
        product_snapshot=[p],
        component_snapshot=[],
        product_state={1: "anyhash"},
        component_state={},
    )
    assert delta.tombstones == [1]
    # Inativo should NOT also be in products as upsert
    assert all(x.seq != 1 for x in delta.products)


def test_disappeared_seq_yields_tombstone():
    delta = compute_delta(
        product_snapshot=[],  # SEQ 99 not present anymore
        component_snapshot=[],
        product_state={99: "h"},
        component_state={},
    )
    assert delta.tombstones == [99]


def test_components_added_and_removed():
    delta = compute_delta(
        product_snapshot=[],
        component_snapshot=[_c(2, 100, 200, 1.0)],
        product_state={},
        component_state={1: "old_hash"},
    )
    assert any(c.codigo == 2 for c in delta.components)
    assert delta.component_tombstones == [1]


def test_unchanged_component_no_delta():
    c = _c(1, 100, 200, 1.0)
    h = canonical_hash(build_component_payload(c))
    delta = compute_delta(
        product_snapshot=[],
        component_snapshot=[c],
        product_state={},
        component_state={1: h},
    )
    assert delta.is_empty()
