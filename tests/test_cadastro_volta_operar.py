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

_CAMPOS = {"DESCRICAO": "TENIS X", "UNIDADE": "PAR", "GRUPO": 3, "CODIGO_EAN13": None,
           "CLASSIF_FISCAL": "6404"}
_PLANO = json.dumps({"campos": _CAMPOS})


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
    """ERP falso: registra os SQL e recusa qualquer um que não seja SELECT.
    `janelas` é a fila de respostas de `candidatos_por_data` (a última se repete)."""

    def __init__(self, produtos=None, janela=None, kits=None, versao="2.5.9"):
        self.produtos, self.kits, self.versao = produtos or {}, kits or {}, versao
        self.janelas = [janela or []]
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
        cols = cadastro_fire._CONFERENCIA
        if "ENGINE_VERSION" in sql:
            self.rows = [(self.erp.versao,)]
        elif "PRODUTOS_KIT" in sql:
            self.rows = list(self.erp.kits.get(params[0], []))
        elif "WHERE SEQ" in sql:
            p = self.erp.produtos.get(params[0])
            self.rows = [tuple(p[c] for c in cols)] if p else []
        else:
            fila = self.erp.janelas
            janela = fila.pop(0) if len(fila) > 1 else fila[0]
            self.rows = [tuple(p[c] for c in cols) for p in janela]

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

    return op.main(["mm", *argv], abrir_livro=abrir_livro, abrir_erp=erp.abrir,
                   abrir_erp_bloqueante=erp.abrir)


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
    assert "TENIS X" in out and "gravar o produto de novo no ERP (duplicado)" in out
    assert _estado(livro)["estado"] == "incerto"
    erp = _Erp(janela=[_prod(4005, "OUTRO"), _prod(4006, "MAIS UM")])
    base = ("liberar", "c1", "1", "--operador", "ana", "--confirmar")
    assert _rodar(livro, erp, *base) == 1  # sem a lista conferida
    assert _rodar(livro, erp, *base, "--candidatos-conferidos", "4005") == 1  # lista diferente
    assert _estado(livro)["estado"] == "incerto"
    assert _rodar(livro, erp, *base, "--candidatos-conferidos", "4006, 4005") == 0
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


def test_seq_invalido_e_operador_ausente_sao_rejeitados(livro):
    with pytest.raises(SystemExit):
        _rodar(livro, _Erp(), "adotar", "c1", "1", "0", "--operador", "a")
    with pytest.raises(SystemExit):
        _rodar(livro, _Erp(), "adotar", "c1", "1", "4001")  # falta --operador


# ---- rodada de correção 1 ----

def test_adotar_recusa_seq_de_outra_linha_mesmo_orfao_fechado(livro, capsys):
    _linha(livro, "incerto")
    _linha(livro, "orfao_fechado", cid="c2", erp="4001")
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a", "--confirmar") == 1
    assert "outra linha" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto" and erp.aberturas == 0


def test_adotar_recusa_seq_de_outra_linha_por_seq_reservado(livro):
    _linha(livro, "incerto")
    _linha(livro, "transitorio", cid="c2", seq=4001)
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a", "--confirmar") == 1


def test_adotar_a_propria_linha_nao_conta_como_outra(livro):
    _linha(livro, "incerto", seq=4001)
    erp = _Erp({4001: _prod(4001)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a", "--confirmar") == 0


def test_adotar_recusa_seq_diferente_do_reservado(livro, capsys):
    _linha(livro, "incerto", seq=4001)
    erp = _Erp({4009: _prod(4009)})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4009", "--operador", "a", "--confirmar") == 1
    assert "reservou o SEQ 4001" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"


def test_adotar_recusa_produto_cadastrado_antes_da_reserva(livro, capsys):
    _linha(livro, "incerto")
    velho = _prod(17)
    velho["DATA_CAD"] = date(2019, 1, 1)
    assert _rodar(livro, _Erp({17: velho}), "adotar", "c1", "1", "17", "--operador", "a", "--confirmar") == 1
    assert "antes da reserva" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"


def test_adotar_recusa_data_cad_ilegivel(livro, capsys):
    _linha(livro, "incerto")
    p = _prod(17)
    p["DATA_CAD"] = None
    assert _rodar(livro, _Erp({17: p}), "adotar", "c1", "1", "17", "--operador", "a", "--confirmar") == 1
    assert "ilegível" in capsys.readouterr().err


def test_adotar_divergencia_marca_coluna_e_exige_aceite(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp({4001: _prod(4001, "NOME DIFERENTE")})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a") == 0
    out = capsys.readouterr().out
    assert "DIVERGE em DESCRICAO" in out and "<-- DIVERGE" in out
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a", "--confirmar") == 1
    assert "--aceito-divergencia" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"
    args = ("adotar", "c1", "1", "4001", "--operador", "a", "--confirmar", "--aceito-divergencia")
    assert _rodar(livro, erp, *args) == 0
    assert _estado(livro)["estado"] == "gravado_sem_volta"


def test_adotar_composicao_divergente_e_marcada(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp({4001: _prod(4001)}, kits={4001: [(10, 2.0)]})
    assert _rodar(livro, erp, "adotar", "c1", "1", "4001", "--operador", "a") == 0
    assert "DIVERGE em COMPOSICAO" in capsys.readouterr().out


def test_adotar_bate_mostra_veredito(livro, capsys):
    _linha(livro, "incerto")
    assert _rodar(livro, _Erp({4001: _prod(4001)}), "adotar", "c1", "1", "4001", "--operador", "a") == 0
    assert "BATE com o plano" in capsys.readouterr().out


def test_adotar_limpa_erro_e_tentativas(livro):
    _linha(livro, "incerto")
    livro.execute("UPDATE cadastro_volta SET erro_codigo = 'X', erro_mensagem = 'm', tentativas = 3")
    assert _rodar(livro, _Erp({4001: _prod(4001)}), "adotar", "c1", "1", "4001",
                  "--operador", "a", "--confirmar") == 0
    r = _estado(livro)
    assert (r["erro_codigo"], r["erro_mensagem"], r["tentativas"]) == (None, None, 0)


def test_liberar_candidato_com_mesmo_nome_e_marcado_e_nao_libera_sem_lista(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp(janela=[_prod(4005, "tenis x")])
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "a") == 0
    assert "MESMO NOME" in capsys.readouterr().out
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "a", "--confirmar") == 1
    assert _estado(livro)["estado"] == "incerto"
    assert repo.reservar(livro, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                         tipo="simples", modo="criar", plano_json="{}") is False


def test_liberar_janela_vazia_exige_nenhum_candidato(livro):
    _linha(livro, "incerto")
    base = ("liberar", "c1", "1", "--operador", "a", "--confirmar")
    assert _rodar(livro, _Erp(), *base) == 1
    assert _rodar(livro, _Erp(), *base, "--candidatos-conferidos", "4005") == 1
    assert _rodar(livro, _Erp(), *base, "--nenhum-candidato") == 0
    assert _estado(livro)["estado"] == "liberado"


def test_liberar_nenhum_candidato_com_janela_cheia_recusa(livro):
    _linha(livro, "incerto")
    erp = _Erp(janela=[_prod(4005, "OUTRO")])
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "a", "--confirmar",
                  "--nenhum-candidato") == 1
    assert _estado(livro)["estado"] == "incerto"


def test_liberar_recusa_se_a_janela_mudou_entre_a_leitura_e_a_liberacao(livro, capsys):
    _linha(livro, "incerto")
    erp = _Erp()
    erp.janelas = [[], [_prod(4005, "TENIS X")]]
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "a", "--confirmar",
                  "--nenhum-candidato") == 1
    assert "janela mudou" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"


def test_liberar_candidatos_lista_invalida(livro, capsys):
    _linha(livro, "incerto")
    assert _rodar(livro, _Erp(), "liberar", "c1", "1", "--operador", "a", "--confirmar",
                  "--candidatos-conferidos", "abc") == 1
    assert "lista de SEQs" in capsys.readouterr().err


@pytest.mark.parametrize("versao", ["4.0.1", "5.0.0", "WI-V", None])
def test_liberar_recusa_motor_4_ou_ilegivel(livro, capsys, versao):
    _linha(livro, "incerto", seq=4001)
    assert _rodar(livro, _Erp(versao=versao), "liberar", "c1", "1", "--operador", "a", "--confirmar") == 1
    assert "motor" in capsys.readouterr().err
    assert _estado(livro)["seq_reservado"] == 4001


def test_liberar_recusa_linha_recente(livro, capsys):
    _linha(livro, "incerto", seq=4001)
    livro.execute("UPDATE cadastro_volta SET iniciado_em = datetime('now')")
    assert _rodar(livro, _Erp(), "liberar", "c1", "1", "--operador", "a", "--confirmar") == 1
    assert "recente" in capsys.readouterr().err
    assert _estado(livro)["estado"] == "incerto"


def test_liberar_so_passa_select_pelo_guarda(livro):
    _linha(livro, "incerto", seq=4001)
    erp = _Erp()
    assert _rodar(livro, erp, "liberar", "c1", "1", "--operador", "a", "--confirmar") == 0
    assert erp.sql and all(q.lstrip().upper().startswith("SELECT") for q in erp.sql)


def test_commit_que_falha_diz_que_nada_foi_alterado(livro, capsys):
    _linha(livro, "orfao", erp="3992")
    livro.commit()

    class _Livro:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, n):
            return getattr(self.c, n)

        def commit(self):
            raise sqlite3.OperationalError("disco cheio /x/y")

        def rollback(self):
            self.c.rollback()

    @contextmanager
    def abrir_livro(slug):
        yield _Livro(livro)

    rc = op.main(["mm", "fechar-orfao", "c1", "1", "--operador", "a", "--nota", "n", "--confirmar"],
                 abrir_livro=abrir_livro, abrir_erp=_Erp().abrir, abrir_erp_bloqueante=_Erp().abrir)
    err = capsys.readouterr().err
    assert rc == 1 and "nada foi alterado" in err and "OperationalError" in err and "/x/y" not in err
    assert _estado(livro)["estado"] == "orfao"


def test_listar_inclui_liberado_com_seq_e_avisa_quando_corta(livro, capsys):
    _linha(livro, "liberado", cid="l1", seq=4001)
    _linha(livro, "liberado", cid="l2")  # sem SEQ: fora
    for i in range(op._LIMITE_LISTA + 3):
        _linha(livro, "incerto", cid=f"i{i:03d}")
    assert _rodar(livro, _Erp(), "listar") == 0
    out = capsys.readouterr().out
    assert "l1" in out and "l2" not in out
    assert f"mostrando {op._LIMITE_LISTA} de {op._LIMITE_LISTA + 3}" in out


@pytest.mark.parametrize("operador", ["", "   "])
def test_operador_vazio_e_rejeitado(livro, operador):
    _linha(livro, "orfao", erp="1")
    with pytest.raises(SystemExit):
        _rodar(livro, _Erp(), "fechar-orfao", "c1", "1", "--operador", operador, "--nota", "n")


def test_operador_e_aparado(livro):
    _linha(livro, "orfao", erp="1")
    assert _rodar(livro, _Erp(), "fechar-orfao", "c1", "1", "--operador", "  ana ", "--nota", "n",
                  "--confirmar") == 0
    assert _estado(livro)["alerta"] == "fechado_manual:ana"


def test_erro_inesperado_mostra_so_o_tipo_e_recusa_propria_mostra_a_mensagem(livro, capsys):
    assert _rodar(livro, _Erp(), "adotar", "zz", "1", "4001", "--operador", "a") == 1
    assert "não existe no livro" in capsys.readouterr().err


def test_erp_real_leitura_usa_somente_leitura_e_nunca_connect_with_config(monkeypatch):
    chamadas = []

    class _Fb:
        def connect_somente_leitura(self, cfg):
            chamadas.append("somente_leitura")
            return "cm"

        def connect_leitura_bloqueante(self, cfg):
            chamadas.append("bloqueante")
            return "cm"

        def connect_with_config(self, cfg):
            chamadas.append("with_config")
            raise AssertionError("escrita não é permitida nesta ferramenta")

    from app.erp import connection
    from app.persistence import environments_repo

    monkeypatch.setattr(connection, "FirebirdConnection", _Fb)
    monkeypatch.setattr(environments_repo, "get_by_slug", lambda slug: {"slug": slug})
    monkeypatch.setattr(environments_repo, "to_fb_config", lambda env: {"password": "SENHA"})
    assert op._erp_real("mm") == "cm"
    assert op._erp_bloqueante_real("mm") == "cm"
    assert chamadas == ["somente_leitura", "bloqueante"]


def test_erp_real_ambiente_inexistente_e_recusa_segura(monkeypatch):
    from app.persistence import environments_repo

    monkeypatch.setattr(environments_repo, "get_by_slug", lambda slug: None)
    with pytest.raises(op._RecusaError, match="ambiente não encontrado"):
        op._erp_real("zz")


def test_importar_o_modulo_nao_carrega_app_nem_fdb_nem_roda_main():
    import subprocess
    import sys
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent
    codigo = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import tools.cadastro_volta_operar as m; "
        "assert callable(m.main); "
        "assert not any(k == 'app' or k.startswith('app.') or k == 'fdb' for k in sys.modules)"
    )
    r = subprocess.run([sys.executable, "-I", "-c", codigo, str(raiz)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
