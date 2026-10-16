import pytest

from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)
from app.integrations.flowpcp.client import (
    APPLY_TIMEOUT_SECONDS,
    FlowPCPApplyEmAndamentoError,
    FlowPCPClient,
    FlowPCPClientError,
    _idem_catalogo,
)


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
        self.last_kw = None

    def post_json(self, path, *, json, idempotency_key, **kw):
        self.last_path = path
        self.last_json = json
        self.last_key = idempotency_key
        self.last_kw = kw
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
    out = _FakeOutbound(
        _Resp(
            200,
            {
                "firePkPresente": "todos",
                "contagens": {"matchLimpo": 1, "fireOnly": 3420},
            },
        )
    )
    client = FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)
    rep = client.send_catalogo(_req())
    assert out.last_path == "/api/portal-pedidos/catalogo"
    assert out.last_json["schema"] == "catalogo.produtos.v1"
    assert out.last_json["dryRun"] is True
    assert rep.contagens.match_limpo == 1
    assert rep.contagens.fire_only == 3420
    assert rep.fire_pk_presente == "todos"


def test_send_catalogo_erro_http_vira_FlowPCPClientError():  # noqa: N802
    out = _FakeOutbound(_Resp(500, text="boom"))
    client = FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)
    with pytest.raises(FlowPCPClientError):
        client.send_catalogo(_req())


def _req_apply(nome="X"):
    r = _req()
    return r.model_copy(update={"dryRun": False, "itens": [r.itens[0].model_copy(update={"nome": nome})]})


def _cliente(resp):
    out = _FakeOutbound(resp)
    return out, FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)


def test_apply_vai_com_politica_e_timeout_proprios():
    out, client = _cliente(_Resp(200, {"contagens": {}}))
    client.send_catalogo(_req_apply())
    assert out.last_kw["timeout"] == APPLY_TIMEOUT_SECONDS
    assert out.last_kw["retry_policy"].max_attempts >= 1
    assert out.last_kw["retry_policy"].retry_on_status == frozenset()


def test_dry_run_segue_a_politica_de_hoje():
    out, client = _cliente(_Resp(200, {"contagens": {}}))
    client.send_catalogo(_req())
    assert out.last_kw == {}


def test_409_vira_apply_em_andamento():
    _, client = _cliente(_Resp(409, text="execução em andamento"))
    with pytest.raises(FlowPCPApplyEmAndamentoError):
        client.send_catalogo(_req_apply())


def test_idempotencia_muda_quando_o_conteudo_muda_com_mesma_contagem():
    a, b = _req_apply("X"), _req_apply("X NOVO")
    assert _idem_catalogo(a) != _idem_catalogo(b)
    assert _idem_catalogo(a) == _idem_catalogo(_req_apply("X"))  # estável em retry
