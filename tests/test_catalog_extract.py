from app.erp.catalog_extract import ProdutoFireDTO, extract_produtos
from app.erp.queries import LIST_PRODUTOS_CATALOGO, LIST_PRODUTOS_CATALOGO_MEIAS


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


def test_extract_default_usa_query_padrao():
    """Sem `apenas_meias`, roda LIST_PRODUTOS_CATALOGO (tudo) — comportamento atual."""
    conn = _FakeConn([])
    extract_produtos(conn)
    assert conn._cur.executed == LIST_PRODUTOS_CATALOGO


def test_extract_apenas_meias_false_usa_query_padrao():
    conn = _FakeConn([])
    extract_produtos(conn, apenas_meias=False)
    assert conn._cur.executed == LIST_PRODUTOS_CATALOGO


def test_extract_apenas_meias_true_usa_query_filtrada():
    """`apenas_meias=True` roda a variante filtrada pelo subgrupo MEIAS."""
    conn = _FakeConn([])
    extract_produtos(conn, apenas_meias=True)
    assert conn._cur.executed == LIST_PRODUTOS_CATALOGO_MEIAS
    assert conn._cur.executed != LIST_PRODUTOS_CATALOGO


def test_tipo_kit_sem_composicao_pela_unidade_ou_pelo_nome():
    """Kit sem composição no Fire vai como kit, nunca como simples: para o Flow
    simples = par e vira OP; kit sem composição fica fora do pool (15/10/2026)."""
    rows = [
        (2133, "KIT COM 6 PARES DE MEIA CANO BAIXO ATOALHADO KINGS", "KIT", None, "Nao", 0),
        (148, "KIT COM 3 PARES DE MEIA CANO CURTO BRANCA - POLO MATCH M", "unid", None, "Nao", 0),
        (3587, "KIT C/3 PARES DE MEIAS CHICAGO CANO LONGO BRANCA 39-44", "KG", None, "Nao", 0),
        (3792, "KIT C/1 MEIA CANO MÉDIO CINZA 37/44", "KG", None, "Nao", 0),
    ]
    assert [p.tipo for p in extract_produtos(_FakeConn(rows))] == ["kit"] * 4


def test_tipo_pack_pela_unidade_ou_pelo_nome_com_ou_sem_composicao():
    rows = [
        (2646, "PACK C/36 KITS DE MEIA INFANTIL CANO LONGO", "PACK", None, "Nao", 1),
        (2637, "PACK C/36 KITS DE MEIA INFANTIL SAPATILHA", "unid", None, "Sim", 0),
    ]
    assert [p.tipo for p in extract_produtos(_FakeConn(rows))] == ["pack", "pack"]


def test_tipo_simples_so_para_par_sem_kit_no_nome():
    rows = [
        (3187, "ACT VITTA 33/38 INV LISA BCO  ( SCARPINA", "PARES", None, "Nao", 0),
        (3145, "BRANCO COM PRETO", "PAR", None, "Nao", 0),
        (900, "MEIAS SOQUETE M", "UNID", None, "Nao", 0),
        (901, "KITCHEN TOALHA", "UNID", None, "Nao", 0),  # KIT só como palavra inteira
    ]
    assert [p.tipo for p in extract_produtos(_FakeConn(rows))] == ["simples"] * 4


def test_recorte_meias_inclui_os_componentes_dos_kits():
    """Os pares que compõem os kits não são vendidos (não estão em pedido nem no
    subgrupo MEIAS) e ficavam fora: 181 pares produzidos só existiam no Flow."""
    sql = " ".join(LIST_PRODUTOS_CATALOGO_MEIAS.split())
    assert "WITH RECURSIVE" in sql
    assert "JOIN RECORTE R ON K.CODPRODUTO_PAI = R.SEQ" in sql
    # ciclo em PRODUTOS_KIT não pode derrubar o catálogo (FB aborta no nível 1024)
    assert "R.NIVEL < 5" in sql and "K.CODPRODUTO <> K.CODPRODUTO_PAI" in sql
    assert "F.DESCRICAO NOT CONTAINING 'CUECA'" in sql
    assert "NOT CONTAINING 'CUECA'" in sql and "NOT CONTAINING 'BOXER'" in sql


def test_tipo_casos_de_borda():
    rows = [
        (1, "MEIA SOQUETE BRANCA", "PARES", None, "Nao", 1),  # composição vence o nome
        (3151, "PACK C/22 KITS DE MEIAS", "Pack", None, "Nao", 1),  # caixa mista
        (2, "3 KITS MEIA", "unid", None, "Nao", 0),
        (3, "PACKS 5 MEIAS", "unid", None, "Nao", 0),
        (4, "KIT3 MEIA SOQUETE", "unid", None, "Nao", 0),
        (5, "KIT-3 MEIAS", "unid", None, "Nao", 0),
        (6, "MEIA SOQUETE", "Kit", None, "Nao", 0),
    ]
    tipos = [p.tipo for p in extract_produtos(_FakeConn(rows))]
    assert tipos == ["kit", "pack", "kit", "pack", "kit", "kit", "kit"]
