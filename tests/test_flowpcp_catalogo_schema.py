from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoReconciliacaoResponse,
    CatalogoRequest,
)


def test_item_serializa_camelcase_e_aceita_snake_na_entrada():
    item = CatalogoProdutoItem(
        fireProdutoId="3566",
        codigo="5035G",
        nome="KALLAN 39/44 SP LISA MESCLA",
        unidade="PC",
        ean=None,
        ativo=True,
    )
    dumped = item.model_dump(by_alias=True)
    assert dumped["fireProdutoId"] == "3566"
    assert dumped["tipo"] is None  # Fire não tem tipo
    assert dumped["ativo"] is True


def test_request_usa_schema_alias_e_default_v1():
    req = CatalogoRequest(
        dryRun=True,
        fullSync=True,
        itens=[
            CatalogoProdutoItem(
                fireProdutoId="1", codigo=None, nome="X", unidade=None, ean=None, ativo=True
            )
        ],
        origem=CatalogoOrigem(importadorVersao="1.0.0", extraidoEm="2026-06-29T00:00:00Z"),
    )
    body = req.model_dump(by_alias=True)
    assert body["schema"] == "catalogo.produtos.v1"
    assert body["dryRun"] is True and body["fullSync"] is True
    assert body["itens"][0]["nome"] == "X"


def test_response_tolera_campos_extra_do_flow():
    resp = CatalogoReconciliacaoResponse.model_validate(
        {"match_limpo": 10, "fire_only": 3400, "campo_novo_do_flow": "ok"}
    )
    assert resp.match_limpo == 10
    assert resp.fire_only == 3400
    assert resp.criados == 0  # default
