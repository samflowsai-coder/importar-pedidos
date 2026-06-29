from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request


def test_build_request_mapeia_itens_e_origem():
    dtos = [
        ProdutoFireDTO("3381", "3381", "KIT C/5", "PC", None, True, "kit"),
        ProdutoFireDTO("3170", "3170", "BRANCO COM BORDO", "PC", "789", False, "simples"),
    ]
    req = build_catalogo_request(
        dtos,
        dry_run=True,
        full_sync=True,
        importador_versao="1.0.0",
        extraido_em="2026-06-29T12:00:00Z",
    )
    body = req.model_dump(by_alias=True)
    assert body["schema"] == "catalogo.produtos.v1"
    assert body["dryRun"] is True and body["fullSync"] is True
    assert len(body["itens"]) == 2
    assert body["itens"][0]["fireProdutoId"] == "3381"
    assert body["itens"][0]["codigo"] == "3381"
    assert body["itens"][0]["tipo"] == "kit"
    assert body["itens"][1]["ativo"] is False
    assert body["origem"]["importadorVersao"] == "1.0.0"
    assert body["origem"]["extraidoEm"] == "2026-06-29T12:00:00Z"
