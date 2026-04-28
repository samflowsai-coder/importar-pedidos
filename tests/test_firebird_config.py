"""Tests for app.firebird_config — UI-editable Firebird config persistence."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def fbcfg(monkeypatch, tmp_path):
    """Point firebird_config and secret_store at tmp_path."""
    from app import firebird_config
    from app.security import secret_store

    monkeypatch.setattr(firebird_config, "_CONFIG_FILE", tmp_path / "firebird.json")
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    yield firebird_config


def test_load_returns_empty_when_no_file(fbcfg):
    cfg = fbcfg.load()
    assert cfg == {
        "path": "", "host": "", "port": "", "user": "", "charset": "",
        "password_enc": "",
    }


def test_save_and_load_roundtrip(fbcfg):
    fbcfg.save(
        {"path": "/data/empresa.fdb", "host": "10.0.0.1", "port": "3050",
         "user": "SYSDBA", "charset": "WIN1252"},
        password="masterkey",
    )
    cfg = fbcfg.load()
    assert cfg["path"] == "/data/empresa.fdb"
    assert cfg["host"] == "10.0.0.1"
    assert cfg["port"] == "3050"
    assert cfg["user"] == "SYSDBA"
    assert cfg["charset"] == "WIN1252"
    # Password is encrypted, never plaintext
    assert cfg["password_enc"]
    assert "masterkey" not in cfg["password_enc"]
    # And recoverable via get_password()
    assert fbcfg.get_password() == "masterkey"


def test_public_view_omits_password(fbcfg):
    fbcfg.save({"path": "/data/empresa.fdb"}, password="secret")
    pv = fbcfg.public_view()
    assert "password" not in pv
    assert "password_enc" not in pv
    assert pv["path"] == "/data/empresa.fdb"


def test_save_with_password_none_keeps_existing(fbcfg):
    fbcfg.save({"path": "/a.fdb"}, password="orig")
    fbcfg.save({"path": "/b.fdb", "host": "h"}, password=None)
    assert fbcfg.load()["path"] == "/b.fdb"
    assert fbcfg.load()["host"] == "h"
    assert fbcfg.get_password() == "orig"


def test_save_with_empty_password_clears(fbcfg):
    fbcfg.save({"path": "/a.fdb"}, password="orig")
    fbcfg.save({"path": "/a.fdb"}, password="")
    assert fbcfg.load()["password_enc"] == ""
    assert fbcfg.get_password() is None


def test_is_configured(fbcfg):
    assert fbcfg.is_configured() is False
    fbcfg.save({"path": "/a.fdb"}, password=None)
    assert fbcfg.is_configured() is True


def test_apply_to_env_injects_only_nonempty(fbcfg, monkeypatch):
    for k in ("FB_DATABASE", "FB_HOST", "FB_PORT", "FB_USER", "FB_CHARSET", "FB_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    fbcfg.save(
        {"path": "/data/empresa.fdb", "host": "", "port": "3050", "user": "SYSDBA",
         "charset": ""},
        password="masterkey",
    )
    fbcfg.apply_to_env()
    assert os.environ["FB_DATABASE"] == "/data/empresa.fdb"
    assert os.environ["FB_PORT"] == "3050"
    assert os.environ["FB_USER"] == "SYSDBA"
    assert os.environ["FB_PASSWORD"] == "masterkey"
    # Empty fields don't override env
    assert "FB_HOST" not in os.environ
    assert "FB_CHARSET" not in os.environ


def test_apply_to_env_skips_password_when_undecryptable(fbcfg, monkeypatch, tmp_path):
    monkeypatch.delenv("FB_PASSWORD", raising=False)
    fbcfg.save({"path": "/a.fdb"}, password="orig")
    # Wipe key to simulate loss
    (tmp_path / ".secret.key").unlink()
    fbcfg.apply_to_env()
    # path still applied, password not
    assert os.environ["FB_DATABASE"] == "/a.fdb"
    assert "FB_PASSWORD" not in os.environ


def test_load_handles_corrupted_json(fbcfg, tmp_path):
    (tmp_path / "firebird.json").write_text("{not valid json")
    cfg = fbcfg.load()
    assert cfg["path"] == ""  # falls back to empty payload, no exception
