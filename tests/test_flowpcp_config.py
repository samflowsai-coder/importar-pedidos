from __future__ import annotations

import pytest

from app.integrations.flowpcp.config import (
    FlowPCPConfig,
    enabled_flowpcp_envs,
    flowpcp_config_for_slug,
)
from app.persistence import environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def _mk_env(slug: str, **flow):
    env = environments_repo.create(
        slug=slug, name=slug.upper(), watch_dir="/a", output_dir="/b", fb_path="/c.fdb"
    )
    if flow:
        environments_repo.set_flowpcp_config(env["id"], **flow)
    return env


def test_for_slug_none_when_disabled(fresh_shared):
    _mk_env("mm")  # criado mas nunca configurado → disabled
    assert flowpcp_config_for_slug("mm") is None


def test_for_slug_none_when_env_missing(fresh_shared):
    assert flowpcp_config_for_slug("inexistente") is None


def test_for_slug_returns_config_with_decrypted_token(fresh_shared):
    _mk_env(
        "mm",
        enabled=True,
        base_url="https://flow.test",
        tenant_id="uuid-mm",
        dry_run=True,
        poll_interval_s=45,
        service_token="svc-tok",
    )
    cfg = flowpcp_config_for_slug("mm")
    assert isinstance(cfg, FlowPCPConfig)
    assert cfg.enabled is True
    assert cfg.base_url == "https://flow.test"
    assert cfg.tenant_id == "uuid-mm"
    assert cfg.service_token == "svc-tok"
    assert cfg.dry_run is True
    assert cfg.poll_interval_s == 45


def test_enabled_envs_gates_to_enabled_only(fresh_shared):
    _mk_env("mm", enabled=True, base_url="x", tenant_id="t", service_token="tok")
    _mk_env("nasmar")  # disabled — só vende
    envs = enabled_flowpcp_envs()
    assert set(envs) == {"mm"}
    assert envs["mm"].service_token == "tok"


def test_catalogo_push_default_off_e_ligavel(fresh_shared):
    """Gate do envio de catálogo ao Flow: default OFF; setável por ambiente."""
    env = _mk_env("mm", enabled=True, base_url="https://x", tenant_id="t")
    cfg = flowpcp_config_for_slug("mm")
    assert cfg.catalogo_push is False  # default: puxa do Fire, NÃO envia ao Flow

    environments_repo.set_flowpcp_config(
        env["id"], enabled=True, base_url="https://x", tenant_id="t", catalogo_push=True
    )
    cfg2 = flowpcp_config_for_slug("mm")
    assert cfg2.catalogo_push is True
    # e aparece no public view (UI lê daqui)
    assert environments_repo.get_by_slug("mm")["flowpcp_catalogo_push"] == 1


def test_catalogo_apenas_meias_default_off_e_ligavel(fresh_shared):
    """Filtro do catálogo (só subgrupo MEIAS): default OFF; setável por ambiente."""
    env = _mk_env("mm", enabled=True, base_url="https://x", tenant_id="t")
    cfg = flowpcp_config_for_slug("mm")
    assert cfg.catalogo_apenas_meias is False  # default: extrai PRODUTOS inteiro

    environments_repo.set_flowpcp_config(
        env["id"],
        enabled=True,
        base_url="https://x",
        tenant_id="t",
        catalogo_apenas_meias=True,
    )
    cfg2 = flowpcp_config_for_slug("mm")
    assert cfg2.catalogo_apenas_meias is True
    # e aparece no public view (UI lê daqui)
    assert environments_repo.get_by_slug("mm")["flowpcp_catalogo_apenas_meias"] == 1
