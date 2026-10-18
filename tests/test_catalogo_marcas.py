import sqlite3

import pytest

from app.erp.catalog_extract import ProdutoFireDTO, extract_com_nota, extract_marcas
from app.erp.queries import LIST_PRODUTOS_COM_NOTA, LIST_PRODUTOS_MARCA
from app.integrations.flowpcp import catalogo_marcas as cm
from app.integrations.flowpcp import catalogo_sync
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoContagens,
    CatalogoReconciliacaoResponse,
)
from app.persistence import router, schema_env


def test_filtra_vazia_longa_e_codigo_invalido():
    enviadas, filtradas = cm.filtrar(
        [(1, ""), (2, "  Kolosh  "), (0, "Zero"), (3, "x" * 31), (4, None), (5, "Sam's Club")]
    )
    assert enviadas == [{"codigo": 2, "nome": "Kolosh"}, {"codigo": 5, "nome": "Sam's Club"}]
    assert filtradas == [
        {"codigo": 1, "motivo": "nome_vazio"},
        {"codigo": 0, "motivo": "codigo_invalido"},
        {"codigo": 3, "motivo": "nome_longo"},
        {"codigo": 4, "motivo": "nome_vazio"},
    ]


def test_limite_de_500():
    enviadas, filtradas = cm.filtrar([(i, f"M{i}") for i in range(1, 503)])
    assert len(enviadas) == 500 and [f["motivo"] for f in filtradas] == ["excede_500"] * 2


def test_codigo_acima_do_maximo_e_nome_de_30_passa():
    enviadas, filtradas = cm.filtrar([(1_000_000_000, "A"), (7, "x" * 30)])
    assert [e["codigo"] for e in enviadas] == [7]
    assert filtradas == [{"codigo": 1_000_000_000, "motivo": "codigo_invalido"}]


def test_interruptores_por_slug(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_MARCAS", " MM , nasmar")
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_NOTA", raising=False)
    assert cm.envio_ligado("mm") and cm.envio_ligado("Nasmar") and not cm.envio_ligado("x")
    assert not cm.nota_ligada("mm")
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_NOTA", "mm")
    assert cm.nota_ligada("mm")


class _Fire:
    def __init__(self, rows):
        self.rows, self.sqls = rows, []

    def cursor(self):
        dono = self

        class _C:
            def execute(self, sql):
                dono.sqls.append(sql)

            def fetchall(self):
                return dono.rows

            def close(self):
                pass

        return _C()


def test_extract_marcas_le_codigo_e_nome_bruto():
    fire = _Fire([(1, ""), (2, "Kolosh"), (3, None)])
    assert extract_marcas(fire) == [(1, ""), (2, "Kolosh"), (3, None)]
    assert fire.sqls == [LIST_PRODUTOS_MARCA]


def test_extract_marcas_nao_converte_codigo_estranho():
    assert extract_marcas(_Fire([(None, "A"), ("12x", "B")])) == [(None, "A"), ("12x", "B")]


def test_filtrar_nome_so_com_espacos_codigo_bool_e_texto():
    enviadas, filtradas = cm.filtrar([(1, "   "), (True, "Sim"), ("7", "Sete"), (None, "Nulo")])
    assert enviadas == []
    assert filtradas == [
        {"codigo": 1, "motivo": "nome_vazio"},
        {"codigo": True, "motivo": "codigo_invalido"},
        {"codigo": "7", "motivo": "codigo_invalido"},
        {"codigo": None, "motivo": "codigo_invalido"},
    ]


def test_extract_com_nota_devolve_seqs_como_texto_e_ignora_nulo():
    fire = _Fire([(3306,), (2544,), (None,)])
    assert extract_com_nota(fire) == {"3306", "2544"}
    assert fire.sqls == [LIST_PRODUTOS_COM_NOTA]


# ── no sync ──────────────────────────────────────────────────────────────────


class _Cfg:
    enabled = True
    catalogo_push = True
    catalogo_apenas_meias = False


class _Client:
    def __init__(self, **extra):
        self.extra, self.sent = extra, None

    def send_catalogo(self, request):
        self.sent = request
        return CatalogoReconciliacaoResponse(contagens=CatalogoContagens(), **self.extra)

    def close(self):
        pass


class _Env:
    def __init__(self, em_aberto=()):
        self.em_aberto = list(em_aberto)

    def execute(self, sql, params=()):
        dono = self

        class C:
            fetchone = staticmethod(lambda: (0,))

            @staticmethod
            def fetchall():
                if "cadastro_volta" in sql:
                    return [(s, None) for s in dono.em_aberto]
                return []

        return C()

    def executemany(self, *a):
        pass


def _dto(seq):
    return ProdutoFireDTO(str(seq), str(seq), f"P{seq}", "PARES", None, True, "simples")


@pytest.fixture
def logs():
    from app.utils.logger import logger

    msgs: list[tuple[str, str]] = []
    hid = logger.add(lambda m: msgs.append((m.record["level"].name, str(m))), level="WARNING")
    yield msgs
    logger.remove(hid)


@pytest.fixture
def rodar(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_MARCAS", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_NOTA", raising=False)
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.setattr(
        catalogo_sync, "extract_produtos", lambda conn, **kw: [_dto(3306), _dto(3992)]
    )
    monkeypatch.setattr(
        catalogo_sync, "extract_marcas", lambda conn: [(1, ""), (2, "Kolosh"), (3, "Sam's")]
    )
    monkeypatch.setattr(catalogo_sync, "extract_com_nota", lambda conn: {"3306"})

    def _rodar(*, marcas=False, nota=False, cliente=None, env=None, **kw):
        if marcas:
            monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_MARCAS", "mm")
        if nota:
            monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_NOTA", "mm")
        cliente = cliente or _Client(capacidades=["marcas.v1", "produtos.nota.v1"])
        rep = catalogo_sync.run_catalogo_sync(
            "mm",
            now_iso="t",
            _client=cliente,
            _fire_conn=object(),
            _env_conn=env or _Env(),
            **kw,
        )
        return rep, cliente.sent.model_dump(by_alias=True)

    return _rodar


def test_marcas_ligado_manda_sem_a_linha_vazia(rodar):
    rep, corpo = rodar(marcas=True)
    assert corpo["marcasErp"] == [{"codigo": 2, "nome": "Kolosh"}, {"codigo": 3, "nome": "Sam's"}]
    mp = rep.model_extra["marcas_portal"]
    assert mp["enviadas"] == 2 and mp["capacidade_ok"] is True
    assert mp["filtradas"] == [{"codigo": 1, "motivo": "nome_vazio"}]
    assert "temNotaFiscal" not in corpo["itens"][0]


def test_marcas_desligado_corpo_sem_a_chave(rodar):
    rep, corpo = rodar()
    assert "marcasErp" not in corpo
    assert all("temNotaFiscal" not in i for i in corpo["itens"])
    assert "marcas_portal" not in (rep.model_extra or {})
    assert "nota_portal" not in (rep.model_extra or {})


def test_nota_ligada_marca_cada_item(rodar):
    rep, corpo = rodar(nota=True)
    assert [i["temNotaFiscal"] for i in corpo["itens"]] == [True, False]
    assert "marcasErp" not in corpo
    assert rep.model_extra["nota_portal"] == {
        "com_nota": 1,
        "enviados": 2,
        "leitura_ok": True,
        "capacidade_ok": True,
    }


def test_nota_com_leitura_quebrada_nenhum_item_com_a_chave(rodar, monkeypatch):
    def _quebra(conn):
        raise RuntimeError("NOTAPROD fora do ar")

    monkeypatch.setattr(catalogo_sync, "extract_com_nota", _quebra)
    rep, corpo = rodar(nota=True)
    assert all("temNotaFiscal" not in i for i in corpo["itens"])
    assert rep.model_extra["nota_portal"]["leitura_ok"] is False


def test_marcas_com_leitura_quebrada_nao_manda_marcas_erp(rodar, monkeypatch):
    def _quebra(conn):
        raise RuntimeError("PRODUTOS_MARCA fora do ar")

    monkeypatch.setattr(catalogo_sync, "extract_marcas", _quebra)
    rep, corpo = rodar(marcas=True)
    assert "marcasErp" not in corpo
    assert rep.model_extra["marcas_portal"]["leitura_ok"] is False


def test_resposta_sem_capacidade_marca_false_e_loga_erro(rodar, logs):
    rep, _ = rodar(marcas=True, nota=True, cliente=_Client())
    assert rep.model_extra["marcas_portal"]["capacidade_ok"] is False
    assert rep.model_extra["nota_portal"]["capacidade_ok"] is False
    erros = [m for nivel, m in logs if nivel == "ERROR"]
    assert any("marcas" in m and "marcas.v1" in m for m in erros)
    assert any("notaFiscal" in m and "produtos.nota.v1" in m for m in erros)


def test_resposta_guarda_o_que_o_flow_devolveu(rodar):
    cli = _Client(
        capacidades=["marcas.v1", "produtos.nota.v1"],
        marcas={"vinculadas": 3, "jaVinculadas": 5, "semPar": [], "conflitos": []},
        notaFiscal={"atualizados": 4},
    )
    rep, _ = rodar(marcas=True, nota=True, cliente=cli)
    mp, np_ = rep.model_extra["marcas_portal"], rep.model_extra["nota_portal"]
    assert (mp["vinculadas"], mp["jaVinculadas"], mp["semPar"], mp["conflitos"]) == (3, 5, [], [])
    assert "reenviar" not in mp and np_["atualizados"] == 4 and "reenviar" not in np_


def test_dry_run_guarda_recebidas(rodar):
    cli = _Client(capacidades=["marcas.v1"], marcas={"recebidas": 2})
    rep, _ = rodar(marcas=True, cliente=cli)
    assert rep.model_extra["marcas_portal"]["recebidas"] == 2


def test_erro_do_flow_vira_error_e_reenviar_nos_dois_blocos(rodar, logs):
    cli = _Client(
        capacidades=["marcas.v1", "produtos.nota.v1"],
        marcas={"erro": True},
        notaFiscal={"erro": True, "atualizados": 1},
    )
    rep, _ = rodar(marcas=True, nota=True, cliente=cli)
    assert rep.model_extra["marcas_portal"]["reenviar"] is True
    assert rep.model_extra["nota_portal"]["reenviar"] is True
    assert rep.model_extra["nota_portal"]["atualizados"] == 1
    erros = [m for nivel, m in logs if nivel == "ERROR"]
    assert len(erros) == 2


def test_sem_par_e_conflitos_viram_warning_com_os_nomes(rodar, logs):
    cli = _Client(
        capacidades=["marcas.v1"],
        marcas={"vinculadas": 0, "jaVinculadas": 0, "semPar": ["Kolosh"], "conflitos": ["Sam's"]},
    )
    rodar(marcas=True, cliente=cli)
    avisos = [m for nivel, m in logs if nivel == "WARNING"]
    assert any("semPar" in m and "Kolosh" in m for m in avisos)
    assert any("conflitos" in m and "Sam's" in m for m in avisos)


def test_seq_em_aberto_na_volta_nao_vai_no_corpo(rodar):
    rep, corpo = rodar(env=_Env(em_aberto=["3992", "55555"]))
    assert [i["fireProdutoId"] for i in corpo["itens"]] == ["3306"]
    assert rep.model_extra["fora_do_envio_volta"] == ["3992"]


def test_seq_orfao_de_criar_fica_fora_da_ida_e_o_de_atualizar_nao(rodar):
    """O Flow criaria um 2º produto para o SEQ que o Portal criou e o Flow não vinculou."""
    env = sqlite3.connect(":memory:")
    env.executescript(schema_env.TABLES_SQL)
    router._apply_column_migrations(env, schema_env.COLUMN_MIGRATIONS)
    for cid, acao, seq in (("a", "criar", "3992"), ("b", "atualizar", "3306")):
        env.execute(
            "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, "
            "modo, erp_produto_id) VALUES (?, 1, 'orfao', ?, 'simples', ?, 'criar', ?)",
            (cid, acao, f"p{cid}", seq),
        )
    rep, corpo = rodar(env=env)
    assert [i["fireProdutoId"] for i in corpo["itens"]] == ["3306"]
    assert rep.model_extra["fora_do_envio_volta"] == ["3992"]


def test_seq_em_aberto_soma_a_fora_sem_disparar_inexistente(rodar):
    rep, corpo = rodar(env=_Env(em_aberto=["3992", "55555"]), fora_do_envio=frozenset({"3306"}))
    assert corpo["itens"] == []
    assert rep.model_extra["fora_do_envio_volta"] == ["3992"]


def test_sem_nada_em_aberto_nao_reporta_volta(rodar):
    rep, corpo = rodar()
    assert len(corpo["itens"]) == 2
    assert "fora_do_envio_volta" not in (rep.model_extra or {})


def test_leituras_ok_marcam_leitura_ok_true(rodar):
    rep, _ = rodar(marcas=True, nota=True)
    assert rep.model_extra["marcas_portal"]["leitura_ok"] is True
    assert rep.model_extra["nota_portal"]["leitura_ok"] is True


def test_nota_lida_vazia_nao_manda_false_para_todos(rodar, monkeypatch, logs):
    monkeypatch.setattr(catalogo_sync, "extract_com_nota", lambda conn: set())
    rep, corpo = rodar(nota=True)
    assert all("temNotaFiscal" not in i for i in corpo["itens"])
    assert rep.model_extra["nota_portal"]["leitura_ok"] is False
    assert any(n == "ERROR" and "NOTAPROD" in m for n, m in logs)


def test_nota_lida_sem_nenhum_seq_do_lote_nao_manda_a_chave(rodar, monkeypatch, logs):
    monkeypatch.setattr(catalogo_sync, "extract_com_nota", lambda conn: {"1", "2"})
    rep, corpo = rodar(nota=True)
    assert all("temNotaFiscal" not in i for i in corpo["itens"])
    assert rep.model_extra["nota_portal"]["leitura_ok"] is False
    assert any(n == "ERROR" and "NOTAPROD" in m for n, m in logs)


def test_400_com_so_marcas_ligada_loga_corpo_e_cita_o_interruptor(rodar, logs):
    from app.integrations.flowpcp.client import FlowPCPClientError

    class _Recusa(_Client):
        def send_catalogo(self, request):
            raise FlowPCPClientError("HTTP 400", status_code=400, body="marcasErp.0.nome: too_big")

    with pytest.raises(FlowPCPClientError) as e:
        rodar(marcas=True, cliente=_Recusa())
    assert "PORTAL_FLOWPCP_CATALOGO_MARCAS" in str(e.value)
    assert "COMPOSICAO" not in str(e.value) and "NOTA" not in str(e.value)
    assert any("marcasErp.0.nome" in m for n, m in logs if n == "ERROR")


def test_capacidade_sem_bloco_no_apply_avisa(rodar, logs):
    cli = _Client(capacidades=["marcas.v1"])
    rodar(marcas=True, cliente=cli, dry_run=False)
    assert any(n == "WARNING" and "não devolveu o bloco" in m for n, m in logs)


def test_falha_na_leitura_do_livro_aborta_mas_mantem_a_copia_local(rodar):
    class _EnvQuebraNoLivro(_Env):
        gravou = False

        def execute(self, sql, params=()):
            if "cadastro_volta" in sql:
                raise RuntimeError("livro ilegível")
            return super().execute(sql, params)

        def executemany(self, *a):
            self.gravou = True

    env = _EnvQuebraNoLivro()
    cli = _Client()
    with pytest.raises(RuntimeError):
        rodar(env=env, cliente=cli)
    assert env.gravou is True and cli.sent is None
