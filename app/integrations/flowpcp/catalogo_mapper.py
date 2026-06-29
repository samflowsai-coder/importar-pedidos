from __future__ import annotations

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def build_catalogo_request(
    dtos: list[ProdutoFireDTO],
    *,
    dry_run: bool,
    full_sync: bool,
    importador_versao: str,
    extraido_em: str,
) -> CatalogoRequest:
    itens = [
        CatalogoProdutoItem(
            fireProdutoId=d.fire_produto_id,
            codigo=d.codigo,
            nome=d.nome,
            unidade=d.unidade,
            ean=d.ean,
            ativo=d.ativo,
            tipo=d.tipo,
        )
        for d in dtos
    ]
    return CatalogoRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=itens,
        origem=CatalogoOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
    )
