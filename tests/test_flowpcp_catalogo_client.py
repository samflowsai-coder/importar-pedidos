import pytest

from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeOutbound:
    def __init__(self, resp):
        self._resp = resp
        self.last_path = None
        self.last_json = None
        self.last_key = None

    def post_json(self, path, *, json, idempotency_key):
        self.last_path = path
        self.last_json = json
        self.last_key = idempotency_key
        return self._resp

    def close(self):
        pass


def _req():
    return CatalogoRequest(
        dryRun=True,
        fullSync=True,
        itens=[
            CatalogoProdutoItem(
                fireProdutoId="1", codigo="A", nome="X", unidade="PC", ean=None, ativo=True
            )
        ],
        origem=CatalogoOrigem(importadorVersao="1.0.0", extraidoEm="2026-06-29T00:00:00Z"),
    )


def test_send_catalogo_posta_no_path_certo_e_parseia_relatorio():
    out = _FakeOutbound(_Resp(200, {"match_limpo": 1, "fire_only": 3420, "fire_pk_presente": True}))
    client = FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)
    rep = client.send_catalogo(_req())
    assert out.last_path == "/api/portal-pedidos/catalogo"
    assert out.last_json["schema"] == "catalogo.produtos.v1"
    assert out.last_json["dryRun"] is True
    assert rep.match_limpo == 1 and rep.fire_only == 3420 and rep.fire_pk_presente is True


def test_send_catalogo_erro_http_vira_FlowPCPClientError():  # noqa: N802
    out = _FakeOutbound(_Resp(500, text="boom"))
    client = FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)
    with pytest.raises(FlowPCPClientError):
        client.send_catalogo(_req())
