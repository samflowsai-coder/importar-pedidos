"""Recuperação de cadastro interrompido (spec §6.3): adota, libera ou `incerto`.

Fire só-leitura em memória (qualquer escrita derruba o teste); SQLite do ambiente real.
"""

import json
import re
import sqlite3
import time
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.integrations.flowpcp import cadastro_recuperacao as rec
from app.integrations.flowpcp import cadastro_volta as cv
from app.persistence import cadastro_volta_repo as repo
from app.persistence import router
from app.utils.logger import logger
from tests.test_cadastro_volta_gravar import CONEXAO, LOCK, PAR, FireGravavel, Flow, Relogio

AGORA = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)
_SQL_VERSAO = "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"
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
    """`produtos` {seq: dict} e `kit` {pai: [(filho, qtd)]}. Só SELECT; o resto falha.
    A versão do motor (`versao`) responde à parte: não entra em `consultas` nem em `falha`."""

    def __init__(self, produtos=None, kit=None, falha=None, versao="2.5.9"):
        self.produtos, self.kit, self.falha, self.versao = produtos or {}, kit or {}, falha, versao
        self.consultas: list = []

    def cursor(self):
        return _Cur(self)

    def commit(self):
        raise AssertionError("a recuperação não comita no ERP")

    def ler(self, sql, params):
        if sql == _SQL_VERSAO:
            return [(self.versao,)]
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
        m = re.fullmatch(r"SELECT (.+) FROM PRODUTOS WHERE DATA_CAD BETWEEN \? AND \?", sql)
        if m:
            de, ate = params
            cols = [c.strip() for c in m.group(1).split(",")]
            return [tuple(s if c == "SEQ" else p.get(c) for c in cols)
                    for s, p in sorted(self.produtos.items())
                    if isinstance(p.get("DATA_CAD"), date)
                    and de <= p["DATA_CAD"] <= ate]  # BETWEEN do SQL: nulo fica de fora
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
    env_conn.commit()  # como o ciclo: a recuperação pode dar rollback no SQLite


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


def test_libera_quando_o_seq_tem_produto_cadastrado_com_folga_antes(env_conn):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO="OUTRA COISA",
                                               DATA_CAD=date(2026, 10, 15))})
    r = rec.recuperar("mm", env_conn, fire, agora=AGORA)
    assert r == {"adotado": 0, "liberado": 1, "incerto": 0, "reaplicar": 0}
    assert _estado(env_conn) == ("liberado", None, None)  # Emenda 3: o SEQ zera junto


@pytest.mark.parametrize("dia", [date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)])
def test_descricao_diferente_na_janela_da_reserva_fica_incerto(env_conn, dia):
    """Renomeado na tela ou por trigger: dentro da janela, outra descrição não prova nada."""
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO="OUTRA COISA", DATA_CAD=dia)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn) == ("incerto", 3992, None)


@pytest.mark.parametrize("descricao", [b"MEIA SOQUETE", "", "   ", 123])
def test_descricao_ilegivel_fica_incerto(env_conn, descricao):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO=descricao)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


@pytest.mark.parametrize("data_cad", [None, "2026-10-01", 20261001])
def test_data_cad_ilegivel_com_divergencia_fica_incerto(env_conn, data_cad):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DESCRICAO="OUTRA", DATA_CAD=data_cad)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


@pytest.mark.parametrize("data_cad", [None, "2026-10-17"])
def test_data_cad_ilegivel_com_tudo_batendo_fica_incerto(env_conn, data_cad):
    """O gravador sempre grava DATA_CAD = hoje: nulo ou ilegível é linha mexida."""
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DATA_CAD=data_cad)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


@pytest.mark.parametrize("grupo", [8.0, Decimal("8"), Decimal("8.000"), "8"])
def test_grupo_numerico_inteiro_adota(env_conn, grupo):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(GRUPO=grupo)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1


@pytest.mark.parametrize("campos", [
    {"GRUPO": None}, {"GRUPO": 8.5}, {"GRUPO": "oito"}, {"GRUPO": 9},
    {"CLASSIF_FISCAL": "61159900"},  # editada à mão no mesmo dia
    {"DESCRICAO": "meia soquete"},  # mesma descrição em outra caixa
    {"DESCRICAO": None}, {"UNIDADE": "UN"}, {"CODIGO_EAN13": "7890000000000"},
])
def test_mesma_descricao_e_dia_com_outra_divergencia_fica_incerto(env_conn, erros, campos):
    """Sem prova forte de que é outro produto, nunca libera (seria gravado de novo)."""
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(**campos)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn) == ("incerto", 3992, None)


def test_produto_anterior_a_reserva_com_folga_libera_mesmo_com_a_descricao_igual(env_conn):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(produtos={3992: _no_erp(DATA_CAD=date(2026, 10, 15))})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["liberado"] == 1
    assert _estado(env_conn) == ("liberado", None, None)


def test_dia_anterior_nao_libera_quando_o_fuso_do_gravador_difere(env_conn, monkeypatch):
    """Sonda edge_tz: gravado às 22:30 de 16/10 em São Paulo (01:30 UTC de 17/10) e
    recuperado num processo em UTC. O DATA_CAD 16/10 é o nosso produto: adota."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        _linha(env_conn, seq=3992, iniciado="2026-10-17 01:30:00")
        fire = FireLeitura(produtos={3992: _no_erp(DATA_CAD=date(2026, 10, 16))})
        assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1
    finally:
        monkeypatch.undo()
        time.tzset()


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
    fire = FireLeitura(produtos={4001: _no_erp(),
                                 4002: _no_erp(DESCRICAO="OUTRA", DATA_CAD=date(2026, 10, 15))})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1
    assert _estado(env_conn) == ("gravado_sem_volta", None, "4001")


def test_exato_ao_lado_de_outro_produto_na_janela_fica_incerto(env_conn, erros):
    """Sem SEQ, só um candidato na janela inteira adota: o outro pode ser o nosso renomeado."""
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(), 4002: _no_erp(DESCRICAO="OUTRA", GRUPO=9)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"
    assert any("[4001, 4002]" in e for e in erros)


def test_incerto_com_dois_candidatos_por_nome(env_conn, erros):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(), 4002: _no_erp(DESCRICAO="meia soquete")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"
    assert any("[4001, 4002]" in e for e in erros)


def test_libera_com_a_janela_vazia(env_conn):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=date(2026, 10, 15)),  # antes da janela
                                 4002: _no_erp(DATA_CAD=date(2026, 10, 19))})  # depois
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["liberado"] == 1
    assert _estado(env_conn) == ("liberado", None, None)


def test_outros_produtos_na_janela_ficam_incerto(env_conn, erros):
    """Qualquer produto na janela pode ser o nosso mexido na tela: sem SEQ, não libera."""
    _linha(env_conn)
    fire = FireLeitura(produtos={4002: _no_erp(GRUPO=9), 4003: _no_erp(DESCRICAO="MEIA CANO ALTO")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert any("[4002, 4003]" in e for e in erros)


@pytest.mark.parametrize("nome", ["MEIA SOQUETE BRANCA", "MEIA-SOQUETE", "MEIA\u200bSOQUETE",
                                  "MEIA SOQUETE\u200b"])
def test_renomeado_alem_do_parecido_fica_incerto(env_conn, erros, nome):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(DESCRICAO=nome)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"
    assert any("SEQs candidatos [4001]" in e for e in erros)


def test_mesmo_nome_com_grupo_trocado_fica_incerto(env_conn, erros):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(GRUPO=9)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"
    assert any("SEQs candidatos [4001]" in e for e in erros)


def test_um_candidato_por_nome_com_campo_diferente_e_incerto(env_conn):
    """O nome está lá: nunca libera nem adota um produto que difere do plano."""
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(CLASSIF_FISCAL="64000000")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


def _janela(fire):
    (sql, params), = [c for c in fire.consultas if "BETWEEN" in c[0]]
    return params


def test_busca_por_nome_atravessa_a_meia_noite_local(env_conn, fuso_sp):
    # 02:58 UTC de 18/10 = 23:58 de 17/10 em São Paulo; o INSERT pode ter caído no dia 18.
    _linha(env_conn, iniciado="2026-10-18 02:58:00")
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=date(2026, 10, 18))})
    agora = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    assert rec.recuperar("mm", env_conn, fire, agora=agora)["adotado"] == 1
    assert _janela(fire) == (date(2026, 10, 16), date(2026, 10, 19))


def test_busca_por_nome_usa_a_data_local_com_um_dia_de_folga(env_conn, fuso_sp):
    # 01:00 UTC de 18/10 = 22:00 de 17/10 em São Paulo: janela de 16 a 18.
    _linha(env_conn, iniciado="2026-10-18 01:00:00")
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=date(2026, 10, 19))})
    agora = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
    assert rec.recuperar("mm", env_conn, fire, agora=agora)["liberado"] == 1
    assert _janela(fire) == (date(2026, 10, 16), date(2026, 10, 18))


@pytest.mark.parametrize("nome", ["Meia  Soquete", "MÉIA SOQUETE", " meia soquete "])
def test_nome_parecido_mas_nao_exato_fica_incerto(env_conn, nome):
    """Renomeado na tela (caixa, acento, espaços): o nome parecido não libera nem adota."""
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(DESCRICAO=nome)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


def test_nome_parecido_ao_lado_do_exato_fica_incerto(env_conn):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(), 4002: _no_erp(DESCRICAO="Meia Soquete")})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


@pytest.mark.parametrize("descricao", [None, b"MEIA SOQUETE", 7, "", "   "])
def test_descricao_nao_texto_na_janela_fica_incerto(env_conn, descricao):
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(DESCRICAO=descricao)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1


def test_data_cad_nulo_fica_fora_da_janela_da_busca(env_conn):
    """Como o BETWEEN do SQL: DATA_CAD nulo não é candidato (o gravador nunca grava nulo)."""
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp(DATA_CAD=None)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["liberado"] == 1


def test_plano_sem_unidade_busca_e_compara_vazio(env_conn):
    """A busca não filtra UNIDADE no SQL: UNIDADE nula dos dois lados bate."""
    _linha(env_conn, plano=_plano({k: v for k, v in SOQUETE.items() if k != "UNIDADE"}))
    fire = FireLeitura(produtos={4001: _no_erp(UNIDADE=None)})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1


def test_plano_sem_nome_ou_grupo_nao_busca_e_fica_incerto(env_conn):
    _linha(env_conn, plano=_plano({k: v for k, v in SOQUETE.items() if k != "GRUPO"}))
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


def test_nao_toca_linha_com_menos_da_idade_minima(env_conn):
    # 15 min: maior que PRAZO_LOTE (8 min), a gravação viva de um ciclo não é tocada.
    assert rec.IDADE_MINIMA_RECUPERACAO == timedelta(minutes=15)
    _linha(env_conn, seq=3992, iniciado="2026-10-17 12:46:00")
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


@pytest.mark.parametrize("falha", [LOCK, CONEXAO])
def test_lock_ou_conexao_na_leitura_para_e_deixa_as_linhas(env_conn, erros, falha):
    """Registro pendente de outra transação (NO_REC_VERSION) = conflito de lock, não ausência."""
    _linha(env_conn, "c1", seq=3992)
    _linha(env_conn, "c2", seq=3993)
    fire = FireLeitura(falha=falha())
    r = rec.recuperar("mm", env_conn, fire, agora=AGORA)
    assert r == {"adotado": 0, "liberado": 0, "incerto": 0, "reaplicar": 0, "parado": "FdbError"}
    assert _estado(env_conn, "c1")[0] == _estado(env_conn, "c2")[0] == "em_andamento"
    assert len(fire.consultas) == 1  # parou na primeira
    assert any("FdbError" in e and "VALOR-DO-USUARIO" not in e for e in erros)


def test_outra_falha_na_leitura_fica_incerto_so_naquela_linha(env_conn, erros):
    _linha(env_conn, "c1", seq=3992)
    _linha(env_conn, "c2", acao="atualizar")

    class _Falha(FireLeitura):
        def ler(self, sql, params):
            if sql == _SQL_VERSAO:
                return [("2.5.9",)]
            raise ValueError("coluna estranha")

    r = rec.recuperar("mm", env_conn, _Falha(), agora=AGORA)
    assert (r["incerto"], r["reaplicar"], "parado" in r) == (1, 1, False)
    assert _estado(env_conn, "c1")[0] == "incerto"
    assert any("ValueError" in e and "coluna estranha" not in e for e in erros)


def test_iniciado_em_ilegivel_fica_incerto(env_conn, erros):
    _linha(env_conn, seq=3992, iniciado="ontem à tarde")
    fire = FireLeitura(produtos={3992: _no_erp()})
    assert rec.ha_o_que_recuperar(env_conn, agora=AGORA)
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert fire.consultas == []
    assert any("iniciado_em" in e for e in erros)


@pytest.mark.parametrize("ligacao", ["gravado_de_outro_produto", "outra_versao_do_produto"])
def test_candidato_por_nome_ja_ligado_a_outra_linha_fica_incerto(env_conn, ligacao):
    if ligacao == "gravado_de_outro_produto":
        repo.reservar(env_conn, cadastro_id="c9", versao=1, produto_id="p9", acao="criar",
                      tipo="simples", modo="criar", plano_json=_plano())
        repo.marcar_gravado(env_conn, "c9", 1, erp_produto_id="4001", corpo_json="{}")
        repo.marcar_desfecho(env_conn, "c9", 1, "confirmado")
    else:  # v1 do mesmo produto reservou o 4001 e ficou incerto
        repo.reservar(env_conn, cadastro_id="c9", versao=1, produto_id="p1", acao="criar",
                      tipo="simples", modo="criar", plano_json=_plano())
        repo.marcar_seq(env_conn, "c9", 1, 4001)
        repo.marcar_desfecho(env_conn, "c9", 1, "incerto")
    _linha(env_conn)
    fire = FireLeitura(produtos={4001: _no_erp()})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"


def test_transicao_recusada_pelo_livro_nao_conta(env_conn, monkeypatch, erros):
    _linha(env_conn, seq=3992)
    monkeypatch.setattr(repo, "liberar_comprovado", lambda *a: False)
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["liberado"] == 0
    assert any("não aplicado no livro" in e for e in erros)


class _Sonda(FireLeitura):
    """Aceita qualquer SQL (o ERP de verdade aceitaria): só o guarda pode recusar."""

    def ler(self, sql, params):
        if sql == _SQL_VERSAO:
            return [("2.5.9",)]
        self.consultas.append((sql, params))
        return []


@pytest.mark.parametrize("sql", ["UPDATE PRODUTOS SET DESCRICAO = ? WHERE SEQ = ?",
                                 "  insert into PRODUTOS (SEQ) VALUES (?)",
                                 "DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?",
                                 "SELECT SEQ FROM PRODUTOS WHERE SEQ = ? WITH LOCK",
                                 "select seq from produtos where seq = ? with\n  lock",
                                 "SELECT GEN_ID(G_PRODUTOS, 1) FROM RDB$DATABASE",
                                 "select gen_id (g, 1) from rdb$database",
                                 "SELECT NEXT VALUE FOR G_PRODUTOS FROM RDB$DATABASE",
                                 "select next  value\nfor g from rdb$database",
                                 "SELECT 1 FROM RDB$DATABASE /* ; */",
                                 "SELECT 1 FROM RDB$DATABASE -- x"])
def test_guarda_da_recuperacao_so_deixa_passar_select(sql):
    fire = _Sonda()
    guardada = rec.SoSelect(fire)
    with pytest.raises(RuntimeError, match="só SELECT"):
        guardada.cursor().execute(sql, (1,))
    with pytest.raises(RuntimeError, match="só SELECT"):
        guardada.commit()
    assert fire.consultas == []
    guardada.cursor().execute("SELECT 1 FROM RDB$DATABASE")
    assert len(fire.consultas) == 1


def test_recuperar_le_o_erp_pelo_guarda(env_conn, monkeypatch, erros):
    """Uma escrita que escapasse no caminho da recuperação é barrada e vira incerto."""
    _linha(env_conn, seq=3992)
    fire = _Sonda()

    def _escreve(conn, seq):
        conn.cursor().execute("UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?", (seq,))

    monkeypatch.setattr(rec.cadastro_fire, "ler_produto", _escreve)
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert fire.consultas == []


# --- falha no meio da decisão (sonda edge_f) ------------------------------------


class _KitFalha(FireLeitura):
    """`ler_produto` responde; a leitura da composição falha."""

    def __init__(self, exc, **kw):
        super().__init__(**kw)
        self.exc = exc

    def ler(self, sql, params):
        if "PRODUTOS_KIT" in sql:
            raise self.exc
        return super().ler(sql, params)


def _dois(env_conn, transitorio=False):
    _linha(env_conn, "c1", seq=3992)
    _linha(env_conn, "c2", seq=3993)
    if transitorio:
        env_conn.execute("UPDATE cadastro_volta SET estado = 'transitorio' WHERE cadastro_id = 'c1'")
        env_conn.commit()
    return {3992: _no_erp(), 3993: _no_erp()}


@pytest.mark.parametrize("falha", [LOCK, CONEXAO])
def test_lock_ou_conexao_depois_de_ler_o_produto_para_sem_marcar(env_conn, falha):
    """F1/F2: a falha vem depois de `ler_produto`; nada é marcado e o SQLite fica limpo."""
    fire = _KitFalha(falha(), produtos=_dois(env_conn))
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["parado"] == "FdbError"
    assert _estado(env_conn, "c1") == ("em_andamento", 3992, None)
    assert _estado(env_conn, "c2") == ("em_andamento", 3993, None)
    assert not env_conn.in_transaction


def test_outra_falha_depois_de_ler_o_produto_fica_incerto_e_segue(env_conn):
    """F3: falha que não é lock nem conexão: incerto só naquela linha, as outras seguem."""
    fire = _KitFalha(ValueError("x"), produtos=_dois(env_conn))
    r = rec.recuperar("mm", env_conn, fire, agora=AGORA)
    assert (r["incerto"], "parado" in r) == (2, False)
    assert not env_conn.in_transaction


def test_transitorio_com_lock_no_meio_continua_transitorio(env_conn):
    """F4: a retomada não acontece sem a decisão."""
    fire = _KitFalha(LOCK(), produtos=_dois(env_conn, transitorio=True))
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["parado"] == "FdbError"
    assert _estado(env_conn, "c1") == ("transitorio", 3992, None)
    assert _estado(env_conn, "c2") == ("em_andamento", 3993, None)


@pytest.mark.parametrize("transitorio", [False, True])
def test_erro_no_livro_desfaz_a_linha_e_segue(env_conn, monkeypatch, erros, transitorio):
    """D3: exceção numa porta do livro depois da retomada: rollback e a linha fica como estava."""
    _dois(env_conn, transitorio=transitorio)

    def _quebra(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "liberar_comprovado", _quebra)
    r = rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)
    assert r["liberado"] == 0 and "parado" not in r
    assert _estado(env_conn, "c1")[:2] == ("transitorio" if transitorio else "em_andamento", 3992)
    assert _estado(env_conn, "c2")[:2] == ("em_andamento", 3993)
    assert not env_conn.in_transaction
    assert any("erro no livro" in e and "OperationalError" in e for e in erros)
    assert not any("ERP indisponível" in e for e in erros)


def test_rollback_do_livro_que_falha_nao_vira_erp_indisponivel(env_conn, monkeypatch, erros):
    _dois(env_conn)

    class _ConnRollbackFalha:
        """O livro de verdade, com rollback que levanta."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, nome):
            return getattr(self._conn, nome)

        def rollback(self):
            raise sqlite3.OperationalError("disk I/O error")

    def _quebra(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "liberar_comprovado", _quebra)
    r = rec.recuperar("mm", _ConnRollbackFalha(env_conn), FireLeitura(), agora=AGORA)
    assert r["liberado"] == 0
    assert sum("erro no livro" in e for e in erros) >= 2
    assert not any("ERP indisponível" in e for e in erros)


# --- versão do motor -------------------------------------------------------------


@pytest.fixture
def motor_limpo(monkeypatch):
    monkeypatch.setattr(rec, "_motor_avisado", set())


@pytest.mark.parametrize("versao", ["2.5.9", "3.0.12"])
def test_motor_com_no_rec_version_roda(env_conn, motor_limpo, versao):
    _linha(env_conn, seq=3992)
    fire = FireLeitura(versao=versao)
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["liberado"] == 1


def test_versao_do_motor_que_nao_le_para(env_conn, motor_limpo, erros):
    class _SemVersao(FireLeitura):
        def ler(self, sql, params):
            raise LOCK()

    _linha(env_conn, seq=3992)
    parado = {"parado": "motor_sem_bloqueio"}
    assert rec.recuperar("mm", env_conn, _SemVersao(), agora=AGORA) == parado
    assert rec.recuperar("mm", env_conn, _SemVersao(), agora=AGORA) == parado
    assert _estado(env_conn)[0] == "em_andamento"
    avisos = [e for e in erros if "motor_sem_bloqueio" in e]
    assert len(avisos) == 1 and "FdbError" in avisos[0]  # uma vez por processo, com o tipo


@pytest.mark.parametrize("versao", ["5.0.1", "4.0.5", None, "WI-V", 4])
def test_motor_sem_bloqueio_ou_ilegivel_nao_roda(env_conn, motor_limpo, erros, versao):
    """ReadConsistency (4.0+) faz o read committed ignorar o NO_REC_VERSION; sem versão
    legível, nada prova o contrário."""
    _linha(env_conn, seq=3992)
    fire = FireLeitura(versao=versao)
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA) == {"parado": "motor_sem_bloqueio"}
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA) == {"parado": "motor_sem_bloqueio"}
    assert _estado(env_conn)[0] == "em_andamento" and fire.consultas == []
    assert sum("motor_sem_bloqueio" in e for e in erros) == 1  # uma vez por processo


# --- transitorio com SEQ (M3 da revisão da Task 4) ----------------------------


def _transitorio(env_conn, seq=3992):
    _linha(env_conn, seq=seq)
    repo.marcar_erro_a_enviar(env_conn, "c1", 1, codigo="x", mensagem="m", corpo_json="{}",
                              rollback_comprovado=False)
    assert repo.marcar_desfecho(env_conn, "c1", 1, "transitorio")
    env_conn.commit()


def test_transitorio_com_seq_adota_quando_o_produto_esta_la(env_conn):
    _transitorio(env_conn)
    fire = FireLeitura(produtos={3992: _no_erp()})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["adotado"] == 1
    assert _estado(env_conn) == ("gravado_sem_volta", 3992, "3992")


def test_transitorio_com_seq_libera_quando_ausente(env_conn):
    _transitorio(env_conn)
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["liberado"] == 1
    assert _estado(env_conn) == ("liberado", None, None)


def test_transitorio_com_leitura_que_falha_continua_transitorio(env_conn):
    _transitorio(env_conn)
    r = rec.recuperar("mm", env_conn, FireLeitura(falha=LOCK()), agora=AGORA)
    assert r["parado"] == "FdbError"
    assert _estado(env_conn) == ("transitorio", 3992, None)


def test_transitorio_com_porta_recusada_continua_transitorio(env_conn, monkeypatch):
    """Retomada e porta no mesmo commit: sem a porta, a retomada também não fica."""
    _transitorio(env_conn)
    monkeypatch.setattr(repo, "liberar_comprovado", lambda *a: False)
    assert rec.recuperar("mm", env_conn, FireLeitura(), agora=AGORA)["liberado"] == 0
    assert _estado(env_conn) == ("transitorio", 3992, None)


def test_transitorio_com_seq_e_composicao_diferente_fica_incerto(env_conn):
    _transitorio(env_conn)
    fire = FireLeitura(produtos={3992: _no_erp()}, kit={3992: [(3170, 1.0)]})
    assert rec.recuperar("mm", env_conn, fire, agora=AGORA)["incerto"] == 1
    assert _estado(env_conn)[0] == "incerto"


# --- no ciclo de gravação -------------------------------------------------------


class FireCiclo(FireGravavel):
    """O Fire gravável do ciclo, com as leituras da recuperação."""

    def _responder(self, sql, params):
        if sql == _SQL_VERSAO:
            return [("2.5.9",)], 1
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
    monkeypatch.setattr(cv, "_abrir_fire_recuperacao", _nao_abre)
    r = cv.processar_ambiente("mm", _client=Flow([]), _fire_conn=None, _env_conn=env_conn,
                              _relogio=Relogio())
    assert "recuperacao" not in r


def test_ciclo_com_erp_fora_adia_a_recuperacao(gravacao, env_conn, monkeypatch, erros):
    _interrompido(env_conn, 3171)

    def _fora(slug):
        raise OSError("sem rede")

    monkeypatch.setattr(cv, "_abrir_fire_recuperacao", _fora)
    flow = Flow([])
    r = cv.processar_ambiente("mm", _client=flow, _fire_conn=None, _env_conn=env_conn,
                              _relogio=Relogio())
    assert r["recuperacao"] == {"erro": "OSError"} and flow.gets  # o ciclo segue
    assert _estado(env_conn, PAR["cadastroId"])[0] == "em_andamento"
    assert any("recuperação" in e for e in erros)


def test_ciclo_recupera_pela_leitura_bloqueante_nao_pela_escrita(gravacao, env_conn, monkeypatch):
    _interrompido(env_conn, 3171)
    abertas = []

    def _escrita(slug):
        raise AssertionError("a recuperação não usa a conexão de escrita")

    def _leitura(slug):
        abertas.append(slug)
        return nullcontext(FireCiclo())

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _escrita)
    monkeypatch.setattr(cv, "_abrir_fire_recuperacao", _leitura)
    r = cv.processar_ambiente("mm", _client=Flow([]), _fire_conn=None, _env_conn=env_conn,
                              _relogio=Relogio())
    assert abertas == ["mm"] and r["recuperacao"]["liberado"] == 1


def test_recuperacao_conta_liberado_e_incerto_na_metrica(env_conn):
    from prometheus_client import REGISTRY

    def lido(resultado):
        return REGISTRY.get_sample_value(
            "portal_flowpcp_cadastro_volta_total", {"env": "mm", "resultado": resultado}
        ) or 0.0

    antes = lido("liberado"), lido("incerto")
    _linha(env_conn, "c1", seq=3992)  # SEQ inexistente no ERP: libera
    _linha(env_conn, "c2", seq=3993)
    fire = FireLeitura(produtos={3993: {"SEQ": 3993, "DESCRICAO": "OUTRA COISA", "UNIDADE": "PAR",
                                        "GRUPO": 8, "CLASSIF_FISCAL": "61159500",
                                        "CODIGO_EAN13": None, "DATA_CAD": date(2026, 10, 17)}})
    r = rec.recuperar("mm", env_conn, fire, agora=AGORA)
    assert (r["liberado"], r["incerto"]) == (1, 1)
    assert (lido("liberado"), lido("incerto")) == (antes[0] + 1, antes[1] + 1)
