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


def _forcar(conn, cid, estado, versao=1, seq=None):
    """Põe a linha num estado qualquer (cenário que a API pública não alcança)."""
    conn.execute(
        "UPDATE cadastro_volta SET estado = ?, seq_reservado = COALESCE(?, seq_reservado) "
        "WHERE cadastro_id = ? AND versao = ?",
        (estado, seq, cid, versao),
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
    repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False)
    assert repo.marcar_desfecho(conn, "c1", 1, "transitorio") is True
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
    _forcar(conn, "c1", "liberado")
    assert repo.criar_do_produto(conn, "p1")["estado"] == "liberado"


def test_criar_do_produto_ignora_liberado_sem_seq(conn):
    _reservar(conn)
    assert repo.liberar_comprovado(conn, "c1", 1) is True
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
    repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False)
    assert repo.buscar(conn, "c1", 1)["estado"] == "gravado_sem_volta"


def test_erro_a_enviar_em_linha_em_andamento(conn):
    _reservar(conn)
    repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False)
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
    _forcar(conn, "d", "liberado")
    _reservar(conn, cid="e", produto="pe")
    repo.liberar_comprovado(conn, "e", 1)  # sem SEQ: fora
    _reservar(conn, cid="f", produto="pf")
    repo.marcar_seq(conn, "f", 1, 500)
    _forcar(conn, "f", "transitorio")  # transitorio com SEQ pode ter gravado: dentro
    _reservar(conn, cid="g", produto="pg")
    repo.marcar_erro_a_enviar(
        conn, "g", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=True
    )
    repo.marcar_desfecho(conn, "g", 1, "transitorio")  # transitorio sem SEQ: fora
    assert repo.seqs_em_aberto(conn) == {"100", "200", "300", "400", "500"}


def test_reservar_nao_reaproveita_liberado_com_seq(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    _forcar(conn, "c1", "liberado")
    assert _reservar(conn) is False
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["estado"], linha["seq_reservado"]) == ("liberado", 4001)


def test_reservar_zera_rastros_ao_reaproveitar(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00", alerta="a", resposta="r") is True
    _forcar(conn, "c1", "transitorio")
    assert _reservar(conn) is True
    linha = repo.buscar(conn, "c1", 1)
    assert linha["erp_produto_id"] is None
    assert linha["tentativas"] == 0
    assert linha["proxima_tentativa_em"] is None
    assert linha["alerta"] is None
    assert linha["resposta_flow"] is None


def test_cadeia_orfao_nao_volta_a_gravar(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.marcar_desfecho(conn, "c1", 1, "orfao") is True
    assert repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False) is False
    assert repo.buscar(conn, "c1", 1)["estado"] == "orfao"
    assert repo.marcar_desfecho(conn, "c1", 1, "transitorio") is False
    assert repo.buscar(conn, "c1", 1)["estado"] == "orfao"
    assert _reservar(conn) is False


def test_confirmado_nao_tem_saida(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.marcar_desfecho(conn, "c1", 1, "confirmado")
    for estado in ("transitorio", "orfao", "erro_registrado", "incerto", "superado"):
        assert repo.marcar_desfecho(conn, "c1", 1, estado) is False
    assert repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False) is False
    assert repo.buscar(conn, "c1", 1)["estado"] == "confirmado"
    assert _reservar(conn) is False


def test_marcar_gravado_so_uma_vez(conn):
    _reservar(conn)
    assert repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json='{"a":1}') is True
    assert repo.marcar_gravado(conn, "c1", 1, erp_produto_id="9999", corpo_json='{"b":2}') is False
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["erp_produto_id"], linha["corpo_json"]) == ("3992", '{"a":1}')


def test_marcar_gravado_sem_linha(conn):
    assert repo.marcar_gravado(conn, "nada", 1, erp_produto_id="1", corpo_json="{}") is False


def test_resposta_flow_truncada_em_64(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00", resposta="x" * 200)
    assert len(repo.buscar(conn, "c1", 1)["resposta_flow"]) == 64
    repo.marcar_desfecho(conn, "c1", 1, "confirmado", resposta="y" * 200)
    assert len(repo.buscar(conn, "c1", 1)["resposta_flow"]) == 64


def test_marcar_desfecho_estado_desconhecido(conn):
    _reservar(conn)
    with pytest.raises(ValueError):
        repo.marcar_desfecho(conn, "c1", 1, "inventado")


def test_marcar_seq_so_em_andamento(conn):
    _reservar(conn)
    assert repo.marcar_seq(conn, "c1", 1, 10) is True
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.marcar_seq(conn, "c1", 1, 11) is False
    assert repo.buscar(conn, "c1", 1)["seq_reservado"] == 10


def test_criar_do_produto_desempate_andamento_vs_incerto(conn):
    _reservar(conn, cid="a", versao=1)  # em_andamento primeiro
    _reservar(conn, cid="b", versao=2)
    repo.marcar_desfecho(conn, "b", 2, "incerto")  # incerto depois
    assert repo.criar_do_produto(conn, "p1")["cadastro_id"] == "a"


def test_criar_do_produto_mais_recente_no_mesmo_estado(conn):
    _reservar(conn, cid="a", versao=1)
    repo.marcar_desfecho(conn, "a", 1, "incerto")
    _reservar(conn, cid="b", versao=2)
    repo.marcar_desfecho(conn, "b", 2, "incerto")
    assert repo.criar_do_produto(conn, "p1")["cadastro_id"] == "b"


def test_ensure_schema_migra_banco_antigo(tmp_path):
    caminho = tmp_path / "antigo.db"
    c = sqlite3.connect(caminho)
    c.execute(
        "CREATE TABLE cadastro_volta (cadastro_id TEXT NOT NULL, versao INTEGER NOT NULL, "
        "estado TEXT NOT NULL, acao TEXT, tipo TEXT, seq_reservado INTEGER, erro_codigo TEXT, "
        "erro_mensagem TEXT, plano_json TEXT, iniciado_em TEXT NOT NULL DEFAULT (datetime('now')), "
        "atualizado_em TEXT NOT NULL DEFAULT (datetime('now')), PRIMARY KEY (cadastro_id, versao))"
    )
    c.execute("INSERT INTO cadastro_volta (cadastro_id, versao, estado) VALUES ('x', 1, 'simulado')")
    c.commit()
    c.close()
    router.reset_init_cache()
    router._ensure_schema(caminho, schema_env)
    c = sqlite3.connect(caminho)
    try:
        colunas = {r[1] for r in c.execute("PRAGMA table_info(cadastro_volta)")}
        esperadas = {c for t, c, _ in schema_env.COLUMN_MIGRATIONS if t == "cadastro_volta"}
        assert "nota_operador" in esperadas and esperadas <= colunas
        indices = {r[1] for r in c.execute("PRAGMA index_list(cadastro_volta)")}
        assert "idx_cadastro_volta_produto" in indices
        assert c.execute("SELECT tentativas FROM cadastro_volta").fetchone() == (0,)
    finally:
        c.close()
        router.reset_init_cache()


@pytest.mark.parametrize("origem", sorted(repo.ESTADOS))
@pytest.mark.parametrize("destino", sorted(repo.ESTADOS))
def test_matriz_marcar_desfecho(conn, origem, destino):
    _reservar(conn)
    _forcar(conn, "c1", origem)
    if destino not in repo.TRANSICOES:
        with pytest.raises(ValueError):
            repo.marcar_desfecho(conn, "c1", 1, destino)
        esperado, mudou = origem, None
    else:
        mudou = repo.marcar_desfecho(conn, "c1", 1, destino)
        permitido = origem in repo.TRANSICOES[destino]
        assert mudou is permitido
        esperado = destino if permitido else origem
    assert repo.buscar(conn, "c1", 1)["estado"] == esperado


def test_tabela_de_transicoes_e_a_combinada():
    assert repo.TRANSICOES == {
        "confirmado": {"gravado_sem_volta"},
        "orfao": {"gravado_sem_volta"},
        "erro_registrado": {"erro_a_enviar"},
        "transitorio": {"erro_a_enviar"},
        "superado": {"erro_a_enviar"},
        "incerto": {"em_andamento"},
    }


def test_cadeia_incerto_nao_chega_a_reservar(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    repo.marcar_desfecho(conn, "c1", 1, "incerto")
    assert repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False) is False
    for destino in ("transitorio", "superado", "erro_registrado"):
        assert repo.marcar_desfecho(conn, "c1", 1, destino) is False
    assert repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00") is False
    assert _reservar(conn) is False
    assert repo.buscar(conn, "c1", 1)["estado"] == "incerto"


def test_cadeia_liberado_com_seq_nao_chega_a_reservar(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    _forcar(conn, "c1", "liberado")
    assert repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False) is False
    assert repo.marcar_desfecho(conn, "c1", 1, "transitorio") is False
    assert repo.liberar_comprovado(conn, "c1", 1) is False
    assert _reservar(conn) is False
    assert repo.buscar(conn, "c1", 1)["seq_reservado"] == 4001


def test_cadeia_orfao_fechado_nao_chega_a_reservar(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.marcar_desfecho(conn, "c1", 1, "orfao")
    assert repo.fechar_orfao(conn, "c1", 1, operador="samuel", nota="conferido") is True
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["estado"], linha["alerta"]) == ("orfao_fechado", "fechado_manual:samuel")
    assert repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=False) is False
    for destino in repo.TRANSICOES:
        assert repo.marcar_desfecho(conn, "c1", 1, destino) is False
    assert repo.fechar_orfao(conn, "c1", 1, operador="x", nota="y") is False
    assert _reservar(conn) is False
    assert repo.buscar(conn, "c1", 1)["estado"] == "orfao_fechado"


def test_fechar_orfao_so_de_orfao(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.fechar_orfao(conn, "c1", 1, operador="s", nota="n") is False


def test_liberar_comprovado_zera_seq_e_reservar_reaproveita(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    assert repo.liberar_comprovado(conn, "c1", 1) is True
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["estado"], linha["seq_reservado"]) == ("liberado", None)
    assert _reservar(conn) is True
    assert repo.buscar(conn, "c1", 1)["estado"] == "em_andamento"


def test_adiar_so_em_pendente(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.marcar_desfecho(conn, "c1", 1, "confirmado")
    assert repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00") is False
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["tentativas"], linha["proxima_tentativa_em"]) == (0, None)


def _cadeia_b(conn, rollback):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    assert repo.marcar_erro_a_enviar(
        conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}", rollback_comprovado=rollback
    ) is True
    assert repo.marcar_desfecho(conn, "c1", 1, "transitorio") is True


def test_cadeia_b_com_rollback_comprovado_chega_a_reservar(conn):
    _cadeia_b(conn, True)
    assert repo.buscar(conn, "c1", 1)["seq_reservado"] is None
    assert _reservar(conn) is True


def test_cadeia_b_sem_rollback_comprovado_nao_chega_a_reservar(conn):
    _cadeia_b(conn, False)
    assert repo.buscar(conn, "c1", 1)["seq_reservado"] == 4001
    assert _reservar(conn) is False
    assert repo.buscar(conn, "c1", 1)["estado"] == "transitorio"


def test_rollback_comprovado_e_obrigatorio_e_so_por_palavra_chave(conn):
    with pytest.raises(TypeError):
        repo.marcar_erro_a_enviar(conn, "c1", 1, codigo="X", mensagem="m", corpo_json="{}")


def test_fechar_orfao_trunca_alerta_e_nota(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.marcar_desfecho(conn, "c1", 1, "orfao")
    assert repo.fechar_orfao(conn, "c1", 1, operador="o" * 100, nota="n" * 900) is True
    linha = repo.buscar(conn, "c1", 1)
    assert len(linha["alerta"]) == 64 and linha["alerta"].startswith("fechado_manual:")
    assert len(linha["nota_operador"]) == 500
    assert linha["erro_mensagem"] is None


def test_invariante_ja_no_erp_nunca_reaproveitavel():
    assert not set(repo._JA_NO_ERP) & set(repo.ESTADOS_DESCARTAVEIS)
    for destino, origens in repo.TRANSICOES.items():
        if destino in repo.ESTADOS_DESCARTAVEIS:
            assert not set(repo._JA_NO_ERP) & set(origens)


def test_criar_do_produto_acha_transitorio_com_seq(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    _forcar(conn, "c1", "transitorio")
    assert repo.criar_do_produto(conn, "p1")["estado"] == "transitorio"


def test_criar_do_produto_ignora_transitorio_sem_seq(conn):
    _reservar(conn)
    _forcar(conn, "c1", "transitorio")
    assert repo.criar_do_produto(conn, "p1") is None


def test_linha_do_produto_com_seq_acha_por_seq_reservado_ou_codigo(conn):
    _reservar(conn, "c1")
    repo.marcar_seq(conn, "c1", 1, 4001)
    _reservar(conn, "c2", acao="atualizar")
    repo.marcar_gravado(conn, "c2", 1, erp_produto_id="3100", corpo_json="{}")
    assert repo.linha_do_produto_com_seq(conn, "p1", 4001)["cadastro_id"] == "c1"
    assert repo.linha_do_produto_com_seq(conn, "p1", 3100)["cadastro_id"] == "c2"
    assert repo.linha_do_produto_com_seq(conn, "p1", 9999) is None
    assert repo.linha_do_produto_com_seq(conn, "outro", 4001) is None


def test_criar_do_produto_acha_orfao_fechado(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    repo.marcar_desfecho(conn, "c1", 1, "orfao")
    repo.fechar_orfao(conn, "c1", 1, operador="ana", nota="x")
    assert repo.criar_do_produto(conn, "p1")["estado"] == "orfao_fechado"


# --- Task 8: transitorio com SEQ, desempate e a busca em largura das portas -----


@pytest.mark.parametrize("mais_novo", ["liberado", "transitorio"])
def test_criar_do_produto_desempate_transitorio_liberado_mais_recente_vence(conn, mais_novo):
    """`liberado` e `transitorio` com SEQ têm a mesma prioridade: vale a mais recente."""
    mais_velho = "transitorio" if mais_novo == "liberado" else "liberado"
    for cid, estado, quando in (("a", mais_velho, "2026-10-17 10:00:00"),
                                ("b", mais_novo, "2026-10-17 11:00:00")):
        _reservar(conn, cid=cid, versao=1)
        repo.marcar_seq(conn, cid, 1, 4001)
        _forcar(conn, cid, estado)
        conn.execute("UPDATE cadastro_volta SET iniciado_em = ? WHERE cadastro_id = ?", (quando, cid))
    linha = repo.criar_do_produto(conn, "p1")
    assert (linha["cadastro_id"], linha["estado"]) == ("b", mais_novo)


def test_retomar_transitorio_com_seq_volta_para_em_andamento_com_o_seq(conn):
    _reservar(conn)
    repo.marcar_seq(conn, "c1", 1, 4001)
    _forcar(conn, "c1", "transitorio")
    conn.execute("UPDATE cadastro_volta SET iniciado_em = '2026-10-17 10:00:00'")
    assert [r["cadastro_id"] for r in repo.transitorios_com_seq(conn)] == ["c1"]
    assert repo.retomar_transitorio_com_seq(conn, "c1", 1) is True
    linha = repo.buscar(conn, "c1", 1)
    assert (linha["estado"], linha["seq_reservado"]) == ("em_andamento", 4001)
    assert linha["iniciado_em"] == "2026-10-17 10:00:00"  # a idade conta da reserva
    assert repo.transitorios_com_seq(conn) == []
    assert repo.retomar_transitorio_com_seq(conn, "c1", 1) is False


@pytest.mark.parametrize("estado", sorted(repo.ESTADOS - {"transitorio"}))
def test_retomar_transitorio_so_de_transitorio_com_seq(conn, estado):
    _reservar(conn)
    _forcar(conn, "c1", "transitorio")  # sem SEQ: reaproveitável, nada a retomar
    assert repo.transitorios_com_seq(conn) == []
    assert repo.retomar_transitorio_com_seq(conn, "c1", 1) is False
    _forcar(conn, "c1", estado, seq=4001)
    assert repo.retomar_transitorio_com_seq(conn, "c1", 1) is False
    assert repo.buscar(conn, "c1", 1)["estado"] == estado


def _linha_em(estado, seq):
    c = sqlite3.connect(":memory:")
    c.executescript(schema_env.TABLES_SQL)
    router._apply_column_migrations(c, schema_env.COLUMN_MIGRATIONS)
    c.executescript(schema_env.INDEXES_SQL)
    c.execute(
        "INSERT INTO cadastro_volta (cadastro_id, versao, estado, acao, tipo, produto_id, modo, "
        "plano_json, seq_reservado) VALUES ('c1', 1, ?, 'criar', 'simples', 'p1', 'criar', '{}', ?)",
        (estado, seq),
    )
    return c


def _no(c):
    r = repo.buscar(c, "c1", 1)
    return (r["estado"], r["seq_reservado"])


def _erro_a_enviar(rb):
    return lambda c: repo.marcar_erro_a_enviar(c, "c1", 1, codigo="x", mensagem="m",
                                               corpo_json="{}", rollback_comprovado=rb)


# Toda função pública que muda o estado da linha. As PORTAS são as que afirmam,
# com prova, que nada do nosso produto está no ERP: só elas podem levar a reservar.
_OPERACOES = {
    "marcar_seq": lambda c: repo.marcar_seq(c, "c1", 1, 7),
    "marcar_gravado": lambda c: repo.marcar_gravado(c, "c1", 1, erp_produto_id="9",
                                                    corpo_json="{}"),
    "erro_a_enviar(rb=False)": _erro_a_enviar(False),
    "erro_a_enviar(rb=True)": _erro_a_enviar(True),
    "liberar_comprovado": lambda c: repo.liberar_comprovado(c, "c1", 1),
    "retomar_transitorio_com_seq": lambda c: repo.retomar_transitorio_com_seq(c, "c1", 1),
    "fechar_orfao": lambda c: repo.fechar_orfao(c, "c1", 1, operador="o", nota="n"),
    "adotar_manual": lambda c: repo.adotar_manual(c, "c1", 1, seq=4001, operador="o", corpo_json="{}"),
    "liberar_manual": lambda c: repo.liberar_manual(c, "c1", 1, operador="o"),
    "adiar": lambda c: repo.adiar(c, "c1", 1, ate="2099-01-01T00:00:00"),
    "registrar": lambda c: repo.registrar(
        c, cadastro_id="c1", versao=1, estado="transitorio", acao="criar", tipo="simples",
        erro_codigo=None, erro_mensagem=None, plano_json=None),
    **{f"desfecho({d})": (lambda d: lambda c: repo.marcar_desfecho(c, "c1", 1, d))(d)
       for d in repo.TRANSICOES},
}
_PORTAS = {"liberar_comprovado", "erro_a_enviar(rb=True)", "liberar_manual"}
# Estados de onde o produto pode estar (ou já está) no ERP. `em_andamento` sem SEQ
# fica de fora desta busca, mas pode ter gravado: com WAL e `synchronous=NORMAL`, uma
# queda de energia pode perder o commit de `marcar_seq` e manter o INSERT no Fire.
# Esse caso é da recuperação, pela busca por nome (`cadastro_recuperacao._por_nome`).
_PODE_TER_GRAVADO = [
    ("gravado_sem_volta", None), ("confirmado", None), ("orfao", None), ("orfao_fechado", None),
    ("incerto", 4001), ("incerto", None), ("liberado", 4001), ("transitorio", 4001),
    ("em_andamento", 4001), ("erro_a_enviar", 4001), ("erro_registrado", 4001),
    ("superado", 4001), ("gravado_sem_volta", 4001),
]


@pytest.mark.parametrize(("estado", "seq"), _PODE_TER_GRAVADO)
def test_sem_as_portas_nada_que_pode_ter_gravado_volta_a_reservar(estado, seq):
    """Busca em largura sobre as funções públicas, sem as portas declaradas."""
    vistos, fila = {(estado, seq)}, [(estado, seq)]
    while fila:
        no = fila.pop(0)
        c = _linha_em(*no)
        assert not repo.reservar(c, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                                 tipo="simples", modo="criar", plano_json="{}"), no
        for nome, op in _OPERACOES.items():
            if nome in _PORTAS:
                continue
            c = _linha_em(*no)
            if op(c) and _no(c) not in vistos:
                vistos.add(_no(c))
                fila.append(_no(c))


def test_listar_por_estado_so_colunas_da_tela(conn):
    _reservar(conn, "c1")
    _reservar(conn, "c2", produto="p2")
    _forcar(conn, "c1", "orfao", seq=3992)
    _forcar(conn, "c2", "incerto")
    conn.execute("UPDATE cadastro_volta SET corpo_json = 'SEGREDO', nota_operador = 'NOTA'")
    (linha,) = repo.listar_por_estado(conn, "orfao")
    assert set(linha) == {"cadastro_id", "versao", "acao", "erp_produto_id", "seq_reservado",
                          "tentativas", "alerta", "atualizado_em"}
    assert linha["cadastro_id"] == "c1" and linha["seq_reservado"] == 3992
    assert [x["cadastro_id"] for x in repo.listar_por_estado(conn, "incerto")] == ["c2"]
    assert repo.listar_por_estado(conn, "confirmado") == []


def test_listar_por_estado_limite_e_so_com_seq(conn):
    for i in range(3):
        _reservar(conn, f"c{i}", produto=f"p{i}")
        _forcar(conn, f"c{i}", "transitorio", seq=100 + i if i else None)
    assert len(repo.listar_por_estado(conn, "transitorio", limite=2)) == 2
    assert {x["cadastro_id"] for x in repo.listar_por_estado(conn, "transitorio", so_com_seq=True)} == {"c1", "c2"}


def test_contar_e_recentes_de_transitorios_com_seq(conn):
    for i, ini in enumerate(["2026-10-17 12:00:00", "2026-10-17 12:50:00", "2026-10-17 12:55:00"]):
        _reservar(conn, f"c{i}", produto=f"p{i}")
        _forcar(conn, f"c{i}", "transitorio", seq=100 + i if i != 2 else None)
        conn.execute("UPDATE cadastro_volta SET iniciado_em = ? WHERE cadastro_id = ?", (ini, f"c{i}"))
    assert repo.contar_transitorios_com_seq(conn) == 2
    assert repo.transitorios_com_seq_recentes(conn, "2026-10-17 12:30:00") == {("c1", 1)}


# Estados em que o produto já está no ERP ou a linha já terminou: as portas manuais
# não podem tocar neles. `liberar_manual` entra na busca como operação comum.
_FORA_DAS_PORTAS_MANUAIS = [
    ("gravado_sem_volta", 4001), ("confirmado", None), ("orfao", None), ("orfao_fechado", None),
    ("em_andamento", 4001), ("em_andamento", None), ("erro_a_enviar", 4001), ("erro_registrado", 4001),
    ("simulado", None), ("superado", None), ("erro_definitivo", None),
    ("liberado", None), ("transitorio", None),
]


@pytest.mark.parametrize(("estado", "seq"), _FORA_DAS_PORTAS_MANUAIS)
def test_portas_manuais_recusam_e_nao_mudam_nada(estado, seq):
    for op in (
        lambda c: repo.adotar_manual(c, "c1", 1, seq=4001, operador="o", corpo_json="{}"),
        lambda c: repo.liberar_manual(c, "c1", 1, operador="o"),
    ):
        c = _linha_em(estado, seq)
        antes = repo.buscar(c, "c1", 1)
        assert op(c) is False
        assert repo.buscar(c, "c1", 1) == antes


@pytest.mark.parametrize(("estado", "seq"), _FORA_DAS_PORTAS_MANUAIS[:4])
def test_com_as_portas_manuais_como_operacao_comum_nada_no_erp_volta_a_reservar(estado, seq):
    """`liberar_manual` solto na busca: não pode abrir caminho a partir de quem já gravou.
    (De `em_andamento`/`erro_a_enviar` com SEQ ele É a porta: lá a prova é do operador.)"""
    ops = {k: v for k, v in _OPERACOES.items() if k not in
           {"liberar_comprovado", "erro_a_enviar(rb=True)"}}
    vistos, fila = {(estado, seq)}, [(estado, seq)]
    while fila:
        no = fila.pop(0)
        c = _linha_em(*no)
        assert not repo.reservar(c, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                                 tipo="simples", modo="criar", plano_json="{}"), no
        for op in ops.values():
            c = _linha_em(*no)
            if op(c) and _no(c) not in vistos:
                vistos.add(_no(c))
                fila.append(_no(c))


@pytest.mark.parametrize(("estado", "seq"), [("incerto", None), ("incerto", 4001),
                                             ("liberado", 4001), ("transitorio", 4001)])
def test_adotar_manual_grava_seq_corpo_e_alerta(estado, seq):
    c = _linha_em(estado, seq)
    assert repo.adotar_manual(c, "c1", 1, seq=4002, operador="ana", corpo_json='{"k": 1}') is True
    r = repo.buscar(c, "c1", 1)
    assert r["estado"] == "gravado_sem_volta"
    assert r["erp_produto_id"] == "4002" and r["corpo_json"] == '{"k": 1}'
    assert r["alerta"] == "adotado_manual:ana"
    assert r["seq_reservado"] == seq  # o SEQ reservado fica como prova


def test_adotar_manual_trunca_alerta_em_64():
    c = _linha_em("incerto", None)
    assert repo.adotar_manual(c, "c1", 1, seq=1, operador="x" * 200, corpo_json="{}")
    assert len(repo.buscar(c, "c1", 1)["alerta"]) == 64


@pytest.mark.parametrize(("estado", "seq"), [("incerto", None), ("incerto", 4001),
                                             ("liberado", 4001), ("transitorio", 4001)])
def test_liberar_manual_zera_seq_e_fica_reservavel(estado, seq):
    c = _linha_em(estado, seq)
    assert repo.liberar_manual(c, "c1", 1, operador="ana") is True
    r = repo.buscar(c, "c1", 1)
    assert (r["estado"], r["seq_reservado"], r["alerta"]) == ("liberado", None, "liberado_manual:ana")
    assert repo.reservar(c, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                         tipo="simples", modo="criar", plano_json="{}") is True


def test_liberar_manual_trunca_alerta_em_64():
    c = _linha_em("incerto", None)
    assert repo.liberar_manual(c, "c1", 1, operador="x" * 200)
    assert len(repo.buscar(c, "c1", 1)["alerta"]) == 64
