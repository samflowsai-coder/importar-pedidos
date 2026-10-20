"""Regressão da chave exata: guarda de leitura, ligação ao pedido do Fire e
comparação item a item. Sem rede e sem Firebird: tudo com fakes."""

from __future__ import annotations

import pytest

from app.models.order import Order, OrderHeader, OrderItem
from tools import regressao_chave_exata as rg

CNPJ = "11222333000181"


def _order(numero="AF049", cnpj=CNPJ, n=2, entrega=None):
    return Order(
        header=OrderHeader(order_number=numero, customer_cnpj=cnpj, customer_name="X"),
        items=[OrderItem(product_code=f"C{i}", delivery_cnpj=entrega) for i in range(n)],
    )


def _check(*estados):
    """estados: 'ok:2077' ou 'pendente'."""
    itens = []
    for e in estados:
        if e.startswith("ok:"):
            itens.append({"produto": {"estado": "ok", "fire_seq": int(e[3:])}})
        else:
            itens.append({"produto": {"estado": e, "fire_seq": None}})
    return {"available": True, "items": itens}


# ── guarda de leitura ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "sql",
    ["SELECT 1 FROM RDB$DATABASE", "  select * from x", "WITH a AS (SELECT 1) SELECT * FROM a"],
)
def test_guarda_deixa_select_e_with(sql):
    assert rg.so_leitura(sql) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE CAB_VENDAS SET STATUS = 'X'",
        "DELETE FROM CAB_VENDAS",
        "INSERT INTO X VALUES (1)",
        "EXECUTE PROCEDURE P",
        "SELECT 1; DELETE FROM X",
        "",
    ],
)
def test_guarda_recusa_o_resto(sql):
    with pytest.raises(RuntimeError):
        rg.so_leitura(sql)


def test_cursor_so_leitura_nao_executa_escrita():
    executados = []

    class Cur:
        def execute(self, sql, params=()):
            executados.append(sql)

        def fetchall(self):
            return []

        def close(self):
            pass

    cur = rg.CursorSoLeitura(Cur())
    cur.execute("SELECT 1 FROM RDB$DATABASE")
    with pytest.raises(RuntimeError):
        cur.execute("DELETE FROM X")
    assert executados == ["SELECT 1 FROM RDB$DATABASE"]


# ── comparação ───────────────────────────────────────────────────────────────
def test_comparar_igual_diferente_e_pendente():
    r = rg.comparar_itens(_check("ok:10", "ok:20", "sem_candidato")["items"], [10, 99, 30])
    assert [x["status"] for x in r] == ["ok_igual", "ok_seq_diferente", "pendente"]
    assert r[1]["seq_decidido"] == 20 and r[1]["seq_fire"] == 99


def test_comparar_ordem_diferente_com_mesma_contagem_acha_no_conjunto():
    r = rg.comparar_itens(_check("ok:10", "ok:20")["items"], [20, 10])
    assert [x["status"] for x in r] == ["ok_igual", "ok_igual"]


def test_comparar_mesmo_seq_duas_vezes_consome_uma_por_vez():
    r = rg.comparar_itens(_check("ok:10", "ok:10")["items"], [10, 77])
    assert [x["status"] for x in r] == ["ok_igual", "ok_seq_diferente"]


def test_comparar_contagem_diferente_usa_conjunto():
    r = rg.comparar_itens(_check("ok:10", "ok:20")["items"], [10])
    assert [x["status"] for x in r] == ["ok_igual", "ok_seq_diferente"]
    assert r[0]["pareamento"] == "conjunto"


def test_comparar_pendente_nunca_consome_o_seq_do_fire():
    r = rg.comparar_itens(_check("pendente", "ok:10")["items"], [10, 5])
    assert [x["status"] for x in r] == ["pendente", "ok_igual"]


# ── ligação ao pedido do Fire ────────────────────────────────────────────────
def _linha(numero, codigo, status, data, cli_codigo, cnpj, mecanico=99):
    return (numero, codigo, status, data, cli_codigo, cnpj, mecanico)


def test_ligar_exige_mecanico_99_e_cliente():
    cand = rg.candidato_do_pedido(_order(), data_pedido="2026-10-19")
    linhas = [
        _linha("AF049", 1, "PEDIDO", "2026-10-19", 5, "11.222.333/0001-81", mecanico=None),
        _linha("AF049", 2, "PEDIDO", "2026-10-19", 6, "99.999.999/0001-99", mecanico=99),
        _linha("AF049", 3, "PEDIDO", "2026-10-19", 5, "11.222.333/0001-81", mecanico=99),
    ]
    assert rg.ligar_pedido(cand, linhas) == 3


def test_ligar_sem_mecanico_99_nao_liga():
    cand = rg.candidato_do_pedido(_order(), data_pedido="2026-10-19")
    linhas = [_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, "11222333000181", mecanico=0)]
    assert rg.ligar_pedido(cand, linhas) is None


def test_ligar_mecanico_como_texto():
    cand = rg.candidato_do_pedido(_order(), data_pedido="2026-10-19")
    linhas = [_linha("AF049", 4, "PEDIDO", "2026-10-19", 5, "11222333000181", mecanico="99")]
    assert rg.ligar_pedido(cand, linhas) == 4


def test_ligar_janela_descarta_pedido_antigo():
    cand = rg.candidato_do_pedido(_order(), data_pedido="2026-10-19")
    linhas = [_linha("AF049", 1, "PEDIDO", "2025-01-10", 5, "11222333000181")]
    assert rg.ligar_pedido(cand, linhas) is None


def test_ligar_pedido_sem_cnpj_no_header_usa_todas_as_lojas():
    o = _order(cnpj=None, entrega=CNPJ)
    cand = rg.candidato_do_pedido(o, data_pedido=None)
    assert cand.cnpjs_entrega == (CNPJ,)
    assert rg.ligar_pedido(cand, [_linha("AF049", 8, "PEDIDO", None, 5, CNPJ)]) == 8


# ── execução completa com fakes ──────────────────────────────────────────────
class FakeFire:
    def __init__(self, linhas, itens):
        self._linhas, self._itens = linhas, itens

    def pedidos_por_numero(self, numeros):
        return [linha for linha in self._linhas if linha[0] in numeros]

    def seqs_do_pedido(self, codigo):
        return self._itens[codigo]


def test_executar_conta_cada_balde_e_lista_os_perigosos():
    fire = FakeFire(
        [_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)],
        {1: [10, 99]},
    )
    pedidos = {
        "a.pdf": _order("AF049"),
        "b.pdf": _order("ZZ999"),  # não está no Fire
        "c.pdf": None,  # não parseou
    }

    out = rg.executar(
        pedidos,
        checar=lambda o: _check("ok:10", "ok:20") if o.header.order_number == "AF049"
        else _check("ok:1", "pendente"),
        fire=fire,
        datas={},
    )

    t = out["totais"]
    assert t["ok_igual"] == 1
    assert t["ok_seq_diferente"] == 1
    assert t["sem_pedido_fire"] == 2
    assert t["sem_parse"] == 1
    assert [p["arquivo"] for p in out["perigosos"]] == ["a.pdf"]
    assert out["perigosos"][0]["seq_decidido"] == 20


def test_executar_check_indisponivel_nao_vira_pendente_em_silencio():
    out = rg.executar(
        {"a.pdf": _order()},
        checar=lambda o: {"available": False, "reason": "X", "items": []},
        fire=FakeFire([], {}),
        datas={},
    )
    assert out["totais"]["erro_check"] == 1
    assert out["totais"]["pendente"] == 0
    assert out["ok"] is False


# ── costuras ─────────────────────────────────────────────────────────────────
def test_costuras_sao_so_leitura_e_restauram_ao_sair(tmp_path):
    import sqlite3
    from pathlib import Path

    from app import pipeline
    from app.erp.connection import FirebirdConnection
    from app.persistence import router

    db = tmp_path / "app_state_mm.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (x)")
    c.commit()
    c.close()

    class Conn:
        def __init__(self):
            self.rollbacks = 0
            self.fechou = False

        def cursor(self):
            raise AssertionError("não deveria abrir cursor")

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.fechou = True

    bruta = Conn()
    fire = rg.ConexaoSoLeitura(bruta)
    original_env_connect = router.env_connect

    with rg._costuras(db_path=Path(db), fire_conn=fire):
        with router.env_connect("mm") as sconn, pytest.raises(sqlite3.OperationalError):
            sconn.execute("INSERT INTO t VALUES (1)")
        assert pipeline._llm.parse(None) is None
        with FirebirdConnection().connect_with_config({"path": "x"}) as conn:
            assert conn is fire
        assert not bruta.fechou  # o check não fecha a conexão única
        with pytest.raises(RuntimeError):
            fire.commit()

    assert router.env_connect is original_env_connect
    with fire:
        pass
    assert bruta.fechou and bruta.rollbacks == 1


def test_check_order_roda_pelas_costuras_e_todo_sql_passa_na_guarda(tmp_path, monkeypatch):
    from pathlib import Path

    from app.erp import product_check
    from app.persistence import router

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm"):
        pass
    vistos: list[str] = []

    class Cur:
        def execute(self, sql, params=()):
            vistos.append(sql)

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cur()

        def rollback(self):
            pass

        def close(self):
            pass

    order = _order(n=1)
    with rg._costuras(db_path=Path(tmp_path / "app_state_mm.db"), fire_conn=rg.ConexaoSoLeitura(Conn())):
        check = product_check.check_order(order, env={"slug": "mm"})

    assert check["available"] is True
    assert vistos  # o check conversou com o Fire fake, e a guarda deixou passar
    assert check["items"][0]["produto"]["estado"] != "fire_indisponivel"
