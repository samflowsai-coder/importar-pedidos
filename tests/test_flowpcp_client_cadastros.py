"""Cliente do Flow para a fila de cadastros: parâmetros do GET e POST do resultado."""
import httpx
import pytest

from app.http.client import OutboundClient
from app.http.policies import RetryPolicy, idempotent_post_policy
from app.integrations.flowpcp import client as flow_client
from app.integrations.flowpcp.client import FlowPCPClient

_CORPO_VAZIO = {"cadastros": [], "limite": 50, "capacidades": []}


@pytest.fixture(autouse=True)
def _sem_espera(monkeypatch):
    """Mesma política de repetição, sem esperar segundos de verdade."""
    base = idempotent_post_policy()
    rapida = RetryPolicy(
        max_attempts=base.max_attempts,
        retry_on_status=base.retry_on_status,
        wait_initial_seconds=0.0,
        wait_max_seconds=0.0,
        wait_jitter_seconds=0.0,
    )
    monkeypatch.setattr(flow_client, "idempotent_post_policy", lambda: rapida)


def _cliente(handler) -> FlowPCPClient:
    outbound = OutboundClient(
        base_url="https://flow.test", transport=httpx.MockTransport(handler)
    )
    return FlowPCPClient(
        base_url="https://flow.test", service_token="t", tenant_id="x", outbound=outbound
    )


def _vistos_get(**kw):
    vistos = []

    def handler(req):
        vistos.append(dict(req.url.params))
        return httpx.Response(200, json=_CORPO_VAZIO)

    _cliente(handler).list_cadastros_pendentes(**kw)
    return vistos


def test_simular_manda_simular_1_sem_acoes():
    assert _vistos_get(limite=50, simular=True) == [{"limite": "50", "simular": "1"}]


def test_simular_com_acoes():
    vistos = _vistos_get(limite=50, acoes=("atualizar",), simular=True)
    assert vistos == [{"limite": "50", "simular": "1", "acoes": "atualizar"}]


def test_acoes_em_ordem_canonica():
    vistos = _vistos_get(limite=20, acoes=("atualizar", "criar"))
    assert vistos == [{"limite": "20", "acoes": "criar,atualizar"}]


def test_sem_acoes_nem_simular_so_manda_limite():
    assert _vistos_get() == [{"limite": "20"}]


def test_post_resultado_devolve_409_sem_levantar():
    def handler(req):
        assert req.url.path == "/api/portal-pedidos/cadastros/2222/gravado"
        return httpx.Response(409, json={"error": "conflito_codigo"})

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert (r.status_http, r.corpo, r.retry_after) == (409, {"error": "conflito_codigo"}, None)


def test_post_resultado_le_retry_after():
    def handler(req):
        return httpx.Response(
            429, json={"error": "rate_limit_excedido"}, headers={"Retry-After": "7"}
        )

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert (r.status_http, r.retry_after) == (429, 7)


def test_post_resultado_sem_rede_vira_none():
    def handler(req):
        raise httpx.ConnectTimeout("x")

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert (r.status_http, r.corpo, r.retry_after) == (None, None, None)


def test_post_resultado_erro_de_programacao_sobe():
    def handler(req):
        raise TypeError("bug")

    with pytest.raises(TypeError):
        _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})


def test_retry_after_ignora_digito_nao_ascii_e_limita_a_3600():
    def com(valor):
        return _cliente(
            lambda req: httpx.Response(429, json={}, headers={b"Retry-After": valor.encode("latin-1")})
        ).post_cadastro_resultado("2222", {"versao": 1}).retry_after

    assert com("²") is None
    assert com("99999") == 3600


def test_acao_desconhecida_levanta_antes_da_rede():
    chamadas = []

    def handler(req):
        chamadas.append(req)
        return httpx.Response(200, json=_CORPO_VAZIO)

    with pytest.raises(ValueError):
        _cliente(handler).list_cadastros_pendentes(acoes=("inativa",))
    assert chamadas == []


_UUID = "0b9f2c1e-4d3a-4f6b-8a7c-1e2d3c4b5a69"


def test_post_repete_em_503_com_mesma_idempotency_key():
    vistos = []

    def handler(req):
        vistos.append(req.headers["Idempotency-Key"])
        return httpx.Response(503 if len(vistos) < 3 else 200, json={"ok": True})

    r = _cliente(handler).post_cadastro_resultado(
        _UUID, {"versao": 1, "resultado": "gravado"}
    )
    assert vistos == [f"cadastro-{_UUID}-1-gravado"] * 3
    assert r.status_http == 200


def test_post_500_nas_tres_tentativas_devolve_status():
    chamadas = []

    def handler(req):
        chamadas.append(req)
        return httpx.Response(500, json={"error": "x"})

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert len(chamadas) == 3 and r.status_http == 500


def test_post_429_nao_repete():
    chamadas = []

    def handler(req):
        chamadas.append(req)
        return httpx.Response(429, json={})

    _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert len(chamadas) == 1


def test_acoes_com_excluir_em_ordem_canonica():
    vistos = _vistos_get(limite=20, acoes=("excluir", "inativar", "atualizar", "criar"))
    assert vistos == [{"limite": "20", "acoes": "criar,atualizar,inativar,excluir"}]


def test_acao_fora_da_lista_continua_recusada():
    with pytest.raises(ValueError):
        _cliente(lambda req: None).list_cadastros_pendentes(acoes=("apagar",))
