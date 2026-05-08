from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.sync.models import (
    ComponentRow,
    ProductRow,
    RunResult,
    RunStatus,
    SyncDelta,
    Trigger,
)


def test_product_row_required_fields():
    p = ProductRow(
        seq=10042,
        codprod_altern="CAL-0042-PR",
        descricao="TENIS XYZ",
        unidade="un",
        codigo_ean13="7891234567890",
        inativo=False,
        is_kit=True,
    )
    assert p.seq == 10042
    assert p.is_kit is True


def test_product_row_rejects_blank_descricao():
    with pytest.raises(ValidationError):
        ProductRow(
            seq=1,
            codprod_altern=None,
            descricao="",
            unidade="un",
            codigo_ean13=None,
            inativo=False,
            is_kit=False,
        )


def test_component_row_rejects_zero_qtd():
    with pytest.raises(ValidationError):
        ComponentRow(codigo=1, codproduto_pai=10, codproduto=20, qtd=0.0)


def test_sync_delta_default_empty():
    d = SyncDelta()
    assert d.products == []
    assert d.components == []
    assert d.tombstones == []
    assert d.component_tombstones == []
    assert d.is_empty()


def test_run_status_enum():
    assert RunStatus.RUNNING.value == "running"
    assert RunStatus.APPLIED.value == "applied"
    assert RunStatus.PARTIAL.value == "partial"
    assert RunStatus.FAILED.value == "failed"


def test_trigger_enum():
    assert {"scheduler", "manual", "reconcile"} == {t.value for t in Trigger}


def test_run_result_carries_counters():
    r = RunResult(
        sync_id="01HX",
        status=RunStatus.APPLIED,
        delta_count_produtos=3,
        delta_count_componentes=1,
        delta_count_tombstones=0,
        applied_count=4,
        errors=[],
    )
    assert r.applied_count == 4
