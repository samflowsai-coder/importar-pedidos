"""Volta Flow -> Fire nos modos de gravação. Flow e Fire falsos; SQLite do ambiente real.

O gravador real (`app/erp/cadastro_fire.py`) roda sobre o `FireGravavel`: nada é mockado
entre o ciclo e o SQL.
"""

import copy
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.erp import cadastro_fire
from app.erp.exceptions import FirebirdConnectionError
from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_resultado import mensagem_para_flow
from app.integrations.flowpcp.cadastro_schema import PendentesResposta
from app.integrations.flowpcp.client import RespostaResultado
from app.persistence import cadastro_volta_repo as repo
from app.persistence import router
from app.utils.logger import logger

PROD = "77777777-7777-4777-8777-777777777771"
PAR = {
    "cadastroId": "22222222-2222-4222-8222-222222222222", "produtoId": PROD, "versao": 1,
    "acao": "criar", "erpProdutoId": None, "tipo": "simples", "nome": "MEIA SOQUETE",
    "unidadeErp": "PAR", "ean": None, "ncm": "61159500", "tipoItemSped": 3,
    "grupoErp": {"codigo": "8"}, "codigoNfe": None, "cor": None, "componentes": None,
    "marcaErp": {"codigo": 17}, "codigoMes": "81991", "aprovadoEm": "2026-10-17T13:00:00.123Z",
}
JARGAO = ("Fire", "SEQ", "erpProdutoId", "componentes[")

_FLOWS: list = []


class Flow:
    def __init__(self, itens, caps=("cadastros.v1",), respostas=None):
        self.itens, self.caps = itens, list(caps)
        self.respostas = list(respostas or [])
        self.gets, self.posts = [], []
        _FLOWS.append(self)

    def list_cadastros_pendentes(self, limite=20, **kw):
        self.gets.append({"limite": limite, **kw})
        return PendentesResposta(cadastros=self.itens, capacidades=self.caps)

    def post_cadastro_resultado(self, cadastro_id, corpo):
        self.posts.append((cadastro_id, corpo))
        if self.respostas:
            return self.respostas.pop(0)
        if corpo["resultado"] == "gravado":
            status = "gravado"
        elif corpo["erro"]["tipo"] == "transitorio":
            return RespostaResultado(200, {"status": "transitorio_registrado", "tentativas": 1}, None)
        else:
            status = "erro_registrado"
        return RespostaResultado(200, {"status": status}, None)

    def close(self):
        pass


class FdbError(Exception):
    """Como o fdb monta: args = (mensagem, sqlcode, gdscode). A mensagem traz valor do usuário."""


def _erro(sqlcode, gds):
    return FdbError("erro do driver com VALOR-DO-USUARIO", sqlcode, gds)


def LOCK():  # noqa: N802
    return _erro(-913, 335544345)


def RECUSA():  # noqa: N802
    return _erro(-530, 335544466)


def CONEXAO():  # noqa: N802
    return _erro(-902, 335544721)


def COLISAO():  # noqa: N802
    return _erro(-803, 335544665)


class _Cur:
    def __init__(self, fire):
        self.fire, self.rows, self.rowcount = fire, [], 0

    def execute(self, sql, params=()):
        self.rows, self.rowcount = self.fire.executar(" ".join(sql.split()), tuple(params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FireGravavel:
    """Fire em memória: PRODUTOS {seq: dict}, PRODUTOS_KIT [(CODIGO, PAI, FILHO, QTD)],
    PRODUTOS_MARCA {codigo: nome}. Transação de verdade: rollback desfaz as escritas.

    `falhas`: prefixo do SQL -> lista de exceções levantadas em ordem (uma por execução).
    `lento`: cada INSERT em PRODUTOS avança `relogio.agora` nesse tanto.
    `antes_do_insert(seq)`: chamado no INSERT em PRODUTOS, antes de gravar.
    """

    def __init__(self, produtos=None, marcas=None, lento=timedelta(0), relogio=None,
                 falhas=None, antes_do_insert=None):
        base = {3170: {"DESCRICAO": "PAR BASE", "BLOQUEADO": "Nao"}}
        self.produtos = copy.deepcopy(produtos if produtos is not None else base)
        self.marcas = marcas if marcas is not None else {17: "Kolosh"}
        self.kit, self.commits, self.rollbacks, self.escritas = [], 0, 0, []
        self.lento, self.relogio = lento, relogio
        self.falhas = {k: list(v) for k, v in (falhas or {}).items()}
        self.antes_do_insert = antes_do_insert
        self._foto = None

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1
        self._foto = None

    def rollback(self):
        self.rollbacks += 1
        if self._foto is not None:
            self.produtos, self.kit = self._foto
            self._foto = None

    def executar(self, sql, params):
        if sql.split(" ", 1)[0] in ("INSERT", "UPDATE", "DELETE"):
            self.escritas.append((sql, params))
            if self._foto is None:
                self._foto = (copy.deepcopy(self.produtos), list(self.kit))
        for prefixo, fila in self.falhas.items():
            if sql.startswith(prefixo) and fila:
                raise fila.pop(0)
        return self._responder(sql, params)

    def _responder(self, sql, params):  # noqa: C901 — um ramo por SQL que o Portal manda
        if sql == "SELECT SEQ, DESCRICAO FROM PRODUTOS":
            return [(s, p.get("DESCRICAO")) for s, p in self.produtos.items()], 0
        if sql == "SELECT CODIGO, MARCA FROM PRODUTOS_MARCA":
            return list(self.marcas.items()), 0
        if sql == "SELECT 1 FROM RDB$DATABASE":
            return [(1,)], 1
        if sql == "SELECT MAX(SEQ) FROM PRODUTOS":
            return [(max(self.produtos, default=None),)], 1
        if sql == "SELECT MAX(CODIGO) FROM PRODUTOS_KIT":
            return [(max((k[0] for k in self.kit), default=None),)], 1
        m = re.fullmatch(r"SELECT (.+) FROM PRODUTOS WHERE SEQ = \?", sql)
        if m:
            p = self.produtos.get(params[0])
            if p is None:
                return [], 0
            cols = [c.strip() for c in m.group(1).split(",")]
            return [tuple(params[0] if c == "SEQ" else p.get(c) for c in cols)], 1
        m = re.fullmatch(r"INSERT INTO PRODUTOS \((.+)\) VALUES \(.+\)", sql)
        if m:
            if self.antes_do_insert:
                self.antes_do_insert(params[0])
            if self.relogio is not None:
                self.relogio.agora += self.lento
            linha = dict(zip([c.strip() for c in m.group(1).split(",")], params, strict=True))
            seq = linha.pop("SEQ")
            if seq in self.produtos:
                raise COLISAO()
            self.produtos[seq] = linha
            return [], 1
        if sql.startswith("INSERT INTO PRODUTOS_KIT (CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD)"):
            self.kit.append(params)
            return [], 1
        if sql == "DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?":
            antes = len(self.kit)
            self.kit = [k for k in self.kit if k[1] != params[0]]
            return [], antes - len(self.kit)
        if sql == "UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?":
            if params[0] not in self.produtos:
                return [], 0
            self.produtos[params[0]]["BLOQUEADO"] = "Sim"
            return [], 1
        m = re.fullmatch(r"UPDATE PRODUTOS SET (.+) WHERE SEQ = \?", sql)
        if m:
            cols = [c.split("=")[0].strip() for c in m.group(1).split(",")]
            seq = params[-1]
            if seq not in self.produtos:
                return [], 0
            self.produtos[seq].update(dict(zip(cols, params[:-1], strict=True)))
            return [], 1
        raise AssertionError(f"SQL inesperado no Fire: {sql}")


class Relogio:
    def __init__(self):
        self.agora = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)

    def __call__(self):
        return self.agora


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    for var in (cv.VAR_MODO, cv.VAR_P0, cv.VAR_INATIVAR):
        monkeypatch.delenv(var, raising=False)
    cv._avisados.clear()
    monkeypatch.setattr(cv, "_ultima_volta", None)
    monkeypatch.setattr(cv, "_ultimo_ciclo", {})


@pytest.fixture(autouse=True)
def _sem_jargao_nos_erros():
    """Toda mensagem de erro que vai ao Flow, em todo teste deste arquivo, é para gente."""
    _FLOWS.clear()
    yield
    for flow in _FLOWS:
        for _, corpo in flow.posts:
            if corpo["resultado"] == "erro":
                msg = corpo["erro"]["mensagem"]
                assert not any(j in msg for j in JARGAO), msg
                assert "VALOR-DO-USUARIO" not in msg


@pytest.fixture
def env_slug():
    return "mm"


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


@pytest.fixture
def modo(monkeypatch, env_slug):
    def _set(m, p0=True, inativar=False):
        monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:{m}")
        if p0:
            monkeypatch.setenv(cv.VAR_P0, env_slug)
        else:
            monkeypatch.delenv(cv.VAR_P0, raising=False)
        if inativar:
            monkeypatch.setenv(cv.VAR_INATIVAR, env_slug)
        else:
            monkeypatch.delenv(cv.VAR_INATIVAR, raising=False)
    return _set


def _log(nivel):
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level=nivel,
                     filter=lambda r: r["level"].name == nivel)
    return msgs, hid


@pytest.fixture
def erros():
    msgs, hid = _log("ERROR")
    yield msgs
    logger.remove(hid)


@pytest.fixture
def avisos():
    msgs, hid = _log("WARNING")
    yield msgs
    logger.remove(hid)


def _rodar(env_slug, flow, fire, env_conn, relogio=None):
    return cv.processar_ambiente(env_slug, _client=flow, _fire_conn=fire, _env_conn=env_conn,
                                 _relogio=relogio or Relogio())


def _reserva(flow):
    """O GET que reserva (sem `simular`); a contagem de "aguardando modo" vem antes dele."""
    return next(g for g in flow.gets if not g.get("simular"))


def _mais(horas=2):
    r = Relogio()
    r.agora += timedelta(hours=horas)
    return r


def _no_catalogo(env_conn, seq):
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em) "
        "VALUES (?, ?, 'X', '2026-10-16')",
        (seq, seq),
    )


def _outro(n, **kw):
    """Outro produto do Flow (cadastroId e produtoId próprios)."""
    # Como o Flow (fila.ts): só o criar de par leva codigoMes; kit e pack vão sem.
    sem_codigo = {"codigoMes": None} if kw.get("tipo") in ("kit", "pack") else {}
    return {**PAR, "cadastroId": f"5555555{n}-5555-4555-8555-555555555555",
            "produtoId": f"6666666{n}-6666-4666-8666-666666666666", **sem_codigo, **kw}


def _atualizar(seq, **kw):
    return {**PAR, "cadastroId": "44444444-4444-4444-8444-444444444444", "versao": 2,
            "acao": "atualizar", "erpProdutoId": seq, "marcaErp": None, **kw}


# --- brief ---------------------------------------------------------------


def test_sem_p0_recusa_iniciar(modo, env_slug, env_conn, erros):
    modo("criar", p0=False)
    flow = Flow([PAR])
    r = _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert "recusado" in r and flow.gets == []
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert sum(cv.VAR_P0 in e for e in erros) == 1  # uma vez por motivo


def test_criar_grava_e_confirma(modo, env_slug, env_conn):
    modo("criar")
    flow, fire = Flow([PAR]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert _reserva(flow)["acoes"] == ("criar",) and _reserva(flow)["limite"] == cv.LIMITE_GRAVACAO
    assert 3171 in fire.produtos
    assert fire.produtos[3171]["MARCA"] == "Kolosh" and fire.produtos[3171]["GRUPO"] == 8
    # DESCRICAO2 é a descrição interna da tela: recebe a descrição, nunca a cor (17/10 e 27/10/2026).
    assert fire.produtos[3171]["DESCRICAO2"] == fire.produtos[3171]["DESCRICAO"]
    cid, corpo = flow.posts[0]
    assert cid == PAR["cadastroId"]
    assert corpo["erpProdutoId"] == "3171" and corpo["gravadoEm"].endswith("Z")
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert (linha["estado"], linha["erp_produto_id"], linha["produto_id"]) == (
        "confirmado", "3171", PROD)


def test_sem_capacidade_nao_grava(modo, env_slug, env_conn):
    modo("criar")
    flow, fire = Flow([PAR], caps=()), FireGravavel()
    r = _rodar(env_slug, flow, fire, env_conn)
    assert fire.escritas == [] and flow.posts == []
    assert r["capacidade_ok"] is False
    assert repo.buscar(env_conn, PAR["cadastroId"], 1) is None


def test_criar_reprocessa_item_simulado(modo, env_slug, env_conn):
    repo.registrar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, estado="simulado",
                   acao="criar", tipo="simples", erro_codigo=None, erro_mensagem=None,
                   plano_json="{}")
    modo("criar")
    flow, fire = Flow([PAR]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert 3171 in fire.produtos


def test_post_perdido_vira_gravado_sem_volta_e_reenvia_sem_fire(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(None, None, None)])
    fire = FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "gravado_sem_volta"
    escritas = len(fire.escritas)
    flow2 = Flow([])
    _rodar(env_slug, flow2, fire, env_conn, _mais())
    assert flow2.posts[0][1]["erpProdutoId"] == "3171"
    assert len(fire.escritas) == escritas
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "confirmado"


def test_pendencia_reenviada_com_modo_desligado(modo, monkeypatch, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(503, None, None)]),
           FireGravavel(), env_conn)
    monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:desligado")
    flow2 = Flow([])
    cv.processar_ambiente(env_slug, _client=flow2, _fire_conn=None, _env_conn=env_conn,
                          _relogio=_mais())
    assert len(flow2.posts) == 1 and flow2.gets == []
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "confirmado"


def test_pendencia_reenviada_com_modo_simular(modo, monkeypatch, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(503, None, None)]),
           FireGravavel(), env_conn)
    monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:simular")
    flow2 = Flow([])
    cv.processar_ambiente(env_slug, _client=flow2, _fire_conn=None, _env_conn=env_conn,
                          _relogio=_mais())
    assert len(flow2.posts) == 1 and flow2.gets[0].get("simular") is True


def test_9b_criar_repetido_reenvia_v1_e_cala_v2(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]),
           FireGravavel(), env_conn)
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn, _mais())
    assert [p[0] for p in flow.posts] == [PAR["cadastroId"]]
    assert flow.posts[0][1]["versao"] == 1
    assert fire.escritas == []
    assert repo.buscar(env_conn, v2["cadastroId"], 2) is None


def test_9b_v1_ainda_em_espera_nao_reenvia_nem_responde_v2(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]),
           FireGravavel(), env_conn)
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)  # mesma hora: a espera da v1 não venceu
    assert flow.posts == [] and fire.escritas == []
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "gravado_sem_volta"


def test_nome_existe_vai_com_codigo_na_mensagem(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel(produtos={3100: {"DESCRICAO": "meia soquete", "BLOQUEADO": "Nao"}})
    flow = Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert erro == {"tipo": "definitivo", "codigo": "nome_existe",
                    "mensagem": "Já existe produto com esse nome no ERP (código 3100)"}
    assert fire.escritas == []
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "erro_registrado"


def test_marca_ausente_vai_como_definitivo(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([{**PAR, "marcaErp": {"codigo": 99}}])
    fire = FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["erro"]["tipo"] == "definitivo"
    assert flow.posts[0][1]["erro"]["codigo"] == "marca_ausente_erp"
    assert fire.escritas == []
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "erro_registrado"


def test_orfao_em_conflito_codigo(modo, env_slug, env_conn, erros):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(409, {"error": "conflito_codigo"}, None)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "orfao"
    assert any("3171" in e for e in erros)


def test_versao_superada_em_criar_vira_orfao(modo, env_slug, env_conn, erros):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(409, {"error": "versao_superada",
                                                          "versaoAtual": 2}, None)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "orfao"
    assert any("órfão" in e and "3171" in e for e in erros)


@pytest.mark.parametrize("erro", ["versao_superada", "conflito_codigo"])
@pytest.mark.parametrize("acao", ["atualizar", "inativar"])
def test_409_em_atualizar_ou_inativar_fecha_superado_sem_orfao(modo, env_slug, env_conn, erros,
                                                                acao, erro):
    """Contrato §5: órfão só se o Portal CRIOU o produto; em atualizar/inativar o 409
    `versao_superada` é o caso comum, e `conflito_codigo` ganha alerta próprio."""
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar", inativar=True)
    item = _atualizar("3170", acao=acao, versao=3)
    flow = Flow([item], respostas=[RespostaResultado(409, {"error": erro, "versaoAtual": 4}, None)])
    antes = _amostra("portal_flowpcp_cadastro_volta_total", resultado="superado")
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    linha = repo.buscar(env_conn, item["cadastroId"], 3)
    assert linha["estado"] == "superado"
    assert linha["alerta"] == ("conflito_codigo" if erro == "conflito_codigo" else None)
    assert linha["resposta_flow"] == f"409:{erro}"
    assert not any("órfão" in e for e in erros)
    assert (erro == "conflito_codigo") == any("conflito_codigo" in e for e in erros)
    assert _amostra("portal_flowpcp_cadastro_volta_total", resultado="superado") == antes + 1
    assert repo.listar_por_estado(env_conn, "orfao") == []


def test_atualizar_de_seq_criado_pelo_portal_esta_no_escopo(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR]), FireGravavel(), env_conn)
    modo("criar_e_atualizar")
    fire = FireGravavel(produtos={3171: {"DESCRICAO": "MEIA SOQUETE", "BLOQUEADO": "Nao"}})
    flow = Flow([_atualizar("3171", unidadeErp="PR")])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert flow.posts[0][1]["erpProdutoId"] == "3171"
    assert fire.produtos[3171]["UNIDADE"] == "PR"
    assert "MARCA" not in fire.produtos[3171]  # marcaErp null = não mexer


def test_inativar_so_com_portao(modo, env_slug, env_conn):
    modo("criar_e_atualizar")
    flow = Flow([])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert _reserva(flow)["acoes"] == ("criar", "atualizar")
    modo("criar_e_atualizar", inativar=True)
    flow = Flow([])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert _reserva(flow)["acoes"] == ("criar", "atualizar", "inativar")


def test_prazo_do_lote_para_de_iniciar_itens(modo, env_slug, env_conn):
    modo("criar")
    relogio = Relogio()
    itens = [_outro(i, nome=f"MEIA {i}") for i in range(3)]
    fire = FireGravavel(lento=timedelta(minutes=5), relogio=relogio)
    flow = Flow(itens)
    _rodar(env_slug, flow, fire, env_conn, relogio)
    assert len(flow.posts) == 2  # o 3º começaria depois de 10 min do GET
    assert repo.buscar(env_conn, itens[2]["cadastroId"], 1) is None


def test_fire_fora_responde_transitorio(modo, env_slug, env_conn, monkeypatch):
    modo("criar")
    flow = Flow([PAR])

    def _falha(*a, **k):
        raise FirebirdConnectionError("recusou")

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _falha)
    cv.processar_ambiente(env_slug, _client=flow, _fire_conn=None, _env_conn=env_conn,
                          _relogio=Relogio())
    assert flow.posts[0][1]["erro"] == {"tipo": "transitorio", "codigo": "conexao_recusada",
                                        "mensagem": "O ERP recusou a conexão."}
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "transitorio"


# --- Emenda 1 ------------------------------------------------------------


@pytest.mark.parametrize("resposta", [None, RespostaResultado(None, None, None)])
def test_erp_fora_com_item_gravado_sem_volta_reentregue_so_reenvia_o_gravado(
    modo, env_slug, env_conn, monkeypatch, resposta
):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]),
           FireGravavel(), env_conn)

    def _falha(*a, **k):
        raise FirebirdConnectionError("recusou")

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _falha)
    flow = Flow([PAR], respostas=[resposta] if resposta else None)
    cv.processar_ambiente(env_slug, _client=flow, _fire_conn=None, _env_conn=env_conn,
                          _relogio=_mais())
    assert len(flow.posts) == 1
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] in (
        "gravado_sem_volta", "confirmado")


def test_bug_corpo_mantem_gravado_sem_volta(modo, env_slug, env_conn, erros):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(400, {"error": "payload inválido"}, None)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert (linha["estado"], linha["alerta"]) == ("gravado_sem_volta", "bug_corpo")
    assert linha["proxima_tentativa_em"] == "2026-10-17T14:00:00"
    assert erros


def test_pendencias_antes_da_p0(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]),
           FireGravavel(), env_conn)
    modo("criar", p0=False)
    flow = Flow([PAR])
    r = _rodar(env_slug, flow, FireGravavel(), env_conn, _mais())
    assert len(flow.posts) == 1 and "recusado" in r and flow.gets == []


def test_9b_orfao_responde_ja_criado_no_erp(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(409, {"error": "conflito_codigo"},
                                                              None)]),
           FireGravavel(), env_conn)
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert fire.escritas == []
    (cid, corpo), = flow.posts
    assert cid == v2["cadastroId"] and corpo["versao"] == 2
    assert corpo["erro"]["tipo"] == "definitivo"
    assert corpo["erro"]["codigo"] == "ja_criado_no_erp"
    assert "(código 3171)" in corpo["erro"]["mensagem"]


def _incerto_v1(env_conn):
    repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, produto_id=PROD,
                  acao="criar", tipo="simples", modo="criar", plano_json="{}")
    repo.marcar_seq(env_conn, PAR["cadastroId"], 1, 3171)
    repo.marcar_desfecho(env_conn, PAR["cadastroId"], 1, "incerto")


def test_9b_incerto_nao_grava_nem_responde(modo, env_slug, env_conn, erros):
    _incerto_v1(env_conn)
    modo("criar")
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == []
    assert repo.buscar(env_conn, v2["cadastroId"], 2) is None
    assert any("9b:" in e and "ferramenta do operador" in e for e in erros)


def test_9b_liberado_com_seq_nao_grava_nem_responde(modo, env_slug, env_conn, erros):
    repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, produto_id=PROD,
                  acao="criar", tipo="simples", modo="criar", plano_json="{}")
    repo.marcar_seq(env_conn, PAR["cadastroId"], 1, 3171)
    env_conn.execute("UPDATE cadastro_volta SET estado = 'liberado'")  # cenário legado
    modo("criar")
    flow, fire = Flow([PAR]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == []
    assert erros


@pytest.mark.parametrize("estado", ["erro_a_enviar", "erro_registrado", "superado", "confirmado"])
def test_9b_erro_ou_superado_com_seq_nao_grava_nem_responde(modo, env_slug, env_conn, erros,
                                                             estado):
    """Um `criar` com SEQ nesses estados pode ter gravado: a v2 não cria outro produto."""
    repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, produto_id=PROD,
                  acao="criar", tipo="simples", modo="criar", plano_json="{}")
    repo.marcar_seq(env_conn, PAR["cadastroId"], 1, 3171)
    env_conn.execute("UPDATE cadastro_volta SET estado = ?", (estado,))  # cenário forçado
    modo("criar")
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == []
    # N3: a ferramenta do operador não age nesses estados; o log aponta o caminho manual.
    (msg,) = [e for e in erros if "9b:" in e]
    assert "tools/produto_no_fire.py" in msg and "Caminhos manuais" in msg
    assert "ferramenta do operador" not in msg


def test_9b_em_andamento_pula(modo, env_slug, env_conn):
    repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, produto_id=PROD,
                  acao="criar", tipo="simples", modo="criar", plano_json="{}")
    modo("criar")
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == []


def test_nome_existe_do_proprio_produto_vai_para_recuperacao(modo, env_slug, env_conn, erros):
    # O produto já existe no ERP como 3100, por uma linha do livro que não é `criar`.
    repo.reservar(env_conn, cadastro_id="99999999-9999-4999-8999-999999999999", versao=1,
                  produto_id=PROD, acao="atualizar", tipo="simples", modo="criar_e_atualizar",
                  plano_json="{}")
    repo.marcar_gravado(env_conn, "99999999-9999-4999-8999-999999999999", 1,
                        erp_produto_id="3100", corpo_json="{}")
    repo.marcar_desfecho(env_conn, "99999999-9999-4999-8999-999999999999", 1, "confirmado")
    modo("criar")
    fire = FireGravavel(produtos={3100: {"DESCRICAO": "MEIA SOQUETE", "BLOQUEADO": "Nao"}})
    flow = Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == []  # nem nome_existe, nem gravação
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert linha["estado"] == "incerto"
    assert any("3100" in e for e in erros)


def test_erro_recusa_do_erp_vira_definitivo_erp_recusou(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel(falhas={"INSERT INTO PRODUTOS (": [RECUSA()]})
    flow = Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn)
    assert 3171 not in fire.produtos and fire.rollbacks == 2  # o do gravador e a prova
    erro = flow.posts[0][1]["erro"]
    assert (erro["tipo"], erro["codigo"]) == ("definitivo", "erp_recusou")
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert linha["estado"] == "erro_registrado" and linha["seq_reservado"] is None
    assert "VALOR-DO-USUARIO" not in (linha["erro_mensagem"] or "")


def test_lock_do_erp_vira_transitorio(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel(falhas={"INSERT INTO PRODUTOS (": [LOCK()]})
    flow = Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert (erro["tipo"], erro["codigo"]) == ("transitorio", "erp_ocupado")
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert linha["estado"] == "transitorio" and linha["seq_reservado"] is None
    # O item volta no próximo GET e grava.
    flow2 = Flow([PAR])
    _rodar(env_slug, flow2, fire, env_conn)
    assert flow2.posts[0][1]["resultado"] == "gravado" and 3171 in fire.produtos


def test_seq_tomado_vira_transitorio_erp_ocupado(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel(falhas={"INSERT INTO PRODUTOS (": [COLISAO(), COLISAO(), COLISAO()]})
    flow = Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert (erro["tipo"], erro["codigo"]) == ("transitorio", "erp_ocupado")
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert linha["estado"] == "transitorio" and linha["seq_reservado"] is None


def test_conexao_caida_na_gravacao_deixa_em_andamento_e_para_o_lote(modo, env_slug, env_conn,
                                                                     erros):
    modo("criar")
    fire = FireGravavel(falhas={"INSERT INTO PRODUTOS (": [CONEXAO()]})
    dois = _outro(1, nome="MEIA OUTRA")
    flow = Flow([PAR, dois])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == []
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert (linha["estado"], linha["seq_reservado"]) == ("em_andamento", 3171)
    assert repo.buscar(env_conn, dois["cadastroId"], 1) is None
    assert erros


def test_seq_no_disco_antes_do_insert(modo, env_slug, env_conn):
    modo("criar")
    vistos = []

    def _confere(seq):
        with router.env_connect("mm") as outra:  # outra conexão só vê o que foi commitado
            vistos.append(outra.execute(
                "SELECT seq_reservado FROM cadastro_volta WHERE cadastro_id = ?",
                (PAR["cadastroId"],)).fetchone()[0])

    fire = FireGravavel(antes_do_insert=_confere)
    _rodar(env_slug, Flow([PAR]), fire, env_conn)
    assert vistos == [3171]


def test_atualizar_ean_null_limpa_no_erp(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = FireGravavel(produtos={3170: {
        "DESCRICAO": "MEIA SOQUETE", "BLOQUEADO": "Nao", "CODIGO_EAN13": "7891234567895",
        "CODPARANFE": "NFE-1", "CLASSIF_FISCAL": "61159500", "CODITEMSPED": 4}})
    item = _atualizar("3170", ean=None, codigoNfe=None, ncm=None, tipoItemSped=None)
    flow = Flow([item])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    p = fire.produtos[3170]
    assert p["CODIGO_EAN13"] is None and p["CODPARANFE"] is None
    assert p["CLASSIF_FISCAL"] == "61159500" and p["CODITEMSPED"] == 4  # null = não mexer
    (update,) = [s for s, _ in fire.escritas if s.startswith("UPDATE")]
    assert "CLASSIF_FISCAL" not in update and "CODITEMSPED" not in update


def test_atualizar_kit_troca_a_composicao(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3990")
    modo("criar_e_atualizar")
    fire = FireGravavel(produtos={
        3170: {"DESCRICAO": "PAR", "BLOQUEADO": "Nao"},
        3990: {"DESCRICAO": "KIT", "BLOQUEADO": "Nao"}})
    fire.kit = [(1, 3990, 3100, 2.0)]
    item = _atualizar("3990", tipo="kit", unidadeErp="KIT",
                      componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    _rodar(env_slug, Flow([item]), fire, env_conn)
    assert fire.kit == [(1, 3990, 3170, 3.0)]  # só a composição do pai foi trocada


def test_atualizar_no_modo_criar_e_processado(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar")
    fire = FireGravavel()
    flow = Flow([_atualizar("3170", marcaErp={"codigo": 17})])
    _rodar(env_slug, flow, fire, env_conn)
    assert _reserva(flow)["acoes"] == ("criar",)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert fire.produtos[3170]["DESCRICAO"] == "MEIA SOQUETE"
    assert fire.produtos[3170]["MARCA"] == "Kolosh"


def test_mesmo_nome_duas_vezes_no_lote(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel()
    segundo = _outro(1)
    flow = Flow([PAR, segundo])
    _rodar(env_slug, flow, fire, env_conn)
    assert [s for s in fire.produtos if s > 3170] == [3171]
    assert flow.posts[0][1]["resultado"] == "gravado"
    erro = flow.posts[1][1]["erro"]
    assert erro["codigo"] == "nome_existe" and "(código 3171)" in erro["mensagem"]


def test_kit_criado_com_composicao(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel()
    kit = _outro(1, tipo="kit", nome="KIT 3 PARES", unidadeErp="KIT",
                 componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    _rodar(env_slug, Flow([kit]), fire, env_conn)
    assert fire.kit == [(1, 3171, 3170, 3.0)] and fire.commits == 1


def test_componente_ausente_e_definitivo(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel()
    kit = _outro(1, tipo="kit", nome="KIT", unidadeErp="KIT",
                 componentes=[{"erpProdutoId": "4000", "quantidade": 3}])
    flow = Flow([kit])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["erro"]["codigo"] == "componente_ausente"
    assert fire.escritas == []


def test_item_invalido_responde_definitivo(modo, env_slug, env_conn):
    modo("criar")
    flow, fire = Flow([{**PAR, "ncm": "123"}]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert (erro["tipo"], erro["codigo"]) == ("definitivo", "contrato_invalido")
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert linha["estado"] == "erro_registrado" and "ncm" in linha["erro_mensagem"]
    assert fire.escritas == []


def test_componente_invalido_vai_com_frase_de_contrato(modo, env_slug, env_conn):
    modo("criar")
    kit = {**PAR, "tipo": "kit", "componentes": [], "codigoMes": None}
    flow = Flow([kit])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    erro = flow.posts[0][1]["erro"]
    assert erro["codigo"] == "componente_invalido"
    assert erro["mensagem"] == mensagem_para_flow("contrato_invalido")


def test_item_sem_id_legivel_so_loga(modo, env_slug, env_conn, avisos):
    modo("criar")
    flow = Flow([{"cadastroId": "<b>x</b>", "versao": 1}])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert flow.posts == [] and repo.listar(env_conn) == []
    assert avisos and not any("<b>" in a for a in avisos)


def test_material_nao_e_gravado(modo, env_slug, env_conn):
    modo("criar")
    flow, fire = Flow([{**PAR, "tipo": "material"}]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert (erro["tipo"], erro["codigo"]) == ("definitivo", "tipo_nao_suportado")
    assert fire.escritas == []


def test_atualizar_fora_do_escopo(modo, env_slug, env_conn):
    modo("criar_e_atualizar")
    flow, fire = Flow([_atualizar("3170")]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["erro"]["codigo"] == "seq_fora_do_escopo"
    assert fire.escritas == []


def test_atualizar_seq_que_sumiu_do_fire(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3500")
    modo("criar_e_atualizar")
    flow = Flow([_atualizar("3500")])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    erro = flow.posts[0][1]["erro"]
    assert (erro["tipo"], erro["codigo"]) == ("definitivo", "seq_inexistente")


def test_inativar_com_portao_bloqueia(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar", inativar=True)
    fire = FireGravavel()
    item = _atualizar("3170", acao="inativar", versao=3)
    flow = Flow([item])
    _rodar(env_slug, flow, fire, env_conn)
    assert fire.produtos[3170]["BLOQUEADO"] == "Sim"
    assert flow.posts[0][1]["resultado"] == "gravado"


def test_inativar_sem_portao_nao_processa_nem_responde(modo, env_slug, env_conn, avisos):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = FireGravavel()
    flow = Flow([_atualizar("3170", acao="inativar", versao=3)])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == []
    assert repo.listar(env_conn) == []
    assert any("inativar" in a for a in avisos)


def test_credencial_para_os_posts_do_ciclo(modo, env_slug, env_conn, erros):
    modo("criar")
    ruins = [_outro(1, ncm="1"), _outro(2, ncm="2")]
    flow = Flow(ruins, respostas=[RespostaResultado(401, {"error": "unauthorized"}, None)])
    fire = FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert len(flow.posts) == 1 and fire.escritas == []
    linha = repo.buscar(env_conn, ruins[0]["cadastroId"], 1)
    assert (linha["estado"], linha["proxima_tentativa_em"]) == (
        "erro_a_enviar", "2026-10-17T13:05:00")
    assert erros


def test_espera_crescente_e_retry_after(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(429, None, 600)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    linha = repo.buscar(env_conn, PAR["cadastroId"], 1)
    assert linha["proxima_tentativa_em"] == "2026-10-17T13:10:00"  # 10 min > 1 min
    assert cv.reenviar_pendencias(env_slug, env_conn, Flow([]), agora=Relogio()()) == {}


def test_chave_desconhecida_avisa_uma_vez_no_modo_de_gravacao(modo, env_slug, env_conn, avisos):
    modo("criar")
    for _ in range(2):
        _rodar(env_slug, Flow([{**PAR, "campoNovo": 1}]), FireGravavel(), env_conn)
    assert sum("'campoNovo'" in a for a in avisos) == 1


def test_mensagem_ao_flow_sem_jargao(modo, env_slug, env_conn):
    """Um lote com um erro de cada classe; o fixture autouse confere todos os POSTs."""
    _no_catalogo(env_conn, "3500")
    modo("criar_e_atualizar")
    fire = FireGravavel(produtos={3100: {"DESCRICAO": "MEIA 9", "BLOQUEADO": "Nao"},
                                  3170: {"DESCRICAO": "PAR", "BLOQUEADO": "Sim"}})
    itens = [
        _outro(1, ncm="1"),
        _outro(2, tipo="material"),
        _outro(3, marcaErp={"codigo": 99}),
        _outro(4, tipo="kit", unidadeErp="KIT", componentes=[{"erpProdutoId": "3170",
                                                                "quantidade": 1}]),
        _outro(5, tipo="kit", unidadeErp="KIT", componentes=[{"erpProdutoId": "4999",
                                                                "quantidade": 1}]),
        _outro(6, acao="atualizar", erpProdutoId="2000", marcaErp=None),
        _outro(7, acao="atualizar", erpProdutoId="3500", marcaErp=None),
        _outro(9, nome="MEIA 9"),
    ]
    flow = Flow(itens)
    _rodar(env_slug, flow, fire, env_conn)
    assert len(flow.posts) == len(itens)
    assert all(c["resultado"] == "erro" for _, c in flow.posts)


# --- rodar() --------------------------------------------------------------


def test_rodar_visita_ambiente_com_pendencia_mesmo_sem_variavel(monkeypatch, env_conn):
    repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, produto_id=PROD,
                  acao="criar", tipo="simples", modo="criar", plano_json="{}")
    repo.marcar_gravado(env_conn, PAR["cadastroId"], 1, erp_produto_id="3171", corpo_json="{}")
    env_conn.commit()
    monkeypatch.setattr(cv, "_slugs_flowpcp", lambda: ["mm", "nasmar"])
    visitados = []
    monkeypatch.setattr(cv, "processar_ambiente",
                        lambda slug, **k: visitados.append(slug) or {"modo": "desligado"})
    monkeypatch.setattr(cv, "enabled_flowpcp_envs",
                        lambda: pytest.fail("sem variável não decifra token"))
    cv.rodar()
    assert visitados == ["mm"]


def test_rodar_sem_variavel_e_sem_pendencia_nao_processa(monkeypatch, env_conn):
    monkeypatch.setattr(cv, "_slugs_flowpcp", lambda: ["mm"])
    monkeypatch.setattr(cv, "processar_ambiente",
                        lambda slug, **k: pytest.fail("não podia abrir token"))
    monkeypatch.setattr(cv, "enabled_flowpcp_envs", lambda: pytest.fail("token"))
    assert cv.rodar() == {}


def test_rodar_com_variavel_inclui_desligado_com_pendencia(monkeypatch, env_conn):
    repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, produto_id=PROD,
                  acao="criar", tipo="simples", modo="criar", plano_json="{}")
    repo.marcar_gravado(env_conn, PAR["cadastroId"], 1, erp_produto_id="3171", corpo_json="{}")
    env_conn.commit()
    monkeypatch.setenv(cv.VAR_MODO, "nasmar:simular")
    monkeypatch.setattr(cv, "enabled_flowpcp_envs", lambda: {"mm": object(), "nasmar": object()})
    visitados = []
    monkeypatch.setattr(cv, "processar_ambiente",
                        lambda slug, **k: visitados.append(slug) or {"modo": "x"})
    cv.rodar()
    assert sorted(visitados) == ["mm", "nasmar"]


def test_recusa_sem_rollback_comprovado_fica_para_a_recuperacao(modo, env_slug, env_conn, erros):
    """O ERP recusou o kit, o rollback falhou e o PRODUTOS ficou visível: sem prova
    de que nada comitou, a linha fica em_andamento (nem erro, nem resposta)."""

    class _SemRollback(FireGravavel):
        def rollback(self):
            self.rollbacks += 1
            raise OSError("rollback caiu")

    modo("criar")
    fire = _SemRollback(falhas={"INSERT INTO PRODUTOS_KIT": [RECUSA()]})
    kit = _outro(1, tipo="kit", nome="KIT", unidadeErp="KIT",
                 componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    flow = Flow([kit, _outro(2, nome="OUTRO")])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == []
    linha = repo.buscar(env_conn, kit["cadastroId"], 1)
    assert (linha["estado"], linha["seq_reservado"]) == ("em_andamento", 3171)
    assert repo.buscar(env_conn, _outro(2)["cadastroId"], 1) is None  # lote parado
    assert erros


def test_duas_versoes_criar_do_mesmo_produto_no_mesmo_lote(modo, env_slug, env_conn):
    modo("criar")
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    fire = FireGravavel()
    flow = Flow([PAR, v2])
    _rodar(env_slug, flow, fire, env_conn)
    assert [s for s in fire.produtos if s > 3170] == [3171]
    assert [p[0] for p in flow.posts] == [PAR["cadastroId"]]
    assert repo.buscar(env_conn, v2["cadastroId"], 2) is None


def test_item_repetido_no_mesmo_get_grava_uma_vez(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel()
    flow = Flow([PAR, dict(PAR)])
    _rodar(env_slug, flow, fire, env_conn)
    assert len([s for s, _ in fire.escritas if s.startswith("INSERT INTO PRODUTOS (")]) == 1
    assert len(flow.posts) == 1


# --- fix round 1 ------------------------------------------------------------


class _RollbackFalha(FireGravavel):
    """Rollback que falha nas `falhas_rb` primeiras vezes (o pendente fica)."""

    def __init__(self, falhas_rb=10**6, **kw):
        super().__init__(**kw)
        self.falhas_rb = falhas_rb

    def rollback(self):
        self.rollbacks += 1
        if self.falhas_rb > 0:
            self.falhas_rb -= 1
            raise OSError("rollback caiu")
        super().rollback()
        self.rollbacks -= 1  # super() contou de novo


def test_9b_orfao_fechado_responde_ja_criado_e_nao_grava(modo, env_slug, env_conn):
    """Sonda 7b: v1 órfã, fechada pela operação; a v2 com outro nome não vira 2º produto."""
    modo("criar")
    fire = FireGravavel()
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(409, {"error": "conflito_codigo"},
                                                              None)]), fire, env_conn)
    assert repo.fechar_orfao(env_conn, PAR["cadastroId"], 1, operador="ana", nota="x")
    env_conn.commit()
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2,
          "nome": "MEIA SOQUETE CORRIGIDA"}
    flow = Flow([v2])
    _rodar(env_slug, flow, fire, env_conn)
    assert sorted(fire.produtos) == [3170, 3171]
    (cid, corpo), = flow.posts
    assert cid == v2["cadastroId"] and corpo["erro"]["codigo"] == "ja_criado_no_erp"
    assert corpo["erro"]["tipo"] == "definitivo" and "(código 3171)" in corpo["erro"]["mensagem"]


def test_atualizar_kit_com_rollback_falhando_nao_responde_e_para_o_lote(modo, env_slug, env_conn,
                                                                         erros):
    """Sonda 7, cenário 1: o DELETE da composição pendente nunca é comitado depois."""
    _no_catalogo(env_conn, "3990")
    modo("criar_e_atualizar")
    fire = _RollbackFalha(produtos={3170: {"DESCRICAO": "PAR", "BLOQUEADO": "Nao"},
                                    3990: {"DESCRICAO": "KIT", "BLOQUEADO": "Nao"}},
                          falhas={"INSERT INTO PRODUTOS_KIT": [RECUSA()]})
    fire.kit = [(1, 3990, 3100, 2.0)]
    item = _atualizar("3990", tipo="kit", unidadeErp="KIT",
                      componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    seguinte = _outro(2, nome="OUTRO")
    flow = Flow([item, seguinte])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.commits == 0
    assert repo.buscar(env_conn, item["cadastroId"], 2)["estado"] == "em_andamento"
    assert repo.buscar(env_conn, seguinte["cadastroId"], 1) is None
    assert erros


def test_seq_sumido_com_rollback_falhando_nao_responde_e_para_o_lote(modo, env_slug, env_conn,
                                                                    monkeypatch, erros):
    """Sonda 7c: `LookupError` de atualizar sem prova de rollback. O UPDATE pendente
    nunca é comitado depois, nada vai ao Flow e a linha fica para a recuperação."""
    _no_catalogo(env_conn, "3990")
    modo("criar_e_atualizar")
    fire = _RollbackFalha(produtos={3170: {"DESCRICAO": "PAR", "BLOQUEADO": "Nao"},
                                    3990: {"DESCRICAO": "KIT", "BLOQUEADO": "Nao"}})

    def _sumiu(conn, *, seq, campos, componentes):
        conn.cursor().execute("UPDATE PRODUTOS SET DESCRICAO = ? WHERE SEQ = ?", ("X", seq))
        cadastro_fire.desfazer(conn)  # falha: o UPDATE fica pendente
        raise LookupError("SEQ sumiu")

    monkeypatch.setattr(cadastro_fire, "atualizar", _sumiu)
    item, seguinte = _atualizar("3990"), _outro(2, nome="OUTRO")
    flow = Flow([item, seguinte])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.commits == 0
    assert repo.buscar(env_conn, item["cadastroId"], 2)["estado"] == "em_andamento"
    assert repo.buscar(env_conn, seguinte["cadastroId"], 1) is None  # lote parado
    assert any("rollback não confirmou" in e for e in erros)


class _Tr:
    default_tpb = None


class _ConnFdb(_RollbackFalha):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.main_transaction, self.fechada = _Tr(), False

    def begin(self, tpb=None):
        pass

    def close(self):
        self.fechada = True


@pytest.mark.firebird_stub_proprio
def test_criar_kit_com_rollback_falhando_nao_comita_na_saida(modo, env_slug, env_conn,
                                                            monkeypatch, erros):
    """Sonda 7, cenário 2: a conexão de escrita real (com fdb.connect falso) sai com
    rollback; o PRODUTOS pendente do kit recusado não fica gravado."""
    import fdb

    from app.erp.connection import FirebirdConnection

    modo("criar")
    fire = _ConnFdb(falhas_rb=2, falhas={"INSERT INTO PRODUTOS_KIT": [RECUSA()]})
    monkeypatch.setattr(FirebirdConnection, "_configure_library", lambda self: None)
    monkeypatch.setattr(fdb, "connect", lambda **k: fire)
    monkeypatch.setattr(cv, "_abrir_fire_escrita",
                        lambda slug: FirebirdConnection().connect_escrita_cadastro({"path": "x"}))
    kit = _outro(1, tipo="kit", nome="KIT", unidadeErp="KIT",
                 componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    flow = Flow([kit])
    cv.processar_ambiente(env_slug, _client=flow, _fire_conn=None, _env_conn=env_conn,
                          _relogio=Relogio())
    assert flow.posts == [] and fire.commits == 0 and fire.fechada
    assert sorted(fire.produtos) == [3170] and fire.kit == []  # a saída desfez o pendente
    assert repo.buscar(env_conn, kit["cadastroId"], 1)["estado"] == "em_andamento"


def test_marcar_gravado_que_falha_para_o_lote(modo, env_slug, env_conn, monkeypatch, erros):
    modo("criar")
    monkeypatch.setattr(repo, "marcar_gravado", lambda *a, **k: False)
    fire, segundo = FireGravavel(), _outro(1, nome="OUTRO")
    flow = Flow([PAR, segundo])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and 3171 in fire.produtos
    assert repo.buscar(env_conn, segundo["cadastroId"], 1) is None
    assert any("3171" in e for e in erros)


def test_marcar_seq_que_falha_nao_insere_e_para_o_lote(modo, env_slug, env_conn, monkeypatch,
                                                      erros):
    modo("criar")
    monkeypatch.setattr(repo, "marcar_seq", lambda *a, **k: False)
    fire, segundo = FireGravavel(), _outro(1, nome="OUTRO")
    flow = Flow([PAR, segundo])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts == [] and fire.escritas == [] and sorted(fire.produtos) == [3170]
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "em_andamento"
    assert repo.buscar(env_conn, segundo["cadastroId"], 1) is None


def test_consulta_que_falha_vai_como_consulta_erp_falhou(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel(falhas={"SELECT SEQ, BLOQUEADO FROM PRODUTOS": [RuntimeError("x")]})
    kit = _outro(1, tipo="kit", nome="KIT", unidadeErp="KIT",
                 componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    flow = Flow([kit])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["erro"] == {
        "tipo": "transitorio", "codigo": "consulta_erp_falhou",
        "mensagem": "Não foi possível consultar o ERP agora; o Portal tenta de novo."}
    assert fire.escritas == []


def test_reenvio_na_triagem_sem_espaco_nas_pendencias(modo, env_slug, env_conn, monkeypatch):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]),
           FireGravavel(), env_conn)
    monkeypatch.setattr(cv, "LIMITE_PENDENCIAS", 0)  # o reenvio inicial não pega nada
    fire, flow = FireGravavel(), Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn, _mais())
    (cid, corpo), = flow.posts
    assert cid == PAR["cadastroId"] and corpo["resultado"] == "gravado"
    assert fire.escritas == []
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "confirmado"


def test_reenvio_da_v1_pela_9b_sem_espaco_nas_pendencias(modo, env_slug, env_conn, monkeypatch):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]),
           FireGravavel(), env_conn)
    monkeypatch.setattr(cv, "LIMITE_PENDENCIAS", 0)
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    fire, flow = FireGravavel(), Flow([v2])
    _rodar(env_slug, flow, fire, env_conn, _mais())
    (cid, corpo), = flow.posts
    assert cid == PAR["cadastroId"] and corpo["versao"] == 1
    assert fire.escritas == [] and repo.buscar(env_conn, v2["cadastroId"], 2) is None


# --- Task 12: métricas e "aguardando modo" ------------------------------------


def _amostra_ou_none(nome, **rotulos):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(nome, {"env": "mm", **rotulos})


def _amostra(nome, **rotulos):
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(nome, {"env": "mm", **rotulos}) or 0.0


class FlowAguarda(Flow):
    """Responde `n` itens à leitura sem reserva (`simular`) e a fila normal ao resto."""

    def __init__(self, itens, n=0, erro=None, **kw):
        super().__init__(itens, **kw)
        self.n, self.erro = n, erro

    def list_cadastros_pendentes(self, limite=20, **kw):
        if kw.get("simular"):
            self.gets.append({"limite": limite, **kw})
            if self.erro:
                raise self.erro
            return PendentesResposta(cadastros=[_outro(i) for i in range(self.n)], capacidades=self.caps)
        return super().list_cadastros_pendentes(limite, **kw)


def test_aguardando_modo_em_criar_conta_o_que_o_modo_nao_grava(modo, env_slug, env_conn):
    modo("criar")
    flow = FlowAguarda([], n=3)
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert flow.gets[0] == {"limite": 50, "acoes": ("atualizar", "inativar"), "simular": True}
    assert not flow.gets[1].get("simular")  # a ordem: contagem sem reserva, depois o GET que reserva
    assert cv.ultimo_ciclo(env_slug)["resumo"]["aguardando_modo"] == 3
    assert _amostra("portal_flowpcp_cadastro_volta_aguardando_modo") == 3


def test_aguardando_modo_sem_portao_de_inativar_pede_so_inativar(modo, env_slug, env_conn):
    modo("criar_e_atualizar")
    flow = FlowAguarda([], n=2)
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert flow.gets[0]["acoes"] == ("inativar",) and flow.gets[0]["simular"] is True
    assert cv.ultimo_ciclo(env_slug)["resumo"]["aguardando_modo"] == 2


def test_aguardando_modo_com_portao_nao_consulta(modo, env_slug, env_conn):
    modo("criar_e_atualizar", inativar=True)
    flow = FlowAguarda([], n=2)
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert len(flow.gets) == 1
    assert cv.ultimo_ciclo(env_slug)["resumo"]["aguardando_modo"] == 0
    assert _amostra_ou_none("portal_flowpcp_cadastro_volta_aguardando_modo") == 0  # zero medido


def test_aguardando_modo_400_vira_none_sem_derrubar_o_ciclo(modo, env_slug, env_conn, avisos):
    from app.integrations.flowpcp.client import FlowPCPClientError

    modo("criar")
    erro = FlowPCPClientError("x", status_code=400, body="VALOR-DO-FLOW")
    flow = FlowAguarda([PAR], erro=erro)
    r = _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert r["aguardando_modo"] is None and r["gravados"] == 1
    resumo = cv.ultimo_ciclo(env_slug)["resumo"]
    assert resumo["aguardando_modo"] is None and resumo["erro"] is None  # 400 não é erro do ciclo
    assert _amostra_ou_none("portal_flowpcp_cadastro_volta_aguardando_modo") is None
    _rodar(env_slug, FlowAguarda([], erro=erro), FireGravavel(), env_conn, _mais())
    assert sum("aguardando" in a for a in avisos) == 1  # WARNING uma vez
    assert not any("VALOR-DO-FLOW" in a for a in avisos)


def test_aguardando_modo_com_flow_fora_vira_none(modo, env_slug, env_conn):
    modo("criar")
    flow = FlowAguarda([PAR], erro=RuntimeError("rede"))
    r = _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert r["aguardando_modo"] is None and r["gravados"] == 1


def test_resultados_viram_metrica_por_rotulo_fixo(modo, env_slug, env_conn):
    modo("criar")
    antes = {k: _amostra("portal_flowpcp_cadastro_volta_total", resultado=k)
             for k in ("criado", "confirmado")}
    _rodar(env_slug, Flow([PAR]), FireGravavel(), env_conn)
    for k in antes:
        assert _amostra("portal_flowpcp_cadastro_volta_total", resultado=k) == antes[k] + 1


def test_transitorio_registrado_conta_como_transitorio(modo, env_slug, env_conn):
    modo("criar")
    antes = _amostra("portal_flowpcp_cadastro_volta_total", resultado="transitorio")
    _rodar(env_slug, Flow([PAR]), FireGravavel(falhas={"INSERT INTO PRODUTOS ": [LOCK()]}), env_conn)
    assert _amostra("portal_flowpcp_cadastro_volta_total", resultado="transitorio") == antes + 1


def test_gauge_sem_volta_conta_gravado_sem_volta_e_erro_a_enviar(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(None, None, None)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "gravado_sem_volta"
    assert _amostra("portal_flowpcp_cadastro_volta_sem_volta") == 1
    _rodar(env_slug, Flow([]), FireGravavel(), env_conn, _mais())
    assert _amostra("portal_flowpcp_cadastro_volta_sem_volta") == 0


def test_gauge_aguardando_some_fora_dos_modos_de_gravacao(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, FlowAguarda([], n=4), FireGravavel(), env_conn)
    assert _amostra_ou_none("portal_flowpcp_cadastro_volta_aguardando_modo") == 4
    modo("simular")
    _rodar(env_slug, Flow([]), FireGravavel(), env_conn, _mais())
    assert _amostra_ou_none("portal_flowpcp_cadastro_volta_aguardando_modo") is None


# --- Task 16: camposAlterados (contrato P4a rev. 5 §2.3) ---------------------


def _fire_3170(extra=None):
    return FireGravavel(produtos={
        3170: {"DESCRICAO": "PAR BASE", "BLOQUEADO": "Nao", "UNIDADE": "PR", "MARCA": "Antiga",
               "CODIGO_EAN13": "7891234567895", "CODPARANFE": "NFE-1", "GRUPO": 3,
               "CLASSIF_FISCAL": "61159500", "CODITEMSPED": 4},
        **(extra or {})})


def _updates(fire):
    return [(s, p) for s, p in fire.escritas if s.startswith("UPDATE PRODUTOS SET")]


def test_lista_so_ean_null_limpa_so_o_ean(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170()
    item = _atualizar("3170", ean=None, codigoNfe=None, marcaErp={"codigo": 17},
                      camposAlterados=["ean"])
    flow = Flow([item])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert _updates(fire) == [("UPDATE PRODUTOS SET CODIGO_EAN13 = ? WHERE SEQ = ?", (None, 3170))]
    p = fire.produtos[3170]
    assert p["CODPARANFE"] == "NFE-1" and p["MARCA"] == "Antiga" and p["DESCRICAO"] == "PAR BASE"


def test_lista_so_nome_nao_toca_marca_nem_composicao(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3990")
    modo("criar_e_atualizar")
    fire = _fire_3170({3990: {"DESCRICAO": "KIT", "BLOQUEADO": "Nao", "MARCA": "Antiga"}})
    fire.kit = [(1, 3990, 3100, 2.0)]
    item = _atualizar("3990", tipo="kit", unidadeErp="KIT", nome="KIT NOVO",
                      marcaErp={"codigo": 17}, camposAlterados=["nome"],
                      componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    flow = Flow([item])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert _updates(fire) == [("UPDATE PRODUTOS SET DESCRICAO = ? WHERE SEQ = ?", ("KIT NOVO", 3990))]
    assert fire.kit == [(1, 3990, 3100, 2.0)] and fire.produtos[3990]["MARCA"] == "Antiga"
    assert not any("PRODUTOS_KIT" in s for s, _ in fire.escritas)


def test_lista_so_componentes_troca_so_a_composicao(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3990")
    modo("criar_e_atualizar")
    fire = _fire_3170({3990: {"DESCRICAO": "KIT", "BLOQUEADO": "Nao", "MARCA": "Antiga"}})
    fire.kit = [(1, 3990, 3100, 2.0)]
    item = _atualizar("3990", tipo="kit", unidadeErp="KIT", nome="KIT NOVO",
                      marcaErp={"codigo": 17}, camposAlterados=["componentes"],
                      componentes=[{"erpProdutoId": "3170", "quantidade": 3}])
    flow = Flow([item])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert _updates(fire) == []
    assert fire.kit == [(1, 3990, 3170, 3.0)]
    assert fire.produtos[3990]["DESCRICAO"] == "KIT" and fire.produtos[3990]["MARCA"] == "Antiga"


def test_fora_da_lista_nao_e_conferido_no_erp(modo, env_slug, env_conn):
    """Marca e componente fora da lista são referência: não barram nem são escritos."""
    _no_catalogo(env_conn, "3990")
    modo("criar_e_atualizar")
    fire = _fire_3170({3990: {"DESCRICAO": "KIT", "BLOQUEADO": "Nao"}})
    item = _atualizar("3990", tipo="kit", unidadeErp="KIT", marcaErp={"codigo": 99},
                      camposAlterados=["unidadeErp"],
                      componentes=[{"erpProdutoId": "4999", "quantidade": 3}])
    flow = Flow([item])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert _updates(fire) == [("UPDATE PRODUTOS SET UNIDADE = ? WHERE SEQ = ?", ("KIT", 3990))]
    assert not any("BLOQUEADO FROM" in s for s, _ in fire.escritas)


def test_marca_listada_que_nao_existe_e_marca_ausente(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170()
    flow = Flow([_atualizar("3170", marcaErp={"codigo": 99}, camposAlterados=["marcaErp"])])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["erro"]["codigo"] == "marca_ausente_erp" and fire.escritas == []


def test_marca_listada_grava_o_nome_da_marca(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170()
    _rodar(env_slug, Flow([_atualizar("3170", marcaErp={"codigo": 17},
                                      camposAlterados=["marcaErp"])]), fire, env_conn)
    assert _updates(fire) == [("UPDATE PRODUTOS SET MARCA = ? WHERE SEQ = ?", ("Kolosh", 3170))]


def test_nome_listado_que_existe_em_outro_seq_e_nome_existe(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170({3100: {"DESCRICAO": "Meia  soquete", "BLOQUEADO": "Nao"}})
    flow = Flow([_atualizar("3170", nome="MEIA SOQUETE", camposAlterados=["nome"])])
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert erro["tipo"] == "definitivo" and erro["codigo"] == "nome_existe"
    assert "(código 3100)" in erro["mensagem"] and fire.escritas == []


def test_nome_listado_igual_ao_do_proprio_seq_grava(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170()
    flow = Flow([_atualizar("3170", nome="PAR BASE", camposAlterados=["nome"])])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"


def test_nome_fora_da_lista_nao_e_conferido(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170({3100: {"DESCRICAO": "MEIA SOQUETE", "BLOQUEADO": "Nao"}})
    flow = Flow([_atualizar("3170", nome="MEIA SOQUETE", camposAlterados=["ean"])])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert fire.produtos[3170]["DESCRICAO"] == "PAR BASE"


@pytest.mark.parametrize("m", ["criar", "criar_e_atualizar"])
def test_lista_vazia_nao_abre_o_erp_e_responde_gravado(modo, env_slug, env_conn, monkeypatch, m):
    _no_catalogo(env_conn, "3170")
    modo(m)

    def _nao_abre(slug):
        raise AssertionError("a lista vazia não abre o ERP")

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _nao_abre)
    antes = _amostra("portal_flowpcp_cadastro_volta_total", resultado="sem_mudanca")
    item = _atualizar("3170", camposAlterados=[], marcaErp={"codigo": 99})
    flow = Flow([item])
    r = cv.processar_ambiente(env_slug, _client=flow, _fire_conn=None, _env_conn=env_conn,
                              _relogio=Relogio())
    ((cid, corpo),) = flow.posts
    assert cid == item["cadastroId"]
    assert corpo["resultado"] == "gravado" and corpo["erpProdutoId"] == "3170"
    linha = repo.buscar(env_conn, item["cadastroId"], 2)
    assert (linha["estado"], linha["erp_produto_id"], linha["acao"]) == (
        "confirmado", "3170", "atualizar")
    assert linha["seq_reservado"] is None
    assert r["sem_mudanca"] == 1 and r["gravados"] == 0
    # Mesmas portas de um atualizar gravado: a linha não volta a ser reservável.
    assert not repo.reservar(env_conn, cadastro_id=item["cadastroId"], versao=2, produto_id=PROD,
                             acao="atualizar", tipo="simples", modo=m, plano_json="{}")
    assert _amostra("portal_flowpcp_cadastro_volta_total", resultado="sem_mudanca") == antes + 1


def test_lista_vazia_fora_do_escopo_e_seq_fora_do_escopo(modo, env_slug, env_conn, monkeypatch):
    modo("criar_e_atualizar")
    monkeypatch.setattr(cv, "_abrir_fire_escrita", lambda s: (_ for _ in ()).throw(AssertionError))
    flow = Flow([_atualizar("3170", camposAlterados=[])])
    cv.processar_ambiente(env_slug, _client=flow, _fire_conn=None, _env_conn=env_conn,
                          _relogio=Relogio())
    assert flow.posts[0][1]["erro"]["codigo"] == "seq_fora_do_escopo"


def test_lista_vazia_reentregue_confirmada_nao_responde_de_novo(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    item = _atualizar("3170", camposAlterados=[])
    _rodar(env_slug, Flow([item]), FireGravavel(), env_conn)
    flow = Flow([item])
    _rodar(env_slug, flow, FireGravavel(), env_conn, _mais())
    assert flow.posts == []


def test_atualizar_sem_a_chave_grava_tudo_como_na_rev4(modo, env_slug, env_conn):
    _no_catalogo(env_conn, "3170")
    modo("criar_e_atualizar")
    fire = _fire_3170()
    item = _atualizar("3170", marcaErp={"codigo": 17}, codigoMes=None)
    assert "camposAlterados" not in item
    _rodar(env_slug, Flow([item]), fire, env_conn)
    ((sql, params),) = _updates(fire)
    assert sql == ("UPDATE PRODUTOS SET DESCRICAO = ?, UNIDADE = ?, CODIGO_EAN13 = ?, "
                   "CLASSIF_FISCAL = ?, CODITEMSPED = ?, CODPARANFE = ?, GRUPO = ?, MARCA = ? "
                   "WHERE SEQ = ?")
    assert params == ("MEIA SOQUETE", "PAR", None, "61159500", 3, None, 8, "Kolosh", 3170)


@pytest.mark.parametrize("tipo", ["simples", "kit"])
def test_criar_que_virou_atualizar_com_lista_completa_igual_a_rev4(modo, env_slug, tmp_path,
                                                                    monkeypatch, tipo):
    """Seção 4: a lista traz todos os campos preenchidos; o UPDATE é o mesmo da rev. 4."""
    comps = [{"erpProdutoId": "3100", "quantidade": 3}] if tipo == "kit" else None
    base = _atualizar("3170", tipo=tipo, marcaErp={"codigo": 17}, ean="7891234567895",
                      codigoNfe="NFE-2", componentes=comps, codigoMes=None,
                      unidadeErp="KIT" if tipo == "kit" else "PAR")
    lista = ["nome", "unidadeErp", "ean", "ncm", "tipoItemSped", "grupoErp", "codigoNfe",
             *(["componentes"] if tipo == "kit" else []), "marcaErp"]
    escritas = []
    for n, item in enumerate((base, {**base, "camposAlterados": lista})):
        monkeypatch.setenv("APP_DATA_DIR", str(tmp_path / str(n)))
        router.reset_init_cache()
        with router.shared_connect():
            pass
        with router.env_connect("mm") as conn:
            _no_catalogo(conn, "3170")
            modo("criar_e_atualizar")
            fire = _fire_3170({3100: {"DESCRICAO": "OUTRO", "BLOQUEADO": "Nao"}})
            flow = Flow([item])
            _rodar(env_slug, flow, fire, conn)
            assert flow.posts[0][1]["resultado"] == "gravado"
            escritas.append(fire.escritas)
    assert escritas[0] == escritas[1] and escritas[0]
