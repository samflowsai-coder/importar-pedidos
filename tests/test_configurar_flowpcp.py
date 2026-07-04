"""Tool de configuração da integração FlowPCP (tools/configurar_flowpcp.py).

Cobre o que é crítico: grava a config no app_shared.db, cifra os segredos,
respeita a semântica 'None mantém', e o gate do Firebird falha graciosamente
(sem derrubar) — o que garante que um deploy sem Firebird não quebra nada.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.persistence import environments_repo, router

# tools/ não é pacote — carrega o módulo pelo caminho.
_TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "configurar_flowpcp.py"
_spec = importlib.util.spec_from_file_location("configurar_flowpcp", _TOOL_PATH)
cfgtool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfgtool)


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


@pytest.fixture
def mm_env(fresh_shared):
    return environments_repo.create(
        slug="mm",
        name="MM Americanense",
        watch_dir="/tmp/mm/in",
        output_dir="/tmp/mm/out",
        fb_path="/tmp/nao-existe.fdb",
    )


def test_gravar_flowpcp_liga_e_cifra_token(mm_env):
    env = cfgtool.gravar_flowpcp(
        "mm",
        service_token="tok-secreto-123",
        base_url="https://gestor.samflowsai.com.br",
        tenant_id="1798c3c5-0fb6-4edb-a523-e13fb5bf52a0",
    )
    assert bool(env["flowpcp_enabled"]) is True
    assert env["flowpcp_base_url"] == "https://gestor.samflowsai.com.br"
    assert env["flowpcp_tenant_id"] == "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"
    # dry_run=1 por segurança (só afeta a VOLTA, não usada hoje)
    assert bool(env["flowpcp_dry_run"]) is True
    # token cifrado e recuperável
    assert environments_repo.get_flowpcp_token(env["id"]) == "tok-secreto-123"


def test_token_none_mantem_o_atual(mm_env):
    cfgtool.gravar_flowpcp("mm", service_token="tok-original")
    env = cfgtool.gravar_flowpcp("mm", service_token=None)  # Enter vazio
    assert environments_repo.get_flowpcp_token(env["id"]) == "tok-original"


def test_gravar_senha_firebird_cifra(mm_env):
    env = cfgtool.gravar_senha_firebird("mm", "masterkey-do-cliente")
    assert environments_repo.get_password(env["id"]) == "masterkey-do-cliente"


def test_senha_none_mantem(mm_env):
    cfgtool.gravar_senha_firebird("mm", "pw1")
    env = cfgtool.gravar_senha_firebird("mm", None)
    assert environments_repo.get_password(env["id"]) == "pw1"


def test_slug_inexistente_erra(fresh_shared):
    with pytest.raises(SystemExit):
        cfgtool.gravar_flowpcp("nao-existe", service_token="x")


def test_testar_firebird_falha_graciosamente(mm_env):
    """Sem Firebird conectável, retorna (False, str) — nunca levanta. É o gate
    que impede o deploy de quebrar a operação."""
    ok, err = cfgtool.testar_firebird(mm_env)
    assert ok is False
    assert isinstance(err, str) and err


def test_ligar_push_pedido_grava_both(tmp_path, monkeypatch):
    from app import config as app_config

    monkeypatch.setattr(app_config, "_CONFIG_FILE", tmp_path / "config.json")
    cfg = cfgtool.ligar_push_pedido()
    assert cfg["export_mode"] == "both"
