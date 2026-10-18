"""Gravador do cadastro no Fire: SQL parametrizado, uma transação por cadastro."""

import re
import sqlite3
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
    def __init__(self, max_seq=3991, max_kit=900, colide=0, existe=True, colide_kit=0):
        self.max_seq, self.max_kit, self.colide, self.existe = max_seq, max_kit, colide, existe
        self.colide_kit = colide_kit
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
        if sql.lstrip().upper().startswith("INSERT INTO PRODUTOS_KIT") and self.colide_kit:
            self.colide_kit -= 1
            raise Exception("violation of PRIMARY or UNIQUE KEY constraint")
        if sql.lstrip().upper().startswith("SELECT SEQ FROM PRODUTOS"):
            return ([(params[0],)], 1) if self.existe else ([], 0)
        if sql.lstrip().upper().startswith("UPDATE"):
            return [], 1 if self.existe else 0
        return [], 1


CAMPOS = {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8, "MARCA": "Kolosh"}


def test_criar_reserva_antes_do_insert_e_commita():
    fire, reservas, vistos = _Fire(), [], []

    def reservar(seq):
        reservas.append(seq)
        vistos.append([q for q, _ in fire.sqls])

    g = cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservar)
    assert g == cf.Gravacao(seq=3992, acao="criar")
    assert reservas == [3992]
    assert len(vistos[0]) == 1 and "MAX(SEQ)" in vistos[0][0]
    insert = [s for s in fire.sqls if s[0].startswith("INSERT INTO PRODUTOS ")][0]
    assert "?" in insert[0] and 3992 in insert[1] and "MEIA SOQUETE" in insert[1]
    assert fire.commits == 1 and fire.rollbacks == 0


def test_criar_colisao_tenta_de_novo_em_transacao_nova():
    fire, reservas, antes = _Fire(colide=1), [], []

    def reservar(seq):
        reservas.append(seq)
        antes.append(len(fire.sqls))

    g = cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservar)
    assert reservas == [3992, 3993] and g.seq == 3993
    # 1a tentativa: MAX; falha no INSERT (2 sqls). 2a: só o MAX da tentativa novo antes de reservar.
    assert antes == [1, 3]
    assert "MAX(SEQ)" in fire.sqls[antes[1] - 1][0]
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
    assert fire.commits == 1 and fire.rollbacks == 0


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
    assert fire.commits == 1 and fire.rollbacks == 0


def test_inativar_seq_inexistente():
    fire = _Fire(existe=False)
    with pytest.raises(LookupError):
        cf.inativar(fire, seq=3990)
    assert fire.commits == 0 and fire.rollbacks == 1


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
    assert not re.search(r'(?i)delete\s+from\s+"?produtos"?\b(?!_)', fonte)
    assert re.search(r"(?i)delete\s+from\s+produtos_kit", fonte)  # a regex enxerga o que deve


class _DbError(Exception):
    """Como o fdb monta: DatabaseError(msg, sqlcode, gdscode)."""


def _fdb(sqlcode, gds, extra="x"):
    return _DbError(f"Error while executing SQL statement:\n- SQLCODE: {sqlcode}\n- {extra}", sqlcode, gds)


@pytest.mark.parametrize(
    ("gds", "classe"),
    [
        (335544665, "colisao"), (335544349, "colisao"),
        (335544336, "lock"), (335544345, "lock"), (335544451, "lock"), (335544510, "lock"),
        (335544721, "conexao"), (335544726, "conexao"), (335544727, "conexao"), (335544741, "conexao"),
        (335544466, "recusa"),
    ],
)
def test_classificar_erro_por_gdscode(gds, classe):
    assert cf.classificar_erro(_fdb(-999, gds)) == classe


@pytest.mark.parametrize(
    ("sqlcode", "classe"), [(-803, "colisao"), (-913, "lock"), (-902, "conexao"), (-530, "recusa")]
)
def test_classificar_erro_por_sqlcode_quando_gdscode_nao_decide(sqlcode, classe):
    assert cf.classificar_erro(_fdb(sqlcode, 1)) == classe


def test_fk_com_valor_unique_na_mensagem_e_recusa():
    exc = _fdb(-530, 335544466, "violation of FOREIGN KEY constraint 'UNIQUE connection'")
    assert cf.classificar_erro(exc) == "recusa"


def test_erro_de_conversao_com_valor_unique_connection_e_recusa():
    exc = _fdb(-303, 335544334, 'conversion error from string "UNIQUE connection"')
    assert cf.classificar_erro(exc) == "recusa"
    assert cf.classificar_erro(Exception('conversion error from string "UNIQUE connection"')) == "recusa"


def test_texto_misto_conexao_antes_de_lock_e_palavra_solta_nao_conta():
    assert cf.classificar_erro(Exception("deadlock\nunable to complete network request")) == "conexao"
    assert cf.classificar_erro(Exception("connection lost")) == "conexao"
    assert cf.classificar_erro(Exception("attempt to store duplicate value")) == "colisao"
    assert cf.classificar_erro(Exception("valor 'unique' na coluna")) == "recusa"


def test_atualizar_com_composicao_repete_em_colisao_e_commita_uma_vez():
    fire = _Fire(colide_kit=1)
    g = cf.atualizar(fire, seq=3990, campos={}, componentes=[(3170, 2.0)])
    assert g.seq == 3990 and fire.commits == 1 and fire.rollbacks == 1


def test_atualizar_com_composicao_esgota_tentativas():
    fire = _Fire(colide_kit=3)
    with pytest.raises(cf.SeqTomadoError):
        cf.atualizar(fire, seq=3990, campos={}, componentes=[(3170, 2.0)])
    assert fire.commits == 0 and fire.rollbacks == 3


def test_atualizar_campos_vazios_seq_inexistente():
    fire = _Fire(existe=False)
    with pytest.raises(LookupError):
        cf.atualizar(fire, seq=3990, campos={}, componentes=None)
    assert fire.commits == 0 and fire.rollbacks == 1


def test_kit_com_dois_componentes_na_mesma_transacao():
    fire = _Fire()
    cf.criar(fire, campos=CAMPOS, componentes=[(3170, 1.0), (3171, 2.0)],
             hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    kits = [p for q, p in fire.sqls if q.startswith("INSERT INTO PRODUTOS_KIT")]
    assert kits == [(901, 3992, 3170, 1.0), (902, 3992, 3171, 2.0)]
    assert fire.commits == 1 and fire.rollbacks == 0


def test_falha_do_livro_nao_e_classificada_nem_repetida():
    fire, chamadas = _Fire(), []

    def reservar(seq):
        chamadas.append(seq)
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    with pytest.raises(cf.ReservaFalhouError) as ei:
        cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservar)
    assert isinstance(ei.value.__cause__, sqlite3.IntegrityError)
    assert chamadas == [3992] and fire.rollbacks == 1 and fire.commits == 0


def test_rollback_que_falha_nao_mascara_a_excecao_original():
    fire = _Fire(existe=False)

    def quebra():
        raise RuntimeError("rollback quebrou")

    fire.rollback = quebra
    with pytest.raises(LookupError):
        cf.inativar(fire, seq=3990)


def test_padroes_p0_validos():
    cf._valida_padroes(cf.PADROES_P0)
    for ruim in ({"seq": 1}, {"SEQ": 1}, {"DATA_CAD": 1}, {"DESCRICAO": "x"}):
        with pytest.raises(ValueError):
            cf._valida_padroes(ruim)
