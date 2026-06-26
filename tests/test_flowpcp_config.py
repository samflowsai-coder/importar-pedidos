from __future__ import annotations

from app.integrations.flowpcp.config import FlowPCPConfig, load_flowpcp_config


def test_default_is_disabled():
    cfg = load_flowpcp_config({})
    assert cfg.enabled is False
    assert cfg.timezone == "America/Sao_Paulo"
    assert cfg.poll_interval_s == 30


def test_loads_enabled_mm_config():
    cfg = load_flowpcp_config(
        {
            "flowpcp": {
                "enabled": True,
                "base_url": "https://flow.test",
                "service_token": "tok",
                "tenant_id": "uuid-mm",
                "dry_run": True,
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.base_url == "https://flow.test"
    assert cfg.dry_run is True
    assert isinstance(cfg, FlowPCPConfig)
