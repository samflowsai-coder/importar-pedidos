"""Ferramenta do operador (tools/cadastro_volta_operar.py): dublês de ERP e SQLite em memória."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date

import pytest

from app.erp import cadastro_fire
from app.persistence import cadastro_volta_repo as repo
from app.persistence import router, schema_env
from tools import cadastro_volta_operar as op

_PLANO = json.dumps({"campos": {"DESCRICAO": "TENIS X", "GRUPO": 3}})


@pytest.fixture
def livro():
    c = sqlite3.connect(":memory:")
    c.executescript(schema_env.TABLES_SQL)
    router._apply_column_migrations(c, schema_env.COLUMN_MIGRATIONS)
    c.executescript(schema_env.INDEXES_SQL)
    yield c
    c.close()


def _linha(livro, estado, *, cid="c1", versao=1, seq=None, erp=None):
    livro.execute(
        "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, modo, "
        "plano_json, seq_reservado, erp_produto_id, corpo_json, iniciado_em) VALUES "
        "(?, ?, ?, 'criar', 'simples', 'p1', 'criar', ?, ?, ?, 'SEGREDO', '2026-10-17 12:00:00')",
        (cid, versao, estado, _PLANO, seq, erp),
    )


class _Erp:
    """ERP falso, só leitura: registra os SELECT e recusa qualquer outro SQL."""

    def __init__(self, produtos=None, janela=None):
        self.produtos = produtos or {}
        self.janela = janela or []
        self.sql = []
        self.aberturas = 0

    def abrir(self, slug):
        @contextmanager
        def _cm():
            self.aberturas += 1
            yield self

        return _cm()

    def cursor(self):
        return _Cur(self)


class _Cur:
    def __init__(self, erp):
        self.erp, self.rows = erp, []

    def execute(self, sql, params=()):
        assert sql.lstrip().upper().startswith("SELECT"), "ERP é só leitura"
        self.erp.sql.append(sql)
        if "WHERE SEQ" in sql:
            p = self.erp.produtos.get(params[0])
            self.rows = [tuple(p[c] for c in cadastro_fire._CONFERENCIA)] if p else []
        else:
            self.rows = [tuple(p[c] for c in cadastro_fire._CONFERENCIA) for p in self.erp.janela]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


def _prod(seq, nome="TENIS X"):
    return {"SEQ": seq, "DESCRICAO": nome, "UNIDADE": "PAR", "GRUPO": 3,
            "CODIGO_EAN13": None, "CLASSIF_FISCAL": "6404", "DATA_CAD": date(2026, 10, 17)}


def _rodar(livro, erp, *argv):
    @contextmanager
    def abrir_livro(slug):
        yield livro

    return op.main(["mm", *argv], abrir_livro=abrir_livro, abrir_erp=erp.abrir)


def _estado(livro, cid="c1"):
    return repo.buscar(livro, cid, 1)


# ---- listar ----

def test_listar_mostra_so_incerto_orfao_e_transitorio_com_seq_sem_segredos(livro, capsys):
    _linha(livro, "incerto", cid="i1")
    _linha(livro, "orfao", cid="o1", erp="3992")
    _linha(livro, "transitorio", cid="t1", seq=4001)
    _linha(livro, "transitorio", cid="t2")  # sem SEQ: fora
    _linha(livro, "confirmado", cid="k1", erp="1")
    assert _rodar(livro, _Erp(), "listar") == 0
    out = capsys.readouterr().out
    for presente in ("i1", "o1", "t1"):
        assert presente in out
    assert "t2" not in out and "k1" not in out
    assert "SEGREDO" not in out and "TENIS X" not in out


def test_listar_vazio(livro, capsys):
    assert _rodar(livro, _Erp(), "listar") == 0
    assert "nenhuma linha" in capsys.readouterr().out


# ---- adotar ----

def test_adotar_sem_confirmar_so_mostra_e_nao_muda(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "ana") == 0
    out = capsys.readouterr().out
    assert "TENIS X" in out and "4001" in out and "--confirmar" in out
    assert _estado(livro)["estado"] == "incerto"


def test_adotar_confirmado_grava_corpo_e_alerta(livro):
    _linha(livro, "incerto")
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "ana", "--confirmar") == 0
    r = _estado(livro)
    assert r["estado"] == "gravado_sem_volta" and r["erp_produto_id"] == "4001"
    assert r["alerta"] == "adotado_manual:ana"
    corpo = json.loads(r["corpo_json"])
    assert corpo["resultado"] == "gravado" and corpo["erpProdutoId"] == "4001" and corpo["versao"] == 1
    assert corpo["gravadoEm"].endswith("Z")
    assert all(s.lstrip().upper().startswith("SELECT") for s in erp.sql)


def test_adotar_recusa_quando_o_seq_nao_existe_no_erp(livro, capsys):
    _linha(livro, "incerto")
    assert _rodar(livro, _Erp(), "adotar", "c1", "1", "4001", "--operador", "ana", "--confirmar") == 1
    assert "não existe no ERP" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"


def test_adotar_recusa_estado_errado_sem_abrir_o_erp(livro, capsys):
    _linha(livro, "orfao", erp="3992")
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "ana", "--confirmar") == 1
    assert "orfao" in capsys.readouterr().err
    assert erp.aberturas == 0 and _estado(livro)["estado"] == "orfao"


def test_adotar_linha_inexistente(livro, capsys):
    assert _rodar(livro, _Erp(), "adotar", "zz", "1", "4001", "--operador", "ana") == 1
    assert "não existe no livro" in capsys.readouterr().err


def test_adotar_seq_largo_demais_vira_mensagem(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp({1234567890: _prod(1234567890)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "1234567890", "--operador", "a", "--confirmar") == 1
    assert "1 a 9 dígitos" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"


def test_adotar_resultado_false_do_repo_vira_saida_1(livro, monkeypatch, capsys):
    _linha(livro, "incerto")
    monkeypatch.setattr(repo, "adotar_manual", lambda *a, **k: False)
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a", "--confirmar") == 1
    assert "mudou de estado" in capsys.readouterr().err


# ---- liberar ----

def test_liberar_com_seq_ausente_no_erp_confirmado(livro):
    _linha(livro, "incerto", seq=4001)
    assert _rodar(livro, _Erp(), "liberar", "c1", "1", "--operador", "ana", "--confirmar") == 0
    r = _estado(livro)
    assert (r["estado"], r["seq_reservado"], r["alerta"]) == ("liberado", None, "liberado_manual:ana")


def test_liberar_sem_confirmar_nao_muda(livro, capsys):
    _linha(livro, "transitorio", seq=4001)
    assert _rodar(livro, _Erp(), "liberar", "c1", "1", "--operador", "ana") == 0
    assert "não existe no ERP" in capsys.readouterr().out
    r = _estado(livro)
    assert (r["estado"], r["seq_reservado"]) == ("transitorio", 4001)


def test_liberar_recusa_quando_o_produto_existe(livro, capsys):
    _linha(livro, "incerto", seq=4001)
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "ana", "--confirmar") == 1
    assert "EXISTE no ERP" in capsys.readouterr().err
    r = _estado(livro)
    assert (r["estado"], r["seq_reservado"]) == ("incerto", 4001)


def test_liberar_sem_seq_mostra_candidatos_da_janela(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp(janela=[_prod(4005, "OUTRO"), _prod(4006, "MAIS UM")])
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "ana") == 0
    out = capsys.readouterr().out
    assert "2 produto(s)" in out and "4005" in out and "OUTRO" in out
    assert _estado(livro)["estado"] == "incerto"
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "ana", "--confirmar") == 0
    assert _estado(livro)["estado"] == "liberado"


def test_liberar_recusa_estado_errado(livro, capsys):
    _linha(livro, "gravado_sem_volta", erp="9")
    assert _rodar(livro, _Erp(), "liberar", "c1", "1", "--operador", "ana", "--confirmar") == 1
    assert "gravado_sem_volta" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "gravado_sem_volta"


def test_liberar_resultado_false_do_repo_vira_saida_1(livro, monkeypatch, capsys):
    _linha(livro, "incerto", seq=4001)
    monkeypatch.setattr(repo, "liberar_manual", lambda *a, **k: False)
    assert _rodar(livro, _Erp(), "liberar", "c1", "1", "--operador", "a", "--confirmar") == 1
    assert "mudou de estado" in capsys.readouterr().err


# ---- fechar-orfao ----

def test_fechar_orfao_sem_confirmar_nao_muda(livro, capsys):
    _linha(livro, "orfao", erp="3992")
    assert _rodar(livro, _Erp(), "fechar-orfao", "c1", "1", "--operador", "ana", "--nota", "duplicado") == 0
    assert "--confirmar" in capsys.readouterr().out
    assert _estado(livro)["estado"] == "orfao"


def test_fechar_orfao_confirmado(livro):
    _linha(livro, "orfao", erp="3992")
    args = ("fechar-orfao", "c1", "1", "--operador", "ana", "--nota", "duplicado", "--confirmar")
    assert _rodar(livro, _Erp(), *args) == 0
    r = _estado(livro)
    assert (r["estado"], r["nota_operador"], r["alerta"]) == ("orfao_fechado", "duplicado", "fechado_manual:ana")


def test_fechar_orfao_recusa_estado_errado_e_false_do_repo(livro, monkeypatch, capsys):
    _linha(livro, "incerto")
    args = ("fechar-orfao", "c1", "1", "--operador", "ana", "--nota", "n", "--confirmar")
    assert _rodar(livro, _Erp(), *args) == 1
    assert "incerto" in capsys.readouterr().err
    _linha(livro, "orfao", cid="o1", erp="1")
    monkeypatch.setattr(repo, "fechar_orfao", lambda *a, **k: False)
    assert _rodar(livro, _Erp(), "fechar-orfao", "o1", "1", "--operador", "a", "--nota", "n", "--confirmar") == 1
    assert "mudou de estado" in capsys.readouterr().err


def test_falha_de_conexao_nao_vaza_a_mensagem(livro, capsys):
    _linha(livro, "incerto")

    class _Quebrado(_Erp):
        def abrir(self, slug):
            raise RuntimeError("password=SENHA-SECRETA")

    assert _rodar(livro, _Quebrado(), "adotar", "c1", "1", "4001", "--operador", "a") == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "SENHA-SECRETA" not in err


def test_nada_roda_no_import_e_seq_invalido_e_rejeitado(livro):
    with pytest.raises(SystemExit):
        _rodar(livro, _Erp(), "adotar", "c1", "1", "0", "--operador", "a")
    with pytest.raises(SystemExit):
        _rodar(livro, _Erp(), "adotar", "c1", "1", "4001")  # falta --operador
