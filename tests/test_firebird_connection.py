"""Montagem do DSN na conexão Firebird (embedded vs TCP).

O firebird-driver espera `connect(database, ...)` com o DSN — no TCP,
`host/port:database`. O path TCP nunca era exercitado (só usávamos embedded).
"""
from __future__ import annotations

import firebird.driver as drv
import pytest

from app.erp.connection import FirebirdConnection
from app.erp.exceptions import FirebirdConnectionError


def _capture_connect(monkeypatch):
    captured: dict = {}

    def fake_connect(database, **kw):
        captured["database"] = database
        captured.update(kw)
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(drv, "connect", fake_connect)
    return captured


def test_tcp_monta_dsn_host_port_database(monkeypatch):
    captured = _capture_connect(monkeypatch)
    cfg = {
        "host": "192.168.15.4", "port": "3050", "path": r"C:\Fire\x.fdb",
        "user": "samuel", "password": "p", "charset": "WIN1252",
    }
    with pytest.raises(FirebirdConnectionError):
        with FirebirdConnection().connect_with_config(cfg):
            pass
    assert captured["database"] == r"192.168.15.4/3050:C:\Fire\x.fdb"
    assert captured["user"] == "samuel"
    assert captured["password"] == "p"
    assert captured["charset"] == "WIN1252"


def test_embedded_usa_path_direto(monkeypatch):
    captured = _capture_connect(monkeypatch)
    cfg = {
        "host": "", "port": "", "path": "/tmp/x.fdb",
        "user": "SYSDBA", "password": "", "charset": "WIN1252",
    }
    with pytest.raises(FirebirdConnectionError):
        with FirebirdConnection().connect_with_config(cfg):
            pass
    assert captured["database"] == "/tmp/x.fdb"  # sem host/port
