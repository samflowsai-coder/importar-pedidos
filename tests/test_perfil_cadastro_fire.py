"""Ferramentas SÓ LEITURA da P0 (tools/perfil_cadastro_fire.py e tools/produto_no_fire.py).

Só as funções puras: escolha de tabelas/colunas, montagem do SQL e perfil. Nada
conecta no Firebird."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"


def _carrega(nome: str):
    spec = importlib.util.spec_from_file_location(nome, _TOOLS / f"{nome}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


perfil_tool = _carrega("perfil_cadastro_fire")
rastro = _carrega("produto_no_fire")


def test_perfil_coluna_sempre_preenchida_e_coluna_sempre_nula():
    linhas = [
        {"X": "A", "Y": None},
        {"X": "A", "Y": None},
        {"X": "A", "Y": None},
    ]
    r = {p["coluna"]: p for p in perfil_tool.perfil(["X", "Y"], linhas)}
    assert r["X"] == {"coluna": "X", "preenchidos": 3, "mais_comum": "A"}
    assert r["Y"]["preenchidos"] == 0
    assert r["Y"]["mais_comum"] is None


def test_perfil_vazio_e_espaco_nao_contam_e_mais_comum_ganha():
    linhas = [{"X": "A"}, {"X": "B"}, {"X": "B"}, {"X": "  "}, {"X": ""}]
    (p,) = perfil_tool.perfil(["X"], linhas)
    assert p["preenchidos"] == 3
    assert p["mais_comum"] == "B"


def test_perfil_zero_e_valor_preenchido():
    (p,) = perfil_tool.perfil(["X"], [{"X": 0}, {"X": 0}])
    assert p["preenchidos"] == 2
    assert p["mais_comum"] == 0


def test_perfil_sem_linhas():
    (p,) = perfil_tool.perfil(["X"], [])
    assert p == {"coluna": "X", "preenchidos": 0, "mais_comum": None}


CATALOGO = [
    ("PRODUTOS", "SEQ", 8),
    ("NOTAPROD", "CODPRODUTO", 8),
    ("NOTAPROD", "VALOR", 8),
    ("PRODUTOS_KIT", "CODPRODUTO_PAI", 8),
    ("PRODUTOS_KIT", "CODPRODUTO", 16),
    ("RDB$RELATIONS", "SEQ", 8),
    ("MON$STATEMENTS", "PRODUTO", 8),
    ("SEC$USERS", "SEQ", 8),
    ("CLIENTES", "NOME", 37),
    ("OBS", "PRODUTO", 37),  # VARCHAR candidata: fica fora
]


def test_alvos_filtra_colunas_candidatas_e_tabelas_de_sistema():
    alvos = rastro.escolher_alvos(CATALOGO)
    assert alvos == [
        ("NOTAPROD", "CODPRODUTO"),
        ("PRODUTOS", "SEQ"),
        ("PRODUTOS_KIT", "CODPRODUTO"),
        ("PRODUTOS_KIT", "CODPRODUTO_PAI"),
    ]


def test_sql_do_rastro_e_select_limitado_e_parametrizado():
    sql = rastro.montar_sql("NOTAPROD", "CODPRODUTO")
    assert sql == 'SELECT FIRST 50 * FROM "NOTAPROD" WHERE "CODPRODUTO" = ?'


@pytest.mark.parametrize(
    "nome", ["notaprod", "NOTA PROD", 'X"; DROP TABLE Y; --', "1ABC", "", "A-B", "ABC\n"]
)
def test_nome_de_tabela_invalido_e_recusado(nome):
    with pytest.raises(ValueError):
        rastro.montar_sql(nome, "SEQ")
    with pytest.raises(ValueError):
        rastro.montar_sql("PRODUTOS", nome)


def test_so_select_passa():
    rastro.so_select("  select 1 from rdb$database")
    with pytest.raises(RuntimeError):
        rastro.so_select("DELETE FROM PRODUTOS")
    perfil_tool.so_select("SELECT 1 FROM RDB$DATABASE")
    with pytest.raises(RuntimeError):
        perfil_tool.so_select("UPDATE PRODUTOS SET GRUPO = 1")


def test_alvo_do_catalogo_com_nome_invalido_e_recusado_no_sql():
    # Mesmo vindo do catálogo, o identificador é validado antes de virar texto de SQL.
    alvos = rastro.escolher_alvos([("T-RUIM", "SEQ", 8)])
    with pytest.raises(ValueError):
        rastro.montar_sql(*alvos[0])


class _Cursor:
    """Cursor falso: registra os SQLs e responde pelo texto."""

    def __init__(self, respostas, falha_em=()):
        self.sqls: list[tuple[str, tuple]] = []
        self._respostas = respostas
        self._falha_em = falha_em
        self._atual: list = []
        self.description = []

    def execute(self, sql, params=()):
        self.sqls.append((sql, tuple(params)))
        for trecho in self._falha_em:
            if trecho in sql:
                raise RuntimeError("segredo-do-banco")
        for trecho, (desc, linhas) in self._respostas.items():
            if trecho in sql:
                self.description = [(d,) for d in desc]
                self._atual = linhas
                return
        raise AssertionError(f"SQL inesperado: {sql}")

    def fetchall(self):
        return self._atual


def _instala(monkeypatch, cur):
    import app.erp.connection as connection
    from app.persistence import environments_repo

    class _Conn:
        def cursor(self):
            return cur

    class _FB:
        def connect_somente_leitura(self, cfg):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield _Conn()

            return _cm()

    monkeypatch.setattr(connection, "FirebirdConnection", _FB)
    monkeypatch.setattr(environments_repo, "get_by_slug", lambda slug: {"slug": slug})
    monkeypatch.setattr(environments_repo, "to_fb_config", lambda env: {})


def test_main_rastro_imprime_formato_pula_tabela_que_falha_e_so_le(monkeypatch, capsys):
    cur = _Cursor(
        {
            "RDB$RELATION_FIELDS": (
                ["T", "C", "TIPO"],
                [
                    ("PRODUTOS", "SEQ", 8),
                    ("NOTAPROD", "CODPRODUTO", 8),
                    ("QUEBRADA", "SEQ", 8),
                    ("RDB$RELATIONS", "SEQ", 8),
                    ("TEXTO", "PRODUTO", 37),
                    ("RUIM-ID", "SEQ", 8),
                ],
            ),
            '"PRODUTOS"': (["SEQ", "DESCRICAO"], [(77, "BOTA")]),
            '"NOTAPROD"': (["CODPRODUTO"], []),
        },
        falha_em=['"QUEBRADA"'],
    )
    _instala(monkeypatch, cur)
    assert rastro.main(["mm", "--seq", "77"]) == 0
    out, err = capsys.readouterr()
    assert "==== PRODUTOS.SEQ = 77: 1 linha(s) ====" in out
    assert "   DESCRICAO: 'BOTA'" in out
    assert "NOTAPROD" not in out  # sem linhas, não imprime
    assert "AVISO QUEBRADA.SEQ: RuntimeError" in err
    assert "segredo-do-banco" not in err
    assert "AVISO RUIM-ID.SEQ: ValueError" in err
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in cur.sqls)
    consultas = [(s, p) for s, p in cur.sqls if "RDB$RELATION_FIELDS" not in s]
    assert all(p == (77,) and "= ?" in s for s, p in consultas)
    texto = " ".join(s for s, _ in consultas)
    assert "RDB$RELATIONS" not in texto and '"TEXTO"' not in texto


def test_main_perfil_so_select_e_parametros(monkeypatch, capsys):
    cur = _Cursor(
        {
            "RDB$RELATION_FIELDS": (["N"], [("SEQ",), ("DESCRICAO",)]),
            "TRIM(UNIDADE)": (["SEQ"], [(3,), (2,)]),
            "CODPRODUTO_PAI": (["SEQ"], [(9,)]),
            "WHERE SEQ IN": (["SEQ", "DESCRICAO"], [(3, "A"), (2, "A")]),
        }
    )
    _instala(monkeypatch, cur)
    assert perfil_tool.main(["mm", "--ultimos", "30"]) == 0
    out, _ = capsys.readouterr()
    assert "==== PAR (unidade PAR): 2 produtos (SEQ 2..3) ====" in out
    assert "==== KIT (pai em PRODUTOS_KIT): 1 produtos" in out
    assert "DESCRICAO" in out and "'A'" in out
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in cur.sqls)
    por_seq = [(s, p) for s, p in cur.sqls if s.rstrip().endswith(("ORDER BY SEQ DESC",))]
    assert por_seq and all(p == (8,) for _, p in por_seq)  # GRUPO como parâmetro
    com_in = [(s, p) for s, p in cur.sqls if "WHERE SEQ IN" in s]
    assert com_in and all(p in ((3, 2), (9,)) for _, p in com_in)  # SEQs como parâmetro
    assert all("RDB$RELATIONS" not in s for s, _ in cur.sqls)


def test_texto_longo_e_truncado():
    assert len(rastro._texto("x" * 1000)) <= 202
