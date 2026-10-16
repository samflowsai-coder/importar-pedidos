"""Volta Flow -> Fire em modo simular. Flow e Fire são falsos; o SQLite do ambiente é real."""

import json
from datetime import datetime, timedelta

import pytest

from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_schema import PendentesResposta
from app.persistence import cadastro_volta_repo, router
from app.utils.logger import logger

BOM = {
    "cadastroId": "7b0c1c3e-0f7e-4a51-9c39-2a8f3f1d9a10",
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
    "codigoMes": None,
    "aprovadoEm": "2026-10-20T12:00:00Z",
}


class _Client:
    """Sem método de confirmação: se alguém chamar, explode."""

    def __init__(self, cadastros, caps=("cadastros.v1",)):
        self.resp = PendentesResposta(cadastros=cadastros, capacidades=list(caps))
        self.limites = []

    def list_cadastros_pendentes(self, limite=20):
        self.limites.append(limite)
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
    def __init__(self, nomes=None, produtos=None):
        self.nomes = nomes or {}  # NOME -> SEQ
        self.produtos = produtos if produtos is not None else {3170: "Nao"}
        self.executados = []

    def cursor(self):
        return _Cur(self)

    def responder(self, sql, params):
        if "DESCRICAO" in sql:
            seq = self.nomes.get(params[0])
            return [(seq,)] if seq else []
        if params[0] in self.produtos:
            return [(params[0], self.produtos[params[0]])]
        return []


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    monkeypatch.delenv(cv.VAR_MODO, raising=False)
    cv._avisados.clear()
    monkeypatch.setattr(cv, "_ultima_volta", None)


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


def test_tres_itens_bom_extra_e_seq_fora(env_conn, monkeypatch):
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
    assert (r["simulados"], r["erros"]) == (1, 2)
    livro = _livro(env_conn)
    assert livro[(BOM["cadastroId"], 1)]["estado"] == "simulado"
    e = livro[("aaaaaaaa-0f7e-4a51-9c39-2a8f3f1d9a10", 0)]
    assert (e["estado"], e["erro_codigo"]) == ("erro_definitivo", "contrato_invalido")
    f = livro[("bbbbbbbb-0f7e-4a51-9c39-2a8f3f1d9a10", 1)]
    assert (f["estado"], f["erro_codigo"]) == ("erro_definitivo", "seq_fora_do_escopo")
    assert fire.executados and all(
        s.upper().startswith("SELECT") and "?" in s for s, _ in fire.executados
    )


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
    mat = dict(BOM, tipo="material", componentes=None, acao="inativar", erpProdutoId="250")
    cv.processar_ambiente("mm", _client=_Client([mat]), _fire_conn=_Fire(), _env_conn=env_conn)
    assert _livro(env_conn)[(BOM["cadastroId"], 1)]["estado"] == "simulado"


def test_criar_com_nome_existente(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire(nomes={"KIT3 MEIA CURTO PRETO": 4321})
    cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=fire, _env_conn=env_conn)
    r = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert r["erro_codigo"] == "nome_existe" and "4321" in r["erro_mensagem"]
    assert fire.executados[0][1] == ("KIT3 MEIA CURTO PRETO",)  # parâmetro, não texto no SQL


def test_nome_com_aspas_vai_como_parametro(env_conn, monkeypatch):
    _simular(monkeypatch)
    fire = _Fire()
    item = dict(BOM, nome="X' OR 1=1 --")
    cv.processar_ambiente("mm", _client=_Client([item]), _fire_conn=fire, _env_conn=env_conn)
    sql, params = fire.executados[0]
    assert "OR 1=1" not in sql and params == ("X' OR 1=1 --",)


@pytest.mark.parametrize("produtos", [{}, {3170: "Sim"}])
def test_componente_inexistente_ou_bloqueado(env_conn, monkeypatch, produtos):
    _simular(monkeypatch)
    cv.processar_ambiente(
        "mm", _client=_Client([BOM]), _fire_conn=_Fire(produtos=produtos), _env_conn=env_conn
    )
    r = _livro(env_conn)[(BOM["cadastroId"], 1)]
    assert (r["estado"], r["erro_codigo"]) == ("erro_definitivo", "componente_invalido")


def test_plano_do_item_bom(env_conn, monkeypatch):
    _simular(monkeypatch)
    cv.processar_ambiente("mm", _client=_Client([BOM]), _fire_conn=_Fire(), _env_conn=env_conn)
    plano = json.loads(_livro(env_conn)[(BOM["cadastroId"], 1)]["plano_json"])
    assert plano["acao"] == "criar" and plano["tabela"] == "PRODUTOS"
    assert plano["campos"] == {
        "DESCRICAO": "KIT3 MEIA CURTO PRETO",
        "DESCRICAO2": "PRETO",
        "UNIDADE": "KIT",
        "CLASSIF_FISCAL": "61159500",
        "CODITEMSPED": 4,
        "GRUPO": "8",
        "CODPARANFE": "KIT3-MEIA-CURTO/PRT",
    }
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
def test_modos_de_escrita_recusam(env_conn, monkeypatch, erros, m):
    monkeypatch.setenv(cv.VAR_MODO, f"mm:{m}")
    cliente = _Client([BOM])
    r = cv.processar_ambiente("mm", _client=cliente, _env_conn=env_conn)
    assert r == {"modo": m, "recusado": "gravação exige P0 e cadastros.v1 (Etapa 3)"}
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
        def list_cadastros_pendentes(self, limite=20):
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
