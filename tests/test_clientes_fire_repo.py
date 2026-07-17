import sqlite3

from app.erp.cliente_extract import ClienteFireDTO
from app.persistence import clientes_fire_repo
from app.persistence.schema_env import TABLES_SQL


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(TABLES_SQL)
    return conn


def _dto(codigo: str, cnpj: str) -> ClienteFireDTO:
    return ClienteFireDTO(
        fire_cliente_id=codigo,
        cnpj=cnpj,
        nome=f"CLIENTE {codigo}",
        grupo_codigo="12",
        ativo=True,
    )


def test_replace_all_snapshot_and_count():
    conn = _conn()
    n = clientes_fire_repo.replace_all(
        conn,
        [_dto("1", "11111111111111"), _dto("2", "22222222222222")],
        extraido_em="2026-07-17T12:00:00Z",
    )
    assert n == 2
    assert clientes_fire_repo.count(conn) == 2
    # substituição: segunda carga menor apaga a anterior
    clientes_fire_repo.replace_all(
        conn, [_dto("3", "33333333333333")], extraido_em="2026-07-17T13:00:00Z"
    )
    rows = clientes_fire_repo.list_all(conn)
    assert [r["fire_cliente_id"] for r in rows] == ["3"]
    assert rows[0]["cnpj"] == "33333333333333"
    assert rows[0]["ativo"] == 1
