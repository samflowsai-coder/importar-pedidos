import sqlite3
from datetime import date, timedelta

import pytest

from app.erp.cliente_extract import ClienteFireDTO, ExtracaoClientesResult
from app.integrations.flowpcp import clientes_sync
from app.integrations.flowpcp.config import FlowPCPConfig
from app.persistence import clientes_fire_repo
from app.persistence.schema_env import TABLES_SQL


class _FakeClient:
    def __init__(self):
        self.sent = None

    def send_clientes(self, request):
        self.sent = request

        class _R:
            dry_run = True
        return _R()

    def close(self):
        pass


def _dto(codigo, cnpj):
    return ClienteFireDTO(fire_cliente_id=codigo, cnpj=cnpj, nome=f"C{codigo}", grupo_codigo=None, ativo=True)


def _env_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(TABLES_SQL)
    return conn


@pytest.fixture
def _patch(monkeypatch):
    def _apply(cfg, extracao):
        monkeypatch.setattr(clientes_sync, "flowpcp_config_for_slug", lambda slug: cfg)
        monkeypatch.setattr(clientes_sync, "extract_clientes_ativos", lambda conn, *, desde_data: extracao)
    return _apply


def test_returns_none_when_no_flowpcp(_patch):
    _patch(None, None)
    assert clientes_sync.run_clientes_sync("mm", _fire_conn=object(), _env_conn=_env_conn()) is None


def test_empty_extraction_skips_write_and_push(_patch):
    cfg = FlowPCPConfig(enabled=True, clientes_push=True)
    _patch(cfg, ExtracaoClientesResult(clientes=[], descartados_cpf=2, descartados_invalidos=0, colisoes_dedup=0))
    conn = _env_conn()
    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", _client=client, _fire_conn=object(), _env_conn=conn)
    assert res.skipped_empty is True
    assert res.itens == 0
    assert res.descartados_cpf == 2
    assert client.sent is None
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 0


def test_gate_off_writes_local_only(_patch):
    cfg = FlowPCPConfig(enabled=True, clientes_push=False)
    _patch(cfg, ExtracaoClientesResult(clientes=[_dto("1", "11111111111111")], descartados_cpf=0, descartados_invalidos=0, colisoes_dedup=1))
    conn = _env_conn()
    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", _client=client, _fire_conn=object(), _env_conn=conn)
    assert res.reconciliacao is None
    assert res.itens == 1
    assert res.colisoes_dedup == 1
    assert client.sent is None
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 1


def test_gate_on_pushes_and_returns_reconciliacao(_patch):
    cfg = FlowPCPConfig(enabled=True, clientes_push=True)
    _patch(cfg, ExtracaoClientesResult(clientes=[_dto("1", "11111111111111")], descartados_cpf=0, descartados_invalidos=0, colisoes_dedup=0))
    conn = _env_conn()
    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", dry_run=True, _client=client, _fire_conn=object(), _env_conn=conn)
    assert res.reconciliacao is not None
    assert client.sent is not None
    assert client.sent.fullSync is False  # I7
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 1


def test_window_cutoff_is_365_days_before_hoje(monkeypatch):
    """Proves that extract_clientes_ativos is called with desde_data = _hoje - 365 days."""
    captured = {}

    def stub_extract(conn, *, desde_data):
        captured["desde_data"] = desde_data
        return ExtracaoClientesResult(clientes=[_dto("1", "11111111111111")], descartados_cpf=0, descartados_invalidos=0, colisoes_dedup=0)

    cfg = FlowPCPConfig(enabled=True, clientes_push=False)
    monkeypatch.setattr(clientes_sync, "flowpcp_config_for_slug", lambda slug: cfg)
    monkeypatch.setattr(clientes_sync, "extract_clientes_ativos", stub_extract)

    test_hoje = date(2026, 7, 17)
    conn = _env_conn()
    client = _FakeClient()
    clientes_sync.run_clientes_sync("mm", _hoje=test_hoje, _client=client, _fire_conn=object(), _env_conn=conn)

    expected_desde = test_hoje - timedelta(days=365)
    assert captured["desde_data"] == expected_desde


def test_empty_extraction_preserves_existing_snapshot(_patch):
    """Proves that an empty extraction leaves pre-existing rows intact."""
    cfg = FlowPCPConfig(enabled=True, clientes_push=False)
    _patch(cfg, ExtracaoClientesResult(clientes=[], descartados_cpf=0, descartados_invalidos=0, colisoes_dedup=0))

    conn = _env_conn()
    # Seed with one existing cliente
    clientes_fire_repo.replace_all(conn, [_dto("42", "99999999999999")], extraido_em="2026-01-01T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 1

    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", _client=client, _fire_conn=object(), _env_conn=conn)

    # Verify empty extraction was skipped
    assert res.skipped_empty is True
    # Verify snapshot was preserved
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 1
    row = conn.execute("SELECT fire_cliente_id FROM clientes_fire").fetchone()
    assert row[0] == "42"
    # Verify client was not called
    assert client.sent is None
