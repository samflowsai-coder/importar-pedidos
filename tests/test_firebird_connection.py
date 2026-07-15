"""Tradução da config do ambiente para os kwargs do fdb.connect.

Função pura — sem tocar no driver/lib nativa (roda igual no CI). Com o `fdb`
passamos `host`/`port`/`database` SEPARADOS (não um DSN `host/port:C:\\...`),
justamente pra o drive-letter do Windows não confundir o parser de DSN. TCP
quando há host; embedded quando não há.
"""
from __future__ import annotations

from app.erp.connection import _fb_connect_kwargs


def test_tcp_inclui_host_e_port():
    k = _fb_connect_kwargs({"path": r"C:\Fire\x.fdb", "host": "192.168.15.7", "port": "3050"})
    assert k["host"] == "192.168.15.7"
    assert k["port"] == 3050
    assert k["database"] == r"C:\Fire\x.fdb"


def test_embedded_sem_host_nao_inclui_host_nem_port():
    k = _fb_connect_kwargs({"path": "/tmp/x.fdb"})
    assert "host" not in k and "port" not in k
    assert k["database"] == "/tmp/x.fdb"


def test_porta_default_3050_quando_vazia():
    k = _fb_connect_kwargs({"path": "db", "host": "h", "port": ""})
    assert k["port"] == 3050


def test_defaults_user_e_charset():
    k = _fb_connect_kwargs({"path": "db"})
    assert k["user"] == "SYSDBA"
    assert k["charset"] == "WIN1252"


def test_host_e_valores_normalizados():
    k = _fb_connect_kwargs({"path": "db", "host": "  10.0.0.1  ", "port": "3055", "user": " SYSDBA "})
    assert k["host"] == "10.0.0.1"
    assert k["port"] == 3055
    assert k["user"] == "SYSDBA"
