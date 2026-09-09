from __future__ import annotations

from pathlib import Path

import pytest

from app.extractors.xls_extractor import XLSExtractor
from app.ingestion.file_loader import LoadedFile
from app.parsers.desmembramento_xls_parser import DesmembramentoXlsParser

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _parse(nome):
    p = SAMPLES / nome
    raw = p.read_bytes()
    extracted = XLSExtractor().extract(LoadedFile(path=p, extension=p.suffix.lower(), raw=raw))
    return DesmembramentoXlsParser().parse(extracted)


def test_deriva_por_raiz_majoritaria_e_menor_sufixo():
    p = DesmembramentoXlsParser()
    cols = [
        (10, "Loja A", "05.055.599/0029-85"),
        (11, "Loja B", "05.055.599/0008-50"),
        (12, "Loja C", "05.055.599/0026-32"),
        (13, "Outra", "10.389.941/0001-12"),
    ]
    assert p._derive_customer_cnpj(cols) == "05055599000850"


def test_sem_cnpj_nenhum_devolve_none():
    p = DesmembramentoXlsParser()
    assert p._derive_customer_cnpj([(10, "SHOPPING CENTER NORTE", None)]) is None
    assert p._derive_customer_cnpj([]) is None


def test_empate_de_raizes_nao_inventa_comprador():
    p = DesmembramentoXlsParser()
    cols = [
        (10, "A", "05.055.599/0029-85"),
        (11, "B", "10.389.941/0001-12"),
    ]
    assert p._derive_customer_cnpj(cols) is None


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_magic_feet_passa_a_ter_cnpj_de_cliente():
    order = _parse("Desmembramento Magic Feet.xlsx")
    assert order is not None
    assert order.header.customer_cnpj == "05055599000850"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_authentic_feet_e_nba_seguem_sem_cnpj_e_isso_esta_certo():
    """Os arquivos nao trazem CNPJ nenhum. Cair em 'perguntar' e o desenho,
    nao um bug — a resposta do operador vira memoria e depois historico."""
    for nome in ("Desmembramento Authentic feet (1).xlsx", "PEDIDO NBA 3.xlsx"):
        order = _parse(nome)
        assert order is not None, nome
        assert order.header.customer_cnpj is None, nome


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_itens_nao_mudam():
    """Diff minimo: a derivacao do cabecalho nao pode mexer nos itens."""
    order = _parse("Desmembramento Magic Feet.xlsx")
    assert len(order.items) > 0
    assert all(i.quantity and i.quantity > 0 for i in order.items)
