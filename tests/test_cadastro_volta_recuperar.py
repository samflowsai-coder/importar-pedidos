"""Recuperação de cadastro interrompido (spec §6.3): adota, libera ou `incerto`.

Fire só-leitura em memória (qualquer escrita derruba o teste); SQLite do ambiente real.
"""

import json
import re
import time
from datetime import UTC, date, datetime

import pytest

from app.integrations.flowpcp import cadastro_recuperacao as rec
from app.integrations.flowpcp import cadastro_volta as cv
from app.persistence import cadastro_volta_repo as repo
from app.persistence import router
from app.utils.logger import logger
from tests.test_cadastro_volta_gravar import PAR, FireGravavel, Flow, Relogio

AGORA = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)
ANTIGA = "2026-10-17 12:00:00"
SOQUETE = {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8, "CLASSIF_FISCAL": "61159500"}


class _Cur:
    def __init__(self, fire):
        self.fire, self.rows = fire, []

    def execute(self, sql, params=()):
        self.rows = self.fire.ler(" ".join(sql.split()), tuple(params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class FireLeitura:
    """`produtos` {seq: dict} e `kit` {pai: [(filho, qtd)]}. Só SELECT; o resto falha."""

    def __init__(self, produtos=None, kit=None, falha=None):
        self.produtos, self.kit, self.falha = produtos or {}, kit or {}, falha
        self.consultas: list = []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        raise AssertionError("a recuperação não comita no ERP")

    def ler(self, sql, params):
        self.consultas.append((sql, params))
        if self.falha is not None:
            raise self.falha
        m = re.fullmatch(r"SELECT (.+) FROM PRODUTOS WHERE SEQ = \?", sql)
        if m:
            p = self.produtos.get(params[0])
            cols = [c.strip() for c in m.group(1).split(",")]
            return [tuple(params[0] if c == "SEQ" else p.get(c) for c in cols)] if p else []
        if sql.startswith("SELECT CODPRODUTO, QTD FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?"):
            return sorted(self.kit.get(params[0], []))
        if sql == ("SELECT SEQ, DESCRICAO FROM PRODUTOS WHERE GRUPO = ? AND UNIDADE = ? "
                   "AND DATA_CAD = ?"):
            grupo, unidade, dia = params
            return [(s, p["DESCRICAO"]) for s, p in self.produtos.items()
                    if p.get("GRUPO") == grupo and p.get("UNIDADE") == unidade
                    and p.get("DATA_CAD") == dia]
        raise AssertionError(f"SQL inesperado na recuperação: {sql}")


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
    hid = logger.add(lambda m: msgs.append(str(m)), level="ERROR",
                     filter=lambda r: r["level"].name == "ERROR")
    yield msgs
    logger.remove(hid)


@pytest.fixture
def fuso_sp(monkeypatch):
    """`DATA_CAD` é a data local do servidor (America/Sao_Paulo no cliente)."""
    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _plano(campos=SOQUETE, componentes=None, acao="criar"):
    return json.dumps({"acao": acao, "tabela": "PRODUTOS", "tipo": "simples", "seq": None,
                       "campos": campos, "componentes": componentes})


def _linha(env_conn, cid="c1", *, seq=None, acao="criar", plano=None, iniciado=ANTIGA):
    assert repo.reservar(env_conn, cadastro_id=cid, versao=1, produto_id="p1", acao=acao,
                         tipo="simples", modo="criar", plano_json=plano or _plano(acao=acao))
    if seq is not None:
        assert repo.marcar_seq(env_conn, cid, 1, seq)
    env_conn.execute("UPDATE cadastro_volta SET iniciado_em = ? WHERE cadastro_id = ?",
                     (iniciado, cid))


def _no_erp(**kw):
    return {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8, "CODIGO_EAN13": None,
            "CLASSIF_FISCAL": "61159500", "DATA_CAD": date(2026, 10, 17), **kw}


def _estado(env_conn, cid="c1"):
    linha = repo.buscar(env_conn, cid, 1)
    return linha["estado"], linha["seq_reservado"], linha["erp_produto_id"]


# --- brief ---------------------------------------------------------------


def test_adota_seq_reservado_quando_tudo_bate(env_conn):
    plano = {"acao": "criar", "campos": {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8,
             "CLASSIF_FISCAL": "61159500"}, "componentes": None}
    repo.reservar(env_conn, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                  tipo="simples", modo="criar", plano_json=json.dumps(plano))
    repo.marcar_seq(env_conn, "c1", 1, 3992)
    env_conn.execute("UPDATE cadastro_volta SET iniciado_em = '2026-10-17 12:00:00'")
    fire = FireLeitura(produtos={3992: {"SEQ": 3992, "DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR",
                                        "GRUPO": 8, "CODIGO_EAN13": None, "CLASSIF_FISCAL": "61159500",
                                        "DATA_CAD": date(2026, 10, 17)}})
    r = rec.recuperar("mm", env_conn, fire, agora=datetime(2026, 10, 17, 13, 0, tzinfo=UTC))
    assert r["adotado"] == 1
    linha = repo.buscar(env_conn, "c1", 1)
    assert (linha["estado"], linha["erp_produto_id"]) == ("gravado_sem_volta", "3992")
    corpo = json.loads(linha["corpo_json"])
    assert (corpo["resultado"], corpo["versao"], corpo["erpProdutoId"]) == ("gravado", 1, "3992")
    assert repo.tem_pendencia(env_conn)  # o envio fica para reenviar_pendencias


def test_adota_kit_com_composicao_igual(env_conn):
    comps = [{"erpProdutoId": "3170", "quantidade": 3.0}, {"erpProdutoId": "3100", "quantidade": 1}]
    kit = {**SOQUETE, "DESCRICAO": "KIT", "UNIDADE": "KIT"}
    _linha(env_conn, seq=3992, plano=_plano(kit, comps))
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO="KIT", UNIDADE="KIT")},
                       kit={3992: [(3100, 1.0), (3170, 3.0)]})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1
    assert _estado(env_conn) == ("gravado_sem_volta", 3992, "3992")


def test_campos_aparados_e_vazio_igual_a_none(env_conn):
    _linha(env_conn, seq=3992, plano=_plano({**SOQUETE, "CODIGO_EAN13": ""}))
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO="MEIA SOQUETE  ", GRUPO="8")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1


def test_libera_quando_o_seq_tem_outro_produto(env_conn):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO="OUTRA COISA")})
    r = rec.recuperar("mm", env_conn, fire, agora=AGORA)
    assert r == {"adotado": 0, "liberado": 1, "incerto": 0, "reaplicar": 0}
    assert _estado(env_conn) == ("liberado", None, None)  # Emenda 3: o SEQ zera junto


def test_libera_quando_o_seq_nao_existe(env_conn):
    _linha(env_conn, seq=3992)
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["liberado"] == 1
    assert _estado(env_conn) == ("liberado", None, None)
    # Liberado sem SEQ volta a ser reservável: o item segue pela fila.
    assert repo.reservar(env_conn, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                         tipo="simples", modo="criar", plano_json=_plano())


def test_incerto_quando_a_composicao_difere(env_conn, erros):
    comps = [{"erpProdutoId": "3170", "quantidade": 3}]
    _linha(env_conn, seq=3992, plano=_plano(componentes=comps))
    fire = FireLeitura(produtos={3992: _no_erp()}, kit={3992: [(3170, 2.0)]})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    linha = repo.buscar(env_conn, "c1", 1)
    assert (linha["estado"], linha["alerta"], linha["seq_reservado"]) == (
        "incerto", "recuperacao", 3992)
    assert any("c1 v1" in e and "3992" in e for e in erros)


def test_incerto_quando_simples_tem_composicao_no_erp(env_conn):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp()}, kit={3992: [(3170, 1.0)]})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


def test_adota_por_nome_com_um_candidato(env_conn):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(), 4002: _no_erp(DESCRICAO="OUTRA")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1
    assert _estado(env_conn) == ("gravado_sem_volta", None, "4001")


def test_incerto_com_dois_candidatos_por_nome(env_conn, erros):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(), 4002: _no_erp(DESCRICAO="meia soquete")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"
    assert any("[4001, 4002]" in e for e in erros)


def test_libera_sem_candidato_por_nome(env_conn):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=date(2026, 10, 16))})  # outro dia
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["liberado"] == 1
    assert _estado(env_conn) == ("liberado", None, None)


def test_um_candidato_por_nome_com_campo_diferente_e_incerto(env_conn):
    """O nome está lá: nunca libera nem adota um produto que difere do plano."""
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(CLASSIF_FISCAL="64000000")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


def test_busca_por_nome_atravessa_a_meia_noite_local(env_conn, fuso_sp):
    # 02:58 UTC de 18/10 = 23:58 de 17/10 em São Paulo; o INSERT caiu já no dia 18.
    _linha(env_conn, iniciado="2026-10-18 02:58:00")
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=date(2026, 10, 18))})
    agora = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    assert rec.recuperar("mm", env_conn, fire, agora=agora)["adotado"] == 1
    dias = sorted({p[2] for s, p in fire.consultas if "DATA_CAD = ?" in s})
    assert dias == [date(2026, 10, 17), date(2026, 10, 18)]


def test_busca_por_nome_usa_a_data_local(env_conn, fuso_sp):
    # 01:00 UTC de 18/10 = 22:00 de 17/10 em São Paulo: só o dia 17.
    _linha(env_conn, iniciado="2026-10-18 01:00:00")
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=date(2026, 10, 18))})
    agora = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    assert rec.recuperar("mm", env_conn, fire, agora=agora)["liberado"] == 1
    assert {p[2] for s, p in fire.consultas if "DATA_CAD = ?" in s} == {date(2026, 10, 17)}


def test_plano_sem_unidade_nao_busca_e_fica_incerto(env_conn):
    """UNIDADE nula no ERP nunca casa `UNIDADE = ?`: zero candidatos não provaria nada."""
    _linha(env_conn, plano=_plano({k: v for k, v in SOQUETE.items() if k != "UNIDADE"}))
    fire = FireLeitura()
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert fire.consultas == []


def test_plano_ilegivel_fica_incerto(env_conn):
    _linha(env_conn, seq=3992, plano="{não é json")
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["incerto"] == 1


@pytest.mark.parametrize("acao", ["criar", None])
def test_reserva_sem_plano_e_sem_seq_libera(env_conn, acao):
    """Linha reservada só para responder erro (até item sem ação legível): nada saiu ao ERP."""
    repo.reservar(env_conn, cadastro_id="c1", versao=1, produto_id="p1", acao=acao,
                  tipo="simples", modo="criar", plano_json=None)
    env_conn.execute("UPDATE cadastro_volta SET iniciado_em = ?", (ANTIGA,))
    fire = FireLeitura()
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["liberado"] == 1
    assert fire.consultas == []


@pytest.mark.parametrize("acao", ["atualizar", "inativar"])
def test_libera_atualizar_e_inativar(env_conn, acao):
    _linha(env_conn, acao=acao)
    fire = FireLeitura()
    r = rec.recuperar("mm", env_conn, fire, agora=AGORA)
    assert r == {"adotado": 0, "liberado": 0, "incerto": 0, "reaplicar": 1}
    assert _estado(env_conn) == ("liberado", None, None)
    assert fire.consultas == []


def test_nao_toca_linha_com_menos_de_dois_minutos(env_conn):
    _linha(env_conn, seq=3992, iniciado="2026-10-17 12:58:30")
    fire = FireLeitura()
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA) == dict.fromkeys(
        ("adotado", "liberado", "incerto", "reaplicar"), 0)
    assert _estado(env_conn) == ("em_andamento", 3992, None)
    assert fire.consultas == []
    assert not rec.ha_o_que_recuperar(env_conn, agora=AGORA)


def test_ignora_incerto_da_triagem(env_conn):
    """I3 da Task 7: `incerto` com plano nulo é da ferramenta do operador."""
    _linha(env_conn, seq=3992)
    repo.marcar_desfecho(env_conn, "c1", 1, "incerto", alerta="nome_do_proprio_produto")
    fire = FireLeitura()
    assert sum(rec.recuperar("mm", env_conn, fire, agora=AGORA).values()) == 0
    assert _estado(env_conn)[0] == "incerto" and fire.consultas == []
    assert not rec.ha_o_que_recuperar(env_conn, agora=AGORA)


def test_leitura_que_falha_para_e_deixa_as_linhas(env_conn, erros):
    _linha(env_conn, "c1", seq=3992)
    _linha(env_conn, "c2", seq=3993)
    fire = FireLeitura(falha=OSError("rede caiu"))
    assert sum(rec.recuperar("mm", env_conn, fire, agora=AGORA).values()) == 0
    assert _estado(env_conn, "c1")[0] == _estado(env_conn, "c2")[0] == "em_andamento"
    assert len(fire.consultas) == 1  # parou na primeira
    assert any("OSError" in e and "rede caiu" not in e for e in erros)


def test_transicao_recusada_pelo_livro_nao_conta(env_conn, monkeypatch, erros):
    _linha(env_conn, seq=3992)
    monkeypatch.setattr(repo, "liberar_comprovado", lambda *a: False)
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["liberado"] == 0
    assert any("não aplicado no livro" in e for e in erros)


# --- transitorio com SEQ (M3 da revisão da Task 4) ----------------------------


def _transitorio(env_conn, seq=3992):
    _linha(env_conn, seq=seq)
    repo.marcar_erro_a_enviar(env_conn, "c1", 1, codigo="x", mensagem="m", corpo_json="{}",
                              rollback_comprovado=False)
    assert repo.marcar_desfecho(env_conn, "c1", 1, "transitorio")


def test_transitorio_com_seq_adota_quando_o_produto_esta_la(env_conn):
    _transitorio(env_conn)
    fire = FireLeitura(produtos={3992: _no_erp()})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1
    assert _estado(env_conn) == ("gravado_sem_volta", 3992, "3992")


def test_transitorio_com_seq_libera_quando_ausente(env_conn):
    _transitorio(env_conn)
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["liberado"] == 1
    assert _estado(env_conn) == ("liberado", None, None)


def test_transitorio_com_seq_e_composicao_diferente_fica_incerto(env_conn):
    _transitorio(env_conn)
    fire = FireLeitura(produtos={3992: _no_erp()}, kit={3992: [(3170, 1.0)]})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"


# --- no ciclo de gravação -------------------------------------------------------


class FireCiclo(FireGravavel):
    """O Fire gravável do ciclo, com as leituras da recuperação."""

    def _responder(self, sql, params):
        if sql.startswith("SELECT CODPRODUTO, QTD FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?"):
            return sorted((k[2], k[3]) for k in self.kit if k[1] == params[0]), 0
        return super()._responder(sql, params)


@pytest.fixture
def gravacao(monkeypatch):
    for var in (cv.VAR_MODO, cv.VAR_P0, cv.VAR_INATIVAR):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(cv.VAR_MODO, "mm:criar")
    monkeypatch.setenv(cv.VAR_P0, "mm")
    cv._avisados.clear()
    monkeypatch.setattr(cv, "_ultima_volta", None)
    monkeypatch.setattr(cv, "_ultimo_ciclo", {})


def _interrompido(env_conn, seq):
    """O processo caiu depois do INSERT do PAR com `seq` (comitado ou não)."""
    plano = _plano({**SOQUETE, "MARCA": "Kolosh", "CODITEMSPED": 3})
    assert repo.reservar(env_conn, cadastro_id=PAR["cadastroId"], versao=1,
                         produto_id=PAR["produtoId"], acao="criar", tipo="simples",
                         modo="criar", plano_json=plano)
    assert repo.marcar_seq(env_conn, PAR["cadastroId"], 1, seq)
    env_conn.execute("UPDATE cadastro_volta SET iniciado_em = ?", (ANTIGA,))
    env_conn.commit()


def test_ciclo_adota_antes_do_get_e_envia_o_gravado(gravacao, env_conn):
    _interrompido(env_conn, 3171)
    fire = FireCiclo(produtos={3170: {"DESCRICAO": "PAR BASE", "BLOQUEADO": "Nao"},
                               3171: _no_erp()})
    flow = Flow([PAR])  # o Flow reentrega o item
    r = cv.processar_ambiente("mm", _client=flow, _fire_conn=fire, _env_conn=env_conn,
                              _relogio=Relogio())
    assert r["recuperacao"]["adotado"] == 1
    assert fire.escritas == []  # não grava de novo
    (cid, corpo), = flow.posts
    assert (cid, corpo["resultado"], corpo["erpProdutoId"]) == (PAR["cadastroId"], "gravado", "3171")


def test_ciclo_libera_e_grava_o_item_reentregue(gravacao, env_conn):
    _interrompido(env_conn, 3171)  # o INSERT não comitou
    fire = FireCiclo()
    flow = Flow([PAR])
    r = cv.processar_ambiente("mm", _client=flow, _fire_conn=fire, _env_conn=env_conn,
                              _relogio=Relogio())
    assert r["recuperacao"]["liberado"] == 1
    assert sorted(fire.produtos) == [3170, 3171]  # gravado uma vez, pela fila
    assert _estado(env_conn, PAR["cadastroId"]) == ("confirmado", 3171, "3171")


def test_ciclo_recupera_mesmo_com_a_fila_vazia(gravacao, env_conn):
    _interrompido(env_conn, 3171)
    fire = FireCiclo()
    r = cv.processar_ambiente("mm", _client=Flow([]), _fire_conn=fire, _env_conn=env_conn,
                              _relogio=Relogio())
    assert r["recuperacao"]["liberado"] == 1


def test_ciclo_sem_linha_antiga_nao_abre_o_erp(gravacao, env_conn, monkeypatch):
    def _nao_abre(slug):
        raise AssertionError("abriu o ERP sem nada a recuperar nem a gravar")

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _nao_abre)
    r = cv.processar_ambiente("mm", _client=Flow([]), _fire_conn=None, _env_conn=env_conn,
                              _relogio=Relogio())
    assert "recuperacao" not in r


def test_ciclo_com_erp_fora_adia_a_recuperacao(gravacao, env_conn, monkeypatch, erros):
    _interrompido(env_conn, 3171)

    def _fora(slug):
        raise OSError("sem rede")

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _fora)
    flow = Flow([])
    r = cv.processar_ambiente("mm", _client=flow, _fire_conn=None, _env_conn=env_conn,
                              _relogio=Relogio())
    assert r["recuperacao"] == {"erro": "OSError"} and flow.gets  # o ciclo segue
    assert _estado(env_conn, PAR["cadastroId"])[0] == "em_andamento"
    assert any("recuperação" in e for e in erros)
