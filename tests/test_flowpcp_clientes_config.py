from app.integrations.flowpcp.config import flowpcp_config_from_env


def test_config_reads_clientes_push_on():
    cfg = flowpcp_config_from_env({"flowpcp_enabled": 1, "flowpcp_clientes_push": 1}, service_token="t")
    assert cfg.clientes_push is True


def test_config_clientes_push_defaults_off():
    cfg = flowpcp_config_from_env({"flowpcp_enabled": 1}, service_token="t")
    assert cfg.clientes_push is False
