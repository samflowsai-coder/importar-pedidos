"""Gravador do cadastro no Fire: SQL parametrizado, uma transação por cadastro."""

from datetime import date

import pytest

from app.erp import cadastro_fire as cf
from app.erp.exceptions import FirebirdConnectionError


class _Cur:
    def __init__(self, fire):
        self.fire, self.rows, self.rowcount = fire, [], 0

    def execute(self, sql, params=()):
        self.fire.sqls.append((" ".join(sql.split()), tuple(params)))
        self.rows, self.rowcount = self.fire.responder(sql, tuple(params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Fire:
    def __init__(self, max_seq=3991, max_kit=900, colide=0, existe=True):
        self.max_seq, self.max_kit, self.colide, self.existe = max_seq, max_kit, colide, existe
        self.sqls, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def responder(self, sql, params):
        if "MAX(SEQ)" in sql:
            return [(self.max_seq,)], 1
        if "MAX(CODIGO)" in sql:
            self.max_kit += 1
            return [(self.max_kit - 1,)], 1
        if sql.lstrip().upper().startswith("INSERT INTO PRODUTOS ") and self.colide:
            self.colide -= 1
            self.max_seq += 1
            raise Exception("violation of PRIMARY or UNIQUE KEY constraint")
        if sql.lstrip().upper().startswith("SELECT SEQ FROM PRODUTOS"):
            return ([(params[0],)], 1) if self.existe else ([], 0)
        if sql.lstrip().upper().startswith("UPDATE"):
            return [], 1 if self.existe else 0
        return [], 1


CAMPOS = {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8, "MARCA": "Kolosh"}


def test_criar_reserva_antes_do_insert_e_commita():
    fire, reservas = _Fire(), []
    g = cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservas.append)
    assert g == cf.Gravacao(seq=3992, acao="criar")
    assert reservas == [3992]
    insert = [s for s in fire.sqls if s[0].startswith("INSERT INTO PRODUTOS ")][0]
    assert "?" in insert[0] and 3992 in insert[1] and "MEIA SOQUETE" in insert[1]
    assert fire.commits == 1 and fire.rollbacks == 0


def test_criar_colisao_tenta_de_novo_em_transacao_nova():
    fire, reservas = _Fire(colide=1), []
    g = cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservas.append)
    assert reservas == [3992, 3993] and g.seq == 3993
    assert fire.rollbacks == 1 and fire.commits == 1


def test_criar_desiste_depois_de_tres_colisoes():
    fire = _Fire(colide=3)
    with pytest.raises(cf.SeqTomadoError):
        cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    assert fire.rollbacks == 3 and fire.commits == 0


def test_criar_kit_grava_composicao_na_mesma_transacao():
    fire = _Fire()
    cf.criar(fire, campos=CAMPOS, componentes=[(3170, 3.0)], hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    kits = [s for s in fire.sqls if s[0].startswith("INSERT INTO PRODUTOS_KIT")]
    assert kits[0][1] == (901, 3992, 3170, 3.0)
    assert fire.commits == 1


def test_coluna_fora_da_lista_e_recusada_antes_do_banco():
    fire = _Fire()
    with pytest.raises(ValueError):
        cf.criar(fire, campos={**CAMPOS, "DESCRICAO2": "x"}, componentes=None,
                 hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    assert fire.sqls == []


def test_atualizar_sem_componentes_nao_toca_na_composicao():
    fire = _Fire()
    cf.atualizar(fire, seq=3990, campos={"UNIDADE": "KIT"}, componentes=None)
    assert not any("PRODUTOS_KIT" in s[0] for s in fire.sqls)
    assert fire.sqls[0] == ("UPDATE PRODUTOS SET UNIDADE = ? WHERE SEQ = ?", ("KIT", 3990))


def test_atualizar_troca_composicao_so_do_pai():
    fire = _Fire()
    cf.atualizar(fire, seq=3990, campos={}, componentes=[(3170, 2.0)])
    assert ("DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?", (3990,)) in fire.sqls


def test_atualizar_seq_inexistente():
    fire = _Fire(existe=False)
    with pytest.raises(LookupError):
        cf.atualizar(fire, seq=3990, campos={"UNIDADE": "KIT"}, componentes=None)
    assert fire.rollbacks == 1


def test_atualizar_none_limpa_a_coluna():
    fire = _Fire()
    cf.atualizar(fire, seq=3990, campos={"CODIGO_EAN13": None}, componentes=None)
    assert fire.sqls[0] == ("UPDATE PRODUTOS SET CODIGO_EAN13 = ? WHERE SEQ = ?", (None, 3990))


def test_inativar_so_bloqueia():
    fire = _Fire()
    cf.inativar(fire, seq=3990)
    assert fire.sqls == [("UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?", (3990,))]


@pytest.mark.parametrize(
    ("texto", "classe"),
    [
        ("violation of PRIMARY or UNIQUE KEY constraint", "colisao"),
        ("deadlock\nupdate conflicts with concurrent update", "lock"),
        ("Unable to complete network request to host", "conexao"),
        ('validation error for column DESCRICAO, value "*** null ***"', "recusa"),
    ],
)
def test_classificar_erro_pelo_texto_do_firebird(texto, classe):
    assert cf.classificar_erro(Exception(texto)) == classe


def test_classificar_erro_conexao_pelo_tipo():
    assert cf.classificar_erro(FirebirdConnectionError("sem rede")) == "conexao"


def test_nenhum_delete_em_produtos():
    import inspect
    fonte = inspect.getsource(cf)
    assert "DELETE FROM PRODUTOS " not in fonte and "DELETE FROM PRODUTOS\n" not in fonte
