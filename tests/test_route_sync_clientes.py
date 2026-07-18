from app.integrations.flowpcp import clientes_sync
from app.integrations.flowpcp.clientes_sync import ClientesSyncResult
from app.web import routes_environments


def test_route_returns_counters_local_only(monkeypatch):
    monkeypatch.setattr(routes_environments.environments_repo, "get",
                        lambda env_id: {"id": env_id, "slug": "mm", "flowpcp_enabled": 1})
    monkeypatch.setattr(clientes_sync, "run_clientes_sync",
                        lambda slug, **kw: ClientesSyncResult(
                            itens=5, extraido_em="2026-07-17T12:00:00Z",
                            descartados_cpf=3, descartados_invalidos=1, colisoes_dedup=2))
    body = routes_environments.sync_clientes_flowpcp("env1", apply=False, _=None)
    assert body["local_only"] is True
    assert body["itens"] == 5
    assert body["descartados_cpf"] == 3
    assert body["colisoes_dedup"] == 2
    assert "reconciliacao" not in body
