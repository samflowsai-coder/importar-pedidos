# tests/test_cliente_extract.py
from datetime import date

from app.erp.cliente_extract import ExtracaoClientesResult, extract_clientes_ativos


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self):
        return self._cur


def test_extract_keeps_cnpj_discards_cpf_and_invalid():
    # (CODIGO, RAZAO_SOCIAL, CPF_CNPJ, CODGRUPO)
    rows = [
        (498, "SBF S.A", "06.347.409/0296-51", 12),  # CNPJ 14 díg → mantém
        (10, "JOAO PESSOA FISICA", "123.456.789-09", None),  # CPF 11 díg → descarta
        (11, "LIXO", "abc", None),  # inválido → descarta
    ]
    res = extract_clientes_ativos(_FakeConn(rows), desde_data=date(2025, 7, 17))
    assert isinstance(res, ExtracaoClientesResult)
    assert [c.cnpj for c in res.clientes] == ["06347409029651"]
    assert res.clientes[0].fire_cliente_id == "498"
    assert res.clientes[0].nome == "SBF S.A"
    assert res.clientes[0].grupo_codigo == "12"
    assert res.clientes[0].ativo is True
    assert res.descartados_cpf == 1
    assert res.descartados_invalidos == 1
    assert res.colisoes_dedup == 0


def test_extract_dedups_by_cnpj_keeping_max_codigo():
    rows = [
        (100, "CADASTRO ANTIGO", "06347409029651", 12),
        (200, "CADASTRO NOVO", "06.347.409/0296-51", 12),  # mesmo CNPJ, CODIGO maior
    ]
    res = extract_clientes_ativos(_FakeConn(rows), desde_data=date(2025, 7, 17))
    assert len(res.clientes) == 1
    assert res.clientes[0].fire_cliente_id == "200"
    assert res.clientes[0].nome == "CADASTRO NOVO"
    assert res.colisoes_dedup == 1


def test_extract_dedup_keeps_max_codigo_regardless_of_row_order():
    # Feed rows in DESCENDING CODIGO order (opposite of SQL ORDER BY).
    # Dedup must still select the larger CODIGO via explicit comparison.
    rows = [
        (200, "CADASTRO NOVO", "06.347.409/0296-51", 12),  # CODIGO maior, vem primeiro
        (100, "CADASTRO ANTIGO", "06347409029651", 12),  # mesmo CNPJ, CODIGO menor
    ]
    res = extract_clientes_ativos(_FakeConn(rows), desde_data=date(2025, 7, 17))
    assert len(res.clientes) == 1
    assert res.clientes[0].fire_cliente_id == "200"
    assert res.clientes[0].nome == "CADASTRO NOVO"
    assert res.colisoes_dedup == 1


def test_extract_passes_desde_data_as_bind():
    conn = _FakeConn([])
    extract_clientes_ativos(conn, desde_data=date(2025, 7, 17))
    _sql, params = conn._cur.executed
    assert params == (date(2025, 7, 17),)
