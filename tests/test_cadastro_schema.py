import pytest

from app.integrations.flowpcp.cadastro_schema import (
    chaves_desconhecidas,
    codifica_win1252,
    validar_item,
)

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


def test_item_bom_passa():
    item, cod, _ = validar_item(dict(BOM))
    assert cod is None and item.nome == "KIT3 MEIA CURTO PRETO"


@pytest.mark.parametrize(
    "campo,valor,codigo",
    [
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
    assert validar_item(dict(BOM, tipo="simples"))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, tipo="material"))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, tipo="simples", componentes=None))[1] is None


# --- componentes por ação (contrato fechado com o Flow, c0045bfa) ---------


@pytest.mark.parametrize("tipo", ["simples", "material"])
@pytest.mark.parametrize("acao,erp", [("criar", None), ("atualizar", "2645")])
def test_simples_e_material_com_lista_vazia_e_contrato_invalido(tipo, acao, erp):
    bruto = dict(BOM, tipo=tipo, acao=acao, erpProdutoId=erp, componentes=[])
    assert validar_item(bruto)[1] == "contrato_invalido"


@pytest.mark.parametrize("tipo", ["kit", "pack"])
def test_criar_kit_ou_pack_exige_componentes(tipo):
    assert validar_item(dict(BOM, tipo=tipo, componentes=None))[1] == "componente_invalido"
    assert validar_item(dict(BOM, tipo=tipo, componentes=[]))[1] == "componente_invalido"


@pytest.mark.parametrize("tipo", ["kit", "pack"])
def test_atualizar_kit_sem_componentes_nao_mexe_na_composicao(tipo):
    item, cod, _ = validar_item(
        dict(BOM, tipo=tipo, acao="atualizar", erpProdutoId="2645", componentes=None)
    )
    assert cod is None and item.componentes is None


def test_atualizar_kit_com_lista_segue_as_regras_do_criar():
    base = dict(BOM, acao="atualizar", erpProdutoId="2645")
    assert validar_item(base)[1] is None
    assert validar_item(dict(base, componentes=[]))[1] == "componente_invalido"
    zero = [{"erpProdutoId": "3170", "quantidade": 0}]
    assert validar_item(dict(base, componentes=zero))[1] == "componente_invalido"


@pytest.mark.parametrize(
    "comps", [None, [], "lixo", [{"x": 1}], [{"erpProdutoId": "3170", "quantidade": 3}]]
)
@pytest.mark.parametrize("tipo", ["simples", "kit", "material"])
def test_inativar_ignora_componentes(comps, tipo):
    bruto = dict(BOM, acao="inativar", erpProdutoId="2645", tipo=tipo, componentes=comps, marcaErp=None)
    item, cod, _ = validar_item(bruto)
    assert cod is None and item.componentes is None


@pytest.mark.parametrize("campo", ["ean", "cor", "codigoNfe", "codigoMes", "unidadeErp", "nome"])
def test_texto_vazio_e_contrato_invalido(campo):
    bruto = dict(BOM)
    bruto[campo] = ""
    item, cod, _ = validar_item(bruto)
    assert item is None and cod == "contrato_invalido"


@pytest.mark.parametrize("campo", ["ean", "cor", "codigoNfe", "codigoMes", "unidadeErp"])
def test_texto_opcional_segue_aceitando_null(campo):
    bruto = dict(BOM)
    bruto[campo] = None
    assert validar_item(bruto)[1] is None


# --- envelope da fila ------------------------------------------------------


def test_envelope_tolera_item_que_nao_e_objeto_e_capacidades_ruins():
    from app.integrations.flowpcp import capacidades
    from app.integrations.flowpcp.cadastro_schema import PendentesResposta

    r = PendentesResposta.model_validate({"cadastros": [None, "x", BOM], "capacidades": None})
    assert r.cadastros == [None, "x", BOM] and capacidades.de(r) == frozenset()
    r2 = PendentesResposta.model_validate({"cadastros": [], "capacidades": [1, "cadastros.v1"]})
    assert capacidades.de(r2) == frozenset({"cadastros.v1"})


def test_envelope_com_cadastros_que_nao_e_lista_levanta():
    from pydantic import ValidationError

    from app.integrations.flowpcp.cadastro_schema import PendentesResposta

    with pytest.raises(ValidationError):
        PendentesResposta.model_validate({"cadastros": "x"})


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


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("versao", True),
        ("versao", "1"),
        ("tipoItemSped", True),
        ("nome", b"K"),
        ("nome", "   "),
        ("nome", " KIT"),
        ("nome", "KIT "),
        ("aprovadoEm", "2026-10-20T12:00:00"),
        ("aprovadoEm", 1760961600),
        ("cadastroId", 12345),
        ("cadastroId", "nao-e-uuid"),
        ("erpProdutoId", "0003170"),
        ("grupoErp", {"codigo": "0008"}),
        ("grupoErp", {"codigo": "9999999999"}),
        ("componentes", [{"erpProdutoId": "0003170", "quantidade": 1}]),
        ("componentes", [{"erpProdutoId": "9999999999", "quantidade": 1}]),
        ("componentes", [{"erpProdutoId": "3170", "quantidade": True}]),
    ],
)
def test_coercao_e_formas_frouxas_sao_recusadas(campo, valor):
    bruto = dict(BOM)
    bruto[campo] = valor
    if campo == "erpProdutoId":
        bruto["acao"] = "atualizar"
    item, cod, msg = validar_item(bruto)
    assert item is None and cod == "contrato_invalido" and len(msg) <= 500


def test_estrito_aceita_formas_corretas():
    assert (
        validar_item(dict(BOM, componentes=[{"erpProdutoId": "3170", "quantidade": 2.5}]))[1]
        is None
    )
    assert validar_item(dict(BOM, aprovadoEm="2026-10-20T09:00:00-03:00"))[1] is None
    assert validar_item(dict(BOM, acao="atualizar", erpProdutoId="999999999"))[1] is None


@pytest.mark.parametrize(
    "campo,valor",
    [
        ("versao", 10**30),
        ("versao", 2**31),
        ("erpProdutoId", "1٢٣"),
        ("erpProdutoId", "１２３"),
        ("grupoErp", {"codigo": "1٢٣"}),
        ("grupoErp", {"codigo": "１２３"}),
        ("componentes", [{"erpProdutoId": "1٢٣", "quantidade": 1}]),
        ("componentes", [{"erpProdutoId": "３１７０", "quantidade": 1}]),
        ("ncm", "٦١١٥٩٥٠٠"),
        ("ncm", "６１１５９５００"),
    ],
)
def test_digitos_unicode_e_versao_enorme_recusados(campo, valor):
    bruto = dict(BOM)
    bruto[campo] = valor
    if campo == "erpProdutoId":
        bruto["acao"] = "atualizar"
    item, cod, _ = validar_item(bruto)
    assert item is None and cod == "contrato_invalido"


# --- produtoId e marcaErp (cadastros.v1 rev. 3) ----------------------------

PRODUTO_ID = "77777777-7777-4777-8777-777777777771"


def _item(**mudancas):
    base = {
        "cadastroId": "22222222-2222-4222-8222-222222222222",
        "produtoId": PRODUTO_ID,
        "versao": 1,
        "acao": "criar",
        "erpProdutoId": None,
        "tipo": "simples",
        "nome": "MEIA SOQUETE",
        "unidadeErp": "PAR",
        "ean": None,
        "ncm": "61159500",
        "tipoItemSped": 3,
        "grupoErp": {"codigo": "8"},
        "codigoNfe": None,
        "cor": None,
        "componentes": None,
        "marcaErp": {"codigo": 17},
        "codigoMes": None,
        "aprovadoEm": "2026-10-17T13:00:00.123Z",
    }
    base.update(mudancas)
    return base


def test_produto_id_obrigatorio():
    bruto = _item()
    del bruto["produtoId"]
    item, codigo, _ = validar_item(bruto)
    assert item is None and codigo == "contrato_invalido"


def test_produto_id_vira_uuid():
    item, codigo, _ = validar_item(_item())
    assert codigo is None
    assert str(item.produtoId) == PRODUTO_ID


@pytest.mark.parametrize(
    "marca", [None, {"codigo": 0}, {"codigo": -1}, {"codigo": "17"}, {"codigo": True}, {}]
)
def test_criar_exige_marca_valida(marca):
    item, codigo, _ = validar_item(_item(marcaErp=marca))
    assert item is None and codigo == "contrato_invalido"


def test_atualizar_aceita_marca_null():
    item, codigo, _ = validar_item(_item(acao="atualizar", erpProdutoId="3990", marcaErp=None))
    assert codigo is None and item.marcaErp is None


def test_atualizar_aceita_marca_preenchida():
    item, codigo, _ = validar_item(
        _item(acao="atualizar", erpProdutoId="3990", marcaErp={"codigo": 18})
    )
    assert codigo is None and item.marcaErp.codigo == 18


def test_inativar_com_marca_e_contrato_invalido():
    item, codigo, _ = validar_item(_item(acao="inativar", erpProdutoId="3990"))
    assert item is None and codigo == "contrato_invalido"


def test_marca_acima_do_inteiro_do_fire():
    item, codigo, _ = validar_item(_item(marcaErp={"codigo": 2**31}))
    assert item is None and codigo == "contrato_invalido"


# --- chave nova no topo: ignorada, e listada para o WARNING ----------------


def test_chave_extra_no_topo_e_ignorada():
    bruto = _item(novoCampo=1)
    item, codigo, _ = validar_item(bruto)
    assert codigo is None and item is not None
    assert chaves_desconhecidas(bruto) == ["novoCampo"]


def test_chaves_desconhecidas_saneia_nome_hostil():
    assert chaves_desconhecidas(_item(**{"chave SEGREDO\n": 1})) == ["?"]


def test_chaves_desconhecidas_vazia_quando_tudo_conhecido():
    assert chaves_desconhecidas(_item()) == []


def test_grupo_erp_com_chave_extra_e_contrato_invalido():
    item, codigo, _ = validar_item(_item(grupoErp={"codigo": "8", "novo": 1}))
    assert item is None and codigo == "contrato_invalido"


def test_marca_erp_com_chave_extra_e_contrato_invalido():
    item, codigo, _ = validar_item(_item(marcaErp={"codigo": 17, "novo": 1}))
    assert item is None and codigo == "contrato_invalido"
