from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ClienteItem(BaseModel):
    """Item de identidade do cliente (Fire é dono). camelCase no wire."""

    model_config = ConfigDict(populate_by_name=True)

    fireClienteId: str  # noqa: N815 — CADASTRO.CODIGO (PK durável)
    cnpj: str  # dígitos-only — chave de match
    nome: str  # RAZAO_SOCIAL
    grupoCodigo: str | None = None  # noqa: N815 — CODGRUPO (marca)
    ativo: bool = True


class ClientesOrigem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    importadorVersao: str  # noqa: N815
    extraidoEm: str  # noqa: N815 — ISO8601


class ClientesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="cadastro.clientes.v1", alias="schema")
    dryRun: bool  # noqa: N815
    fullSync: bool  # noqa: N815
    itens: list[ClienteItem]
    origem: ClientesOrigem


class ClientesReconciliacaoResponse(BaseModel):
    """Relatório devolvido pelo Flow. O Flow é dono do contrato de resposta;
    `extra="allow"` tolera campos novos (contagens/amostras aninhados, camelCase)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dry_run: bool | None = Field(default=None, alias="dryRun")
    full_sync: bool | None = Field(default=None, alias="fullSync")
