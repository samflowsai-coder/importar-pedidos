from app.erp.cliente_extract import ClienteFireDTO
from app.integrations.flowpcp.clientes_mapper import build_clientes_request


def _dto() -> ClienteFireDTO:
    return ClienteFireDTO(
        fire_cliente_id="498",
        cnpj="06347409029651",
        nome="SBF S.A",
        grupo_codigo="12",
        ativo=True,
    )


def test_build_request_maps_fields_and_aliases():
    req = build_clientes_request(
        [_dto()],
        dry_run=True,
        full_sync=False,
        importador_versao="1.0.0",
        extraido_em="2026-07-17T12:00:00Z",
    )
    body = req.model_dump(by_alias=True)
    assert body["schema"] == "cadastro.clientes.v1"
    assert body["dryRun"] is True
    assert body["fullSync"] is False
    item = body["itens"][0]
    assert item["fireClienteId"] == "498"
    assert item["cnpj"] == "06347409029651"
    assert item["nome"] == "SBF S.A"
    assert item["grupoCodigo"] == "12"
    assert item["ativo"] is True
    assert body["origem"]["importadorVersao"] == "1.0.0"
