"""Gravador do `excluir` (app/erp/cadastro_fire.py) contra um Fire falso transacional."""

import pytest

from app.erp import cadastro_fire


class Cur:
    def __init__(self, fire):
        self.fire, self.rows, self.rowcount, self.description = fire, [], 0, None

    def execute(self, sql, params=()):
        self.fire.sqls.append((" ".join(sql.split()), tuple(params)))
        self.rows, self.rowcount, self.description = self.fire.responder(
            " ".join(sql.split()), tuple(params)
        )

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class Fire:
    """PRODUTOS {seq: dict}, PRODUTOS_KIT [dict]. `delete_kit_extra`: linhas a mais/menos que o
    DELETE reporta (simula um kit que mudou entre a leitura e o DELETE)."""

    def __init__(self, produtos, kit=(), delete_kit_extra=0, falha_delete_produto=None):
        self.produtos = {s: dict(p) for s, p in produtos.items()}
        self.kit = [dict(k) for k in kit]
        self.sqls, self.commits, self.rollbacks = [], 0, 0
        self.delete_kit_extra, self.falha_delete_produto = delete_kit_extra, falha_delete_produto
        self._foto = None

    def cursor(self):
        return Cur(self)

    def commit(self):
        self.commits += 1
        self._foto = None

    def rollback(self):
        self.rollbacks += 1
        if self._foto:
            self.produtos, self.kit = self._foto
            self._foto = None

    def _escreve(self):
        if self._foto is None:
            self._foto = (
                {s: dict(p) for s, p in self.produtos.items()},
                [dict(k) for k in self.kit],
            )

    def responder(self, sql, params):
        if sql == "SELECT * FROM PRODUTOS WHERE SEQ = ?":
            p = self.produtos.get(params[0])
            if p is None:
                return [], 0, [("SEQ",)]
            return [tuple(p.values())], 1, [(c,) for c in p]
        if sql == "SELECT * FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ? ORDER BY CODIGO":
            linhas = [k for k in self.kit if k["CODPRODUTO_PAI"] == params[0]]
            cols = [(c,) for c in (linhas[0] if linhas else {"CODIGO": 0})]
            return [tuple(k.values()) for k in linhas], len(linhas), cols
        if (
            sql
            == "SELECT CODPRODUTO_PAI FROM PRODUTOS_KIT WHERE CODPRODUTO = ? ORDER BY CODPRODUTO_PAI"
        ):
            return (
                [(k["CODPRODUTO_PAI"],) for k in self.kit if k["CODPRODUTO"] == params[0]],
                0,
                None,
            )
        if sql == "DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?":
            self._escreve()
            antes = len(self.kit)
            self.kit = [k for k in self.kit if k["CODPRODUTO_PAI"] != params[0]]
            return [], antes - len(self.kit) + self.delete_kit_extra, None
        if sql == "DELETE FROM PRODUTOS WHERE SEQ = ?":
            if self.falha_delete_produto:
                raise self.falha_delete_produto
            self._escreve()
            n = 1 if self.produtos.pop(params[0], None) is not None else 0
            return [], n, None
        raise AssertionError(f"SQL inesperado: {sql}")


KIT = [
    {"CODIGO": 90, "CODPRODUTO_PAI": 709219, "CODPRODUTO": 3170, "QTD": 3},
    {"CODIGO": 91, "CODPRODUTO_PAI": 709219, "CODPRODUTO": 3171, "QTD": 3},
]


def _fire(**kw):
    return Fire(
        {
            709219: {"SEQ": 709219, "DESCRICAO": "KIT X", "BLOQUEADO": "Nao"},
            3170: {"SEQ": 3170, "DESCRICAO": "PAR A", "BLOQUEADO": "Nao"},
        },
        KIT,
        **kw,
    )


def test_ler_linha_completa_e_kit():
    fire = _fire()
    assert cadastro_fire.ler_linha_completa(fire, 709219)["DESCRICAO"] == "KIT X"
    assert cadastro_fire.ler_linha_completa(fire, 1) is None
    assert [k["CODIGO"] for k in cadastro_fire.ler_kit_completo(fire, 709219)] == [90, 91]
    assert cadastro_fire.ler_kit_completo(fire, 3170) == []


def test_pais_do_componente():
    fire = _fire()
    assert cadastro_fire.pais_do_componente(fire, 3170) == [709219]
    assert cadastro_fire.pais_do_componente(fire, 709219) == []


def test_excluir_kit_apaga_kit_depois_produto_e_commita():
    fire = _fire()
    g = cadastro_fire.excluir(fire, seq=709219, linhas_kit=2)
    assert (g.seq, g.acao) == (709219, "excluir")
    assert 709219 not in fire.produtos and not [
        k for k in fire.kit if k["CODPRODUTO_PAI"] == 709219
    ]
    escritas = [s for s, _ in fire.sqls if s.startswith("DELETE")]
    assert escritas == [
        "DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?",
        "DELETE FROM PRODUTOS WHERE SEQ = ?",
    ]
    assert fire.commits == 1 and fire.rollbacks == 0


def test_excluir_par_sem_kit():
    fire = _fire()
    cadastro_fire.excluir(fire, seq=3170, linhas_kit=0)
    # sem linhas de kit esperadas, o DELETE em PRODUTOS_KIT nem é enviado
    assert [s for s, _ in fire.sqls if s.startswith("DELETE")] == [
        "DELETE FROM PRODUTOS WHERE SEQ = ?"
    ]
    assert 3170 not in fire.produtos and fire.commits == 1


def test_contagem_do_kit_diferente_desfaz_tudo():
    fire = _fire(delete_kit_extra=1)  # o DELETE reporta 3 em vez de 2
    with pytest.raises(cadastro_fire.ContagemDivergenteError):
        cadastro_fire.excluir(fire, seq=709219, linhas_kit=2)
    assert fire.commits == 0 and fire.rollbacks == 1
    assert 709219 in fire.produtos and len(fire.kit) == 2


def test_produto_que_sumiu_entre_a_leitura_e_o_delete_desfaz():
    fire = _fire()
    fire.produtos.pop(709219)
    with pytest.raises(cadastro_fire.ContagemDivergenteError):
        cadastro_fire.excluir(fire, seq=709219, linhas_kit=2)
    assert fire.commits == 0 and fire.rollbacks == 1 and len(fire.kit) == 2


def test_falha_do_delete_em_produtos_desfaz_o_kit_e_relanca():
    fire = _fire(falha_delete_produto=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        cadastro_fire.excluir(fire, seq=709219, linhas_kit=2)
    assert fire.rollbacks == 1 and fire.commits == 0
    assert 709219 in fire.produtos and len(fire.kit) == 2
