"""Integration tests for the 5 new parsers and 2 EAN fixes."""

from __future__ import annotations

from pathlib import Path

import pytest

SAMPLES = Path(__file__).parent.parent / "samples"


def _load(filename: str):
    from app.ingestion.file_loader import LoadedFile

    p = SAMPLES / filename
    if not p.exists():
        pytest.skip(f"Sample not found: {filename}")
    return LoadedFile(path=p, extension=p.suffix.lower(), raw=p.read_bytes())


def _process(filename: str):
    from app.pipeline import process

    return process(_load(filename))


# ── FIX: Centauro EAN ────────────────────────────────────────────────────────


def test_centauro_ean_extracted():
    order = _process("PEDIDO CENTAURO.pdf")
    assert order is not None
    assert order.items[0].ean == "7909607654377"


def test_centauro_correct_fields():
    order = _process("PEDIDO CENTAURO.pdf")
    assert order.header.order_number == "29852927"
    assert order.header.customer_cnpj == "06.347.409/0296-51"
    assert order.header.customer_name == "Sbf Comercio Produtos Esportivos"
    assert order.items[0].quantity == 4545.0
    assert order.items[0].unit_price == 11.37


def test_centauro_uses_variant_code_not_model_code():
    """CODIGO_PRODUTO tem que ser o código de 'Dados Variante' (986388012210), que
    carrega cor/tamanho e é o que o Fire casa — não o de 'Dados Modelo' (986388),
    que é só a casca: o mesmo modelo sai com variantes diferentes por pedido."""
    order = _process("PEDIDO CENTAURO.pdf")
    item = order.items[0]
    assert item.product_code == "986388012210"
    assert item.product_code != "986388"
    # a variante resolvida tem que ser a DONA do EAN exportado
    assert item.ean == "7909607654377"


def test_centauro_uses_invoicing_cnpj_not_billing():
    """CNPJ deve vir da seção 'Dados para Entrega / Faturamento' (filial cadastrada
    no Fire), não de 'Informações de Cobrança' (matriz SBF /0001-65)."""
    order = _process("Pedido_0036730565.pdf")
    assert order is not None
    assert order.header.order_number == "36730565"
    assert order.header.customer_cnpj == "06.347.409/0296-51"
    assert order.header.customer_cnpj != "06.347.409/0001-65"


def test_centauro_all_bold_font_pdf():
    """Pedido da SBF a partir de 07/2026: declara Helvetica-Bold mas posiciona o
    texto com as métricas da regular, então as larguras declaradas fazem os
    trechos se sobreporem e a ordenação por caractere os intercala — o parser
    deixava de reconhecer a assinatura e o pedido caía no genérico."""
    order = _process("Pedido_0039894911.pdf")
    assert order is not None
    assert order.header.order_number == "39894911"
    assert order.header.customer_cnpj == "06.347.409/0296-51"
    assert order.header.customer_cnpj != "06.347.409/0001-65"
    assert len(order.items) == 1
    item = order.items[0]
    assert item.description == "KIT 3 MEIA MP OXER CANO BAIXO ATOALHADA"
    assert item.quantity == 5000.0
    assert item.unit_price == 11.37
    assert item.ean == "7909607641964"
    assert item.product_code == "986388014917"


# ── FIX: Studio Z EAN ────────────────────────────────────────────────────────


def test_studio_z_ean_extracted():
    order = _process("PEDIDO STUDIO Z.pdf")
    assert order is not None
    assert order.items[0].ean == "7909901749663"


def test_studio_z_correct_fields():
    order = _process("PEDIDO STUDIO Z.pdf")
    assert order.header.order_number == "2600009863"
    assert order.items[0].quantity == 1500.0
    assert order.items[0].unit_price == 11.33


# ── BeiranRioParser ──────────────────────────────────────────────────────────


def test_beira_rio_item_count():
    """5 item codes × 2 size ranges + 2 extra color variants = 14 rows."""
    order = _process("PEDIDO BEIRA RIO.pdf")
    assert order is not None
    assert len(order.items) == 14


def test_beira_rio_header():
    order = _process("PEDIDO BEIRA RIO.pdf")
    assert order.header.order_number == "12909889"
    assert "88.379.771" in (order.header.customer_cnpj or "")


def test_beira_rio_first_items():
    order = _process("PEDIDO BEIRA RIO.pdf")
    first = order.items[0]
    assert first.product_code == "1000626853"
    assert first.quantity == 9000.0
    assert first.unit_price == 8.19
    assert first.obs == "33/38"
    assert first.delivery_date == "17/02/2026"


def test_beira_rio_has_two_size_ranges():
    order = _process("PEDIDO BEIRA RIO.pdf")
    codes = [i.product_code for i in order.items]
    # Each item code appears at least twice (33/38 + 39/44)
    assert codes.count("1000626853") == 2
    assert codes.count("1000626854") == 2


def test_beira_rio_multi_variant_item():
    """1000626856 has 2 color variants → 4 rows total."""
    order = _process("PEDIDO BEIRA RIO.pdf")
    assert [i.product_code for i in order.items].count("1000626856") == 4


# ── KoloshParser ─────────────────────────────────────────────────────────────


def test_kolosh_item_count():
    order = _process("PEDIDO KOLOSH.pdf")
    assert order is not None
    assert len(order.items) == 15


def test_kolosh_header():
    order = _process("PEDIDO KOLOSH.pdf")
    assert order.header.order_number == "77900C"
    assert "00.465.813" in (order.header.customer_cnpj or "")


def test_kolosh_prices():
    order = _process("PEDIDO KOLOSH.pdf")
    prices = {i.unit_price for i in order.items if i.unit_price}
    # Two distinct prices: 9.97 and 11.23 and 9.08
    assert 9.97 in prices
    assert 11.23 in prices
    assert 9.08 in prices


def test_kolosh_delivery_date():
    order = _process("PEDIDO KOLOSH.pdf")
    dates = {i.delivery_date for i in order.items if i.delivery_date}
    # Should have 17/04/2026
    assert any("17/04" in d for d in dates)


# ── KoloshParser: OC 96277C (pedido real 01/09/2026) ─────────────────────────

KOLOSH_96277C = "PEDIDO KOLOSH 96277C.pdf"


def test_kolosh_issue_date_e_a_emissao_nao_a_entrega():
    """`Emissao : 01/09/26` vira DATA_PEDIDO no Fire (mapper.py:64).

    Antes o parser gravava a `Entrega` (01/12/26) aqui: o pedido entrava no
    ERP com data de emissão três meses no futuro.
    """
    order = _process(KOLOSH_96277C)
    assert order.header.issue_date == "01/09/2026"


def test_kolosh_delivery_date_segue_sendo_a_entrega():
    order = _process(KOLOSH_96277C)
    assert {i.delivery_date for i in order.items} == {"01/12/2026"}


def test_kolosh_product_code_e_a_referencia_nasmar():
    """O Fire casa por PRODUTOS.CODPROD_ALTERN, que guarda a ref da Nasmar.

    `04145.007/9` é o COD.CLI. da Dakota e não existe no catálogo da Nasmar.
    A referência real (KL403G-0003) só aparece dentro da DESCRICAO.
    """
    order = _process(KOLOSH_96277C)
    assert [i.product_code for i in order.items] == [
        "KL403G-0003",
        "KL403G-0004",
        "KL401P-0002",
        "KL401G-0002",
    ]


def test_kolosh_cod_cliente_da_dakota_vai_para_obs():
    """A OC exige o código do produto da Dakota na nota fiscal, então ele não
    pode ser descartado ao trocar o product_code."""
    order = _process(KOLOSH_96277C)
    assert [i.obs for i in order.items] == [
        "COD.CLI. 04145.007/9",
        "COD.CLI. 04145.008/8",
        "COD.CLI. 18613.002/1",
        "COD.CLI. 18613.005/8",
    ]


def test_kolosh_descricao_completa_apesar_da_quebra_de_linha():
    """O pdfplumber joga a cauda da descrição para depois dos números.

    A versão antiga parava em `(1 PTA/1` e perdia a cor e a numeração.
    """
    order = _process(KOLOSH_96277C)
    assert order.items[0].description == (
        "KIT 3 PRS MEIA CANO LONGO KOLOSH KL403G-0003 (1 PTA/1 BCA/1 CZA) NR 39/44"
    )


def test_kolosh_ultimo_item_nao_engole_o_rodape_do_pdf():
    """O bloco do último item vai até o fim do texto extraído.

    Sem cortar na linha "N itens TOTAL:", a cauda da descrição arrastaria
    TRANSPORTE / INFORMACOES ADICIONAIS para dentro da DESCRICAO do Fire.
    """
    order = _process(KOLOSH_96277C)
    assert order.items[-1].description == (
        "KIT 3 PRS MEIA SAPATILHA KOLOSH KL401G-0002 (3 PTA DET CZA) NR 39/44"
    )


def test_kolosh_96277c_quantidades_e_total():
    order = _process(KOLOSH_96277C)
    assert order.header.order_number == "96277C"
    assert sum(i.quantity for i in order.items) == 8000.0
    assert round(sum(i.total_price for i in order.items), 2) == 81240.00


def test_kolosh_sample_antigo_mantem_a_referencia_nasmar():
    """Regressão: o formato não mudou, o sample de fevereiro tem que seguir igual."""
    order = _process("PEDIDO KOLOSH.pdf")
    assert order.header.issue_date == "09/02/2026"
    assert order.items[0].product_code == "KL402P-0003"
    assert order.items[0].obs == "COD.CLI. 04032.003/6"


# ── SamsClubParser ───────────────────────────────────────────────────────────


def test_sams_club_item_count():
    order = _process("PEDIDO SAMS CLUB.pdf")
    assert order is not None
    assert len(order.items) == 18


def test_sams_club_header():
    order = _process("PEDIDO SAMS CLUB.pdf")
    assert order.header.order_number == "06654993-0000"
    assert "00.063.960" in (order.header.customer_cnpj or "")


def test_sams_club_eans_present():
    order = _process("PEDIDO SAMS CLUB.pdf")
    # All items should have EANs (EAN is the product code in Sam's Club)
    for item in order.items:
        assert item.ean is not None
        assert len(item.ean) == 13


def test_sams_club_first_item():
    order = _process("PEDIDO SAMS CLUB.pdf")
    first = order.items[0]
    assert first.ean == "7898686876711"
    assert first.quantity == 117.0
    assert first.unit_price == 26.36


def test_sams_club_delivery_date():
    order = _process("PEDIDO SAMS CLUB.pdf")
    dates = {i.delivery_date for i in order.items if i.delivery_date}
    assert any("30/01/2026" in d for d in dates)


# ── SamsClubParser: CD novo no layout consolidado ────────────────────────────

SAMS_CD_DF = "PEDIDO SAMS CLUB CD DF.pdf"


def test_sams_consolidado_captura_o_nome_do_cd():
    """`CNPJ do Local de Entrega: 00.063.960 / 0587-94 CD SAM'S DF`.

    Sem o nome, um CD novo chega no preview só como um CNPJ solto e o
    operador não tem como saber para onde o pedido vai.
    """
    order = _process(SAMS_CD_DF)
    assert {i.delivery_name for i in order.items} == {"CD SAM'S DF"}


def test_sams_consolidado_captura_o_ean_do_local_de_entrega():
    """O EAN do local de entrega é a chave inequívoca do CD e vai para a
    coluna `ean_local_entrega` do XLSX."""
    order = _process(SAMS_CD_DF)
    assert {i.delivery_ean for i in order.items} == {"7891737676667"}


def test_sams_cd_df_nao_muda_numero_nem_valores():
    order = _process(SAMS_CD_DF)
    assert order.header.order_number == "06834549-0000"
    assert {i.delivery_cnpj for i in order.items} == {"00.063.960/0587-94"}
    assert round(sum(i.total_price for i in order.items), 2) == 5983.17


# ── SamsClubParser: o cliente é o LOCAL DE ENTREGA, não o Comprador ──────────


@pytest.mark.parametrize(
    "sample,cnpj_cd,nome_cd,cnpj_comprador",
    [
        (SAMS_CD_DF, "00.063.960/0587-94", "Cd Sam'S Df", "00.063.960/0044-30"),
        (
            "PEDIDO SAMS CLUB 06839396.pdf",
            "00.063.960/0587-94",
            "Cd Sam'S Df",
            "00.063.960/0223-31",
        ),
        (
            "PEDIDO SAMS CLUB.pdf",
            "00.063.960/0069-99",
            "Centro Dist Muribeca Sams",
            "00.063.960/0048-64",
        ),
    ],
)
def test_sams_consolidado_usa_o_cnpj_do_local_de_entrega_como_cliente(
    sample, cnpj_cd, nome_cd, cnpj_comprador
):
    """A MM cadastra no Fire o CD que recebe, não o clube que compra.

    O `CNPJ:` do bloco Comprador muda a cada pedido (/0044-30, /0048-64,
    /0223-31) e nenhum existe no CADASTRO — o pedido não casava com cliente
    nenhum. Mesma regra do SBF/Centauro (CNPJ de faturamento, não a matriz).
    Confirmado pela MM em 10/09/2026.

    O nome esperado já vem mastigado pelo `.title()` do `OrderNormalizer`
    (`CD SAM'S DF` → `Cd Sam'S Df`) — débito conhecido, ver `docs/BACKLOG.md`.
    """
    order = _process(sample)
    assert order.header.customer_cnpj == cnpj_cd
    assert order.header.customer_cnpj != cnpj_comprador
    assert order.header.customer_name == nome_cd


def test_sams_grade_mantem_o_comprador_como_cliente():
    """A GRADE fica fora da regra: o Local de Entrega do cabeçalho é CD de
    trânsito e a mercadoria é cross-docked para N lojas, cada uma já com
    arquivo próprio. Sem caso real reportado, não se mexe (ver BACKLOG)."""
    order = _process(GRADE_FILE)
    assert order.header.customer_cnpj == "00.063.960/0094-08"


def test_sams_nome_do_arquivo_deixa_de_ser_sem_cliente():
    """`SEM_CLIENTE_...xlsx` era sintoma do mesmo bug: o layout consolidado não
    tem `Destinatário:`, então o cliente ficava sem nome."""
    import tempfile

    from app.exporters.erp_exporter import ERPExporter

    order = _process("PEDIDO SAMS CLUB 06839396.pdf")
    with tempfile.TemporaryDirectory() as tmp:
        (path,) = ERPExporter().export(order, tmp)
    assert "SEM_CLIENTE" not in path.name
    assert path.name.startswith("Cd_Sam_S_Df_00063960058794_Pedido_06839396-0000")


def test_sams_consolidado_antigo_tambem_ganha_nome_e_ean_do_cd():
    """Regressão no sample de janeiro: mesmo layout, mesmos campos novos."""
    order = _process("PEDIDO SAMS CLUB.pdf")
    assert {i.delivery_ean for i in order.items} == {"7891737000745"}
    assert {i.delivery_name for i in order.items} == {"CENTRO DIST MURIBECA SAMS"}


# ── SamsClubParser GRADE (Cross Docking) ─────────────────────────────────────

GRADE_FILE = "PEDIDO SAMS CLUB GRADE.pdf"
GRADE_LOJA_EANS = {"7891737001698", "7891737012779", "7891737676568"}
GRADE_LOJA_CNPJS = {
    "00.063.960/0094-08",
    "00.063.960/0570-46",
    "00.063.960/0576-31",
}


def test_sams_grade_header():
    order = _process(GRADE_FILE)
    assert order.header.order_number == "06611415-0000"
    assert order.header.customer_name is not None
    assert "M.M" in order.header.customer_name


def test_sams_grade_item_count():
    """3 lojas decompondo 19 SKUs (uma loja não recebe 1 SKU) → 56 OrderItems."""
    order = _process(GRADE_FILE)
    assert len(order.items) == 56


def test_sams_grade_delivery_ean_populated():
    order = _process(GRADE_FILE)
    eans = {i.delivery_ean for i in order.items}
    assert eans == GRADE_LOJA_EANS


def test_sams_grade_delivery_cnpj_populated():
    order = _process(GRADE_FILE)
    cnpjs = {i.delivery_cnpj for i in order.items}
    assert cnpjs == GRADE_LOJA_CNPJS


def test_sams_grade_qty_sum_matches_consolidated():
    """Soma por SKU bate com a tabela superior (ex: 7898686876711 → 16+16+27=59)."""
    order = _process(GRADE_FILE)
    sums: dict[str, float] = {}
    for it in order.items:
        sums[it.ean] = sums.get(it.ean, 0.0) + it.quantity
    assert sums["7898686876711"] == 59.0
    assert sums["7898686876728"] == 153.0
    assert sums["7898686876735"] == 234.0
    # SKU de KIT (36 peças dentro): a grade já vem em kits, igual à Qtde Pedida
    assert sums["7898686879194"] == 2.0  # Qtde Pedida da tabela superior
    assert sums["7898686879200"] == 4.0


def test_sams_grade_unit_price_lookup():
    """Preço unitário vem da tabela 'Itens do Pedido' via lookup pelo EAN do produto."""
    order = _process(GRADE_FILE)
    for it in order.items:
        if it.ean == "7898686876711":
            assert it.unit_price == 26.36
        if it.ean == "7898686879194":
            assert it.unit_price == 730.44


def test_sams_grade_kit_nao_multiplica_pelo_conteudo():
    """`Qtde na Emb.` é o conteúdo do kit, nunca multiplicador.

    O SKU 7898686879194 tem `Qtde Pedida = 2` na tabela superior e duas linhas
    de `1,00` no Cross Docking: 1 kit por loja. Multiplicar pelas 36 peças de
    dentro mandava 36 kits por loja para o Fire.
    """
    order = _process(GRADE_FILE)
    kits = [i for i in order.items if i.ean == "7898686879194"]
    assert len(kits) == 2
    assert all(i.quantity == 1.0 for i in kits)
    assert all(i.unit_price == 730.44 for i in kits)
    assert all(i.total_price == 730.44 for i in kits)


def test_sams_grade_per_store_split():
    """Cada loja recebe um arquivo XLSX próprio, identificado por SAMS_LOJA_<filial>."""
    import tempfile

    from app.exporters.erp_exporter import ERPExporter

    order = _process(GRADE_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        names = sorted(p.name for p in paths)
        assert len(paths) == 3
        assert any("SAMS_LOJA_0094_08" in n for n in names)
        assert any("SAMS_LOJA_0570_46" in n for n in names)
        assert any("SAMS_LOJA_0576_31" in n for n in names)


# ── KallanXlsParser ──────────────────────────────────────────────────────────


def test_kallan_item_count():
    order = _process("PEDIDO KALLAN K01.xlsx")
    assert order is not None
    assert len(order.items) == 9


def test_kallan_header():
    order = _process("PEDIDO KALLAN K01.xlsx")
    assert "51.540.219" in (order.header.customer_cnpj or "")
    assert order.header.order_number is not None


def test_kallan_first_item():
    order = _process("PEDIDO KALLAN K01.xlsx")
    item = order.items[0]
    assert item.product_code is not None
    assert item.quantity == 36.0
    assert item.unit_price is not None and item.unit_price > 0


def test_kallan_all_quantities_positive():
    order = _process("PEDIDO KALLAN K01.xlsx")
    for item in order.items:
        assert item.quantity is not None and item.quantity > 0


# ── DesmembramentoXlsParser ──────────────────────────────────────────────────


def test_magic_feet_splits_by_store():
    order = _process("Desmembramento Magic Feet.xlsx")
    assert order is not None
    # Magic Feet tem 8 lojas COM CNPJ. A coluna "MF" é o TOTAL/máster (soma das
    # lojas), NÃO uma loja — não pode virar pedido nem duplicar a quantidade.
    assert "MF" not in {it.delivery_name for it in order.items}
    # Sem double-count: soma == grand total real (1560), não 3120.
    assert sum(int(it.quantity) for it in order.items) == 1560

    import tempfile

    from app.exporters.erp_exporter import ERPExporter

    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        assert len(paths) == 8  # 8 lojas reais (a coluna-total "MF" não gera arquivo)


def test_authentic_feet_items():
    order = _process("Desmembramento Authentic feet (1).xlsx")
    assert order is not None
    assert len(order.items) > 0
    # Items should have product codes
    for item in order.items:
        assert item.product_code is not None


# ── NasmarTemplateParser (single-customer "Pedido") ──────────────────────────


def test_authentic_fit_basic():
    order = _process("Pedido Authentic Fit.xlsx")
    assert order is not None
    assert len(order.items) == 12
    assert order.header.customer_cnpj == "62.513.076/0001-78"
    assert order.header.customer_name and "MULTIX" in order.header.customer_name.upper()
    # Soma dos TOTAL KITS deve bater com totalizador da linha 25 (540)
    assert sum(int(it.quantity) for it in order.items) == 540


def test_authentic_fit_first_item():
    order = _process("Pedido Authentic Fit.xlsx")
    item = order.items[0]
    assert item.product_code == "AFK3S-A-100-3338"
    assert item.quantity == 50
    assert item.unit_price == 11.96
    assert item.total_price == 598
    desc = (item.description or "").upper()
    assert "SAPATILHA" in desc
    assert "BRANCO" in desc


def test_magic_feet_pedido_loja_unica():
    # Pedido single-store Magic Feet (mesmo template do Authentic Feet). BUG: caía
    # no GenericParser, que lia o REF COR (cor=100) no lugar da quantidade. Correto:
    # quantidade = TOTAL KITS (36); cliente/CNPJ/código/preço vêm do formulário.
    order = _process("Pedido Magic Feet MF048.xlsx")
    assert order is not None
    assert order.header.customer_cnpj == "25.014.621/0001-55"
    assert "CALÇADOS" in (order.header.customer_name or "").upper()
    assert len(order.items) == 9
    item = order.items[0]
    assert item.product_code == "MFK3C-B-100-1922"
    assert item.quantity == 36  # TOTAL KITS — não 100 (REF COR/cor)
    assert item.unit_price == 8.99
    assert sum(int(it.quantity) for it in order.items) == 324


def test_afeet_pulmao_blank_form_parses():
    # Pedido "Pulmão" do Grupo Afeet: mesmo template de kits (REF. / DESCRIÇÃO
    # PRODUTO / TOTAL KITS / TOTAL R$), mas com os campos de cliente EM BRANCO —
    # o conteúdo não tem nenhum texto 'AUTHENTICFEET'/'MAGICFEET' (a marca só
    # aparece no nome do arquivo). BUG: o gate por assinatura de marca falhava →
    # caía no GenericParser, que lia o REF COR (100) como quantidade. Correto: o
    # gate é o cabeçalho do template; quantidade = TOTAL KITS.
    order = _process("Pedido Grupo Afeet Pulmao.xlsx")
    assert order is not None
    assert len(order.items) == 82
    assert order.header.customer_cnpj == "34.513.679/0001-34"
    item = order.items[0]
    assert item.product_code == "AWK3S-A-100-3338"
    assert item.quantity == 1200  # TOTAL KITS — não 100 (REF COR/cor)
    assert item.unit_price == 11.96
    # single-customer → sem split por loja
    assert not any(it.delivery_cnpj or it.delivery_name for it in order.items)


def test_afeet_blank_razao_social_not_next_label():
    # RAZÃO SOCIAL em branco não pode capturar o rótulo seguinte (FANTASIA:) como
    # nome do cliente — o valor à direita é vazio, não o próximo campo.
    order = _process("Pedido Grupo Afeet Pulmao.xlsx")
    name = (order.header.customer_name or "").upper()
    assert "FANTASIA" not in name


def test_authentic_fit_does_not_match_desmembramento():
    """Não-regressão: o sample de desmembramento continua indo para
    DesmembramentoXlsParser, mesmo com NasmarTemplateParser registrado antes."""
    order = _process("Desmembramento Authentic feet (1).xlsx")
    assert order is not None
    assert any(it.delivery_cnpj or it.delivery_name for it in order.items)


def test_authentic_fit_single_output_file():
    """Pedido single-customer → exportador gera 1 arquivo (sem split)."""
    order = _process("Pedido Authentic Fit.xlsx")
    assert order is not None
    import tempfile

    from app.exporters.erp_exporter import ERPExporter

    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        assert len(paths) == 1


# ── Tennis Station: o MESMO template, com um "K" minúsculo ───────────────────
#
# `TOTAL Kits` em vez de `TOTAL KITS`. O match do cabeçalho era igualdade
# literal (`tok in cells` + `cells.index("TOTAL KITS")`), então o arquivo caía no
# GenericParser — que lê a coluna REF COR (001, 002, 003) como quantidade. Um
# pedido de 8.100 kits / R$ 120.882 entrava como 12 unidades / R$ 0, aprovado
# pelo validador, sem nenhum aviso. Terceira ocorrência da mesma classe de bug
# neste template (Magic Feet e Pulmão foram as outras duas).

_TS = "PEDIDO TENNIS STATION.xlsx"


def _rows(filename: str):
    from app.extractors.xls_extractor import XLSExtractor

    return XLSExtractor().extract(_load(filename))


def test_tennis_station_casa_o_template_apesar_do_caixa():
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser().can_parse(_rows(_TS)) is True


@pytest.mark.parametrize(
    ("cabecalho", "casa"),
    [
        (["REF.", "DESCRIÇÃO PRODUTO", "TOTAL KITS", "TOTAL R$"], True),
        (["REF.", "DESCRIÇÃO PRODUTO", "TOTAL Kits", "TOTAL R$"], True),
        (["ref.", "descrição produto", "total kits", "total r$"], True),
        # Excel guarda o que o usuário digitou: espaço duplo e sobra nas bordas.
        (["REF. ", "DESCRIÇÃO  PRODUTO", " TOTAL KITS", "TOTAL R$"], True),
        # Falta uma coluna do conjunto -> não é este template.
        (["REF.", "DESCRIÇÃO PRODUTO", "TOTAL KITS"], False),
    ],
)
def test_template_casa_cabecalho_normalizado(cabecalho, casa):
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert (NasmarTemplateParser()._match_header(cabecalho) is not None) is casa


def test_tennis_station_quantidade_e_preco_do_primeiro_item():
    order = _process(_TS)
    assert order is not None
    assert len(order.items) == 12
    item = order.items[0]
    assert item.product_code == "K3ABCCIL1FTS"
    assert item.quantity == 800  # TOTAL Kits — não 001 (REF COR/cor)
    assert item.unit_price == 12.18  # CUSTO — não 29.99 (SUGESTÃO/preço de venda)
    assert item.total_price == 9744
    assert "TENNIS STATION" in (item.description or "").upper()


def test_tennis_station_qty_x_custo_bate_item_a_item():
    """Pega troca de coluna sem depender de valor fixo: se a qty vier do REF COR
    ou o preço da SUGESTÃO, a identidade quebra."""
    order = _process(_TS)
    for item in order.items:
        assert item.quantity and item.unit_price and item.total_price
        assert item.quantity * item.unit_price == pytest.approx(item.total_price, abs=0.01)


def test_tennis_station_soma_bate_com_o_totalizador_da_planilha():
    """Linha 23 do arquivo: 8.100 kits, R$ 120.882."""
    order = _process(_TS)
    assert sum(int(i.quantity) for i in order.items) == 8100
    assert sum(i.total_price for i in order.items) == pytest.approx(120882.0, abs=0.01)


def test_tennis_station_nunca_usa_a_sugestao_como_preco():
    """SUGESTÃO é preço de venda ao consumidor. Entrar no ERP como custo infla o
    pedido em ~2,5x — e passa em qualquer validador."""
    order = _process(_TS)
    assert all(i.unit_price not in (29.99, 39.99) for i in order.items)


def test_ordem_de_compra_tem_precedencia_sobre_fantasia():
    """O template da Tennis Station acrescentou um campo `Ordem de compra:` que o
    do Authentic Feet não tem. Onde ele existir e estiver preenchido, ele é o
    número do pedido — FANTASIA é apelido que o comprador digita livre, e foi de
    onde saiu o `AF76 vs AF076` que ficou aberto na reconciliação com o Fire.

    O sample real veio com o campo em branco; este cobre o caminho preenchido.
    """
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    rows = [
        [None, None, None, None, "Ordem de compra:", "TS-4417", None],
        [None, "RAZÃO SOCIAL:", None, "LOJA TESTE LTDA", None, None, None],
        [None, "CNPJ:", None, "52.671.393/0001-69", None, None, None],
        [None, None, None, None, None, None, "FANTASIA:"],
        [None, "REF.", "DESCRIÇÃO PRODUTO", "TOTAL Kits", "TOTAL R$", None, None],
        [None, "K3ABCCIL1FTS", "KIT", 10, 121.80, None, None],
    ]
    order = NasmarTemplateParser().parse({"rows": rows, "text": "", "tables": []})
    assert order is not None
    assert order.header.order_number == "TS-4417"


def test_sample_tennis_station_recebe_numero_gerado_pelo_portal():
    """`Ordem de compra`, FANTASIA e DATA DO PEDIDO em branco: o parser não
    inventa (devolve None) e o pipeline gera `SN-` + hash do arquivo. Antes
    entrava com PEDIDO_CLIENTE = NULL no Fire, sem reconciliação."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser().parse(_rows(_TS)).header.order_number is None
    order = _process(_TS)
    assert order.header.order_number.startswith("SN-")
    assert order.header.order_number_gerado is True


def test_ordem_de_compra_nao_muda_o_numero_dos_pedidos_antigos():
    """Não-regressão: o campo novo não existe no template AF/MF, então a cadeia
    de fallback (FANTASIA com código de loja) tem que continuar valendo lá."""
    assert _process("Pedido Authentic Fit.xlsx").header.order_number == "AF198"
    # 'mf048' no arquivo; o OrderNormalizer faz .upper() depois do parser.
    assert _process("Pedido Magic Feet MF048.xlsx").header.order_number == "MF048"
    assert _process("Pedido Grupo Afeet Pulmao.xlsx").header.order_number.startswith("SN-")


# ── Número do pedido: só de campo que é número ───────────────────────────────
#
# O FANTASIA muda de sentido por cliente. Na H2S4 é o código da loja (`AF198.`,
# `mf048`) e é a convenção da própria MM no Fire — a reconciliação depende dele.
# Na NBA é o NOME da loja: o pedido 4932 (21/09/2026) entrou com PEDIDO_CLIENTE
# `NBA STORE MOGI SHOPP`, que o Fire copia para a nota fiscal. E a DATA colide
# quando a loja manda dois pedidos no mesmo dia. Nenhum dos dois é número.


def _cabecalho_template(*campos):
    """Linhas mínimas do template: os campos pedidos + cabeçalho + 1 item."""
    return [
        *[[None, rotulo, None, valor, None, None, None] for rotulo, valor in campos],
        [None, "REF.", "DESCRIÇÃO PRODUTO", "TOTAL Kits", "TOTAL R$", None, None],
        [None, "K3ABCCIL1FTS", "KIT", 10, 121.80, None, None],
    ]


def _numero(*campos):
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    rows = _cabecalho_template(*campos)
    return NasmarTemplateParser().parse({"rows": rows, "text": "", "tables": []}).header


@pytest.mark.parametrize(
    "fantasia", ["AF198.", "mf048", "AF090 - 3", "AF127 - 66", "AF-198", "AF76", "AF017-99"]
)
def test_fantasia_com_codigo_de_loja_continua_sendo_o_numero(fantasia):
    assert _numero(("FANTASIA:", fantasia)).order_number == fantasia.rstrip(".")


# Nome de loja com dígito é a mesma classe do 4932: repete em todo pedido da
# loja e vai pra nota. A forma medida na Fire é 2 letras + 2 a 4 dígitos.
@pytest.mark.parametrize(
    "fantasia",
    [
        "NBA Store Mogi Shopping",
        "TS",
        "LOJA CENTRO",
        "LOJA 10",
        "NBA 3",
        "RIO 2",
        "CD 1",
        "SHOP 12",
    ],
)
def test_fantasia_com_nome_de_loja_nao_vira_numero(fantasia):
    assert _numero(("FANTASIA:", fantasia)).order_number is None


def test_data_do_pedido_nao_vira_numero():
    header = _numero(("DATA DO PEDIDO:", "16/09/2026"))
    assert header.order_number is None
    assert header.issue_date == "16/09/2026"


def test_ordem_de_compra_sem_digito_nao_vira_numero():
    """`SEM OC`, `-`, `N/A` no campo da OC não são número de pedido."""
    header = _numero(("Ordem de compra:", "SEM OC"), ("FANTASIA:", "AF198"))
    assert header.order_number == "AF198"


def test_ordem_de_compra_so_com_zero_nao_vira_numero():
    assert _numero(("Ordem de compra:", "0")).order_number is None


# ── O template também tinha a bomba da Daju ──────────────────────────────────
#
# `_to_number` fazia `float("1.300")` -> 1.3: quantidade MIL VEZES menor entrando
# no Fire, silenciosa, aprovada pelo validador (qty > 0). Não dispara nos samples
# de hoje porque estes xlsx são nativos e o openpyxl devolve int — mas é o mesmo
# arquivo que passa por conversor quando o cliente exporta de outro sistema, e a
# Daju provou que a mesma coluna vem número hoje e texto amanhã. A regra é a do
# `DajuParser._parse_number` (copiada, não reinventada: os helpers são duplicados
# entre parsers por dívida conhecida — ver docs/BACKLOG.md).


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        # Nativos do openpyxl: o tipo já resolveu.
        (800, 800.0),
        (12.18, 12.18),
        # Texto brasileiro com decimal.
        ("12,18", 12.18),
        ("1.234,56", 1234.56),
        ("R$ 12,18", 12.18),
        # O caso que quebrava: milhar sem vírgula virava 1.3.
        ("1.300", 1300.0),
        ("1.234.567", 1234567.0),
        # E o oposto, que um fix ingênuo (str.replace(".", "")) estragaria:
        # float stringificado tem 1 dígito depois do ponto, é decimal.
        ("300.0", 300.0),
        ("800", 800.0),
        # Vazio / ausente / traço.
        ("", None),
        (None, None),
        ("—", None),
        # bool é int em Python: True não pode virar quantidade 1.
        (True, None),
    ],
)
def test_template_to_number_nao_adivinha(valor, esperado):
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser()._to_number(valor) == esperado


def test_template_quantidade_em_milhar_como_texto_nao_encolhe_o_pedido():
    """Regressão de ponta: uma linha com qty '1.300' como TEXTO tem que virar
    1300 unidades, não 1,3."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    rows = [
        [None, "REF.", "DESCRIÇÃO PRODUTO", "CUSTO", "TOTAL Kits", "TOTAL R$"],
        [None, "K3ABCCIL1FTS", "KIT CANO CURTO", "12,18", "1.300", "15.834,00"],
    ]
    order = NasmarTemplateParser().parse({"rows": rows, "text": "", "tables": []})
    item = order.items[0]
    assert item.quantity == 1300.0
    assert item.unit_price == 12.18
    assert item.quantity * item.unit_price == pytest.approx(item.total_price, abs=0.01)


# ── Achados da revisão adversarial (Fable) ───────────────────────────────────


def test_ordem_de_compra_nao_alcanca_lixo_das_colunas_remotas():
    """O template guarda a lista de validação do dropdown de filiais nas colunas
    X+ — no arquivo real da Tennis Station são 39 CNPJs na linha 6. Nada disso
    termina em ':', então o guard de rótulo do `_next_raw` não segura, e a
    varredura à direita chegaria lá.

    Se um CNPJ de filial virasse `order_number`, ele iria pro Fire como
    PEDIDO_CLIENTE — a chave de idempotência. Distância real entre rótulo e valor
    nos 4 samples do template: no máximo +2. O lixo fica a +19.
    """
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    cells = [None] * 4 + ["Ordem de compra:"] + [None] * 18 + ["52.671.393/0001-69"]
    assert NasmarTemplateParser()._next_raw(cells, 4) is None


def test_next_raw_ainda_acha_o_valor_perto_do_rotulo():
    """O limite não pode comer o caso real: valor a +1 e a +2 (o mais longe que
    aparece nos samples) continuam sendo capturados."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    p = NasmarTemplateParser()
    assert p._next_raw(["CNPJ:", "52.671.393/0001-69"], 0) == "52.671.393/0001-69"
    assert p._next_raw(["FANTASIA:", None, "AF198"], 0) == "AF198"
    assert p._next_raw(["CNPJ:", "", None, "25.014.621/0001-55"], 0) == "25.014.621/0001-55"


def test_ordem_de_compra_date_like_nao_vira_timestamp():
    """`Ordem de compra` é campo de texto livre: o comprador digita `12/08` e o
    Excel coage pra data, então o openpyxl devolve datetime. `str()` cru daria
    '2026-08-12 00:00:00' — que o `mapper.py:64` trunca em 20 chars e manda pro
    Fire como PEDIDO_CLIENTE. Espelha o `_coerce_date`."""
    import datetime as dt

    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    p = NasmarTemplateParser()
    assert p._coerce_text(dt.datetime(2026, 8, 12)) == "12/08/2026"
    assert p._coerce_text(dt.date(2026, 8, 12)) == "12/08/2026"
    # não-regressão: o resto da coerção continua igual
    assert p._coerce_text(4417.0) == "4417"
    assert p._coerce_text("TS-4417") == "TS-4417"
    assert p._coerce_text(None) is None


def test_custo_float_nativo_com_tres_casas_nao_vira_milhar():
    """O único input que distingue `_raw` de `_cell`: float NATIVO cujo `str()`
    casa com a regra de milhar. `str(16.815)` == '16.815' -> _MILHAR_BR casa ->
    16815.0, mil vezes maior. Passar o valor cru resolve.

    Sem este teste, trocar `_raw` de volta por `_cell` nos campos numéricos passa
    nos 1068 testes e reintroduz o erro de 1000x — os outros casos usam floats de
    2 casas, cujo `str()` é inofensivo.
    """
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    rows = [
        [None, "REF.", "DESCRIÇÃO PRODUTO", "CUSTO", "TOTAL Kits", "TOTAL R$"],
        [None, "K3ABCCIL1FTS", "KIT CANO CURTO", 16.815, 200, 3363.0],
    ]
    order = NasmarTemplateParser().parse({"rows": rows, "text": "", "tables": []})
    item = order.items[0]
    assert item.unit_price == 16.815
    assert item.quantity * item.unit_price == pytest.approx(item.total_price, abs=0.01)


def test_template_nao_rouba_o_desmembramento_magic_feet():
    """Não-regressão do widening: o par do `test_authentic_fit_does_not_match_
    desmembramento`, que só pinava o arquivo do Authentic Feet."""
    order = _process("Desmembramento Magic Feet.xlsx")
    assert order is not None
    assert any(it.delivery_cnpj or it.delivery_name for it in order.items)


def test_nba_item_count():
    order = _process("PEDIDO NBA 3.xlsx")
    assert order is not None
    assert len(order.items) > 0


def test_nba_has_product_codes():
    order = _process("PEDIDO NBA 3.xlsx")
    for item in order.items:
        assert item.product_code is not None


def test_nba_splits_by_store():
    """Each store column becomes a separate output file."""
    order = _process("PEDIDO NBA 3.xlsx")
    assert order is not None
    import tempfile

    from app.exporters.erp_exporter import ERPExporter

    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        assert len(paths) == 21, (
            f"Expected 21 store files, got {len(paths)}: {[p.name for p in paths]}"
        )


def test_nba_store_name_in_filename():
    """Store name appears in the output filename."""
    order = _process("PEDIDO NBA 3.xlsx")
    import tempfile

    from app.exporters.erp_exporter import ERPExporter

    with tempfile.TemporaryDirectory() as tmp:
        paths = ERPExporter().export(order, tmp)
        names = [p.name for p in paths]
        assert any("Gramado" in n for n in names), f"No Gramado file found: {names}"


def test_nba_store_as_customer_name():
    """Each file's items use the store name as NOME_CLIENTE."""
    order = _process("PEDIDO NBA 3.xlsx")
    store_names = {i.delivery_name for i in order.items if i.delivery_name}
    # Every item should have a delivery_name set
    assert len(store_names) == 21, f"Expected 21 distinct store names, got {len(store_names)}"


# ── NBA no template de kits: REF. é o MODELO, não a variante ─────────────────
#
# Pedido 4932 (Camila, 21/09/2026), NBA Store Mogi Shopping. Mesmo template do
# Authentic Feet, preenchido de outro jeito: lá `REF.` já é a variante completa
# (`AFK3S-A-100-3338`) e `REF COR` é só a cor (`100`); aqui `REF.` é o modelo
# (`NB01`), `REF COR` é modelo + cor (`NB01 - 1`) e o tamanho vem com letra
# (`M - 33-38`). O parser gravava `REF.` como código: 12 linhas com só 2 códigos
# distintos, nenhum existe no Fire, e o pedido entrou com 2 produtos. No catálogo
# da Nasmar (.4) o kit é `NB01-1M` — o mesmo código que o desmembramento NBA já
# traz pronto (`PEDIDO NBA 3.xlsx`).

_NBA_MOGI = "PEDIDO NBA MOGI SHOPPING.xlsx"


def test_nba_template_codigo_e_a_variante_do_fire():
    order = _process(_NBA_MOGI)
    assert order is not None
    # Modelo x cor (1 = branca, 3 = preta) x tamanho, na ordem da planilha.
    esperado = [f"{m}-{c}{t}" for m in ("NB01", "NB03") for c in "13" for t in ("M", "G", "GG")]
    assert [i.product_code for i in order.items] == esperado


def test_nba_template_soma_bate_com_o_totalizador_da_planilha():
    """Linha 23 do arquivo: 150 kits, R$ 5.832,71."""
    order = _process(_NBA_MOGI)
    assert sum(int(i.quantity) for i in order.items) == 150
    assert sum(i.total_price for i in order.items) == pytest.approx(5832.71, abs=0.01)


@pytest.mark.parametrize(
    ("ref", "ref_cor", "tamanhos", "esperado"),
    [
        # NBA: REF. é o modelo -> compõe REF COR + letra do tamanho.
        ("NB01", "NB01 - 1", "M - 33-38", "NB01-1M"),
        ("NB03", "NB03 - 3", "GG - 45-48", "NB03-3GG"),
        # AF / MF / TS: REF. já é a variante, REF COR é só a cor. Intocado.
        ("AFK3S-A-100-3338", "100", "33 - 38", "AFK3S-A-100-3338"),
        ("MFK3C-B-100-1922", "100", "19 - 22", "MFK3C-B-100-1922"),
        ("K3ABCCIL1FTS", "001", "33 - 38", "K3ABCCIL1FTS"),
        # Cor que COMEÇA com o REF sem ser REF + "-" + cor não é modelo.
        ("10", "100", "33 - 38", "10"),
        # Sem REF COR não há de onde compor.
        ("NB01", "", "M - 33-38", "NB01"),
        # Letra colada à numeração, com outro separador digitado à mão.
        ("NB01", "NB01 - 1", "M/33-38", "NB01-1M"),
        ("NB01", "NB01 - 1", "M 33-38", "NB01-1M"),
        # Sem letra legível: o tamanho vai junto e o código segue distinto por
        # linha (não casa no Fire, cai na vinculação manual, nunca colapsa).
        ("NB01", "NB01 - 1", "33 - 38", "NB01-1 33-38"),
        ("NB01", "NB01 - 1", "P/M - 33-38", "NB01-1 P/M-33-38"),
        ("NB01", "NB01 - 1", "ÚNICO", "NB01-1 ÚNICO"),
        ("NB01", "NB01 - 1", "", "NB01-1"),
    ],
)
def test_template_codigo_da_variante(ref, ref_cor, tamanhos, esperado):
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser()._codigo_variante(ref, ref_cor, tamanhos) == esperado


def test_tamanhos_sem_letra_nunca_dividem_o_codigo():
    """O modo de falha do 4932 com outro gatilho: TAMANHOS digitado sem a letra.
    Se as três linhas de tamanho da mesma cor saíssem com o mesmo código, um
    vínculo de-para as juntaria num produto só."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    p = NasmarTemplateParser()
    codigos = {p._codigo_variante("NB01", "NB01 - 1", t) for t in ("33-38", "39-44", "45-48")}
    assert len(codigos) == 3


# ── Kings: o template do fornecedor com código de modelo + número de cor ─────
#
# A rede Kings (~55 franquias, cada loja com CNPJ próprio, todas na MM `.7`)
# recebeu o template de kits com `REF.` = modelo (`KG 07`) e `REF COR` = número
# da cor (`001`). No Fire o kit é modelo sem espaço + sufixo de cor: `KG07BR`,
# `KG07PR`, `KG10ST` (conferido na Fire viva em 24/09/2026, SEQ 2133–2150 e
# 3945–3954; nenhum KG na Nasmar `.4`). Sem a regra, todas as cores do mesmo
# modelo saíam com `KG 07` — o colapso do NBA 4932 de novo.
#
# O modelo vem em branco (quantidades zeradas); os testes preenchem a coluna
# TOTAL Kits em memória sobre o layout real do arquivo.

_KINGS = "Planilha modelo cliente Kings.xlsx"

# CODPROD_ALTERN dos 28 kits Kings na MM (.7), na ordem das linhas da planilha.
_KINGS_FIRE = [
    "KG07BR",
    "KG07PR",
    "KG08BR",
    "KG08PR",
    *[
        f"KG{m}{c}"
        for m in ("10", "11", "01", "02", "03", "04", "05", "06")
        for c in ("BR", "PR", "ST")
    ],
]


def _kings_preenchido(qtd: int = 10):
    """Linhas reais do modelo Kings com TOTAL Kits e TOTAL R$ preenchidos."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    extracted = _rows(_KINGS)
    rows = extracted["rows"]
    header_idx, col_map = NasmarTemplateParser()._find_header_row(rows)
    for row in rows[header_idx + 1 :]:
        if row[col_map["ref"]]:
            row[col_map["total_kits"]] = qtd
            row[col_map["total_rs"]] = round(qtd * row[col_map["custo"]], 2)
    return extracted


def test_kings_modelo_em_branco_casa_o_template():
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser().can_parse(_rows(_KINGS)) is True


def test_kings_codigo_e_o_do_fire():
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    order = NasmarTemplateParser().parse(_kings_preenchido())
    assert [i.product_code for i in order.items] == _KINGS_FIRE


def test_kings_quantidade_e_custo_nunca_a_sugestao():
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    order = NasmarTemplateParser().parse(_kings_preenchido(qtd=7))
    primeiro = order.items[0]
    assert primeiro.quantity == 7
    assert primeiro.unit_price == 16.66  # CUSTO, não 39.99 (SUGESTÃO)
    assert "Branco" in primeiro.description


def test_kings_obs_de_produto_nao_vira_obs_do_pedido():
    """A coluna OBS do modelo Kings é atributo do produto ("Produto atoalhado",
    "Produto sem toalha com silicone"). O item do Fire não tem OBS: o importador
    grava a de uma linha só no OBS do PEDIDO. No 1279 (Nasmar, 25/09/2026) saiu
    "Produto atoalhado" num pedido com 4 kits de silicone, e a separação se
    confunde. A MM pediu pra tirar só da Kings."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    order = NasmarTemplateParser().parse(_kings_preenchido())
    assert [i.obs for i in order.items] == [None] * 28


def test_obs_dos_outros_clientes_do_template_continua_indo():
    """AF/MF: o "KIT 3" da coluna OBS continua no pedido — a supervisão da MM
    pediu pra manter (25/09/2026)."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    rows = [
        [None, "REF.", "REF COR", "DESCRIÇÃO PRODUTO", "OBS", "TOTAL Kits", "TOTAL R$"],
        [None, "AFK3S-A-100-3338", "100", "KIT", "KIT 3", 10, 121.80],
    ]
    order = NasmarTemplateParser().parse({"rows": rows, "text": "", "tables": []})
    assert order.items[0].obs == "KIT 3"


@pytest.mark.parametrize(
    ("ref", "ref_cor", "cor", "esperado"),
    [
        ("KG 07", "001", "Branco", "KG07BR"),
        ("KG 07", "002", "Preto", "KG07PR"),
        ("KG 10", "003", "Sortido (Branco, Mescla, Preto)", "KG10ST"),
        # Digitado sem espaço, em minúscula, cor no feminino ou plural.
        ("KG07", "002", "preta", "KG07PR"),
        ("kg 05", "003", "Sortidas", "KG05ST"),
        # Número da cor digitado como número (o Excel tira os zeros; o .xls
        # devolve float, que vira "1.0").
        ("KG 07", "1", "Branco", "KG07BR"),
        ("KG 07", "2.0", "Preto", "KG07PR"),
        # Só uma das duas fontes preenchida: ela decide.
        ("KG 07", "", "Branco", "KG07BR"),
        ("KG 07", "002", "", "KG07PR"),
    ],
)
def test_kings_codigo_da_cor(ref, ref_cor, cor, esperado):
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser()._codigo_kings(ref, ref_cor, cor) == esperado


@pytest.mark.parametrize(
    ("ref", "ref_cor", "cor"),
    [
        ("KG 07", "001", "Preto"),  # número e nome discordam: não escolhe
        ("KG 07", "2.0", "Branco"),  # idem, com o float do .xls
        # Nome editado sem número: não adivinha pelo começo do texto.
        ("KG 07", "", "Preto/Branco"),
        ("KG 07", "", "Branco, Mescla, Preto"),
        ("KG 07", "", "Brancoxyz"),
        ("KG 07", "²", "Branco, Mescla"),  # dígito não-ASCII não derruba o arquivo
        ("KG 07", "004", "Azul"),  # cor que não existe no Fire
        ("KG 07", "", ""),  # sem cor nenhuma
        ("KG07", "", ""),
    ],
)
def test_kings_sem_cor_certa_nunca_vira_prefixo_de_codigo_do_fire(ref, ref_cor, cor):
    """O importador de Excel do Fire casa por PREFIXO (BACKLOG 2.15): `KG07`
    entraria como `KG07BR` sem ninguém ver. Sem cor confiável, o código leva um
    espaço — não é prefixo de nenhum KG do Fire e cai na vinculação manual."""
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    codigo = NasmarTemplateParser()._codigo_kings(ref, ref_cor, cor)
    assert codigo is not None and " " in codigo
    assert not any(fire.startswith(codigo) for fire in _KINGS_FIRE)


@pytest.mark.parametrize(
    ("ref", "ref_cor", "cor"),
    [
        ("NB01", "NB01 - 1", "Branca"),
        ("K3ABCCIL1FTS", "001", "Branco"),
        ("AFK3S-A-100-3338", "100", "Branco"),
        ("KG1", "001", "Branco"),
        ("KG 071", "001", "Branco"),
    ],
)
def test_kings_regra_nao_toca_os_outros_clientes(ref, ref_cor, cor):
    from app.parsers.nasmar_template_parser import NasmarTemplateParser

    assert NasmarTemplateParser()._codigo_kings(ref, ref_cor, cor) is None


# ── FIX: Riachuelo ME — page-footer URL not imported as item ─────────────────


def test_riachuelo_me_no_url_items():
    """URL footer artifact from PDF page break must not appear as a product item."""
    order = _process("RIACHUELO - PEDIDO.pdf")
    assert order is not None
    for item in order.items:
        assert item.description is not None
        assert not item.description.startswith("http"), (
            f"URL artifact imported as item: {item.description!r}"
        )


def test_riachuelo_me_no_null_product_codes():
    """Every item exported by the ME parser must have a product code."""
    order = _process("RIACHUELO - PEDIDO.pdf")
    assert order is not None
    for item in order.items:
        assert item.product_code is not None, (
            f"Item with no product_code: description={item.description!r}"
        )


# ── NOVO: Daju — Ordem de Compra XLSX (PDF convertido) ───────────────────────


def test_daju_item_count():
    order = _process("Cliente NOVO OC-70610.xlsx")
    assert order is not None
    assert len(order.items) == 18


def test_daju_header():
    order = _process("Cliente NOVO OC-70610.xlsx")
    assert order.header.order_number == "OC-70610"
    assert order.header.issue_date == "11/08/2026"
    assert order.header.customer_name == "Daju Ltda"
    # CNPJ do comprador (Daju), nunca o do fornecedor (Nasmar 34.513.679/0001-34)
    assert order.header.customer_cnpj == "76.917.624/0004-82"


def test_daju_first_item_uses_ref_forn_not_client_code():
    """CODIGO_PRODUTO deve ser a Ref. Forn. (código que o Fire conhece),
    não o código interno da Daju (271626)."""
    order = _process("Cliente NOVO OC-70610.xlsx")
    item = order.items[0]
    assert item.product_code == "K3CBCCIL1G"
    assert item.product_code != "271626"
    assert item.ean == "7901045304456"
    assert item.quantity == 300.0
    assert item.unit_price == 16.12
    assert item.total_price == 4836.0


def test_daju_delivery_date_without_day_stays_none():
    """O arquivo traz 'Entrega prevista: /09/2026' (dia perdido na conversão).
    Data incompleta não pode virar delivery_date — operador ajusta no preview."""
    order = _process("Cliente NOVO OC-70610.xlsx")
    for item in order.items:
        assert item.delivery_date is None


def test_daju_total_row_not_imported():
    order = _process("Cliente NOVO OC-70610.xlsx")
    assert all(item.quantity == 300.0 for item in order.items)
    assert sum(item.total_price for item in order.items) == 76932.0


# ── Daju: os números, que é onde este repo já se queimou ─────────────────────
#
# Quantidade lida errado é o modo de falha clássico daqui: Magic Feet leu cor no
# lugar de qty, Sam's leu embalagem no lugar de unidade. Entra pedido errado no
# ERP, passa no validador (qty > 0) e ninguém percebe. A conversão PDF->xlsx que
# origina estes arquivos é instável — ela já come o dia da data de entrega —
# então a MESMA coluna pode vir como número nativo hoje e como texto amanhã.


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        # Nativos do openpyxl: o tipo já resolve, não há o que adivinhar.
        (300, 300.0),
        (16.12, 16.12),
        # Texto brasileiro com decimal.
        ("16,12", 16.12),
        ("1.234,56", 1234.56),
        ("R$ 16,12", 16.12),
        # O caso que quebrava: milhar sem vírgula virava 1.3 — pedido MIL VEZES
        # menor entrando no Fire, silencioso.
        ("1.300", 1300.0),
        ("1.234.567", 1234567.0),
        # E o oposto, que um fix ingênuo (str.replace(".", "")) estragaria:
        # float stringificado tem 1 dígito depois do ponto, é decimal.
        ("300.0", 300.0),
        ("300", 300.0),
        # Vazio / ausente.
        ("", None),
        (None, None),
        ("—", None),
    ],
)
def test_daju_parse_number_nao_adivinha(valor, esperado):
    from app.parsers.daju_parser import DajuParser

    assert DajuParser()._parse_number(valor) == esperado


def test_daju_quantidade_bate_com_o_total_de_cada_item():
    """qty × preço == total, item a item. Pega troca de coluna sem depender de
    valor fixo — se a qty vier de outra coluna, a identidade quebra."""
    order = _process("Cliente NOVO OC-70610.xlsx")
    for item in order.items:
        assert item.quantity and item.unit_price and item.total_price
        assert item.quantity * item.unit_price == pytest.approx(item.total_price, abs=0.01)


def test_daju_soma_dos_itens_bate_com_o_total_da_oc():
    order = _process("Cliente NOVO OC-70610.xlsx")
    assert sum(i.total_price for i in order.items) == pytest.approx(76932.0, abs=0.01)


def test_daju_todo_item_tem_ean_de_13_digitos():
    order = _process("Cliente NOVO OC-70610.xlsx")
    for item in order.items:
        assert item.ean is None or (item.ean.isdigit() and len(item.ean) == 13)


def test_daju_data_de_entrega_completa_e_lida():
    """O sample real vem sem o dia; este cobre o caminho em que o regex CASA."""
    from app.parsers.daju_parser import DajuParser

    texto = "Entrega prevista: 15/09/2026"
    assert DajuParser()._find(texto, r"Entrega prevista:\s*(\d{2}/\d{2}/\d{4})") == "15/09/2026"


# ── SamsClubParser: `Qtde na Emb.` é conteúdo do kit, não multiplicador ──────

SAMS_KIT = "PEDIDO SAMS CLUB 06839396.pdf"


def test_sams_kit_quantidade_vem_em_kits_nao_em_pecas():
    """Pedido real 06839396-0000 (Camila, 10/09).

    `Qtde na Emb.=22`, `Qtde Pedida=1`, `Preço Bruto=694,98`, `Valor Total
    Item=694,98`. O produto é um KIT de 22 peças e o Sam's pediu 1 — não 22.
    Multiplicando, o pedido entrava no Fire 22x maior que o real.
    """
    order = _process(SAMS_KIT)
    kit = next(i for i in order.items if i.ean == "7901045301646")
    assert kit.quantity == 1.0
    assert kit.unit_price == 694.98
    assert kit.total_price == 694.98
    assert round(kit.quantity * kit.unit_price, 2) == kit.total_price


def test_sams_item_sem_kit_nao_muda():
    """Emb.=1 → nada a multiplicar. Regressão do caso comum."""
    order = _process(SAMS_KIT)
    avulso = next(i for i in order.items if i.ean == "7898686876865")
    assert avulso.quantity == 12.0
    assert avulso.unit_price == 28.09
    assert avulso.total_price == 337.08


def test_sams_consolidado_antigo_kit_de_36():
    """Sample de janeiro, item 17: emb=36, pedida=6, bruto=730,44, total=4.382,64.

    6 kits × 730,44 fecha. Com a multiplicação dava 216 kits.
    """
    order = _process("PEDIDO SAMS CLUB.pdf")
    kit36 = next(i for i in order.items if i.ean == "7898686879194")
    assert kit36.quantity == 6.0
    assert kit36.unit_price == 730.44
    assert round(kit36.quantity * kit36.unit_price, 2) == kit36.total_price


@pytest.mark.parametrize(
    "sample,total_mercadorias",
    [
        (SAMS_KIT, 1032.06),
        ("PEDIDO SAMS CLUB.pdf", 33577.67),
        ("PEDIDO SAMS CLUB CD DF.pdf", 5983.17),
        ("PEDIDO SAMS CLUB GRADE.pdf", 40891.24),
    ],
)
def test_sams_soma_dos_itens_bate_com_o_sumario_do_pdf(sample, total_mercadorias):
    """Âncora dura: o `Valor Total Mercadorias` impresso no próprio PDF.

    Na GRADE o total de cada item NASCE do parser (`qty × preço`), então este é
    o teste que prova que a quantidade da grade está na unidade certa: com a
    multiplicação por `Qtde na Emb.` o pedido somava 1.412.766,48.

    Tolerância de 1 centavo porque o próprio PDF diverge de si mesmo: no sample
    de janeiro a soma dos 18 totais impressos dá 33.577,68 e o Sumário imprime
    33.577,67. Seguimos a linha do item, que é o que vira CORPO_VENDAS.TOTAL.
    """
    order = _process(sample)
    soma = sum(i.total_price or 0 for i in order.items)
    assert soma == pytest.approx(total_mercadorias, abs=0.01)
