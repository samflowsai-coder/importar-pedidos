"""Volta Flow -> Fire em modo simular. Flow e Fire são falsos; o SQLite do ambiente é real."""

import json
from datetime import datetime, timedelta

import pytest

from app.integrations.flowpcp import cadastro_gravacao
from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_schema import PendentesResposta, validar_item
from app.persistence import cadastro_volta_repo, router
from app.utils.logger import logger

BOM = {
    "cadastroId": "7b0c1c3e-0f7e-4a51-9c39-2a8f3f1d9a10",
    "produtoId": "77777777-7777-4777-8777-777777777771",
    "versao": 1,
    "acao": "criar",
    "erpProdutoId": None,
    "tipo": "kit",
    "nome": "KIT3 MEIA CURTO PRETO",
    "unidadeErp": "KIT",
    "ean": None,
    "ncm": "61159500",
    "tipoItemSped": 4,
    "grupoErp": {"codigo": "8"},
    "codigoNfe": "KIT3-MEIA-CURTO/PRT",
    "cor": "PRETO",
    "componentes": [{"erpProdutoId": "3170", "quantidade": 3}],
    "marcaErp": {"codigo": 17},
    "codigoMes": None,
    "aprovadoEm": "2026-10-20T12:00:00Z",
}


class _Client:
    """Sem método de confirmação: se alguém chamar, explode."""

    def __init__(self, cadastros, caps=("cadastros.v1",)):
        caps = list(caps) if caps is not None else None
        self.resp = PendentesResposta(cadastros=cadastros, capacidades=caps)
        self.limites = []
        self.chamadas = []

    def list_cadastros_pendentes(self, limite=20, **kw):
        self.limites.append(limite)
        self.chamadas.append(kw)
        return self.resp

    def close(self):
        pass


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql, params=()):
        self.conn.executados.append((sql, params))
        if not sql.lstrip().upper().startswith("SELECT"):
            raise AssertionError(f"escrita no Fire: {sql}")
        self.rows = self.conn.responder(sql, params)

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Fire:
    def __init__(self, nomes=None, produtos=None, marcas=None):
        self.nomes = nomes or {}  # NOME -> SEQ
        self.marcas = marcas if marcas is not None else {17: "Kolosh"}
        self.produtos = produtos if produtos is not None else {3170: "Nao"}
        self.executados = []

    def cursor(self):
        return _Cur(self)

    def responder(self, sql, params):
        if "RDB$DATABASE" in sql:
            return [(1,)]
        if "PRODUTOS_MARCA" in sql:
            return [(c, n) for c, n in self.marcas.items()]
        if "DESCRICAO" in sql:
            return [(seq, nome) for nome, seq in self.nomes.items()]
        if params[0] in self.produtos:
            return [(params[0], self.produtos[params[0]])]
        return []


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    monkeypatch.delenv(cv.VAR_MODO, raising=False)
    cv._avisados.clear()
    monkeypatch.setattr(cv, "_ultima_volta", None)
    monkeypatch.setattr(cv, "_ultimo_ciclo", {})
    # rodar() abre o SQLite de cada ambiente desligado atrás de pendência: aqui não há.
    monkeypatch.setattr(cv, "_com_pendencia", lambda slugs: [])


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


@pytest.fixture
def erros():
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="ERROR")
    yield msgs
    logger.remove(hid)


def _simular(monkeypatch):
    monkeypatch.setenv(cv.VAR_MODO, "mm:simular")


def _item(n, nome):
    return dict(BOM, cadastroId=f"{n:08d}-0f7e-4a51-9c39-2a8f3f1d9a10", nome=nome)


def _livro(conn):
    return {
        (r["cadastro_id"], r["versao"]): r for r in cadastro_volta_repo.listar(conn, limite=100)
    }


# --- modo ---------------------------------------------------------------


def test_modo_sem_variavel_e_desligado():
    assert cv.modo("mm") == "desligado"


def test_modo_simular_e_ignora_caixa_e_espacos(monkeypatch):
    monkeypatch.setenv(cv.VAR_MODO, "MM:Simular , nasmar:desligado")
    assert cv.modo("mm") == "simular" and cv.modo("nasmar") == "desligado"


@pytest.mark.parametrize("valor", ["mm", "mm:gravar"])
def test_modo_invalido_e_desligado_com_erro(monkeypatch, erros, valor):
    monkeypatch.setenv(cv.VAR_MODO, valor)
    assert cv.modo("mm") == "desligado"
    assert len(erros) == 1
    cv.modo("mm")
    assert len(erros) == 1  # uma vez por valor


def test_modo_entrada_ruim_nao_derruba_as_boas(monkeypatch, erros):
    monkeypatch.setenv(cv.VAR_MODO, "mm:simular,,nasmar:xx")
    assert cv.modo("mm") == "simular"
    assert cv.modo("nasmar") == "desligado"
    assert len(erros) == 1


# --- processar_ambiente -------------------------------------------------


def test_fila_vazia_nao_abre_fire_nem_grava_livro(env_conn, monkeypatch):
    _simular(monkeypatch)
    monkeypatch.setattr(
        cv.FirebirdConnection,
        "connect_somente_leitura",
        lambda *a, **k: pytest.fail("não podia abrir o Fire"),
    )
    r = cv.processar_ambiente("mm", _client=_Client([]), _env_conn=env_conn)
    assert r["modo"] == "simular" and r["lidos"] == 0
    assert cadastro_volta_repo.listar(env_conn) == []


def test_sem_capacidade_processa_assim_mesmo(env_conn, monkeypatch):
    _simular(monkeypatch)
    r = cv.processar_ambiente(
        "mm", _client=_Client([BOM], caps=()), _fire_conn=_Fire(), _env_conn=env_conn
    )
    assert r["capacidade_ok"] is False and r["simulados"] == 1


def test_tres_itens_bom_chave_nova_e_seq_fora(env_conn, monkeypatch):
    _simular(monkeypatch)
    ruim = dict(BOM, cadastroId="aaaaaaaa-0f7e-4a51-9c39-2a8f3f1d9a10", extra=1)
    fora = dict(
        BOM,
        cadastroId="bbbbbbbb-0f7e-4a51-9c39-2a8f3f1d9a10",
        acao="atualizar",
        erpProdutoId="999999",
    )
    fire = _Fire()
    r = cv.processar_ambiente(
        "mm", _client=_Client([BOM, ruim, fora]), _fire_conn=fire, _env_conn=env_conn
    )
    # Chave de topo nova é ignorada (rev. 3): `ruim` com `extra` vira simulado.
    assert (r["simulados"], r["erros"]) == (2, 1)
    livro = _livro(env_conn)
    assert livro[(BOM["cadastroId"], 1)]["estado"] == "simulado"
    e = livro[("aaaaaaaa-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]
    assert e["estado"] == "simulado"
    f = livro[("bbbbbbbb-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]
    assert (f["estado"], f["erro_codigo"]) == ("erro_definitivo", "seq_fora_do_escopo")
    assert fire.executados and all(s.upper().startswith("SELECT") for s, _ in fire.executados)
    # Só as leituras de nomes e marcas (uma por ciclo, sem valor do item) vão sem parâmetro.
    assert all("?" in s for s, _ in fire.executados if s not in (cv._SQL_NOMES, cv._SQL_MARCAS))


def test_atualizar_de_seq_na_copia_local_passa(env_conn, monkeypatch):
    _simular(monkeypatch)
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em) "
        "VALUES ('2645', '2645', 'X', '2026-10-16')"
    )
    item = dict(BOM, acao="atualizar", erpProdutoId="2645")
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=_Fire(), _env_conn=env_conn)
    assert _livro(env_conn)[(BOM["cadastroId"], 1)]["estado"] == "simulado"


def test_material_atualizar_usa_copia_de_materiais(env_conn, monkeypatch):
    _simular(monkeypatch)
    env_conn.execute(
        "INSERT INTO materiais_fire (erp_produto_id, nome, extraido_em) VALUES ('250', 'FIO', 'x')"
    )
    mat = dict(
        BOM, tipo="material", componentes=None, acao="inativar", erpProdutoId="250", marcaErp=None
    )
    cv.processar_ambiente("mm", _client=_Client([mat]), _fire_conn=_Fire(), _env_conn=env_conn)
    assert _livro(env_conn)[(BOM["cadastroId"], 1)]["estado"] == "simulado"


def test_criar_com_nome_existente(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire(nomes={"KIT3 MEIA CURTO PRETO": 4321})
    cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=fire, _env_conn=env_conn)
    r = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert r["erro_codigo"] == "nome_existe" and "4321" in r["erro_mensagem"]


def test_nome_com_aspas_nunca_vai_ao_sql(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire()
    item = dict(BOM, nome="X' OR 1=1 --")
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    assert not any("OR 1=1" in sql or "OR 1=1" in repr(p) for sql, p in fire.executados)


def test_nome_acentuado_casa_sem_depender_do_upper_do_firebird(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire(nomes={"meia açúcar": 77})
    item = dict(BOM, nome="MEIA AÇÚCAR")
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    r = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert r["erro_codigo"] == "nome_existe" and "77" in r["erro_mensagem"]
    nomes = [sql for sql, _ in fire.executados if "DESCRICAO" in sql]
    assert nomes == ["SELECT SEQ, DESCRICAO FROM PRODUTOS"]  # uma vez por ciclo, sem UPPER


@pytest.mark.parametrize("no_fire", ["meia  soquete", "MEIA\tSOQUETE", " meia \u00a0soquete "])
def test_nome_com_espacos_internos_diferentes_e_o_mesmo_nome(env_conn, monkeypatch, no_fire):
    _simular(monkeypatch)
    fire = _Fire(nomes={no_fire: 78})
    item = dict(BOM, nome="MEIA SOQUETE")
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    r = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert r["erro_codigo"] == "nome_existe" and "78" in r["erro_mensagem"]


def test_nomes_lidos_uma_vez_por_ciclo(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire()
    itens = [_item(1, "A"), _item(2, "B"), _item(3, "C")]
    cv.processar_ambiente("mm", _client=_Client(itens), _fire_conn=fire, _env_conn=env_conn)
    assert sum("DESCRICAO" in sql for sql, _ in fire.executados) == 1


@pytest.mark.parametrize(
    ("produtos", "codigo"), [({}, "componente_ausente"), ({3170: "Sim"}, "componente_bloqueado")]
)
def test_componente_inexistente_ou_bloqueado(env_conn, monkeypatch, produtos, codigo):
    _simular(monkeypatch)
    cv.processar_ambiente(
        "mm", _client=_Client([BOM]), _fire_conn=_Fire(produtos=produtos), _env_conn=env_conn
    )
    r = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert (r["estado"], r["erro_codigo"]) == ("erro_definitivo", codigo)


def test_plano_do_item_bom(env_conn, monkeypatch):
    _simular(monkeypatch)
    cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=_Fire(), _env_conn=env_conn)
    plano = json.loads(_livro(env_conn)[(BOM["cadastroId"], 1)]["plano_json"])
    assert plano["acao"] == "criar" and plano["tabela"] == "PRODUTOS"
    assert plano["campos"] == {
        "DESCRICAO": "KIT3 MEIA CURTO PRETO",
        "UNIDADE": "KIT",
        "CLASSIF_FISCAL": "61159500",
        "CODITEMSPED": 4,
        "GRUPO": 8,
        "CODPARANFE": "KIT3-MEIA-CURTO/PRT",
        "MARCA": "Kolosh",
    }
    assert "DESCRICAO2" not in plano["campos"]  # cor do item não vai à Descrição Interna
    assert plano["componentes"] == [{"erpProdutoId": "3170", "quantidade": 3.0}]


def test_lease_nao_reprocessa(env_conn, monkeypatch):
    _simular(monkeypatch)
    cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=_Fire(), _env_conn=env_conn)
    fire2 = _Fire()
    r = cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=fire2, _env_conn=env_conn)
    assert r["ja_no_livro"] == 1 and r["simulados"] == 0
    assert fire2.executados == []
    assert len(cadastro_volta_repo.listar(env_conn)) == 1


@pytest.mark.parametrize("m", ["criar", "criar_e_atualizar"])
def test_modos_de_escrita_recusam_sem_p0(env_conn, monkeypatch, erros, m):
    monkeypatch.setenv(cv.VAR_MODO, f"mm:{m}")
    monkeypatch.delenv(cv.VAR_P0, raising=False)
    cliente = _Client([BOM])
    r = cv.processar_ambiente("mm", _client=cliente, _env_conn=env_conn)
    assert r == {"modo": m, "recusado": cv._RECUSA_P0, "reenvio": {}}
    assert cliente.limites == [] and erros


def test_fire_fora_nao_grava_livro_nem_levanta(env_conn, monkeypatch):
    _simular(monkeypatch)
    from app.persistence import environments_repo

    class _Ctx:
        def __enter__(self):
            raise ConnectionError("fire fora")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(environments_repo, "get_by_slug", lambda s: {"slug": s})
    monkeypatch.setattr(environments_repo, "to_fb_config", lambda e: {})
    monkeypatch.setattr(cv.FirebirdConnection, "connect_somente_leitura", lambda self, cfg: _Ctx())
    r = cv.processar_ambiente("mm", _client=_Client([BOM]), _env_conn=env_conn)
    assert r["erro_transitorio"] == "ConnectionError"
    assert cadastro_volta_repo.listar(env_conn) == []


def test_flow_fora_nao_levanta(env_conn, monkeypatch):
    _simular(monkeypatch)

    class _Fora(_Client):
        def list_cadastros_pendentes(self, limite=20, **kw):
            raise RuntimeError("segredo-no-corpo")

    r = cv.processar_ambiente("mm", _client=_Fora([]), _env_conn=env_conn)
    assert "erro_transitorio" in r and "segredo" not in json.dumps(r)


# --- rodar / rodar_se_devido -------------------------------------------


def test_rodar_isola_ambiente_que_explode(monkeypatch, erros):
    monkeypatch.setenv(cv.VAR_MODO, "a:simular,b:simular,c:desligado")
    monkeypatch.setattr(
        cv, "enabled_flowpcp_envs", lambda: {"a": object(), "b": object(), "c": object()}
    )

    def fake(slug, **k):
        if slug == "a":
            raise RuntimeError("boom")
        return {"modo": "simular", "lidos": 0}

    monkeypatch.setattr(cv, "processar_ambiente", fake)
    res = cv.rodar()
    assert res["a"] == {"erro": "RuntimeError"} and res["b"]["lidos"] == 0 and "c" not in res
    assert erros


def test_rodar_se_devido_respeita_intervalo(monkeypatch):
    chamadas = []
    monkeypatch.setattr(cv, "rodar", lambda: chamadas.append(1) or {})
    t0 = datetime(2026, 10, 20, 12, 0)
    cv.rodar_se_devido(t0)
    cv.rodar_se_devido(t0 + timedelta(minutes=1))
    assert len(chamadas) == 1
    cv.rodar_se_devido(t0 + timedelta(minutes=6))
    assert len(chamadas) == 2


def test_client_list_cadastros_pendentes_tolera_chave_nova_e_nao_valida_itens():
    from app.integrations.flowpcp.client import FlowPCPClient

    class _Resp:
        is_success = True
        status_code = 200
        text = ""

        def json(self):
            return {
                "cadastros": [{"qualquer": "coisa"}],
                "limite": 20,
                "capacidades": [],
                "novo": 1,
            }

    class _Http:
        def get(self, path, params=None):
            self.visto = (path, params)
            return _Resp()

    c = FlowPCPClient.__new__(FlowPCPClient)
    c._client = _Http()
    r = c.list_cadastros_pendentes(limite=7)
    assert c._client.visto == ("/api/portal-pedidos/cadastros/pendentes", {"limite": 7})
    assert r.cadastros == [{"qualquer": "coisa"}] and r.model_extra["limite"] == 20


# --- fix round 1 ---------------------------------------------------------


class _FireQueQuebraNoSegundo(_Fire):
    def responder(self, sql, params):
        if "BLOQUEADO" in sql and params[0] == 3171:
            raise RuntimeError("segredo-do-driver")
        return super().responder(sql, params)


def test_consulta_que_falha_so_rejeita_o_proprio_item(env_conn, monkeypatch):
    _simular(monkeypatch)
    dois = dict(_item(2, "ITEM DOIS"), componentes=[{"erpProdutoId": "3171", "quantidade": 3}])
    itens = [_item(1, "ITEM UM"), dois, _item(3, "ITEM TRES")]
    r = cv.processar_ambiente(
        "mm", _client=_Client(itens), _fire_conn=_FireQueQuebraNoSegundo(), _env_conn=env_conn
    )
    assert (r["simulados"], r["erros"]) == (2, 1) and "erro_transitorio" not in r
    livro = _livro(env_conn)
    assert livro[("00000001-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]["estado"] == "simulado"
    assert livro[("00000003-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]["estado"] == "simulado"
    dois = livro[("00000002-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]
    assert (dois["estado"], dois["erro_codigo"]) == ("erro_definitivo", "consulta_fire_falhou")
    assert "RuntimeError" in dois["erro_mensagem"] and "segredo" not in dois["erro_mensagem"]


def test_conexao_perdida_no_meio_aborta_o_lote(env_conn, monkeypatch):
    _simular(monkeypatch)

    class _Cai(_Fire):
        def responder(self, sql, params):
            raise ConnectionError("caiu")  # inclusive a sonda

    r = cv.processar_ambiente(
        "mm", _client=_Client([_item(1, "A"), _item(2, "B")]), _fire_conn=_Cai(), _env_conn=env_conn
    )
    assert r["erro_transitorio"] == "ConnectionError"
    assert cadastro_volta_repo.listar(env_conn) == []


def test_id_hostil_nao_chega_ao_livro_nem_ao_log(env_conn, monkeypatch):
    _simular(monkeypatch)
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="DEBUG")
    try:
        ruim = {"cadastroId": "<b>x</b>\nFORJADO", "versao": 3, "extra": 1}
        cv.processar_ambiente("mm", _client=_Client([ruim]), _fire_conn=_Fire(), _env_conn=env_conn)
    finally:
        logger.remove(hid)
    (linha,) = cadastro_volta_repo.listar(env_conn)
    assert linha["cadastro_id"].startswith("sem-id-") and linha["versao"] == 3
    assert not any("FORJADO" in m or "<b>" in m for m in msgs)


def test_versoes_invalidas_do_mesmo_uuid_sao_linhas_distintas(env_conn, monkeypatch):
    _simular(monkeypatch)
    v2 = dict(BOM, versao=2, tipo="servico")
    v3 = dict(BOM, versao=3, tipo="servico")
    cv.processar_ambiente("mm", _client=_Client([v2, v3]), _fire_conn=_Fire(), _env_conn=env_conn)
    livro = _livro(env_conn)
    assert sorted(livro) == [(BOM["cadastroId"], 2), (BOM["cadastroId"], 3)]
    for versao in (2, 3):
        linha = livro[(BOM["cadastroId"], versao)]
        assert (linha["estado"], linha["erro_codigo"]) == ("erro_definitivo", "contrato_invalido")


def test_versao_bool_ou_negativa_cai_em_zero(env_conn, monkeypatch):
    _simular(monkeypatch)
    a = dict(BOM, versao=True)
    cv.processar_ambiente("mm", _client=_Client([a]), _fire_conn=_Fire(), _env_conn=env_conn)
    assert list(_livro(env_conn)) == [(BOM["cadastroId"], 0)]


def test_versao_enorme_nao_trava_a_fila(env_conn, monkeypatch):
    _simular(monkeypatch)
    enorme = dict(BOM, cadastroId="cccccccc-0f7e-4a51-9c39-2a8f3f1d9a10", versao=10**30)
    r = cv.processar_ambiente(
        "mm", _client=_Client([enorme, BOM]), _fire_conn=_Fire(), _env_conn=env_conn
    )
    assert "erro_transitorio" not in r and (r["simulados"], r["erros"]) == (1, 1)
    livro = _livro(env_conn)
    assert livro[("cccccccc-0f7e-4a51-9c39-2a8f3f1d9a10", 0)]["erro_codigo"] == "contrato_invalido"


def test_falha_no_livro_so_rejeita_o_proprio_item(env_conn, monkeypatch):
    _simular(monkeypatch)
    original = cadastro_volta_repo.existe

    def existe(conn, cadastro_id, versao):
        if cadastro_id.startswith("00000002"):
            raise OverflowError("x")
        return original(conn, cadastro_id, versao)

    monkeypatch.setattr(cadastro_volta_repo, "existe", existe)
    itens = [_item(1, "A"), _item(2, "B"), _item(3, "C")]
    r = cv.processar_ambiente("mm", _client=_Client(itens), _fire_conn=_Fire(), _env_conn=env_conn)
    assert "erro_transitorio" not in r and (r["simulados"], r["erros"]) == (2, 1)
    dois = _livro(env_conn)[("00000002-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]
    assert dois["erro_codigo"] == "livro_falhou" and "OverflowError" in dois["erro_mensagem"]


# --- onda final: envelope, fila de 50, último ciclo, componentes ----------


def test_item_que_nao_e_objeto_vira_erro_so_dele(env_conn, monkeypatch):
    _simular(monkeypatch)
    r = cv.processar_ambiente(
        "mm", _client=_Client([None, "x", BOM], caps=None), _fire_conn=_Fire(), _env_conn=env_conn
    )
    assert "erro_transitorio" not in r and (r["simulados"], r["erros"]) == (1, 2)
    assert r["capacidade_ok"] is False
    livro = cadastro_volta_repo.listar(env_conn)
    ruins = [x for x in livro if x["estado"] == "erro_definitivo"]
    assert len(ruins) == 2 and all(x["erro_codigo"] == "contrato_invalido" for x in ruins)
    assert all(x["cadastro_id"].startswith("sem-id-") for x in ruins)


def test_cadastros_que_nao_e_lista_e_transitorio_e_nao_grava(env_conn, monkeypatch, erros):
    _simular(monkeypatch)

    class _Ruim(_Client):
        def list_cadastros_pendentes(self, limite=20, **kw):
            return PendentesResposta.model_validate({"cadastros": "segredo-x"})

    r = cv.processar_ambiente("mm", _client=_Ruim([]), _env_conn=env_conn)
    assert r["erro_transitorio"] == "resposta_invalida"
    assert cadastro_volta_repo.listar(env_conn) == []
    assert erros and not any("segredo" in m for m in erros)


def test_pede_50_a_fila(env_conn, monkeypatch):
    _simular(monkeypatch)
    cliente = _Client([])
    cv.processar_ambiente("mm", _client=cliente, _env_conn=env_conn)
    assert cliente.limites == [50]


def test_segundo_ciclo_todo_no_livro_nao_abre_o_fire(env_conn, monkeypatch):
    _simular(monkeypatch)
    itens = [_item(1, "A"), _item(2, "B")]
    cv.processar_ambiente("mm", _client=_Client(itens), _fire_conn=_Fire(), _env_conn=env_conn)
    from app.persistence import environments_repo

    monkeypatch.setattr(environments_repo, "get_by_slug", lambda s: {"slug": s})
    monkeypatch.setattr(environments_repo, "to_fb_config", lambda e: {})
    monkeypatch.setattr(
        cv.FirebirdConnection,
        "connect_somente_leitura",
        lambda *a, **k: pytest.fail("não podia abrir o Fire"),
    )
    r = cv.processar_ambiente("mm", _client=_Client(itens), _env_conn=env_conn)
    assert r["ja_no_livro"] == 2 and r["simulados"] == 0


def test_gauge_de_pendentes_diz_lidos_na_ultima_leitura():
    from app.observability import metrics

    assert "lidos" in metrics.cadastro_volta_pendentes._documentation


def test_ultimo_ciclo_guardado_no_sucesso(env_conn, monkeypatch):
    _simular(monkeypatch)
    assert cv.ultimo_ciclo("mm") is None
    cv.processar_ambiente(
        "mm", _client=_Client([BOM], caps=()), _fire_conn=_Fire(), _env_conn=env_conn
    )
    u = cv.ultimo_ciclo("mm")
    assert u["em"].endswith("+00:00")
    assert u["resumo"] == {
        "modo": "simular",
        "lidos": 1,
        "capacidade_ok": False,
        "simulados": 1,
        "erros": 0,
        "ja_no_livro": 0,
        "erro": None,
    }


def test_ultimo_ciclo_guardado_no_erro_sem_texto_do_erro(env_conn, monkeypatch):
    _simular(monkeypatch)

    class _Fora(_Client):
        def list_cadastros_pendentes(self, limite=20, **kw):
            raise RuntimeError("segredo-no-corpo")

    cv.processar_ambiente("mm", _client=_Fora([]), _env_conn=env_conn)
    u = cv.ultimo_ciclo("mm")
    assert u["resumo"]["erro"] and "segredo" not in json.dumps(u)


def test_rodar_guarda_ultimo_ciclo_do_ambiente_que_explode(monkeypatch):
    monkeypatch.setenv(cv.VAR_MODO, "a:simular")
    monkeypatch.setattr(cv, "enabled_flowpcp_envs", lambda: {"a": object()})

    def fake(slug, **k):
        raise RuntimeError("segredo")

    monkeypatch.setattr(cv, "processar_ambiente", fake)
    cv.rodar()
    u = cv.ultimo_ciclo("a")
    assert u["resumo"]["erro"] == "RuntimeError" and "segredo" not in json.dumps(u)


@pytest.mark.parametrize("valor", [None, ""])
def test_rodar_sem_variavel_nao_abre_token(monkeypatch, valor):
    if valor is not None:
        monkeypatch.setenv(cv.VAR_MODO, valor)
    monkeypatch.setattr(cv, "enabled_flowpcp_envs", lambda: pytest.fail("não podia abrir token"))
    monkeypatch.setattr(cv, "_slugs_flowpcp", lambda: ["mm"])
    monkeypatch.setattr(cv, "processar_ambiente", lambda s, **k: pytest.fail("sem pendência"))
    assert cv.rodar() == {}


def test_rodar_avisa_slug_que_nao_e_ambiente_flowpcp(monkeypatch):
    monkeypatch.setenv(cv.VAR_MODO, "mm:simular,fantasma:simular,Ruim!<b>:simular")
    monkeypatch.setattr(cv, "enabled_flowpcp_envs", lambda: {"mm": object()})
    monkeypatch.setattr(cv, "processar_ambiente", lambda slug, **k: {"modo": "simular"})
    avisos: list[str] = []
    hid = logger.add(
        lambda m: avisos.append(str(m)),
        level="WARNING",
        filter=lambda r: r["level"].name == "WARNING",
    )
    try:
        cv.rodar()
        cv.rodar()
    finally:
        logger.remove(hid)
    assert sum("'fantasma'" in a for a in avisos) == 1
    assert sum("valor inválido" in a for a in avisos) == 1
    assert not any("<b>" in a or "'mm'" in a for a in avisos)


def test_atualizar_kit_sem_componentes_nao_consulta_componentes(env_conn, monkeypatch):
    _simular(monkeypatch)
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em) "
        "VALUES ('2645', '2645', 'X', '2026-10-16')"
    )
    item = dict(BOM, acao="atualizar", erpProdutoId="2645", componentes=None)
    fire = _Fire()
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    linha = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert linha["estado"] == "simulado"
    assert json.loads(linha["plano_json"])["componentes"] is None
    assert not any("BLOQUEADO" in sql for sql, _ in fire.executados)


def test_simular_atualizar_plano_igual_ao_da_gravacao(env_conn, monkeypatch):
    """`ean`/`codigoNfe` null num `atualizar` limpam no ERP: o plano simulado mostra isso."""
    _simular(monkeypatch)
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em) "
        "VALUES ('2645', '2645', 'X', '2026-10-16')"
    )
    item = dict(BOM, acao="atualizar", erpProdutoId="2645", ean=None, codigoNfe=None)
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=_Fire(), _env_conn=env_conn)
    plano = json.loads(_livro(env_conn)[(BOM["cadastroId"], 1)]["plano_json"])
    assert plano["campos"]["CODIGO_EAN13"] is None and plano["campos"]["CODPARANFE"] is None
    valido = validar_item(item)[0]
    marcas = {17: "Kolosh"}
    campos = cadastro_gravacao._campos(valido, marcas)
    gravacao = cadastro_gravacao._plano(valido, campos, cadastro_gravacao._componentes(valido))
    assert plano == json.loads(json.dumps(gravacao))


def test_inativar_kit_com_componentes_lixo_simula_sem_composicao(env_conn, monkeypatch):
    _simular(monkeypatch)
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em) "
        "VALUES ('2645', '2645', 'X', '2026-10-16')"
    )
    item = dict(BOM, acao="inativar", erpProdutoId="2645", componentes="lixo", marcaErp=None)
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=_Fire(), _env_conn=env_conn)
    linha = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert linha["estado"] == "simulado"
    assert json.loads(linha["plano_json"])["componentes"] is None


# --- Task 5: simular lê sem reservar e confere a marca ---------------------


def test_simular_pede_fila_sem_reserva(env_conn, monkeypatch):
    _simular(monkeypatch)
    monkeypatch.delenv(cv.VAR_INATIVAR, raising=False)
    cliente = _Client([BOM])
    cv.processar_ambiente("mm", _client=cliente, _fire_conn=_Fire(), _env_conn=env_conn)
    assert cliente.limites == [50]
    assert cliente.chamadas == [{"simular": True, "acoes": ("criar", "atualizar")}]


def test_simular_pede_so_as_acoes_que_a_gravacao_consumiria(env_conn, monkeypatch):
    """A janela de 50 do simular (sem cursor) é preenchida pelas inativações mais antigas e
    esconde os `atualizar` atrás delas (21/10/2026: 50 inativar, 0 dos 20 atualizar). O
    simular pede o que o modo de gravação consumiria: sem o portão de inativar, a fila só
    devolve `criar` e `atualizar`."""
    _simular(monkeypatch)
    monkeypatch.delenv(cv.VAR_INATIVAR, raising=False)
    cliente = _Client([BOM])
    cv.processar_ambiente("mm", _client=cliente, _fire_conn=_Fire(), _env_conn=env_conn)
    assert cliente.chamadas[0]["acoes"] == cv._acoes("mm", "criar_e_atualizar")
    assert "inativar" not in cliente.chamadas[0]["acoes"]


def test_simular_com_o_portao_de_inativar_pede_tambem_inativar(env_conn, monkeypatch):
    """Simular nunca grava: com o portão ligado ele mostra as inativações que a gravação faria."""
    _simular(monkeypatch)
    monkeypatch.setenv(cv.VAR_INATIVAR, "mm")
    cliente = _Client([BOM])
    cv.processar_ambiente("mm", _client=cliente, _fire_conn=_Fire(), _env_conn=env_conn)
    assert cliente.chamadas[0]["acoes"] == ("criar", "atualizar", "inativar")
    assert cliente.chamadas[0]["simular"] is True  # segue sem reservar nada no Flow


def test_simular_marca_inexistente_vai_ao_livro(env_conn, monkeypatch):
    _simular(monkeypatch)
    bruto = {**BOM, "marcaErp": {"codigo": 99}}
    cv.processar_ambiente("mm", _client=_Client([bruto]), _fire_conn=_Fire(), _env_conn=env_conn)
    linha = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert (linha["estado"], linha["erro_codigo"]) == ("erro_definitivo", "marca_ausente_erp")
    assert linha["erro_mensagem"] == "marca código 99 não existe no ERP"


def test_simular_plano_leva_nome_da_marca(env_conn, monkeypatch):
    _simular(monkeypatch)
    cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=_Fire(), _env_conn=env_conn)
    plano = json.loads(_livro(env_conn)[(BOM["cadastroId"], 1)]["plano_json"])
    assert plano["campos"]["MARCA"] == "Kolosh"


def test_marcas_lidas_uma_vez_por_ciclo(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire()
    itens = [_item(1, "A"), _item(2, "B")]
    cv.processar_ambiente("mm", _client=_Client(itens), _fire_conn=fire, _env_conn=env_conn)
    assert sum("PRODUTOS_MARCA" in sql for sql, _ in fire.executados) == 1


def test_simular_avisa_chave_desconhecida_uma_vez_por_nome(env_conn, monkeypatch):
    _simular(monkeypatch)
    avisos: list[str] = []
    hid = logger.add(
        lambda m: avisos.append(str(m)),
        level="WARNING",
        filter=lambda r: r["level"].name == "WARNING",
    )
    try:
        novo = dict(BOM, campoNovo=1, **{"chave<b>ruim": 2}, **{"outra<i>": 3})
        for _ in range(2):
            cv.processar_ambiente(
                "mm", _client=_Client([novo]), _fire_conn=_Fire(), _env_conn=env_conn
            )
    finally:
        logger.remove(hid)
    assert sum("chave desconhecida" in a for a in avisos) == 2  # campoNovo e "?" (dedup)
    assert sum("'campoNovo'" in a for a in avisos) == 1 and sum("'?'" in a for a in avisos) == 1
    assert not any("<b>" in a or "<i>" in a for a in avisos)


def test_chave_desconhecida_avisa_uma_vez_por_ambiente(env_conn, monkeypatch):
    monkeypatch.setenv(cv.VAR_MODO, "mm:simular,nasmar:simular")
    avisos: list[str] = []
    hid = logger.add(
        lambda m: avisos.append(str(m)),
        level="WARNING",
        filter=lambda r: r["level"].name == "WARNING",
    )
    try:
        novo = dict(BOM, campoNovo=1)
        for slug in ("mm", "nasmar", "mm"):
            cv.processar_ambiente(
                slug, _client=_Client([novo]), _fire_conn=_Fire(), _env_conn=env_conn
            )
    finally:
        logger.remove(hid)
    assert sum("'campoNovo'" in a for a in avisos) == 2  # um por ambiente, não três
    assert sum("env=mm:" in a for a in avisos) == 1 and sum("env=nasmar:" in a for a in avisos) == 1


def test_livro_da_volta_abre_com_synchronous_full_sem_mudar_o_router(tmp_path, monkeypatch):
    """O livro decide se o ERP grava de novo: o commit do SEQ não pode se perder numa
    queda de energia (WAL + NORMAL perde). Só a conexão da volta muda."""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with cv._env("mm", None) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with router.env_connect("mm") as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    router.reset_init_cache()


# --- Task 16: camposAlterados (contrato P4a rev. 5 §2.3) ---------------------


def _atualizar_2645(env_conn, **kw):
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em) "
        "VALUES ('2645', '2645', 'X', '2026-10-16')"
    )
    return dict(BOM, acao="atualizar", erpProdutoId="2645", **kw)


def test_simular_plano_so_com_as_colunas_listadas(env_conn, monkeypatch):
    _simular(monkeypatch)
    item = _atualizar_2645(env_conn, ean=None, camposAlterados=["nome", "ean"])
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=_Fire(), _env_conn=env_conn)
    linha = _livro(env_conn)[(BOM["cadastroId"], 1)]
    plano = json.loads(linha["plano_json"])
    assert linha["estado"] == "simulado"
    assert plano["campos"] == {"DESCRICAO": BOM["nome"], "CODIGO_EAN13": None}
    assert plano["componentes"] is None and plano["campos_alterados"] == ["nome", "ean"]
    valido = validar_item(item)[0]
    gravacao = cadastro_gravacao._plano(
        valido, cadastro_gravacao._campos(valido, {17: "Kolosh"}),
        cadastro_gravacao._componentes(valido))
    assert plano == json.loads(json.dumps(gravacao))


def test_simular_lista_vazia_e_sem_mudanca(env_conn, monkeypatch):
    _simular(monkeypatch)
    item = _atualizar_2645(env_conn, marcaErp={"codigo": 99}, camposAlterados=[])
    fire = _Fire()
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    linha = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert linha["estado"] == "simulado"
    assert json.loads(linha["plano_json"]) == {"acao": "atualizar", "sem_mudanca": True}
    assert not any("BLOQUEADO" in sql for sql, _ in fire.executados)


def test_simular_nome_listado_em_outro_seq_e_nome_existe(env_conn, monkeypatch):
    _simular(monkeypatch)
    item = _atualizar_2645(env_conn, camposAlterados=["nome"])
    fire = _Fire(nomes={BOM["nome"].lower(): 3100})
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    linha = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert (linha["estado"], linha["erro_codigo"]) == ("erro_definitivo", "nome_existe")
