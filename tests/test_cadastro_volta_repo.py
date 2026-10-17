"""Livro da volta (cadastro_volta) v2: lease, gravação, reenvio e 9b."""

import sqlite3

import pytest

from app.persistence import cadastro_volta_repo as repo
from app.persistence import router, schema_env


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(schema_env.TABLES_SQL)
    router._apply_column_migrations(c, schema_env.COLUMN_MIGRATIONS)
    c.executescript(schema_env.INDEXES_SQL)
    yield c
    c.close()


def _reservar(conn, cid="c1", versao=1, produto="p1", acao="criar", modo="criar"):
    return repo.reservar(
        conn, cadastro_id=cid, versao=versao, produto_id=produto, acao=acao,
        tipo="simples", modo=modo, plano_json="{}",
    )


def _simulado(conn, cid="v1", versao=1):
    repo.registrar(conn, cadastro_id=cid, versao=versao, estado="simulado", acao="criar",
                   tipo="simples", erro_codigo=None, erro_mensagem=None, plano_json="{}")


def test_reservar_uma_vez(conn):
    assert _reservar(conn) is True
    assert _reservar(conn) is False


def test_reservar_retoma_linha_simulada(conn):
    _simulado(conn, "c1")
    assert _reservar(conn) is True
    assert repo.buscar(conn, "c1", 1)["estado"] == "em_andamento"


def test_reservar_retoma_transitorio_mas_nao_gravado(conn):
    _reservar(conn)
    repo.marcar_desfecho(conn, "c1", 1, "transitorio")
    assert _reservar(conn) is True
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert _reservar(conn) is False


def test_pendencias_respeitam_proxima_tentativa(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json='{"versao":1}')
    assert [p["cadastro_id"] for p in repo.pendencias_de_envio(conn, "2026-10-17T12:00:00")] == ["c1"]
    repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00")
    assert repo.pendencias_de_envio(conn, "2026-10-17T12:30:00") == []
    assert repo.buscar(conn, "c1", 1)["tentativas"] == 1
    assert repo.tem_pendencia(conn) is True


def test_adiar_grava_resposta_flow(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00", resposta="HTTP 503")
    assert repo.buscar(conn, "c1", 1)["resposta_flow"] == "HTTP 503"


def test_criar_do_produto_acha_gravado_de_outra_versao(conn):
    _reservar(conn, cid="v1", versao=1)
    repo.marcar_gravado(conn, "v1", 1, erp_produto_id="3992", corpo_json="{}")
    linha = repo.criar_do_produto(conn, "p1")
    assert (linha["cadastro_id"], linha["erp_produto_id"]) == ("v1", "3992")


def test_criar_do_produto_ignora_erro_e_simulado(conn):
    _simulado(conn)
    assert repo.criar_do_produto(conn, "p1") is None


def test_criar_do_produto_acha_incerto(conn):
    _reservar(conn)
    repo.marcar_desfecho(conn, "c1", 1, "incerto")
    assert repo.criar_do_produto(conn, "p1")["estado"] == "incerto"


def test_criar_do_produto_acha_liberado_com_seq(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    repo.marcar_desfecho(conn, "c1", 1, "liberado")
    assert repo.criar_do_produto(conn, "p1")["estado"] == "liberado"


def test_criar_do_produto_ignora_liberado_sem_seq(conn):
    _reservar(conn)
    repo.marcar_desfecho(conn, "c1", 1, "liberado")
    assert repo.criar_do_produto(conn, "p1") is None


def test_criar_do_produto_prefere_gravado_a_confirmado_mais_novo(conn):
    _reservar(conn, cid="a", versao=1)
    repo.marcar_gravado(conn, "a", 1, erp_produto_id="1", corpo_json="{}")
    _reservar(conn, cid="b", versao=2)
    repo.marcar_gravado(conn, "b", 2, erp_produto_id="2", corpo_json="{}")
    repo.marcar_desfecho(conn, "b", 2, "confirmado")
    assert repo.criar_do_produto(conn, "p1")["cadastro_id"] == "a"


def test_seq_criado_pelo_portal(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.seq_criado_pelo_portal(conn, "3992") is True
    assert repo.seq_criado_pelo_portal(conn, "3993") is False


def test_gravado_sem_volta_nao_vira_transitorio(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    for estado in ("transitorio", "liberado", "erro_registrado", "incerto", "em_andamento"):
        assert repo.marcar_desfecho(conn, "c1", 1, estado) is False
        assert repo.buscar(conn, "c1", 1)["estado"] == "gravado_sem_volta"


def test_gravado_sem_volta_vira_confirmado(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.marcar_desfecho(conn, "c1", 1, "confirmado", resposta="aplicado") is True
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["estado"], linha["resposta_flow"]) == ("confirmado", "aplicado")


def test_gravado_sem_volta_vira_orfao(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.marcar_desfecho(conn, "c1", 1, "orfao") is True
    assert repo.buscar(conn, "c1", 1)["estado"] == "orfao"


def test_erro_a_enviar_nao_sobrescreve_gravado(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}")
    assert repo.buscar(conn, "c1", 1)["estado"] == "gravado_sem_volta"


def test_erro_a_enviar_em_linha_em_andamento(conn):
    _reservar(conn)
    repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}")
    assert repo.buscar(conn, "c1", 1)["estado"] == "erro_a_enviar"


def test_seqs_em_aberto(conn):
    _reservar(conn, cid="a", produto="pa")
    repo.marcar_seq(conn, "a", 1, 100)  # em_andamento, SEQ reservado
    _reservar(conn, cid="b", produto="pb")
    repo.marcar_gravado(conn, "b", 1, erp_produto_id="200", corpo_json="{}")
    _reservar(conn, cid="c", produto="pc")
    repo.marcar_seq(conn, "c", 1, 300)
    repo.marcar_desfecho(conn, "c", 1, "incerto")
    _reservar(conn, cid="d", produto="pd")
    repo.marcar_seq(conn, "d", 1, 400)
    repo.marcar_desfecho(conn, "d", 1, "liberado")
    _reservar(conn, cid="e", produto="pe")
    repo.marcar_desfecho(conn, "e", 1, "liberado")  # sem SEQ: fora
    _reservar(conn, cid="f", produto="pf")
    repo.marcar_seq(conn, "f", 1, 500)
    repo.marcar_desfecho(conn, "f", 1, "transitorio")  # fora
    assert repo.seqs_em_aberto(conn) == {"100", "200", "300", "400"}
