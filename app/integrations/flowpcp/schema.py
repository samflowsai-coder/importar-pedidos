"""Wire format of POST /api/portal-pedidos/produtos/sync.

Mirrors the Zod schema in the FlowPCP server. `extra="ignore"` means the
client tolerates new fields the server adds in future versions.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FlowPCPProdutoItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    codigo: str
    codigo_alternativo: str | None = None
    nome: str | None = None
    unidade: str | None = None
    ean: str | None = None
    tipo: str | None = None  # 'simples' | 'kit' | 'pack' | 'composto'
    ativo: bool


class FlowPCPComponenteItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    produto_pai_codigo: str
    produto_filho_codigo: str
    quantidade: float
    posicao: int = 0


class FlowPCPSyncRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tenant_id: str
    sync_id: str
    generated_at: str
    delta_kind: str = "incremental"
    produtos: list[FlowPCPProdutoItem] = Field(default_factory=list)
    componentes: list[FlowPCPComponenteItem] = Field(default_factory=list)


class FlowPCPSyncErrorEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    codigo: str
    reason: str


class FlowPCPSyncResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sync_id: str
    applied: dict
    skipped: int = 0
    errors: list[FlowPCPSyncErrorEntry] = Field(default_factory=list)
