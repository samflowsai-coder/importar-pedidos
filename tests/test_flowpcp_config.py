from __future__ import annotations

from app.integrations.flowpcp.config import (
    FlowPCPConfig,
    load_flowpcp_config,
    load_flowpcp_envs,
)


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


def test_load_envs_empty_when_unset():
    assert load_flowpcp_envs({}) == {}
    assert load_flowpcp_envs({"FLOWPCP_ENVS": "   "}) == {}


def test_load_envs_ignores_malformed_json():
    assert load_flowpcp_envs({"FLOWPCP_ENVS": "{not json"}) == {}


def test_load_envs_parses_per_slug_config():
    raw = (
        '{"mm": {"flowpcp": {"enabled": true, "base_url": "https://flow.test", '
        '"service_token": "tok", "tenant_id": "uuid-mm"}}, '
        '"nasmar": {"flowpcp": {"enabled": false}}}'
    )
    envs = load_flowpcp_envs({"FLOWPCP_ENVS": raw})
    assert set(envs) == {"mm", "nasmar"}
    assert envs["mm"].enabled is True
    assert envs["mm"].tenant_id == "uuid-mm"
    assert envs["nasmar"].enabled is False
