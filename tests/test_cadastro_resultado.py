"""Corpo do POST /cadastros/{id}/gravado e leitura da resposta (contrato P4a §5)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.integrations.flowpcp.cadastro_resultado import (
    Desfecho,
    corpo_erro,
    corpo_gravado,
    ler_resposta,
    mensagem_nome_existe,
    mensagem_para_flow,
)


def test_gravado_em_utc_com_z_e_milissegundos():
    sp = timezone(timedelta(hours=-3))
    corpo = corpo_gravado(1, "3992", datetime(2026, 10, 17, 10, 5, tzinfo=sp))
    assert corpo == {
        "versao": 1,
        "resultado": "gravado",
        "erpProdutoId": "3992",
        "gravadoEm": "2026-10-17T13:05:00.000Z",
    }


@pytest.mark.parametrize("ruim", ["0123", "0", "1234567890", "", "12a"])
def test_gravado_recusa_codigo_fora_da_regex(ruim):
    with pytest.raises(ValueError):
        corpo_gravado(1, ruim, datetime.now(UTC))


def test_gravado_recusa_instante_sem_fuso():
    with pytest.raises(ValueError):
        corpo_gravado(1, "3992", datetime(2026, 10, 17, 13, 5))


@pytest.mark.parametrize(
    "reservado", ["conflito_codigo", "versao_superada", "cancelado_no_flow", "fora_do_contrato"]
)
def test_erro_recusa_codigo_reservado(reservado):
    with pytest.raises(ValueError):
        corpo_erro(1, "definitivo", reservado, "x")


def test_erro_recusa_mensagem_so_de_espaco():
    with pytest.raises(ValueError):
        corpo_erro(1, "definitivo", "contrato_invalido", "   ")


def test_erro_corta_mensagem_em_500():
    corpo = corpo_erro(1, "definitivo", "contrato_invalido", "a" * 900)
    assert len(corpo["erro"]["mensagem"]) == 500


def test_mensagem_nome_existe_tem_so_o_codigo_como_numero():
    msg = mensagem_nome_existe(3100)
    assert msg == "Já existe produto com esse nome no ERP (código 3100)"


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        (
            "contrato_invalido",
            "O cadastro chegou incompleto ou com um campo em formato inesperado. Confira os dados e aprove de novo.",
        ),
        (
            "campo_excede_fire",
            "Um texto do cadastro é maior do que o ERP aceita. Encurte e aprove de novo.",
        ),
        (
            "caractere_invalido",
            "O cadastro tem um caractere que o ERP não grava (por exemplo, emoji). Troque e aprove de novo.",
        ),
        ("componente_ausente", "Um item da composição não existe no ERP."),
        ("componente_bloqueado", "Um item da composição está inativo no ERP."),
        (
            "marca_ausente_erp",
            "A marca do cadastro não existe no ERP. Cadastre a marca no ERP e aprove de novo.",
        ),
        (
            "seq_fora_do_escopo",
            "O produto não está entre os produtos que o Portal pode alterar no ERP.",
        ),
        ("seq_inexistente", "O produto não existe mais no ERP."),
        ("tipo_nao_suportado", "Este tipo de cadastro ainda não é gravado no ERP pelo Portal."),
        (
            "erp_recusou",
            "Um dado do cadastro não está no formato que o ERP aceita. Fale com o suporte do Portal.",
        ),
        ("conexao_recusada", "O ERP recusou a conexão."),
        ("erp_ocupado", "O ERP estava ocupado; o Portal tenta de novo."),
        ("codigo_desconhecido", "Motivo não identificado. Fale com o suporte do Portal."),
    ],
)
def test_mensagem_para_flow_traduz_cada_codigo(codigo, esperado):
    assert mensagem_para_flow(codigo) == esperado


@pytest.mark.parametrize("codigo", ["ja_criado_no_erp", "nome_existe"])
@pytest.mark.parametrize("ruim", [None, "", "0", "1234567890", "abc", "12a"])
def test_mensagem_para_flow_exige_detalhe_de_id_erp(codigo, ruim):
    with pytest.raises(ValueError):
        mensagem_para_flow(codigo, detalhe=ruim)


def test_mensagem_para_flow_nome_existe_delega_para_mensagem_nome_existe():
    assert mensagem_para_flow("nome_existe", detalhe="3100") == mensagem_nome_existe(3100)


def test_mensagem_para_flow_aceita_detalhe_int():
    assert "código 3992" in mensagem_para_flow("ja_criado_no_erp", detalhe=3992)


def test_mensagem_para_flow_recusa_bool_como_detalhe():
    with pytest.raises(ValueError):
        mensagem_para_flow("ja_criado_no_erp", detalhe=True)


def test_mensagem_para_flow_ja_criado_usa_o_detalhe_como_codigo():
    msg = mensagem_para_flow("ja_criado_no_erp", detalhe="3992")
    assert "código 3992" in msg
    assert (
        msg
        == "Este produto já foi criado no ERP (código 3992) e não foi vinculado. Fale com a área de cadastro."
    )


@pytest.mark.parametrize(
    "codigo",
    [
        "contrato_invalido",
        "campo_excede_fire",
        "caractere_invalido",
        "componente_ausente",
        "componente_bloqueado",
        "marca_ausente_erp",
        "seq_fora_do_escopo",
        "seq_inexistente",
        "tipo_nao_suportado",
        "erp_recusou",
        "conexao_recusada",
        "erp_ocupado",
        "codigo_desconhecido",
    ],
)
def test_mensagem_para_flow_nunca_expoe_termo_interno(codigo):
    msg = mensagem_para_flow(codigo)
    for proibido in ("Fire", "SEQ", "erpProdutoId", "componentes["):
        assert proibido not in msg


@pytest.mark.parametrize(
    ("codigo", "detalhe"),
    [("ja_criado_no_erp", "3992"), ("nome_existe", "3100")],
)
def test_mensagem_para_flow_com_detalhe_nunca_expoe_termo_interno(codigo, detalhe):
    msg = mensagem_para_flow(codigo, detalhe=detalhe)
    for proibido in ("Fire", "SEQ", "erpProdutoId", "componentes["):
        assert proibido not in msg


@pytest.mark.parametrize(
    ("resultado", "http", "corpo", "esperado"),
    [
        ("gravado", 200, {"status": "gravado"}, Desfecho.CONFIRMADO),
        ("gravado", 200, {"status": "ja_registrado"}, Desfecho.CONFIRMADO),
        (
            "gravado",
            200,
            {"status": "registrado_versao_superada", "versaoAtual": 2},
            Desfecho.CONFIRMADO,
        ),
        ("erro", 200, {"status": "erro_registrado"}, Desfecho.ERRO_REGISTRADO),
        ("erro", 200, {"status": "ja_registrado"}, Desfecho.ERRO_REGISTRADO),
        ("gravado", 200, {"status": "erro_registrado"}, Desfecho.BUG_CORPO),
        (
            "erro",
            200,
            {"status": "transitorio_registrado", "tentativas": 1},
            Desfecho.TRANSITORIO_REGISTRADO,
        ),
        ("gravado", 200, {"status": "transitorio_registrado", "tentativas": 1}, Desfecho.BUG_CORPO),
        ("gravado", 409, {"error": "versao_superada", "versaoAtual": 2}, Desfecho.ORFAO),
        ("gravado", 409, {"error": "conflito_codigo"}, Desfecho.ORFAO),
        ("erro", 409, {"error": "versao_superada", "versaoAtual": 2}, Desfecho.SUPERADO),
        ("gravado", 409, {"error": "versao_divergente", "versaoAtual": 2}, Desfecho.BUG_CORPO),
        ("gravado", 404, {"error": "cadastro_nao_encontrado"}, Desfecho.BUG_CORPO),
        ("gravado", 400, {"error": "payload inválido"}, Desfecho.BUG_CORPO),
        ("gravado", 401, {"error": "unauthorized"}, Desfecho.CREDENCIAL),
        ("gravado", 429, {"error": "rate_limit_excedido"}, Desfecho.REPETIR),
        ("gravado", 503, {"error": "erro registrando o resultado"}, Desfecho.REPETIR),
        ("gravado", None, None, Desfecho.REPETIR),
        ("gravado", 200, {"status": "novidade"}, Desfecho.REPETIR),
        ("gravado", 200, None, Desfecho.REPETIR),
        ("gravado", 200, {"status": []}, Desfecho.REPETIR),
        ("erro", 200, {"status": []}, Desfecho.REPETIR),
        ("erro", 200, {"status": "gravado"}, Desfecho.BUG_CORPO),
        (
            "erro",
            200,
            {"status": "registrado_versao_superada", "versaoAtual": 2},
            Desfecho.BUG_CORPO,
        ),
        ("erro", 409, {"error": "conflito_codigo"}, Desfecho.BUG_CORPO),
        ("gravado", 403, {"error": "forbidden"}, Desfecho.BUG_CORPO),
        ("erro", 403, {"error": "forbidden"}, Desfecho.BUG_CORPO),
        ("gravado", 422, {"error": "unprocessable"}, Desfecho.BUG_CORPO),
        ("erro", 422, {"error": "unprocessable"}, Desfecho.BUG_CORPO),
        ("gravado", 500, {"error": "boom"}, Desfecho.REPETIR),
        ("erro", 429, {"error": "rate_limit_excedido"}, Desfecho.REPETIR),
        ("gravado", 409, {"error": "algo_novo"}, Desfecho.BUG_CORPO),
    ],
)
def test_ler_resposta(resultado, http, corpo, esperado):
    assert ler_resposta(resultado, http, corpo) is esperado
