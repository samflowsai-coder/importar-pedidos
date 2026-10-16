from app.integrations.flowpcp.catalogo_schema import (
    CatalogoComponente,
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


def _item(**extra):
    return CatalogoProdutoItem(
        fireProdutoId="2544", codigo="2544", nome="KIT 6", ativo=True, tipo="kit", **extra
    )


def _request(**extra):
    return CatalogoRequest(
        dryRun=False,
        fullSync=True,
        itens=[_item()],
        origem=CatalogoOrigem(importadorVersao="1", extraidoEm="t"),
        **extra,
    )


def test_wire_sem_campos_novos_e_igual_ao_de_hoje():
    corpo = _item().model_dump(by_alias=True)
    assert "componentes" not in corpo and "codigoMes" not in corpo


def test_wire_lista_vazia_e_null_aparecem():
    corpo = _item(componentes=[], codigoMes=None).model_dump(by_alias=True)
    assert corpo["componentes"] == [] and corpo["codigoMes"] is None


def test_wire_componentes_em_camel_case():
    corpo = _item(componentes=[CatalogoComponente(erpProdutoId="3306", quantidade=2.0)]).model_dump(
        by_alias=True
    )
    assert corpo["componentes"] == [{"erpProdutoId": "3306", "quantidade": 2.0}]


def test_request_sem_confirmacoes_nao_tem_as_chaves():
    corpo = _request().model_dump(by_alias=True)
    assert "confirmarRemocaoComposicao" not in corpo
    assert "confirmarSubstituicaoComposicao" not in corpo
    assert corpo["itens"][0].keys() == _item().model_dump(by_alias=True).keys()


def test_request_com_confirmacao_no_topo_do_corpo():
    corpo = _request(confirmarSubstituicaoComposicao=True).model_dump(by_alias=True)
    assert corpo["confirmarSubstituicaoComposicao"] is True
    assert "confirmarRemocaoComposicao" not in corpo
