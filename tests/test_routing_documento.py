from __future__ import annotations

from app.routing import documento

NASMAR = "34513679000134"
MM = "35394871000111"
CONHECIDOS = {NASMAR: "nasmar", MM: "americanense"}


def test_acha_cnpj_formatado():
    texto = "PEDIDO DE COMPRA\nFornecedor: NASMAR CNPJ 34.513.679/0001-34\nCliente: DAJU"
    assert documento.cnpjs_no_texto(texto) == {NASMAR}


def test_acha_cnpj_sem_formatacao():
    assert documento.cnpjs_no_texto("CNPJ:35394871000111 M.M.") == {MM}


def test_acha_cnpj_com_separador_de_espaco():
    """PDFs quebram a mascara: '35.394.871 / 0001-11' aparece no Sam's Club."""
    assert documento.cnpjs_no_texto("CNPJ 35.394.871 / 0001-11") == {MM}


def test_ignora_numero_de_14_digitos_que_nao_e_cnpj():
    """Codigo de variante da Centauro tem 12 digitos; EAN tem 13. Nem um nem
    outro pode virar CNPJ por acidente."""
    achados = documento.cnpjs_no_texto("EAN 7891234567890 COD 986388014917")
    assert MM not in achados and NASMAR not in achados


def test_detecta_fornecedor_quando_so_um_ambiente_aparece():
    texto = "Fornecedor NASMAR 34.513.679/0001-34 — pedido 4711"
    assert documento.detectar_fornecedor(texto, CONHECIDOS) == NASMAR


def test_recusa_quando_os_dois_ambientes_aparecem():
    """A regra que impede o caso Centauro: dois CNPJs = sem resposta, nunca
    'o primeiro que apareceu'."""
    texto = f"Faturar {MM} — entregar via {NASMAR}"
    assert documento.detectar_fornecedor(texto, CONHECIDOS) is None


def test_devolve_none_quando_nenhum_aparece():
    assert documento.detectar_fornecedor("PEDIDO KALLAN K01", CONHECIDOS) is None


def test_cnpj_repetido_no_mesmo_documento_ainda_resolve():
    """O CNPJ do fornecedor aparece no cabecalho e no rodape — e um so."""
    texto = f"{MM} ... corpo do pedido ... {MM}"
    assert documento.detectar_fornecedor(texto, CONHECIDOS) == MM
