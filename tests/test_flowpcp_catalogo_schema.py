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
    assert dumped["tipo"] is None  # tipo default None quando o item é construído sem ele
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


def test_response_parseia_shape_real_do_flow():
    resp = CatalogoReconciliacaoResponse.model_validate(
        {
            "dryRun": True,
            "fullSync": True,
            "firePkPresente": "todos",
            "contagens": {
                "flowTotal": 827,
                "fireTotal": 3421,
                "matchLimpo": 261,
                "ambiguo": 0,
                "flowOnly": 566,
                "fireOnly": 3160,
            },
            "amostras": {"ambiguo": [], "flowOnly": ["10791"], "fireOnly": ["1", "10"]},
            "campo_novo_do_flow": "ok",  # extra="allow" tolera
        }
    )
    assert resp.dry_run is True
    assert resp.fire_pk_presente == "todos"
    assert resp.contagens.flow_total == 827
    assert resp.contagens.match_limpo == 261
    assert resp.contagens.fire_only == 3160
    assert resp.amostras.fire_only == ["1", "10"]


def test_response_defaults_quando_vazio():
    resp = CatalogoReconciliacaoResponse.model_validate({})
    assert resp.contagens.match_limpo == 0
    assert resp.amostras.fire_only == []
    assert resp.fire_pk_presente is None
