from __future__ import annotations

from unittest.mock import MagicMock

import app.worker.jobs.drain_outbox as drain
import app.worker.jobs.poll_flowpcp as job
from app.integrations.flowpcp.config import FlowPCPConfig
from app.integrations.flowpcp.schema import (
    ClienteRecebimento,
    ItemRecebimento,
    OrigemRecebimento,
    RecebimentoRequest,
)
from app.persistence.outbox_repo import OutboxRow


def _flowpcp_outbox_row(*, attempts: int = 0) -> OutboxRow:
    req = RecebimentoRequest(
        externalId="imp-1",
        fornecedor="MM",
        pedidoNumero="AW097",
        emitidoEm="2026-06-15T00:00:00.000Z",
        cliente=ClienteRecebimento(nome="MM", cnpj="123"),
        itens=[ItemRecebimento(descricao="meia", quantidade=10)],
        origem=OrigemRecebimento(
            importadorVersao="1.0.0",
            arquivoOriginal="p.pdf",
            parserUsado="t",
            confiancaParser="alta",
        ),
    )
    return OutboxRow(
        id=7,
        import_id="imp-1",
        target="flowpcp",
        endpoint="/api/portal-pedidos/recebimento",
        payload=req.model_dump(by_alias=True),
        idempotency_key="send-imp-1",
        status="pending",
        attempts=attempts,
        next_attempt_at=None,
        last_error=None,
        response=None,
        trace_id=None,
        created_at="2026-06-15T00:00:00",
        sent_at=None,
    )


_CFG = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm")


def test_run_poll_skips_disabled_envs(monkeypatch):
    monkeypatch.setattr(job, "_list_flowpcp_envs", lambda: [])  # nenhum ambiente ligado
    called = MagicMock()
    monkeypatch.setattr(job, "poll_decisoes_once", called)
    job.run_poll_flowpcp()
    called.assert_not_called()


def test_run_poll_invokes_once_per_enabled_env(monkeypatch):
    cfg = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm")
    monkeypatch.setattr(job, "_list_flowpcp_envs", lambda: [("mm", cfg)])
    monkeypatch.setattr(job, "_open_env_conn", lambda slug: MagicMock())
    monkeypatch.setattr(job, "_open_fire_conn", lambda slug: MagicMock())
    monkeypatch.setattr(job, "_build_client", lambda cfg: MagicMock())
    called = MagicMock(return_value=0)
    monkeypatch.setattr(job, "poll_decisoes_once", called)
    job.run_poll_flowpcp()
    assert called.call_count == 1


def test_list_flowpcp_envs_wraps_enabled_envs(monkeypatch):
    # O gating (ativo + enabled) vive em enabled_flowpcp_envs (testado em
    # test_flowpcp_config); aqui só garantimos que _list_flowpcp_envs o expõe.
    cfg = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm")
    monkeypatch.setattr(job, "enabled_flowpcp_envs", lambda: {"mm": cfg})
    result = dict(job._list_flowpcp_envs())
    assert set(result) == {"mm"}
    assert result["mm"] is cfg


def test_process_flowpcp_row_sends_and_marks_sent(monkeypatch):
    fake_client = MagicMock()
    fake_client.send_order.return_value = {"ok": True}
    monkeypatch.setattr(drain, "FlowPCPClient", lambda **kw: fake_client)
    sent = {}
    monkeypatch.setattr(
        drain.outbox_repo,
        "mark_sent",
        lambda rid, *, response=None: sent.update(id=rid, response=response),
    )
    drain._process_flowpcp_row(_flowpcp_outbox_row(), _CFG)
    fake_client.send_order.assert_called_once()
    assert fake_client.send_order.call_args.kwargs["idempotency_key"] == "send-imp-1"
    assert sent["id"] == 7
    fake_client.close.assert_called_once()


def test_process_flowpcp_row_reschedules_on_failure(monkeypatch):
    fake_client = MagicMock()
    fake_client.send_order.side_effect = RuntimeError("rede caiu")
    monkeypatch.setattr(drain, "FlowPCPClient", lambda **kw: fake_client)
    failed = {}
    monkeypatch.setattr(
        drain.outbox_repo,
        "mark_failed",
        lambda rid, *, error, next_attempt_at=None, dead=False: failed.update(
            id=rid, error=error, next_attempt_at=next_attempt_at, dead=dead
        ),
    )
    drain._process_flowpcp_row(_flowpcp_outbox_row(attempts=0), _CFG)
    assert failed["id"] == 7
    assert failed["dead"] is False
    assert failed["next_attempt_at"] is not None


def test_process_flowpcp_row_marks_dead_after_max_attempts(monkeypatch):
    fake_client = MagicMock()
    fake_client.send_order.side_effect = RuntimeError("rede caiu")
    monkeypatch.setattr(drain, "FlowPCPClient", lambda **kw: fake_client)
    failed = {}
    monkeypatch.setattr(
        drain.outbox_repo,
        "mark_failed",
        lambda rid, *, error, next_attempt_at=None, dead=False: failed.update(
            id=rid, dead=dead
        ),
    )
    drain._process_flowpcp_row(_flowpcp_outbox_row(attempts=99), _CFG)
    assert failed["dead"] is True
