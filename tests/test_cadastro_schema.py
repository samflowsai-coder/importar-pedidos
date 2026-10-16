import pytest

from app.integrations.flowpcp.cadastro_schema import codifica_win1252, validar_item

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


def test_item_bom_passa():
    item, cod, _ = validar_item(dict(BOM))
    assert cod is None and item.nome == "KIT3 MEIA CURTO PRETO"


@pytest.mark.parametrize(
    "campo,valor,codigo",
    [
        ("extra", 1, "contrato_invalido"),
        ("acao", "apagar", "contrato_invalido"),
        ("tipo", "servico", "contrato_invalido"),
        ("nome", "x" * 201, "campo_excede_fire"),
        ("cor", "x" * 201, "campo_excede_fire"),
        ("unidadeErp", "UNIDADES123", "campo_excede_fire"),
        ("ean", "1" * 16, "campo_excede_fire"),
        ("codigoNfe", "x" * 21, "campo_excede_fire"),
        ("codigoMes", "1" * 31, "campo_excede_fire"),
        ("ncm", "6115950", "contrato_invalido"),
        ("tipoItemSped", 12, "contrato_invalido"),
        ("nome", "MEIA ☃ NEVE", "caractere_invalido"),
        ("nome", "MEIA\x00X", "caractere_invalido"),
        ("nome", "", "contrato_invalido"),
    ],
)
def test_item_ruim_vira_erro_definitivo(campo, valor, codigo):
    bruto = dict(BOM)
    bruto[campo] = valor
    item, cod, msg = validar_item(bruto)
    assert item is None and cod == codigo and len(msg) <= 500


def test_acento_portugues_passa_win1252():
    assert codifica_win1252("MEIA AÇÚCAR ÇÃO") and not codifica_win1252("☃")
    bruto = dict(BOM, nome="MEIA AÇÚCAR")
    assert validar_item(bruto)[1] is None


def test_kit_sem_componentes_e_regras_de_componente():
    assert validar_item(dict(BOM, componentes=None))[1] == "componente_invalido"
    assert validar_item(dict(BOM, componentes=[]))[1] == "componente_invalido"
    assert (
        validar_item(dict(BOM, componentes=[{"erpProdutoId": "31a", "quantidade": 1}]))[1]
        == "contrato_invalido"
    )
    assert (
        validar_item(dict(BOM, componentes=[{"erpProdutoId": "3170", "quantidade": 0}]))[1]
        == "componente_invalido"
    )
    assert (
        validar_item(dict(BOM, componentes=[{"erpProdutoId": "3170", "quantidade": 100001}]))[1]
        == "componente_invalido"
    )
    muitos = [{"erpProdutoId": str(i), "quantidade": 1} for i in range(1, 52)]
    assert validar_item(dict(BOM, componentes=muitos))[1] == "componente_invalido"


def test_componente_nao_pode_ser_o_proprio_produto():
    bruto = dict(BOM, acao="atualizar", erpProdutoId="3170")
    assert validar_item(bruto)[1] == "componente_invalido"


def test_simples_com_componentes_e_invalido():
    assert validar_item(dict(BOM, tipo="simples"))[1] == "componente_invalido"
    assert validar_item(dict(BOM, tipo="material"))[1] == "componente_invalido"
    assert validar_item(dict(BOM, tipo="simples", componentes=None))[1] is None


def test_atualizar_exige_erp_id_e_criar_proibe():
    assert validar_item(dict(BOM, acao="atualizar"))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, erpProdutoId="2645"))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, acao="atualizar", erpProdutoId="2645"))[1] is None


def test_mensagem_nao_vaza_valor_inteiro():
    _, _, msg = validar_item(dict(BOM, nome="SEGREDO" * 100))
    assert "SEGREDO" * 3 not in msg


def test_mensagem_de_contrato_nao_vaza_valor_nem_chave_hostil():
    _, cod, msg = validar_item(dict(BOM, acao="SEGREDO-ACAO", **{"chave SEGREDO\n": 1}))
    assert cod == "contrato_invalido"
    assert "SEGREDO" not in msg and "\n" not in msg


def test_entrada_que_nao_e_dict():
    assert validar_item("lixo")[1] == "contrato_invalido"
    assert validar_item(None)[1] == "contrato_invalido"
