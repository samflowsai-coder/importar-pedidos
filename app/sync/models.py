"""Pydantic models for the product sync engine.

ProductRow / ComponentRow: snapshot rows read from Firebird.
SyncDelta: result of comparing snapshot vs local state.
RunResult: outcome of a single sync run.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class RunStatus(StrEnum):
    RUNNING = "running"
    APPLIED = "applied"
    PARTIAL = "partial"
    FAILED = "failed"


class Trigger(StrEnum):
    SCHEDULER = "scheduler"
    MANUAL = "manual"
    RECONCILE = "reconcile"


class ProductRow(BaseModel):
    seq: int
    codprod_altern: str | None = None
    descricao: str
    unidade: str = "un"
    codigo_ean13: str | None = None
    inativo: bool
    is_kit: bool

    @field_validator("descricao")
    @classmethod
    def _descr_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("descricao required")
        return v.strip()


class ComponentRow(BaseModel):
    codigo: int  # PRODUTOS_KIT.CODIGO (PK)
    codproduto_pai: int
    codproduto: int
    qtd: float = Field(gt=0)


class ProductDeltaItem(BaseModel):
    """An upsert (full payload) for a product. Tombstones are tracked separately
    in `SyncDelta.tombstones` to keep the type single-purpose."""

    seq: int
    is_tombstone: bool = False
    payload: dict | None = None  # canonical dict; None if tombstone


class ComponentDeltaItem(BaseModel):
    codigo: int
    payload: dict  # canonical dict


class SyncDelta(BaseModel):
    products: list[ProductDeltaItem] = Field(default_factory=list)
    components: list[ComponentDeltaItem] = Field(default_factory=list)
    tombstones: list[int] = Field(default_factory=list)  # product SEQs to mark inactive
    # component CODIGOs that disappeared
    component_tombstones: list[int] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.products or self.components or self.tombstones or self.component_tombstones
        )


class SyncError(BaseModel):
    codigo: str
    reason: str


class RunResult(BaseModel):
    sync_id: str
    status: RunStatus
    delta_count_produtos: int = 0
    delta_count_componentes: int = 0
    delta_count_tombstones: int = 0
    applied_count: int = 0
    errors: list[SyncError] = Field(default_factory=list)
    trace_id: str | None = None
