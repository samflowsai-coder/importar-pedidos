"""fire_reader: reads PRODUTOS + PRODUTOS_KIT, returns ProductRow/ComponentRow."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.sync.fire_reader import (
    SQL_SELECT_PRODUTOS,
    SQL_SELECT_PRODUTOS_KIT,
    read_components_snapshot,
    read_products_snapshot,
)


def _fake_cursor(rows_by_sql: dict[str, list[tuple]]) -> MagicMock:
    cur = MagicMock()
    state = {"current_sql": None}

    def execute(sql, params=None):
        state["current_sql"] = sql
        return cur

    def fetchall():
        return rows_by_sql.get(state["current_sql"], [])

    cur.execute.side_effect = execute
    cur.fetchall.side_effect = fetchall
    return cur


@contextmanager
def _fake_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    yield conn


def test_read_products_classifies_kit_via_kit_ativo():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (10042, "CAL-0042-PR", "Tenis XYZ", "un", "7891234567890", "Nao", "Sim"),
            (10043, None, "Sola", "un", None, "Nao", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    assert len(rows) == 2
    by_seq = {r.seq: r for r in rows}
    assert by_seq[10042].is_kit is True
    assert by_seq[10043].is_kit is False
    assert by_seq[10042].descricao == "Tenis XYZ"
    assert by_seq[10043].codprod_altern is None


def test_read_products_classifies_kit_via_pai_in_produtos_kit():
    """Even if KIT_ATIVO='Nao', if SEQ appears as PAI, it's a kit."""
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (5, None, "Pai sem flag", "un", None, "Nao", "Nao"),
            (10, None, "Filho", "un", None, "Nao", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [(1, 5, 10, 2.0)],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    by_seq = {r.seq: r for r in rows}
    assert by_seq[5].is_kit is True
    assert by_seq[10].is_kit is False


def test_read_products_inativo_sim_marks_inativo_true():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (1, None, "Inativo prod", "un", None, "Sim", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    assert rows[0].inativo is True


def test_read_products_skips_blank_descricao():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (1, None, "", "un", None, "Nao", "Nao"),     # blank — skipped (warning)
            (2, None, "OK", "un", None, "Nao", "Nao"),   # ok
        ],
        SQL_SELECT_PRODUTOS_KIT: [],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    assert len(rows) == 1
    assert rows[0].seq == 2


def test_read_components_filters_invalid_rows():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS_KIT: [
            (1, 100, 200, 1.5),    # OK
            (2, None, 200, 1.5),   # PAI null — skip
            (3, 100, None, 1.5),   # FILHO null — skip
            (4, 100, 200, 0),      # qtd <= 0 — skip
            (5, 100, 200, 2.0),    # OK
        ],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        comps = read_components_snapshot({"path": "/tmp/x.fdb"})
    codigos = {c.codigo for c in comps}
    assert codigos == {1, 5}
