from __future__ import annotations

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoComponente,
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def _item(
    d: ProdutoFireDTO,
    composicao: ComposicaoDoEnvio | None,
    codigos_mes: dict[str, str | None] | None,
) -> CatalogoProdutoItem:
    campos = dict(
        fireProdutoId=d.fire_produto_id,
        codigo=d.codigo,
        nome=d.nome,
        unidade=d.unidade,
        ean=d.ean,
        ativo=d.ativo,
        tipo=d.tipo,
    )
    if composicao is not None:
        campos["codigoMes"] = (codigos_mes or {}).get(d.fire_produto_id)
        if d.fire_produto_id in composicao.por_seq:
            campos["componentes"] = [
                CatalogoComponente(erpProdutoId=f, quantidade=q)
                for f, q in composicao.por_seq[d.fire_produto_id]
            ]
    return CatalogoProdutoItem(**campos)


def build_catalogo_request(
    dtos: list[ProdutoFireDTO],
    *,
    dry_run: bool,
    full_sync: bool,
    importador_versao: str,
    extraido_em: str,
    composicao: ComposicaoDoEnvio | None = None,
    codigos_mes: dict[str, str | None] | None = None,
    confirmar_remocao: bool = False,
    confirmar_substituicao: bool = False,
) -> CatalogoRequest:
    """`composicao=None` (interruptor desligado): itens iguais aos de antes.
    Confirmações só entram no corpo quando pedidas."""
    extras = {}
    if confirmar_remocao:
        extras["confirmarRemocaoComposicao"] = True
    if confirmar_substituicao:
        extras["confirmarSubstituicaoComposicao"] = True
    return CatalogoRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=[_item(d, composicao, codigos_mes) for d in dtos],
        origem=CatalogoOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
        **extras,
    )
