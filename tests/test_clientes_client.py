from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.clientes_schema import ClienteItem, ClientesOrigem, ClientesRequest


class _FakeResp:
    is_success = True
    status_code = 200

    def json(self):
        return {"dryRun": True, "contagens": {"fireTotal": 1}}


class _FakeOutbound:
    def __init__(self):
        self.calls = []

    def post_json(self, path, *, json, idempotency_key):
        self.calls.append((path, json, idempotency_key))
        return _FakeResp()

    def close(self):
        pass


def _req(itens):
    return ClientesRequest(
        dryRun=True,
        fullSync=False,
        itens=itens,
        origem=ClientesOrigem(importadorVersao="1.0.0", extraidoEm="2026-07-17T12:00:00Z"),
    )


def _item(cnpj, nome, grupo="12"):
    return ClienteItem(fireClienteId="1", cnpj=cnpj, nome=nome, grupoCodigo=grupo)


def _client(outbound):
    return FlowPCPClient(base_url="http://x", service_token="t", tenant_id="t1", outbound=outbound)


def test_send_clientes_posts_to_path_and_parses():
    ob = _FakeOutbound()
    resp = _client(ob).send_clientes(_req([_item("06347409029651", "SBF")]))
    assert ob.calls[0][0] == "/api/portal-pedidos/clientes"
    assert resp.dry_run is True


def test_idempotency_key_changes_with_content_not_just_count():
    ob = _FakeOutbound()
    c = _client(ob)
    c.send_clientes(_req([_item("06347409029651", "SBF")]))
    c.send_clientes(
        _req([_item("06347409029651", "SBF CORRIGIDO")])
    )  # mesma contagem, nome diferente
    key1, key2 = ob.calls[0][2], ob.calls[1][2]
    assert key1 != key2


def test_idempotency_key_stable_for_same_content():
    ob = _FakeOutbound()
    c = _client(ob)
    c.send_clientes(_req([_item("06347409029651", "SBF")]))
    c.send_clientes(_req([_item("06347409029651", "SBF")]))
    assert ob.calls[0][2] == ob.calls[1][2]
