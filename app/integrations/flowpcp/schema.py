from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DecisaoFlowPCP(BaseModel):
    id: str
    pedido_erp: str
    cliente_cnpj: str | None = None
    nome_cliente: str | None = None
    prazo_entrega_original: str
    prazo_pactuado: str | None = None
    status: str  # "em_pool" | "rejeitado"
    motivo_decisao: str | None = None
    atualizado_em: str


class DecisoesResponse(BaseModel):
    decisoes: list[DecisaoFlowPCP]
    proximo_cursor: str | None = None


class AcaoReconciliacao(str, Enum):
    DATA_ATUALIZADA = "data_atualizada"
    CANCELAMENTO_PENDENTE_MANUAL = "cancelamento_pendente_manual"
    SEM_ACAO_NECESSARIA = "sem_acao_necessaria"
    PEDIDO_NAO_ENCONTRADO_NO_FIRE = "pedido_nao_encontrado_no_fire"


class ConfirmarReconciliacaoRequest(BaseModel):
    acao: AcaoReconciliacao
    fire_id_externo: str | None = None
    observacoes: str | None = Field(default=None, max_length=1000)


# ── Push de pedido novo (contrato F.5 /recebimento) ───────────────────────────
class ClienteRecebimento(BaseModel):
    nome: str
    cnpj: str | None = None


class ItemRecebimento(BaseModel):
    produtoCodigo: str | None = None  # noqa: N815 — wire é camelCase
    produtoEan: str | None = None  # noqa: N815
    descricao: str
    quantidade: float
    precoUnitario: float | None = None  # noqa: N815


class OrigemRecebimento(BaseModel):
    importadorVersao: str  # noqa: N815
    arquivoOriginal: str  # noqa: N815
    parserUsado: str  # noqa: N815
    confiancaParser: str  # noqa: N815 — "alta" | "media" | "baixa"


class RecebimentoRequest(BaseModel):
    schema_: str = Field(default="pedido.recebimento.v1", alias="schema")
    externalId: str  # noqa: N815
    fornecedor: str
    pedidoNumero: str  # noqa: N815
    emitidoEm: str  # noqa: N815
    prazoSolicitado: str | None = None  # noqa: N815
    cliente: ClienteRecebimento
    itens: list[ItemRecebimento]
    origem: OrigemRecebimento

    model_config = {"populate_by_name": True}
