from datetime import date

from app.erp.catalog_extract import MaterialFireDTO, extract_materiais, extract_ultima_entrada


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *a):
        self.sql = sql

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)


def test_extract_materiais_limpa_e_marca_ativo():
    rows = [
        (250, " FIO 70/2 ", "KG ", None, "Nao", "5402.33.10", 1, 1, "FIO - MATERIA-PRIMA"),
        (251, "SACO PLASTICO", "PC", "789", "Sim", None, None, 19, "EMBALAGEM"),
    ]
    out = extract_materiais(_Conn(rows))
    assert out[0] == MaterialFireDTO(
        "250", "250", "FIO 70/2", "KG", None, True, "5402.33.10", 1, 1, "FIO - MATERIA-PRIMA"
    )
    assert out[1].ativo is False and out[1].sped_bruto is None and out[1].grupo_codigo == 19


def test_extract_ultima_entrada_pega_a_mais_recente():
    rows = [
        (250, date(2026, 1, 5), "54023310"),
        (250, date(2026, 7, 1), "5402.33.90"),
        (251, date(2025, 3, 1), " 39232190 "),
    ]
    assert extract_ultima_entrada(_Conn(rows)) == {
        "250": ("5402.33.90", date(2026, 7, 1)),
        "251": ("39232190", date(2025, 3, 1)),
    }
