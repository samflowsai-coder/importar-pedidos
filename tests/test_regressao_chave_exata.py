"""Regressão da chave exata: guarda de leitura, ligação ao pedido do Fire e
comparação item a item. Sem rede e sem Firebird: tudo com fakes."""

from __future__ import annotations

from datetime import datetime

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
    [
        "SELECT 1 FROM RDB$DATABASE",
        "  select * from x",
        "WITH a AS (SELECT 1) SELECT * FROM a",
        # palavra proibida só dentro de literal/comentário/identificador não conta
        "SELECT 'UPDATE; DELETE' FROM X WHERE A = 'GEN_ID('",
        "SELECT 1 FROM X -- DELETE\n",
        'SELECT "SET" FROM X',
        "SELECT DT_UPDATE FROM X WHERE ID IN (1, 2)",
        "SELECT TRIM(A) FROM X I WHERE I.B IN (?, ?)",
    ],
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
        # sondas do review
        "SELECT GEN_ID(GEN_CORPO_VENDAS_CODIGO, 1) FROM RDB$DATABASE",
        "SELECT NEXT VALUE FOR GEN_X FROM RDB$DATABASE",
        "WITH a AS (SELECT 1 FROM RDB$DATABASE) DELETE FROM CAB_VENDAS",
        "SELECT * FROM CAB_VENDAS FOR UPDATE WITH LOCK",
        "SELECT * FROM CAB_VENDAS WITH LOCK",
        "SELECT * FROM SP_ESCREVE(1)",
        "SELECT * FROM sp_escreve (1, 2)",
        "SELECT * FROM A JOIN sp_escreve(1) S ON 1 = 1",
        "/* SELECT */ DELETE FROM CAB_VENDAS",
        "-- SELECT\nDELETE FROM CAB_VENDAS",
        "SELECT 1 /* x */ ; UPDATE X SET A = 1",
        "SELECT 'aberto",
        "MERGE INTO X USING Y ON 1 = 1 WHEN MATCHED THEN DELETE",
        "SET TRANSACTION READ WRITE",
        "ALTER TABLE X ADD Y INT",
        "CREATE TABLE X (A INT)",
        "DROP TABLE X",
        "GRANT ALL ON X TO Y",
        "select 1 from x where 1=1 and exists (execute procedure p)",
    ],
)
def test_guarda_recusa_o_resto(sql):
    with pytest.raises(RuntimeError):
        rg.so_leitura(sql)


def test_guarda_aceita_todo_sql_que_o_check_usa():
    from app.erp import queries

    so = rg.so_leitura
    so(queries.FIND_CLIENT_BY_CNPJ)
    for nome in dir(queries):
        f = getattr(queries, nome)
        if callable(f) and nome.endswith("_sql"):
            so(f(2))
    so(rg.sql_pedidos_por_numero(3))
    so(queries.itens_erp_por_pedido_sql(1))


def test_cursor_so_expõe_a_lista_de_atributos_permitidos():
    class Cur:
        description = [("A",)]
        rowcount = 1
        transaction = object()

        def prep(self, sql):  # pragma: no cover - não deve ser alcançado
            raise AssertionError

        def callproc(self, *a):  # pragma: no cover
            raise AssertionError

        def fetchone(self):
            return (1,)

        def fetchmany(self, n=1):
            return [(1,)]

        def fetchall(self):
            return [(1,)]

        def close(self):
            pass

    cur = rg.CursorSoLeitura(Cur())
    assert cur.fetchone() == (1,) and cur.fetchall() == [(1,)] and cur.fetchmany(1) == [(1,)]
    assert cur.description == [("A",)] and cur.rowcount == 1
    for proibido in ("prep", "transaction", "callproc", "executemany", "_cur_inexistente"):
        with pytest.raises(AttributeError):
            getattr(cur, proibido)
    cur.close()


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


def test_comparar_ordem_diferente_com_mesma_contagem_e_suspeito():
    r = rg.comparar_itens(_check("ok:10", "ok:20")["items"], [20, 10])
    assert [x["status"] for x in r] == ["suspeito", "suspeito"]


def test_comparar_mesmo_seq_duas_vezes_consome_uma_por_vez():
    r = rg.comparar_itens(_check("ok:10", "ok:10")["items"], [10, 77])
    assert [x["status"] for x in r] == ["ok_igual", "ok_seq_diferente"]


def test_comparar_contagem_diferente_usa_conjunto_e_diz_isso():
    r = rg.comparar_itens(_check("ok:10", "ok:20")["items"], [10])
    assert [x["status"] for x in r] == ["ok_igual_conjunto", "ok_seq_diferente"]
    assert r[0]["pareamento"] == "conjunto"
    r = rg.comparar_itens(_check("ok:20", "ok:10")["items"], [10])
    assert [x["status"] for x in r] == ["ok_seq_diferente", "ok_igual_conjunto"]
    assert r[1]["pareamento"] == "conjunto"


def test_comparar_pendente_nunca_consome_o_seq_do_fire():
    r = rg.comparar_itens(_check("pendente", "ok:10")["items"], [5, 10])
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


IMP = {"imported_at": "2026-10-19T10:00:00", "order_number": None, "customer_cnpj": None}


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
        importacoes={"a.pdf": IMP, "b.pdf": IMP},
    )

    t = out["totais"]
    assert t["ok_igual"] == 1
    assert t["ok_seq_diferente"] == 1
    assert t["sem_pedido_fire"] == 2
    assert t["sem_parse"] == 1
    assert [p["arquivo"] for p in out["perigosos"]] == ["a.pdf"]
    assert out["perigosos"][0]["seq_decidido"] == 20
    assert out["ok"] is False


def test_executar_check_indisponivel_nao_vira_pendente_em_silencio():
    out = rg.executar(
        {"a.pdf": _order()},
        checar=lambda o: {"available": False, "reason": "X", "items": []},
        fire=FakeFire([], {}),
        importacoes={"a.pdf": IMP},
    )
    assert out["totais"]["erro_check"] == 1
    assert out["totais"]["pendente"] == 0
    assert out["ok"] is False


def test_executar_conferencia_incompleta_e_erro_check():
    def checar(o):
        c = _check("ok:10", "ok:20")
        c["produto_conferencia_incompleta"] = True
        return c

    out = rg.executar(
        {"a.pdf": _order()},
        checar=checar,
        fire=FakeFire([_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20]}),
        importacoes={"a.pdf": IMP},
    )
    assert out["totais"]["erro_check"] == 1
    assert out["totais"]["ok_igual"] == 0
    assert out["arquivos"][0]["motivo"] == "conferencia_incompleta"
    assert out["ok"] is False


def test_executar_sem_data_nao_liga_ao_fire():
    fire = FakeFire([_linha("AF049", 1, "PEDIDO", "2024-01-01", 5, CNPJ)], {1: [10, 20]})

    def sem_consulta(numeros):
        raise AssertionError("sem data não pode consultar o Fire")

    fire.pedidos_por_numero = sem_consulta
    out = rg.executar(
        {"a.pdf": _order(), "b.pdf": _order()},
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={"b.pdf": {"imported_at": "", "order_number": None, "customer_cnpj": None}},
    )
    assert out["totais"]["sem_data"] == 4
    assert out["totais"]["ok_igual"] == 0
    assert {a["resultado"] for a in out["arquivos"]} == {"sem_data"}


def test_executar_usa_numero_e_cnpj_da_importacao_e_reporta_conjunto_e_suspeito():
    fire = FakeFire(
        [_linha("AF999", 7, "PEDIDO", "2026-10-19", 5, "55666777000188")],
        {7: [20, 10]},
    )
    imp = {"imported_at": "2026-10-19T10:00:00", "order_number": "AF999",
           "customer_cnpj": "55666777000188"}
    out = rg.executar(
        {"a.pdf": _order("AF049")},  # o re-parse diz AF049/CNPJ; o que foi pro Fire é AF999
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={"a.pdf": imp},
    )
    assert out["totais"]["suspeito"] == 2
    assert len(out["suspeitos"]) == 2
    assert out["ok"] is False  # suspeito derruba o exit


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


# ── leitura recursiva e mapeamento arquivo -> importação ─────────────────────
def _guardar(base, rel, conteudo=b"x"):
    caminho = base / rel
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(conteudo)
    return caminho


def test_nome_original_tira_o_prefixo_de_guarda():
    assert rg.nome_original("20260909-120641_d75720d155b7_AF090.xlsx") == "AF090.xlsx"
    assert rg.nome_original("AF090.xlsx") == "AF090.xlsx"


def test_reler_arquivos_varre_subpastas_e_chama_o_pipeline_com_o_nome_original(
    tmp_path, monkeypatch
):
    from app import pipeline

    _guardar(tmp_path, "2026/09/20260909-120641_d75720d155b7_AF090.xlsx", b"a")
    _guardar(tmp_path, "2026/10/20261001-090000_aaaaaaaaaaaa_PED 1.pdf", b"b")
    _guardar(tmp_path, "2026/10/leiame.txt", b"c")  # ignorado
    vistos = []

    def fake_process(arq):
        vistos.append((arq.path.name, arq.extension, arq.raw))
        return _order("AF090")

    monkeypatch.setattr(pipeline, "process", fake_process)

    relidos = rg.reler_arquivos(str(tmp_path))

    assert [r.chave for r in relidos] == [
        "2026/09/20260909-120641_d75720d155b7_AF090.xlsx",
        "2026/10/20261001-090000_aaaaaaaaaaaa_PED 1.pdf",
    ]
    assert vistos == [("AF090.xlsx", ".xlsx", b"a"), ("PED 1.pdf", ".pdf", b"b")]
    assert relidos[0].guardado == "20260909-120641_d75720d155b7_AF090.xlsx"
    assert len(relidos[0].sha256) == 64


def test_reler_arquivos_erro_num_arquivo_nao_derruba(tmp_path, monkeypatch):
    from app import pipeline

    _guardar(tmp_path, "2026/09/20260909-120641_d75720d155b7_A.xlsx")

    def explode(arq):
        raise ValueError("ruim")

    monkeypatch.setattr(pipeline, "process", explode)
    assert [r.order for r in rg.reler_arquivos(str(tmp_path))] == [None]


def _db_imports(tmp_path, linhas, nome="app_state_mm.db"):
    import sqlite3

    db = tmp_path / nome
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE imports (original_path TEXT, file_sha256 TEXT, imported_at TEXT, "
        "order_number TEXT, customer_cnpj TEXT, source_filename TEXT, "
        "portal_status TEXT DEFAULT 'found_in_fire', fire_codigo INTEGER, "
        "output_files_json TEXT)"
    )
    c.executemany(
        "INSERT INTO imports (original_path, file_sha256, imported_at, order_number, "
        "customer_cnpj, source_filename) VALUES (?, ?, ?, ?, ?, ?)",
        linhas,
    )
    c.commit()
    c.close()
    return db


def test_importacoes_casam_pelo_basename_do_caminho_windows_nao_pelo_nome_original(tmp_path):
    import hashlib

    guardado = "20260909-120641_d75720d155b7_AF090.xlsx"
    db = _db_imports(
        tmp_path,
        [
            # dois pedidos com o MESMO nome original; só o caminho guardado distingue
            ("C:\\SamFlowsAI\\data\\recebidos\\mm\\2026\\09\\" + guardado, None,
             "2026-09-09T12:06:41", "AF090", CNPJ, "AF090.xlsx"),
            ("C:\\SamFlowsAI\\data\\recebidos\\mm\\2026\\10\\20261001-000000_bbbbbbbbbbbb_AF090.xlsx",
             None, "2026-10-01T00:00:00", "OUTRO", CNPJ, "AF090.xlsx"),
        ],
    )
    relido = rg.Relido("2026/09/" + guardado, guardado, hashlib.sha256(b"a").hexdigest(), None)

    out = rg.importacoes_dos_arquivos(db, [relido])

    assert out[relido.chave]["imported_at"] == "2026-09-09T12:06:41"
    assert out[relido.chave]["order_number"] == "AF090"


def test_importacoes_caem_para_o_sha256_e_posix(tmp_path):
    import hashlib

    sha = hashlib.sha256(b"conteudo").hexdigest()
    db = _db_imports(
        tmp_path,
        [("/dados/recebidos/mm/outro-nome.xlsx", sha.upper(), "2026-09-09T12:00:00", "N1",
          None, "x.xlsx")],
    )
    relido = rg.Relido("a/b.xlsx", "20260909-120000_cccccccccccc_x.xlsx", sha, None)
    nao = rg.Relido("c.xlsx", "20260909-120000_dddddddddddd_y.xlsx", "0" * 64, None)

    out = rg.importacoes_dos_arquivos(db, [relido, nao])

    assert out["a/b.xlsx"]["imported_at"] == "2026-09-09T12:00:00"
    assert out["a/b.xlsx"]["mapeamento"]["por"] == "sha256"
    # o que não casa vem SEM imported_at (vira sem_data), com o diagnóstico do que foi tentado
    assert "imported_at" not in out["c.xlsx"]
    assert out["c.xlsx"]["mapeamento"]["por"] is None
    assert out["c.xlsx"]["mapeamento"]["bancos"] == [{"db": "app_state_mm.db", "importacoes": 1}]


# ── ordem de gravação (agrupada por loja) e pedidos de várias lojas ──────────
LOJA_A = "22333444000155"
LOJA_B = "33444555000166"


def _order_lojas(entregas=(LOJA_A, LOJA_B, LOJA_A), numero="AF049"):
    o = Order(
        header=OrderHeader(order_number=numero, customer_cnpj=CNPJ, customer_name="X"),
        items=[
            OrderItem(product_code=f"C{i}", delivery_cnpj=e, quantity=1.0)
            for i, e in enumerate(entregas)
        ],
    )
    return o


def test_ordem_de_insercao_agrupa_por_loja_como_o_exportador():
    assert rg.ordem_de_insercao(_order_lojas()) == [0, 2, 1]
    assert rg.posicoes_por_planilha(_order_lojas()) == [[0, 2], [1]]


def test_comparar_pedido_intercalado_a_b_a_no_insert_direto_nao_e_suspeito():
    o = _order_lojas()
    # decidido por item (ordem do pedido): 10, 20, 30. O Fire grava A, A, B = itens 0, 2, 1.
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire(
        [_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 30, 20]}
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [(i["indice"], i["status"]) for i in itens] == [
        (0, "ok_igual"), (1, "ok_igual"), (2, "ok_igual")
    ]
    assert {i["fire_codigo"] for i in itens} == {1}


def test_comparar_pedido_intercalado_com_ordem_errada_de_verdade_e_suspeito():
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire(
        [_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20, 30]}
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [i["status"] for i in itens] == ["ok_igual", "suspeito", "suspeito"]


def test_comparar_pedido_planilhas_por_loja_compara_so_os_itens_da_loja():
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    # um pedido do Fire por loja: A (itens 0 e 2) e B (item 1), CNPJ do cliente = a loja
    fire = FakeFire(
        [
            _linha("AF049", 11, "PEDIDO", "2026-10-19", 5, "22.333.444/0001-55"),
            _linha("AF049", 12, "PEDIDO", "2026-10-19", 6, "33.444.555/0001-66"),
        ],
        {11: [10, 30], 12: [20]},
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [(i["indice"], i["status"], i["fire_codigo"]) for i in itens] == [
        (0, "ok_igual", 11), (1, "ok_igual", 12), (2, "ok_igual", 11)
    ]


def test_comparar_pedido_planilhas_por_loja_seq_trocado_entre_lojas_e_perigoso():
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire(
        [
            _linha("AF049", 11, "PEDIDO", "2026-10-19", 5, LOJA_A),
            _linha("AF049", 12, "PEDIDO", "2026-10-19", 6, LOJA_B),
        ],
        {11: [10, 99], 12: [20]},
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [i["status"] for i in itens] == ["ok_igual", "ok_igual", "ok_seq_diferente"]


def test_comparar_pedido_planilhas_loja_sem_pedido_no_fire():
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire([_linha("AF049", 11, "PEDIDO", "2026-10-19", 5, LOJA_A)], {11: [10, 30]})
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [i["status"] for i in itens] == ["ok_igual", "sem_pedido_fire", "ok_igual"]


def test_comparar_pedido_pareamento_ambiguo_nao_compara():
    # duas planilhas (split por EAN de entrega) do mesmo CNPJ e do mesmo tamanho
    o = _order_lojas(entregas=(LOJA_A, LOJA_A))
    o.items[0].delivery_ean = "789000000001"
    o.items[1].delivery_ean = "789000000002"
    check = _check("ok:10", "ok:20")["items"]
    fire = FakeFire(
        [
            _linha("AF049", 11, "PEDIDO", "2026-10-19", 5, LOJA_A),
            _linha("AF049", 12, "PEDIDO", "2026-10-19", 5, LOJA_A),
        ],
        {11: [10], 12: [20]},
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [i["status"] for i in itens] == ["ambiguo", "ambiguo"]
    assert itens[0]["candidatos_fire"] == [11, 12]


# ── cobertura: nada comparado nunca parece sucesso ───────────────────────────
def test_cobertura_zero_comparados_falha_com_mensagem():
    out = rg.executar(
        {"a.pdf": _order(), "b.pdf": _order()},
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=FakeFire([], {}),
        importacoes={"a.pdf": IMP, "b.pdf": IMP},
    )
    assert out["totais"]["ok_seq_diferente"] == 0
    assert out["cobertura"]["comparados"] == 0
    assert "NADA FOI COMPARADO" in out["cobertura"]["mensagem"]
    assert out["ok"] is False


def test_cobertura_mais_de_metade_nao_ligada_falha():
    fire = FakeFire([_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20]})
    pedidos = {"a.pdf": _order("AF049"), "b.pdf": _order("X1"), "c.pdf": _order("X2")}
    out = rg.executar(
        pedidos,
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={n: IMP for n in pedidos},
    )
    assert out["totais"]["ok_igual"] == 2
    assert out["cobertura"]["comparados"] == 1
    assert "COBERTURA INSUFICIENTE" in out["cobertura"]["mensagem"]
    assert out["ok"] is False


def test_cobertura_exatamente_metade_passa():
    fire = FakeFire([_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20]})
    pedidos = {"a.pdf": _order("AF049"), "b.pdf": _order("X1")}
    out = rg.executar(
        pedidos,
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={n: IMP for n in pedidos},
    )
    assert out["cobertura"]["ok"] is True and out["ok"] is True


def test_cobertura_sem_arquivo_nenhum_falha():
    out = rg.executar({}, checar=lambda o: {}, fire=FakeFire([], {}), importacoes={})
    assert out["ok"] is False


def test_importacoes_procuram_em_varios_bancos(tmp_path):
    guardado = "20260910-103421_d7483e588500_AF166.xlsx"
    db_mm = _db_imports(tmp_path, [], nome="app_state_mm.db")
    db_nas = _db_imports(
        tmp_path,
        [("C:\\x\\" + guardado, None, "2026-09-10T10:34:30", "AF166", CNPJ, "AF166.xlsx")],
        nome="app_state_nasmar.db",
    )
    relido = rg.Relido("2026/09/" + guardado, guardado, "f" * 64, None)

    out = rg.importacoes_dos_arquivos([db_mm, db_nas], [relido])

    m = out[relido.chave]["mapeamento"]
    assert (m["por"], m["achado_em"]) == ("original_path", "app_state_nasmar.db")
    assert [b["db"] for b in m["bancos"]] == ["app_state_mm.db", "app_state_nasmar.db"]


# ── várias candidatas: horário de inserção, ambiguidade, revenda ─────────────
def _linha_ins(numero, codigo, data, cli, cnpj, ins, status="PEDIDO", mec=99):
    return (numero, codigo, status, data, cli, cnpj, mec, ins)


def _cand(o, data="2026-10-07"):
    return rg.candidato_do_pedido(o, data_pedido=data)


def test_ligar_dois_pedidos_do_mesmo_cliente_e_ambiguo_e_lista_os_cabs():
    # AW088: 5067 e 5068 do mesmo cliente, 7 min de diferença, importados 3 vezes
    o = _order("AW088")
    linhas = [
        _linha_ins("AW088", 5067, "2026-10-07", 5, CNPJ, datetime(2026, 10, 7, 7, 10)),
        _linha_ins("AW088", 5068, "2026-10-07", 5, CNPJ, datetime(2026, 10, 7, 7, 17)),
    ]
    veredito, cabs = rg.ligar_unico(_cand(o), linhas, importado_em="2026-10-07T07:09:17")
    assert (veredito, cabs) == ("ambiguo", [5067, 5068])
    assert rg.ligar_pedido(_cand(o), linhas, importado_em="2026-10-07T07:09:17") is None


def test_ligar_pelo_horario_descarta_pedido_antigo_do_mesmo_numero():
    # AF199: o 4574 é de agosto; o 5077 foi inserido 50 min depois da importação
    o = _order("AF199")
    linhas = [
        _linha_ins("AF199", 4574, "2026-08-03", 5, CNPJ, datetime(2026, 8, 3, 15, 40)),
        _linha_ins("AF199", 5077, "2026-10-09", 5, CNPJ, datetime(2026, 10, 9, 9, 35)),
    ]
    assert rg.ligar_unico(
        _cand(o, "2026-10-09"), linhas, importado_em="2026-10-09T08:45:11"
    ) == ("unico", [5077])


def test_ligar_aceita_pedido_no_nome_da_revenda():
    revenda = "34513679000134"
    o = _order("AF199")
    linhas = [
        _linha_ins("AF199", 5077, "2026-10-09", 9, "34.513.679/0001-34",
                   datetime(2026, 10, 9, 9, 35)),
    ]
    cand = _cand(o, "2026-10-09")
    assert rg.ligar_unico(cand, linhas, importado_em="2026-10-09T08:45:11")[0] == "nenhum"
    assert rg.ligar_unico(
        cand, linhas, importado_em="2026-10-09T08:45:11", cnpjs_extra=[revenda]
    ) == ("unico", [5077])


def test_horario_fora_da_janela_nao_liga_e_sem_horario_no_fire_nao_filtra():
    o = _order("X1")
    antes = _linha_ins("X1", 1, "2026-10-09", 5, CNPJ, datetime(2026, 10, 9, 8, 30))  # -15 min
    longe = _linha_ins("X1", 2, "2026-10-09", 5, CNPJ, datetime(2026, 10, 13, 8, 0))  # +4 dias
    sem = _linha_ins("X1", 3, "2026-10-09", 5, CNPJ, None)
    cand = _cand(o, "2026-10-09")
    assert rg.ligar_unico(cand, [antes, longe], importado_em="2026-10-09T08:45:00")[0] == "nenhum"
    assert rg.ligar_unico(cand, [sem], importado_em="2026-10-09T08:45:00") == ("unico", [3])
    assert rg.ligar_unico(cand, [sem, sem], importado_em="2026-10-09T08:45:00")[0] == "unico"
    # linha de 7 colunas (sem horário) segue valendo
    antiga = ("X1", 4, "PEDIDO", "2026-10-09", 5, CNPJ, 99)
    assert rg.ligar_unico(cand, [antiga], importado_em="2026-10-09T08:45:00") == ("unico", [4])


def test_executar_ambiguo_nao_e_ok_igual_nem_perigoso_e_lista_candidatos():
    linhas = [
        _linha_ins("AW088", 5067, "2026-10-07", 5, CNPJ, datetime(2026, 10, 7, 7, 10)),
        _linha_ins("AW088", 5068, "2026-10-07", 5, CNPJ, datetime(2026, 10, 7, 7, 17)),
        _linha_ins("AF049", 1, "2026-10-07", 5, CNPJ, datetime(2026, 10, 7, 7, 10)),
    ]
    fire = FakeFire(linhas, {1: [10, 20]})
    imp = {"imported_at": "2026-10-07T07:09:17"}
    out = rg.executar(
        {"a.xlsx": _order("AW088"), "b.xlsx": _order("AF049")},
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={"a.xlsx": imp, "b.xlsx": imp},
    )
    assert out["totais"]["ambiguo"] == 2
    assert out["totais"]["ok_igual"] == 2 and out["totais"]["ok_seq_diferente"] == 0
    a = next(x for x in out["arquivos"] if x["arquivo"] == "a.xlsx")
    assert a["resultado"] == "ambiguo"
    assert a["itens"][0]["candidatos_fire"] == [5067, 5068]
    assert out["perigosos"] == []


def test_executar_importacao_parsed_sem_pedido_e_nao_enviado_e_fica_fora_da_cobertura():
    fire = FakeFire([_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20]})
    parsed = {"imported_at": "2026-10-19T10:00:00", "portal_status": "parsed", "fire_codigo": None}
    pedidos = {"a.pdf": _order("AF049")}
    pedidos.update({f"p{i}.pdf": _order(f"ZZ{i}") for i in range(5)})
    out = rg.executar(
        pedidos,
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={"a.pdf": IMP, **{f"p{i}.pdf": parsed for i in range(5)}},
    )
    assert out["totais"]["nao_enviado"] == 10
    assert out["cobertura"]["nao_enviados"] == 5
    assert out["cobertura"]["ok"] is True and out["ok"] is True


def test_executar_leva_o_mapeamento_para_o_json():
    out = rg.executar(
        {"a.pdf": _order()},
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=FakeFire([], {}),
        importacoes={"a.pdf": {"mapeamento": {"por": None, "bancos": []}}},
    )
    assert out["arquivos"][0]["resultado"] == "sem_data"
    assert out["arquivos"][0]["mapeamento"] == {"por": None, "bancos": []}


def test_comparar_pedido_reimportacao_da_mesma_loja_e_ambiguo_nunca_ok_igual():
    # sonda do review: a loja A tem o CAB 11 [10,30] e um CAB 13 [99,98], ambos na janela
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire(
        [
            _linha("AF049", 11, "PEDIDO", "2026-10-19", 5, LOJA_A),
            _linha("AF049", 13, "PEDIDO", "2026-10-19", 5, LOJA_A),
            _linha("AF049", 12, "PEDIDO", "2026-10-19", 6, LOJA_B),
        ],
        {11: [10, 30], 13: [99, 98], 12: [20]},
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [i["status"] for i in itens] == ["ambiguo", "ok_igual", "ambiguo"]
    assert itens[0]["candidatos_fire"] == [11, 13] and itens[2]["candidatos_fire"] == [11, 13]
    assert itens[1]["fire_codigo"] == 12


def test_comparar_pedido_pedido_perdido_sem_cnpj_de_loja_serve_a_todas_e_e_ambiguo():
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire(
        [
            _linha("AF049", 11, "PEDIDO", "2026-10-19", 5, LOJA_A),
            _linha("AF049", 12, "PEDIDO", "2026-10-19", 6, LOJA_B),
            _linha("AF049", 14, "PEDIDO", "2026-10-19", 7, CNPJ),  # cliente do cabeçalho, 1 item
        ],
        {11: [10, 30], 12: [20], 14: [20]},
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert {i["status"] for i in itens} == {"ambiguo"}


def test_comparar_pedido_varios_pedidos_inteiros_e_ambiguo_com_os_cabs():
    o = _order_lojas()
    check = _check("ok:10", "ok:20", "ok:30")["items"]
    fire = FakeFire(
        [
            _linha("AF049", 21, "PEDIDO", "2026-10-19", 5, CNPJ),
            _linha("AF049", 22, "PEDIDO", "2026-10-19", 5, CNPJ),
        ],
        {21: [10, 30, 20], 22: [10, 30, 20]},
    )
    cand = rg.candidato_do_pedido(o, data_pedido="2026-10-19")

    itens = rg.comparar_pedido(o, check, cand, fire.pedidos_por_numero(["AF049"]), fire)

    assert [i["status"] for i in itens] == ["ambiguo"] * 3
    assert itens[0]["candidatos_fire"] == [21, 22]


def test_chave_repetida_conta_como_pendente():
    itens = rg.comparar_itens(
        _check("chave_repetida", "ok:20")["items"], [10, 20]
    )
    assert [i["status"] for i in itens] == ["pendente", "ok_igual"]


def test_nao_enviado_so_sem_planilha_gerada():
    fire = FakeFire([_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20]})
    base = {"imported_at": "2026-10-19T10:00:00", "portal_status": "parsed", "fire_codigo": None}
    pedidos = {"a.pdf": _order("AF049"), "p.pdf": _order("ZZ1"), "x.pdf": _order("ZZ2")}
    out = rg.executar(
        pedidos,
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=fire,
        importacoes={
            "a.pdf": IMP,
            "p.pdf": {**base, "tem_saida": False},  # nada saiu: nunca foi para o Fire
            "x.pdf": {**base, "tem_saida": True},  # saiu .xlsx: o Fire deveria ter importado
        },
    )
    res = {a["arquivo"]: a["resultado"] for a in out["arquivos"]}
    assert res["p.pdf"] == "nao_enviado"
    assert res["x.pdf"] == "sem_pedido_fire"
    assert out["totais"]["sem_pedido_fire"] == 2


def test_importacoes_tem_saida_le_output_files_json(tmp_path):
    import sqlite3

    guardado_a = "20260910-103421_aaaaaaaaaaaa_a.xlsx"
    guardado_b = "20260910-103421_bbbbbbbbbbbb_b.xlsx"
    guardado_c = "20260910-103421_cccccccccccc_c.xlsx"
    db = _db_imports(
        tmp_path,
        [
            ("C:\\x\\" + guardado_a, None, "2026-09-10T10:34:30", "A", CNPJ, "a.xlsx"),
            ("C:\\x\\" + guardado_b, None, "2026-09-10T10:34:30", "B", CNPJ, "b.xlsx"),
            ("C:\\x\\" + guardado_c, None, "2026-09-10T10:34:30", "C", CNPJ, "c.xlsx"),
        ],
    )
    c = sqlite3.connect(db)
    c.execute("UPDATE imports SET output_files_json = '[\"o.xlsx\"]' WHERE order_number = 'A'")
    c.execute("UPDATE imports SET output_files_json = '[]' WHERE order_number = 'B'")
    c.commit()
    c.close()
    rel = [rg.Relido(g, g, "0" * 64, None) for g in (guardado_a, guardado_b, guardado_c)]

    out = rg.importacoes_dos_arquivos(db, rel)

    assert [out[g]["tem_saida"] for g in (guardado_a, guardado_b, guardado_c)] == [
        True, False, False
    ]


def test_cobertura_sem_parse_conta_como_nao_ligado():
    out = rg.executar(
        {"a.pdf": _order("AF049"), "b.pdf": None, "c.pdf": None},
        checar=lambda o: _check("ok:10", "ok:20"),
        fire=FakeFire([_linha("AF049", 1, "PEDIDO", "2026-10-19", 5, CNPJ)], {1: [10, 20]}),
        importacoes={"a.pdf": IMP},
    )
    assert out["totais"]["sem_parse"] == 2
    assert out["cobertura"]["nao_ligados"] == 2
    assert "COBERTURA INSUFICIENTE" in out["cobertura"]["mensagem"]
    assert out["ok"] is False
