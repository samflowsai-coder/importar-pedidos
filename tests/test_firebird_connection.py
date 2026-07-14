"""Montagem do DSN do firebird-driver (embedded vs TCP).

Função pura — sem tocar no driver/lib nativa (roda igual no CI). O
firebird-driver espera `connect(database, ...)` com o DSN; no TCP é
`host/port:database` (o path é do servidor; drive-letter do Windows é OK).
O path TCP nunca era exercitado — só usávamos embedded.
"""
from __future__ import annotations

from app.erp.connection import _build_fb_dsn


def test_tcp_monta_dsn_host_port_database():
    assert _build_fb_dsn("192.168.15.4", "3050", r"C:\Fire\x.fdb") == r"192.168.15.4/3050:C:\Fire\x.fdb"


def test_embedded_usa_path_direto():
    assert _build_fb_dsn("", "", "/tmp/x.fdb") == "/tmp/x.fdb"


def test_porta_default_3050_quando_vazia():
    assert _build_fb_dsn("host", "", "db") == "host/3050:db"


def test_host_com_espacos_e_normalizado():
    assert _build_fb_dsn("  10.0.0.1  ", "3055", "db") == "10.0.0.1/3055:db"
