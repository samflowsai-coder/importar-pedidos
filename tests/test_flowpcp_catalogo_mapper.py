from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio
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


def _dtos():
    return [
        ProdutoFireDTO(
            "2544", "2544", "KIT 6", "KIT", None, True, "kit", codigo_alternativo="986388"
        ),
        ProdutoFireDTO(
            "3306", "3306", "PAR PTO", "PARES", None, True, "simples", codigo_alternativo="8366"
        ),
        ProdutoFireDTO("3381", "3381", "KIT C/5", "KIT", None, True, "kit"),
    ]


def _req(composicao, codigos=None, **kw):
    return build_catalogo_request(
        _dtos(),
        dry_run=False,
        full_sync=True,
        importador_versao="1",
        extraido_em="t",
        composicao=composicao,
        codigos_mes=codigos,
        **kw,
    ).model_dump(by_alias=True)


def test_mapper_sem_composicao_nao_manda_campos_novos():
    corpo = _req(None)
    assert "confirmarRemocaoComposicao" not in corpo
    for item in corpo["itens"]:
        assert "componentes" not in item and "codigoMes" not in item


def test_mapper_com_composicao():
    comp = ComposicaoDoEnvio(por_seq={"2544": [("3306", 6.0)], "3306": []}, sem_linha=("3381",))
    kit, par, c5 = _req(comp, {"2544": None, "3306": "8366", "3381": None})["itens"]
    assert kit["componentes"] == [{"erpProdutoId": "3306", "quantidade": 6.0}]
    assert kit["codigoMes"] is None
    assert par["componentes"] == [] and par["codigoMes"] == "8366"
    assert "componentes" not in c5 and c5["codigoMes"] is None


def test_mapper_leitura_falhou_so_codigo_mes():
    itens = _req(ComposicaoDoEnvio(leitura_ok=False), {"2544": None, "3306": "8366", "3381": None})[
        "itens"
    ]
    assert all("componentes" not in i for i in itens)
    assert itens[1]["codigoMes"] == "8366"


def test_mapper_confirmacoes_so_quando_pedidas():
    corpo = _req(None, confirmar_remocao=True)
    assert corpo["confirmarRemocaoComposicao"] is True
    assert "confirmarSubstituicaoComposicao" not in corpo
