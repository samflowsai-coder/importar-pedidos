from __future__ import annotations

from unittest.mock import MagicMock

import app.integrations.flowpcp.hook as hook
from app.integrations.flowpcp.config import FlowPCPConfig
from app.models.order import Order, OrderHeader, OrderItem

_CFG = FlowPCPConfig(
    enabled=True, base_url="https://flow.test", service_token="t", tenant_id="uuid-mm"
)


def _order() -> Order:
    return Order(
        header=OrderHeader(order_number="AW097", customer_name="MM", customer_cnpj="123"),
        items=[OrderItem(description="meia", quantity=10)],
    )


def _sem_depara(monkeypatch) -> None:
    """Isola estes testes do de-para intercompany (Task 5).

    Sem este mock, `push_new_order` chama `resolucao_para` de verdade, que
    lê `environments_repo.get_by_slug` — ambiente real (`data/app_shared.db`)
    fora do controle deste arquivo. Este módulo testa só o wiring do
    FlowPCP em si; o comportamento do de-para tem cobertura própria em
    `tests/test_flowpcp_intercompany.py`.
    """
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: None)


def test_push_skips_when_env_not_flowpcp(monkeypatch):
    # flowpcp_config_for_slug devolve None quando o env não tem FlowPCP / disabled.
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: None)
    assert hook.push_new_order(_order(), import_id="imp-1", slug="nasmar") is False


def test_push_enqueues_when_enabled(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    _sem_depara(monkeypatch)
    fake_exporter = MagicMock()
    fake_exporter.enqueue.return_value = True
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *, tenant_id: fake_exporter)

    assert hook.push_new_order(_order(), import_id="imp-1", slug="mm") is True
    _, kwargs = fake_exporter.enqueue.call_args
    assert kwargs["import_id"] == "imp-1"


def test_push_swallows_errors_best_effort(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    _sem_depara(monkeypatch)
    boom = MagicMock()
    boom.enqueue.side_effect = RuntimeError("kaboom")
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *, tenant_id: boom)
    assert hook.push_new_order(_order(), import_id="imp-1", slug="mm") is False
