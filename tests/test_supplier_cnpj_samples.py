"""Trava a propriedade que torna o degrau do documento seguro.

Spec, fato 14: dos 29 samples reais, 21 trazem o CNPJ de UMA das duas
empresas e NENHUM traz os dois. Confirmado com esta regex em 2026-09-09,
depois do merge de `main` (`c9d8dac`) nesta branch: 29 arquivos, 21 resolvem,
0 ambíguos, 8 sem CNPJ de fornecedor. Se um parser ou um sample novo quebrar
a propriedade central, este teste quebra antes da produção.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extractors.pdf_extractor import PDFExtractor
from app.extractors.xls_extractor import XLSExtractor
from app.ingestion.file_loader import LoadedFile
from app.routing import documento

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
NASMAR = "34513679000134"
MM = "35394871000111"
CONHECIDOS = {NASMAR: "nasmar", MM: "americanense"}


def _texto(p: Path) -> str:
    raw = p.read_bytes()
    loaded = LoadedFile(path=p, extension=p.suffix.lower(), raw=raw)
    ext = PDFExtractor() if p.suffix.lower() == ".pdf" else XLSExtractor()
    return ext.extract(loaded).get("text", "") or ""


def _arquivos() -> list[Path]:
    return sorted(
        p
        for p in SAMPLES.iterdir()
        if p.is_file() and p.suffix.lower() in (".pdf", ".xls", ".xlsx")
    )


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_nenhum_sample_traz_os_dois_cnpjs():
    """A propriedade central: o documento nunca resolve para dois ambientes."""
    ambiguos = []
    for p in _arquivos():
        achados = documento.cnpjs_no_texto(_texto(p)) & set(CONHECIDOS)
        if len(achados) > 1:
            ambiguos.append((p.name, sorted(achados)))
    assert ambiguos == [], f"sample com dois fornecedores: {ambiguos}"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_cobertura_medida_nao_regride():
    """21 dos 29 samples resolvem pelo documento. Pode subir, nao descer.

    São os números do fato 14 da spec, confirmados com esta regex em
    2026-09-09 depois do merge de `main` nesta branch: 29 arquivos, 21
    resolvem, 0 ambíguos, 8 sem CNPJ. A spec estava certa desde sempre — a
    medição anterior deste teste rodava numa branch 21 commits atrás de
    `main`, sem 3 dos 29 samples.
    """
    resolvidos = [
        p.name
        for p in _arquivos()
        if documento.detectar_fornecedor(_texto(p), CONHECIDOS) is not None
    ]
    assert len(resolvidos) >= 21, f"cobertura caiu para {len(resolvidos)}: {resolvidos}"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_samples_sem_cnpj_devolvem_none_e_nao_palpite():
    """Os 8 sem CNPJ de fornecedor caem para o degrau seguinte, nao para um default.

    Lista fechada e verificada no checkout em 2026-09-09 (pós-merge de
    `main`) — sao exatamente estes oito, nem um a mais.
    """
    sem_cnpj = [
        "Desmembramento Authentic feet (1).xlsx",
        "Desmembramento Magic Feet.xlsx",
        "PEDIDO KALLAN K01.xlsx",
        "PEDIDO NBA 3.xlsx",
        "PEDIDO BEIRA RIO.pdf",
        "Pedido Authentic Fit.xlsx",
        "Pedido Magic Feet MF048.xlsx",
        "PEDIDO TENNIS STATION.xlsx",
    ]
    for nome in sem_cnpj:
        p = SAMPLES / nome
        if not p.exists():
            continue
        assert documento.detectar_fornecedor(_texto(p), CONHECIDOS) is None, nome
