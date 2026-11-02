"""Ação `excluir` da volta Flow -> Fire: guardas, backup, atomicidade, idempotência.

Flow e Fire falsos; SQLite do ambiente real. O Fire falso estende o de
`test_cadastro_volta_gravar` com as leituras e os DELETEs do `excluir`.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.erp import produto_referencias as pr
from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.client import RespostaResultado
from app.persistence import cadastro_exclusao_repo as bk
from app.persistence import cadastro_volta_repo as repo
from app.persistence import router
from app.utils.logger import logger
from tests.test_cadastro_volta_gravar import (
    JARGAO,
    PROD,
    FdbError,
    FireGravavel,
    Flow,
    Relogio,
    _Cur,
)

SEQ = 709219
PAI_KIT = 709300
CRIAR_ID = "11111111-1111-4111-8111-111111111110"
EXCLUIR_ID = "88888888-8888-4888-8888-888888888888"
OUTRO_PRODUTO = "99999999-9999-4999-8999-999999999999"
# tabelas que o catálogo real da .7 tem (as obrigatórias + uma extra com mais de uma coluna)
CATALOGO = [(t, "CODPRODUTO", 8) for t in sorted(pr.TABELAS_OBRIGATORIAS)] + [
    ("NOTAPROD", "CODPRODUTO_ORIGEM", 8)
]


class _CurD(_Cur):
    description = None

    def execute(self, sql, params=()):
        super().execute(sql, params)
        self.description = self.fire.descricao


class FireExcluivel(FireGravavel):
    """PRODUTOS ganha DATA_CAD; kit é lista de (CODIGO, PAI, FILHO, QTD)."""

    def __init__(self, produtos=None, kit=(), usos=None, catalogo=None, kit_a_mais=0, **kw):
        base = {
            SEQ: {
                "SEQ": SEQ,
                "DESCRICAO": "MEIA SOQUETE",
                "BLOQUEADO": "Nao",
                "DATA_CAD": date(2026, 10, 17),
                "UNIDADE": "PAR",
            }
        }
        super().__init__(produtos=produtos if produtos is not None else base, **kw)
        self.kit = [tuple(k) for k in kit]
        self.usos = usos or {}  # (tabela, coluna) -> {seqs}
        self.catalogo = CATALOGO if catalogo is None else catalogo
        self.kit_a_mais = kit_a_mais  # o DELETE do kit reporta linhas a mais
        self.descricao = None
        self.deletes = []

    def cursor(self):
        return _CurD(self)

    def executar(self, sql, params):
        self.descricao = None
        if sql.startswith("DELETE"):
            self.deletes.append(sql)
        return super().executar(sql, params)

    def _responder(self, sql, params):  # noqa: C901
        if sql.startswith("SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION')"):
            return [("2.5.7",)], 1
        if sql == pr.SQL_CATALOGO:
            return [tuple(c) for c in self.catalogo], 0
        if sql.startswith("SELECT FIRST 1 1 FROM"):
            tabela, coluna = sql.split('"')[1], sql.split('"')[3]
            return ([(1,)] if params[0] in self.usos.get((tabela, coluna), ()) else []), 0
        if sql == "SELECT * FROM PRODUTOS WHERE SEQ = ?":
            p = self.produtos.get(params[0])
            if p is None:
                return [], 0
            self.descricao = [(c,) for c in p]
            return [tuple(p.values())], 1
        if sql == "SELECT * FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ? ORDER BY CODIGO":
            self.descricao = [(c,) for c in ("CODIGO", "CODPRODUTO_PAI", "CODPRODUTO", "QTD")]
            return sorted(k for k in self.kit if k[1] == params[0]), 0
        if (
            sql
            == "SELECT CODPRODUTO_PAI FROM PRODUTOS_KIT WHERE CODPRODUTO = ? ORDER BY CODPRODUTO_PAI"
        ):
            return [(k[1],) for k in self.kit if k[2] == params[0]], 0
        if sql == "DELETE FROM PRODUTOS WHERE SEQ = ?":
            return [], 1 if self.produtos.pop(params[0], None) is not None else 0
        achado = super()._responder(sql, params)
        if sql == "DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?":
            return achado[0], achado[1] + self.kit_a_mais
        return achado


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    for var in (cv.VAR_MODO, cv.VAR_P0, cv.VAR_INATIVAR, cv.VAR_EXCLUIR):
        monkeypatch.delenv(var, raising=False)
    cv._avisados.clear()
    monkeypatch.setattr(cv, "_ultima_volta", None)
    monkeypatch.setattr(cv, "_ultimo_ciclo", {})


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _captura(nivel):
    msgs: list[str] = []
    hid = logger.add(
        lambda m: msgs.append(str(m)), level=nivel, filter=lambda r: r["level"].name == nivel
    )
    return msgs, hid


@pytest.fixture
def erros():
    msgs, hid = _captura("ERROR")
    yield msgs
    logger.remove(hid)


@pytest.fixture
def avisos():
    msgs, hid = _captura("WARNING")
    yield msgs
    logger.remove(hid)


@pytest.fixture
def ligar(monkeypatch):
    def _set(excluir=True, modo="criar_e_atualizar"):
        monkeypatch.setenv(cv.VAR_MODO, f"mm:{modo}")
        monkeypatch.setenv(cv.VAR_P0, "mm")
        if excluir:
            monkeypatch.setenv(cv.VAR_EXCLUIR, "mm")

    return _set


def _criado_pelo_flow(
    env_conn, seq=SEQ, produto=PROD, estado="confirmado", quando="2026-10-17 13:00:00"
):
    env_conn.execute(
        "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, "
        "erp_produto_id, seq_reservado, iniciado_em) VALUES (?, 1, ?, 'criar', 'simples', ?, ?, ?, ?)",
        (CRIAR_ID, estado, produto, str(seq), seq, quando),
    )
    env_conn.commit()


def _excluir(seq=SEQ, **kw):
    return {
        "cadastroId": EXCLUIR_ID,
        "produtoId": PROD,
        "versao": 3,
        "acao": "excluir",
        "erpProdutoId": str(seq),
        "camposAlterados": None,
        "tipo": "simples",
        "nome": "MEIA SOQUETE",
        "aprovadoEm": "2026-10-30T12:00:00.000Z",
        **kw,
    }


def _rodar(flow, fire, env_conn):
    return cv.processar_ambiente(
        "mm", _client=flow, _fire_conn=fire, _env_conn=env_conn, _relogio=Relogio()
    )


def _erro(flow):
    corpo = flow.posts[0][1]
    assert corpo["resultado"] == "erro", corpo
    msg = corpo["erro"]["mensagem"]
    assert not any(j in msg for j in JARGAO), msg
    return corpo["erro"]


def _livro(env_conn):
    return repo.buscar(env_conn, EXCLUIR_ID, 3)


# --- portão -----------------------------------------------------------------


def test_sem_portao_a_fila_nao_pede_excluir(ligar, env_conn):
    ligar(excluir=False)
    flow = Flow([])
    _rodar(flow, FireExcluivel(), env_conn)
    pedidas = [g["acoes"] for g in flow.gets]
    assert pedidas and all("excluir" not in a for a in pedidas)


def test_com_portao_a_fila_pede_excluir_so_em_criar_e_atualizar(ligar, env_conn):
    ligar(excluir=True)
    flow = Flow([])
    _rodar(flow, FireExcluivel(), env_conn)
    assert next(g for g in flow.gets if not g.get("simular"))["acoes"] == (
        "criar",
        "atualizar",
        "excluir",
    )


def test_portao_em_modo_criar_nao_pede_excluir(ligar, env_conn):
    ligar(excluir=True, modo="criar")
    flow = Flow([])
    _rodar(flow, FireExcluivel(), env_conn)
    assert all("excluir" not in g["acoes"] for g in flow.gets)


def test_simular_so_pede_excluir_com_portao(ligar, env_conn):
    ligar(excluir=True, modo="simular")
    flow = Flow([])
    _rodar(flow, FireExcluivel(), env_conn)
    assert "excluir" in flow.gets[0]["acoes"]


def test_excluir_que_chega_com_portao_desligado_e_pulado_sem_tocar_no_fire(ligar, env_conn, avisos):
    ligar(excluir=False)
    _criado_pelo_flow(env_conn)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == [] and fire.deletes == []
    assert any("excluir" in a for a in avisos)


# --- caso feliz ---------------------------------------------------------------


def test_exclui_par(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert SEQ not in fire.produtos
    assert fire.deletes == ["DELETE FROM PRODUTOS WHERE SEQ = ?"]
    corpo = flow.posts[0][1]
    assert corpo["resultado"] == "gravado" and corpo["erpProdutoId"] == str(SEQ)
    assert corpo["versao"] == 3
    assert _livro(env_conn)["estado"] == "confirmado"
    linha = bk.buscar(env_conn, EXCLUIR_ID, 3)
    assert linha["aplicado_em"] is not None
    produto, kit = bk.restaurar(linha)
    assert produto["DESCRICAO"] == "MEIA SOQUETE" and produto["DATA_CAD"] == date(2026, 10, 17)
    assert kit == []


def test_exclui_kit_apaga_a_composicao_antes_do_produto_e_guarda_tudo(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn, seq=PAI_KIT)
    kit = [(90, PAI_KIT, 3170, 3), (91, PAI_KIT, 3171, 3), (92, 5555, 3170, 1)]
    produtos = {
        PAI_KIT: {
            "SEQ": PAI_KIT,
            "DESCRICAO": "KIT3",
            "BLOQUEADO": "Nao",
            "DATA_CAD": date(2026, 10, 17),
        }
    }
    flow = Flow([_excluir(seq=PAI_KIT, tipo="kit", nome="KIT3")])
    fire = FireExcluivel(produtos=produtos, kit=kit)
    _rodar(flow, fire, env_conn)
    assert fire.deletes == [
        "DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?",
        "DELETE FROM PRODUTOS WHERE SEQ = ?",
    ]
    assert PAI_KIT not in fire.produtos and fire.kit == [(92, 5555, 3170, 1)]  # só o do pai
    assert flow.posts[0][1]["resultado"] == "gravado"
    _, kit_guardado = bk.restaurar(bk.buscar(env_conn, EXCLUIR_ID, 3))
    assert [k["CODIGO"] for k in kit_guardado] == [90, 91]


def test_backup_ja_esta_no_disco_quando_o_delete_acontece(ligar, env_conn, monkeypatch):
    ligar()
    _criado_pelo_flow(env_conn)
    visto = {}
    original = FireExcluivel._responder

    def espia(self, sql, params):
        if sql.startswith("DELETE"):
            visto["backup"] = bk.buscar(env_conn, EXCLUIR_ID, 3)
            # commitado: outra conexão do mesmo arquivo também enxerga
            with router.env_connect("mm") as outra:
                visto["outra"] = bk.buscar(outra, EXCLUIR_ID, 3)
        return original(self, sql, params)

    monkeypatch.setattr(FireExcluivel, "_responder", espia)
    _rodar(Flow([_excluir()]), FireExcluivel(), env_conn)
    assert visto["backup"] is not None and visto["outra"] is not None
    assert visto["outra"]["aplicado_em"] is None


# --- guarda de uso -----------------------------------------------------------


def test_em_uso_no_erp_nao_exclui_e_diz_a_tabela(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    flow = Flow([_excluir()])
    fire = FireExcluivel(usos={("NOTAPROD", "CODPRODUTO_ORIGEM"): {SEQ}})
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "definitivo" and erro["codigo"] == "em_uso_no_erp"
    assert "NOTAPROD" in erro["mensagem"]
    assert fire.deletes == [] and fire.escritas == [] and SEQ in fire.produtos
    assert bk.buscar(env_conn, EXCLUIR_ID, 3) is None
    assert _livro(env_conn)["estado"] == "erro_registrado"


def test_catalogo_sem_tabela_obrigatoria_nao_exclui(ligar, env_conn, erros):
    ligar()
    _criado_pelo_flow(env_conn)
    flow = Flow([_excluir()])
    fire = FireExcluivel(catalogo=[("ESTOQUE", "CODPRODUTO", 8)])
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "consulta_erp_falhou"
    assert fire.deletes == [] and SEQ in fire.produtos
    assert any("NOTAPROD" in e for e in erros)


# --- guarda de origem ----------------------------------------------------------


def test_produto_antigo_sem_criar_no_livro_nao_exclui_e_nem_abre_o_fire(ligar, env_conn):
    ligar()
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "definitivo" and erro["codigo"] == "produto_nao_criado_pelo_flow"
    assert fire.escritas == [] and fire.deletes == [] and SEQ in fire.produtos


def test_criar_de_outro_produto_com_o_mesmo_seq_nao_vale(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn, produto=OUTRO_PRODUTO)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert _erro(flow)["codigo"] == "produto_nao_criado_pelo_flow" and SEQ in fire.produtos


def test_atualizar_de_produto_antigo_nao_vira_origem(ligar, env_conn):
    """`seq_criado_pelo_portal` aceita qualquer ação; a origem do excluir exige um `criar`."""
    ligar()
    env_conn.execute(
        "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, "
        "erp_produto_id) VALUES ('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 2, 'confirmado', "
        "'atualizar', 'simples', ?, ?)",
        (PROD, str(SEQ)),
    )
    env_conn.commit()
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert _erro(flow)["codigo"] == "produto_nao_criado_pelo_flow"


def test_seq_reaproveitado_com_data_cad_fora_da_janela_nao_exclui(ligar, env_conn):
    """O Fire dá MAX+1: apagado o maior SEQ, o próximo cadastro da fábrica pega o mesmo número."""
    ligar()
    _criado_pelo_flow(env_conn)
    novo = {
        SEQ: {
            "SEQ": SEQ,
            "DESCRICAO": "OUTRO PRODUTO",
            "BLOQUEADO": "Nao",
            "DATA_CAD": date(2026, 10, 29),
        }
    }
    flow, fire = Flow([_excluir()]), FireExcluivel(produtos=novo)
    _rodar(flow, fire, env_conn)
    assert _erro(flow)["codigo"] == "produto_nao_criado_pelo_flow"
    assert fire.deletes == [] and SEQ in fire.produtos


def test_criar_em_orfao_nao_e_origem(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn, estado="orfao")
    flow = Flow([_excluir()])
    _rodar(flow, FireExcluivel(), env_conn)
    assert _erro(flow)["codigo"] == "produto_nao_criado_pelo_flow"


# --- kit antes de par -----------------------------------------------------------


def test_par_ligado_a_kit_e_transitorio(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    flow = Flow([_excluir()])
    fire = FireExcluivel(kit=[(90, PAI_KIT, SEQ, 3), (91, 709301, SEQ, 1)])
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "ligado_a_kit"
    assert str(PAI_KIT) in erro["mensagem"]
    assert fire.deletes == [] and SEQ in fire.produtos
    assert bk.buscar(env_conn, EXCLUIR_ID, 3) is None
    assert _livro(env_conn)["estado"] == "transitorio"


def test_kit_pai_com_codigo_fora_do_formato_continua_transitorio(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    flow = Flow([_excluir()])
    fire = FireExcluivel(kit=[(90, 1234567890, SEQ, 3)])
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "ligado_a_kit"
    assert fire.deletes == []


def test_o_kit_sai_e_o_par_e_reentregue_e_exclui(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    fire = FireExcluivel(kit=[(90, PAI_KIT, SEQ, 3)])
    _rodar(Flow([_excluir()]), fire, env_conn)
    assert SEQ in fire.produtos
    fire.kit = []  # o kit foi excluído
    flow2 = Flow([_excluir()])
    _rodar(flow2, fire, env_conn)
    assert SEQ not in fire.produtos and flow2.posts[0][1]["resultado"] == "gravado"


# --- backup antes de apagar --------------------------------------------------------


def test_backup_que_falha_impede_o_delete(ligar, env_conn, monkeypatch):
    ligar()
    _criado_pelo_flow(env_conn)

    def quebra(*a, **k):
        import sqlite3

        raise sqlite3.OperationalError("disco cheio")

    monkeypatch.setattr(bk, "guardar", quebra)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "backup_falhou"
    assert fire.deletes == [] and fire.escritas == [] and SEQ in fire.produtos


def test_backup_que_nao_volta_na_releitura_impede_o_delete(ligar, env_conn, monkeypatch):
    ligar()
    _criado_pelo_flow(env_conn)
    monkeypatch.setattr(bk, "guardar", lambda *a, **k: True)  # diz que gravou, mas não gravou
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert _erro(flow)["codigo"] == "backup_falhou" and fire.deletes == []


def test_coluna_que_o_backup_nao_serializa_e_definitiva_e_nao_apaga(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    esquisito = {
        SEQ: {
            "SEQ": SEQ,
            "DESCRICAO": "MEIA SOQUETE",
            "DATA_CAD": date(2026, 10, 17),
            "COISA": object(),
        }
    }
    flow, fire = Flow([_excluir()]), FireExcluivel(produtos=esquisito)
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "definitivo" and erro["codigo"] == "backup_falhou"
    assert fire.deletes == []


# --- atomicidade ----------------------------------------------------------------------


def test_contagem_divergente_desfaz_tudo_e_e_transitorio(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn, seq=PAI_KIT)
    produtos = {PAI_KIT: {"SEQ": PAI_KIT, "DESCRICAO": "KIT3", "DATA_CAD": date(2026, 10, 17)}}
    kit = [(90, PAI_KIT, 3170, 3), (91, PAI_KIT, 3171, 3)]
    flow = Flow([_excluir(seq=PAI_KIT, tipo="kit", nome="KIT3")])
    fire = FireExcluivel(produtos=produtos, kit=kit, kit_a_mais=1)
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "erp_ocupado"
    assert PAI_KIT in fire.produtos and len(fire.kit) == 2  # rollback devolveu o kit
    assert fire.commits == 0 and fire.rollbacks >= 1
    assert bk.buscar(env_conn, EXCLUIR_ID, 3)["aplicado_em"] is None


def test_lock_do_firebird_no_delete_e_transitorio_e_desfaz(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    fire = FireExcluivel(falhas={"DELETE FROM PRODUTOS WHERE": [FdbError("x", -913, 335544345)]})
    flow = Flow([_excluir()])
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "erp_ocupado"
    assert SEQ in fire.produtos and fire.rollbacks >= 1


# --- idempotência ---------------------------------------------------------------------------


def test_seq_que_ja_nao_existe_responde_gravado_sem_backup(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    flow, fire = Flow([_excluir()]), FireExcluivel(produtos={})
    _rodar(flow, fire, env_conn)
    corpo = flow.posts[0][1]
    assert corpo["resultado"] == "gravado" and corpo["erpProdutoId"] == str(SEQ)
    assert fire.escritas == [] and bk.buscar(env_conn, EXCLUIR_ID, 3) is None


def test_reentrega_da_mesma_versao_nao_duplica_backup_nem_responde_de_novo(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    fire, flow = FireExcluivel(), Flow([_excluir()])
    _rodar(flow, fire, env_conn)
    flow2 = Flow([_excluir()])
    _rodar(flow2, fire, env_conn)
    assert flow2.posts == []
    n = env_conn.execute("SELECT COUNT(*) FROM cadastro_exclusao_backup").fetchone()[0]
    assert n == 1


def test_delete_comitou_mas_o_livro_nao_soube_responde_gravado_sem_tocar_no_fire(ligar, env_conn):
    """Queda entre o commit do Fire e o livro: o backup está `aplicado`; a reentrega só responde."""
    ligar()
    _criado_pelo_flow(env_conn)
    bk.guardar(env_conn, EXCLUIR_ID, 3, SEQ, {"SEQ": SEQ, "DESCRICAO": "MEIA SOQUETE"}, [])
    bk.marcar_aplicado(env_conn, EXCLUIR_ID, 3)
    env_conn.commit()
    fire, flow = FireExcluivel(), Flow([_excluir()])  # o Fire de teste ainda tem o produto
    _rodar(flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado" and flow.posts[0][1]["erpProdutoId"] == str(
        SEQ
    )
    assert fire.deletes == [] and SEQ in fire.produtos


def test_retentativa_com_o_mesmo_produto_usa_o_backup_que_ja_existe(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    antigo = {
        "SEQ": SEQ,
        "DESCRICAO": "MEIA SOQUETE",
        "BLOQUEADO": "Nao",
        "DATA_CAD": date(2026, 10, 17),
        "UNIDADE": "PAR",
    }
    bk.guardar(env_conn, EXCLUIR_ID, 3, SEQ, antigo, [])
    env_conn.commit()
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert SEQ not in fire.produtos and flow.posts[0][1]["resultado"] == "gravado"


def test_seq_reaproveitado_apos_commit_ambiguo_nao_apaga_o_produto_novo(ligar, env_conn, erros):
    """Achado da revisão: o DELETE comitou, a resposta se perdeu, o Fire deu o mesmo SEQ a um
    produto novo (mesmo nome, mesmo dia). O backup original sobrevive, nada é apagado e o item
    fica `incerto` para uma pessoa (não dá para afirmar ao Flow que nada foi apagado)."""
    ligar()
    _criado_pelo_flow(env_conn)
    original = {
        "SEQ": SEQ,
        "DESCRICAO": "MEIA SOQUETE",
        "BLOQUEADO": "Nao",
        "DATA_CAD": date(2026, 10, 17),
        "UNIDADE": "PAR",
    }
    bk.guardar(env_conn, EXCLUIR_ID, 3, SEQ, original, [])
    env_conn.commit()
    novo = {
        SEQ: {
            "SEQ": SEQ,
            "DESCRICAO": "MEIA SOQUETE",
            "BLOQUEADO": "Nao",
            "DATA_CAD": date(2026, 10, 17),
            "UNIDADE": "CX",
        }
    }
    flow, fire = Flow([_excluir()]), FireExcluivel(produtos=novo)
    _rodar(flow, fire, env_conn)
    assert flow.posts == []  # nem apagou nem erro: a pessoa decide
    assert (
        _livro(env_conn)["estado"] == "incerto"
        and _livro(env_conn)["alerta"] == "excluir_divergente"
    )
    assert fire.deletes == [] and SEQ in fire.produtos
    assert bk.restaurar(bk.buscar(env_conn, EXCLUIR_ID, 3))[0] == original  # intocado
    assert any("incerto" in e for e in erros)


def test_kit_que_mudou_desde_o_backup_nao_apaga(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn, seq=PAI_KIT)
    linha = {"SEQ": PAI_KIT, "DESCRICAO": "KIT3", "DATA_CAD": date(2026, 10, 17)}
    bk.guardar(
        env_conn,
        EXCLUIR_ID,
        3,
        PAI_KIT,
        linha,
        [{"CODIGO": 90, "CODPRODUTO_PAI": PAI_KIT, "CODPRODUTO": 3170, "QTD": 3}],
    )
    env_conn.commit()
    flow = Flow([_excluir(seq=PAI_KIT, tipo="kit", nome="KIT3")])
    fire = FireExcluivel(produtos={PAI_KIT: linha}, kit=[(91, PAI_KIT, 3999, 3)])
    _rodar(flow, fire, env_conn)
    assert flow.posts == [] and fire.deletes == []
    assert _livro(env_conn)["estado"] == "incerto"


def test_seq_reaproveitado_por_produto_de_outro_nome_nao_apaga(ligar, env_conn):
    """Sem backup anterior: a fábrica apagou à mão e cadastrou outro produto no mesmo dia."""
    ligar()
    _criado_pelo_flow(env_conn)
    outro = {
        SEQ: {
            "SEQ": SEQ,
            "DESCRICAO": "CUECA BOXER",
            "BLOQUEADO": "Nao",
            "DATA_CAD": date(2026, 10, 17),
        }
    }
    flow, fire = Flow([_excluir()]), FireExcluivel(produtos=outro)
    _rodar(flow, fire, env_conn)
    assert _erro(flow)["codigo"] == "produto_mudou"
    assert fire.deletes == [] and SEQ in fire.produtos
    assert bk.buscar(env_conn, EXCLUIR_ID, 3) is None


def test_nome_do_criar_tambem_vale_como_identidade(ligar, env_conn):
    """O Flow renomeou depois do criar e o Fire ainda tem o nome do criar: é o mesmo produto."""
    ligar()
    env_conn.execute(
        "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, "
        "erp_produto_id, seq_reservado, iniciado_em, plano_json) "
        "VALUES (?, 1, 'confirmado', 'criar', 'simples', ?, ?, ?, '2026-10-17 13:00:00', ?)",
        (CRIAR_ID, PROD, str(SEQ), SEQ, json.dumps({"campos": {"DESCRICAO": "MEIA SOQUETE"}})),
    )
    env_conn.commit()
    flow, fire = Flow([_excluir(nome="MEIA SOQUETE RENOMEADA")]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert SEQ not in fire.produtos and flow.posts[0][1]["resultado"] == "gravado"


# --- desfecho no Flow ------------------------------------------------------------------------


def test_409_no_gravado_de_excluir_fecha_superado_nunca_orfao(ligar, env_conn, erros):
    ligar()
    _criado_pelo_flow(env_conn)
    flow = Flow(
        [_excluir()], respostas=[RespostaResultado(409, {"error": "versao_superada"}, None)]
    )
    _rodar(flow, FireExcluivel(), env_conn)
    assert _livro(env_conn)["estado"] == "superado"
    assert not any("órfão" in e for e in erros)


# --- simulação -----------------------------------------------------------------------------------


def test_simular_excluir_mostra_o_plano_sem_tocar_no_fire(ligar, env_conn):
    ligar(modo="simular")
    _criado_pelo_flow(env_conn)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    r = _rodar(flow, fire, env_conn)
    assert r["simulados"] == 1 and flow.posts == [] and fire.escritas == []
    plano = json.loads(_livro(env_conn)["plano_json"])
    assert plano["acao"] == "excluir" and plano["seq"] == str(SEQ)
    assert _livro(env_conn)["estado"] == "simulado"


def test_simular_excluir_de_produto_antigo_vira_erro_definitivo(ligar, env_conn):
    ligar(modo="simular")
    r = _rodar(Flow([_excluir()]), FireExcluivel(), env_conn)
    assert r["erros"] == 1
    assert _livro(env_conn)["erro_codigo"] == "produto_nao_criado_pelo_flow"


# --- recuperação --------------------------------------------------------------------------------------


def test_recuperacao_trata_excluir_como_idempotente(ligar, env_conn):
    from app.integrations.flowpcp import cadastro_recuperacao as rec

    linha = {
        "acao": "excluir",
        "seq_reservado": None,
        "plano_json": "{}",
        "iniciado_em": "2026-10-17 13:00:00",
        "cadastro_id": EXCLUIR_ID,
        "versao": 3,
    }
    decisao = rec._decidir(FireExcluivel(), env_conn, linha, rec.iniciado(linha))
    assert decisao[0] == "reaplicar"


def test_nunca_chega_a_apagar_o_que_nao_e_o_seq_pedido(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    produtos = {
        SEQ: {"SEQ": SEQ, "DESCRICAO": "MEIA SOQUETE", "DATA_CAD": date(2026, 10, 17)},
        SEQ + 1: {"SEQ": SEQ + 1, "DESCRICAO": "B", "DATA_CAD": date(2026, 10, 17)},
    }
    fire = FireExcluivel(produtos=produtos)
    _rodar(Flow([_excluir()]), fire, env_conn)
    assert list(fire.produtos) == [SEQ + 1]
    assert all(p == (SEQ,) for s, p in fire.escritas if s.startswith("DELETE FROM PRODUTOS WHERE"))


# --- achados do /revisar ----------------------------------------------------------------------


def test_excluir_fora_do_modo_criar_e_atualizar_nao_apaga(ligar, env_conn, avisos):
    """Variável ligada mas modo `criar`: um `excluir` que o Flow mande sem ser pedido é pulado."""
    ligar(excluir=True, modo="criar")
    _criado_pelo_flow(env_conn)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert flow.posts == [] and fire.deletes == [] and SEQ in fire.produtos
    assert any("criar_e_atualizar" in a for a in avisos)


def test_falha_do_sqlite_do_livro_e_transitoria_e_nao_apaga(ligar, env_conn, monkeypatch):
    import sqlite3

    ligar()
    _criado_pelo_flow(env_conn)

    def trava(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bk, "buscar", trava)
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    erro = _erro(flow)
    assert erro["tipo"] == "transitorio" and erro["codigo"] == "consulta_erp_falhou"
    assert fire.deletes == [] and SEQ in fire.produtos


def test_nome_apagado_sai_do_cache_do_lote(ligar, env_conn):
    """Um `criar` com o mesmo nome, mais adiante no mesmo lote, não é `nome_existe`."""
    from tests.test_cadastro_volta_gravar import PAR

    ligar()
    _criado_pelo_flow(env_conn)
    criar_igual = {
        **PAR,
        "cadastroId": "77777777-0000-4000-8000-000000000001",
        "produtoId": "77777777-0000-4000-8000-000000000002",
        "nome": "MEIA SOQUETE",
        "codigoMes": None,
    }
    fire = FireExcluivel(marcas={17: "Kolosh"})
    flow = Flow([_excluir(), criar_igual])
    _rodar(flow, fire, env_conn)
    resultados = {c: b["resultado"] for c, b in flow.posts}
    assert resultados[EXCLUIR_ID] == "gravado"
    assert resultados["77777777-0000-4000-8000-000000000001"] == "gravado"


def test_criar_mais_recente_de_outro_produto_tira_a_origem(ligar, env_conn):
    """A foi excluído, B (outro produto) ganhou o mesmo SEQ: um `excluir` velho de A não vale."""
    ligar()
    _criado_pelo_flow(env_conn)  # criar de A (PROD) no SEQ
    env_conn.execute(
        "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, "
        "erp_produto_id, seq_reservado, iniciado_em) VALUES (?, 1, 'confirmado', 'criar', "
        "'simples', ?, ?, ?, '2026-10-17 15:00:00')",
        ("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", OUTRO_PRODUTO, str(SEQ), SEQ),
    )
    env_conn.commit()
    flow, fire = Flow([_excluir()]), FireExcluivel()
    _rodar(flow, fire, env_conn)
    assert _erro(flow)["codigo"] == "produto_nao_criado_pelo_flow"
    assert fire.deletes == [] and SEQ in fire.produtos
    # e o dono atual consegue excluir o seu
    assert repo.criar_que_gravou_seq(env_conn, str(SEQ), OUTRO_PRODUTO) is not None


def test_conexao_cai_no_delete_recuperacao_libera_e_a_reentrega_exclui(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    fire = FireExcluivel(falhas={"DELETE FROM PRODUTOS WHERE": [FdbError("x", -902, 335544721)]})
    flow = Flow([_excluir()])
    _rodar(flow, fire, env_conn)
    assert flow.posts == [] and _livro(env_conn)["estado"] == "em_andamento"
    assert SEQ in fire.produtos and bk.buscar(env_conn, EXCLUIR_ID, 3)["aplicado_em"] is None
    flow2 = Flow([_excluir()])
    cv.processar_ambiente(
        "mm", _client=flow2, _fire_conn=fire, _env_conn=env_conn, _relogio=_depois(horas=2)
    )
    assert SEQ not in fire.produtos
    assert flow2.posts[0][1]["resultado"] == "gravado"
    assert bk.buscar(env_conn, EXCLUIR_ID, 3)["aplicado_em"] is not None


def test_conexao_cai_no_delete_e_o_codigo_foi_reaproveitado_vira_incerto(ligar, env_conn):
    ligar()
    _criado_pelo_flow(env_conn)
    fire = FireExcluivel(falhas={"DELETE FROM PRODUTOS WHERE": [FdbError("x", -902, 335544721)]})
    _rodar(Flow([_excluir()]), fire, env_conn)
    original = bk.restaurar(bk.buscar(env_conn, EXCLUIR_ID, 3))[0]
    # o DELETE tinha comitado: o Fire deu o mesmo código a outro produto do mesmo dia
    fire.produtos = {SEQ: {**original, "UNIDADE": "CX"}}
    flow2 = Flow([_excluir()])
    cv.processar_ambiente(
        "mm", _client=flow2, _fire_conn=fire, _env_conn=env_conn, _relogio=_depois(horas=2)
    )
    assert flow2.posts == [] and SEQ in fire.produtos
    assert _livro(env_conn)["estado"] == "incerto"
    assert bk.restaurar(bk.buscar(env_conn, EXCLUIR_ID, 3))[0] == original


def _depois(horas):
    """Relógio no futuro do `iniciado_em` real da linha (que o SQLite carimba com a hora de agora)."""
    r = Relogio()
    r.agora = datetime.now(UTC) + timedelta(hours=horas)
    return r


def test_codigo_reaproveitado_fora_da_janela_com_backup_anterior_vira_incerto(ligar, env_conn):
    """O `excluir` costuma chegar dias depois do `criar`: o produto novo tem DATA_CAD de hoje,
    fora da janela. Com backup anterior isso não prova que nada foi apagado: `incerto`."""
    ligar()
    _criado_pelo_flow(env_conn)
    fire = FireExcluivel(falhas={"DELETE FROM PRODUTOS WHERE": [FdbError("x", -902, 335544721)]})
    _rodar(Flow([_excluir()]), fire, env_conn)
    original = bk.restaurar(bk.buscar(env_conn, EXCLUIR_ID, 3))[0]
    fire.produtos = {SEQ: {**original, "DESCRICAO": "OUTRO", "DATA_CAD": date(2026, 10, 30)}}
    flow2 = Flow([_excluir()])
    cv.processar_ambiente(
        "mm", _client=flow2, _fire_conn=fire, _env_conn=env_conn, _relogio=_depois(horas=2)
    )
    assert flow2.posts == [] and SEQ in fire.produtos
    assert _livro(env_conn)["estado"] == "incerto"


def test_origem_desempata_por_iniciado_em_e_nao_so_por_rowid(env_conn):
    """`reservar` reaproveita a linha antiga (rowid antigo, iniciado_em novo): o criar mais
    recente é o de maior iniciado_em, não o de maior rowid."""

    def criar(cid, produto, quando):
        env_conn.execute(
            "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, "
            "erp_produto_id, seq_reservado, iniciado_em) VALUES (?, 1, 'confirmado', 'criar', "
            "'simples', ?, ?, ?, ?)",
            (cid, produto, str(SEQ), SEQ, quando),
        )

    criar(
        "b1b1b1b1-b1b1-41b1-81b1-b1b1b1b1b1b1", OUTRO_PRODUTO, "2026-10-17 16:00:00"
    )  # B: rowid 1
    criar(CRIAR_ID, PROD, "2026-10-17 13:00:00")  # A: rowid 2, mas gravou antes
    env_conn.commit()
    assert repo.criar_que_gravou_seq(env_conn, str(SEQ), OUTRO_PRODUTO) is not None  # B é o dono
    assert repo.criar_que_gravou_seq(env_conn, str(SEQ), PROD) is None  # A já não é
