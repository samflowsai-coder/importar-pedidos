from __future__ import annotations

from app.erp.cliente_extract import ClienteFireDTO
from app.integrations.flowpcp.clientes_schema import (
    ClienteItem,
    ClientesOrigem,
    ClientesRequest,
)


def build_clientes_request(
    dtos: list[ClienteFireDTO],
    *,
    dry_run: bool,
    full_sync: bool,
    importador_versao: str,
    extraido_em: str,
) -> ClientesRequest:
    itens = [
        ClienteItem(
            fireClienteId=d.fire_cliente_id,
            cnpj=d.cnpj,
            nome=d.nome,
            grupoCodigo=d.grupo_codigo,
            ativo=d.ativo,
        )
        for d in dtos
    ]
    return ClientesRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=itens,
        origem=ClientesOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
    )
