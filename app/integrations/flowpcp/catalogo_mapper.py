from __future__ import annotations

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoComponente,
    CatalogoGrupoErp,
    CatalogoMarcaErp,
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def _item(
    d: ProdutoFireDTO,
    composicao: ComposicaoDoEnvio | None,
    codigos_mes: dict[str, str | None] | None,
    fiscal: dict[str, dict] | None = None,
    com_nota: set[str] | None = None,
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
    if fiscal is not None:
        extra = dict(fiscal.get(d.fire_produto_id, {}))
        if "grupoErp" in extra:
            extra["grupoErp"] = CatalogoGrupoErp(**extra["grupoErp"])
        campos.update(extra)
    if com_nota is not None:
        campos["temNotaFiscal"] = d.fire_produto_id in com_nota
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
    fiscal: dict[str, dict] | None = None,
    marcas: list[dict] | None = None,
    com_nota: set[str] | None = None,
) -> CatalogoRequest:
    """`composicao=None` (interruptor desligado): itens iguais aos de antes.
    `fiscal=None` idem: nenhum campo fiscal no corpo. `marcas=None`/`com_nota=None`
    idem: sem `marcasErp` nem `temNotaFiscal` (ausente = não mexer).
    Confirmações só entram no corpo quando pedidas."""
    extras = {}
    if confirmar_remocao:
        extras["confirmarRemocaoComposicao"] = True
    if confirmar_substituicao:
        extras["confirmarSubstituicaoComposicao"] = True
    if marcas is not None:
        extras["marcasErp"] = [CatalogoMarcaErp(**m) for m in marcas]
    return CatalogoRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=[_item(d, composicao, codigos_mes, fiscal, com_nota) for d in dtos],
        origem=CatalogoOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
        **extras,
    )
