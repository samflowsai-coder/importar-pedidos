from app.erp.catalog_extract import ProdutoFireDTO, extract_produtos


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def execute(self, sql):
        self.executed = sql

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self):
        return self._cur


def test_extract_codigo_seq_e_tipo_kit():
    rows = [
        # (SEQ, DESCRICAO, UNIDADE, CODIGO_EAN13, BLOQUEADO, IS_KIT)
        (3381, "KIT C/5 BRANCO", "PC", None, "Nao", 1),
        (3170, "BRANCO COM BORDO", "PC ", None, "Nao", 0),
        (171, "PROD BLOQUEADO", "PC", "7891234567890", "Sim", 0),
        (10, "SEM EAN NEM UNIDADE", None, "  ", "Nao", 0),
    ]
    out = extract_produtos(_FakeConn(rows))
    # codigo == fire_produto_id == str(SEQ); kit detectado via IS_KIT
    assert out[0] == ProdutoFireDTO(
        fire_produto_id="3381",
        codigo="3381",
        nome="KIT C/5 BRANCO",
        unidade="PC",
        ean=None,
        ativo=True,
        tipo="kit",
    )
    assert out[1].tipo == "simples"
    assert out[2].ativo is False  # BLOQUEADO='Sim'
    # strings em branco viram None
    assert out[3].ean is None and out[3].unidade is None
    assert out[3].codigo == "10"


def test_extract_vazio():
    assert extract_produtos(_FakeConn([])) == []
