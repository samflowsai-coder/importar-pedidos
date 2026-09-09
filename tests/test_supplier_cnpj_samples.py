"""Trava a propriedade que torna o degrau do documento seguro.

Spec, fato 14: dos 29 samples reais, 21 trazem o CNPJ de UMA das duas
empresas e NENHUM traz os dois. Neste checkout (`feat/roteamento-
intercompany-fases-0-1c`, 21 commits atrás de `main`) `samples/` tem só 26
desses arquivos — os números medidos abaixo são os do que existe aqui, não
os da spec. Se um parser ou um sample novo quebrar a propriedade central,
este teste quebra antes da produção.
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
    """19 dos 26 samples DESTE checkout resolvem pelo documento. Pode subir, nao descer.

    O fato 14 da spec fala em 21 de 29, e o brief desta task em 20 de 27 —
    ambos medidos num checkout com mais arquivos em samples/ do que este.
    Este branch (`feat/roteamento-intercompany-fases-0-1c`) está 21 commits
    atrás de `main` e não tem 3 dos arquivos de `main`: "PEDIDO KOLOSH
    96277C.pdf", "PEDIDO SAMS CLUB CD DF.pdf" e "PEDIDO TENNIS STATION.xlsx".
    Verificado manualmente contra o conteúdo desses 3 arquivos (via
    `git show main:samples/...`): a regex acha e resolve o CNPJ certo nos
    dois primeiros (Nasmar e MM, respectivamente) e devolve `None` no
    terceiro (sem CNPJ conhecido) — ou seja, isto não é um regex fraco, é
    inventário de sample ausente neste branch. Medido no que EXISTE aqui:
    26 arquivos, 19 resolvem, 0 ambíguos, 7 sem CNPJ. O piso é o número do
    repo, não o da spec nem o do brief — teste tem que rodar no que existe.
    """
    resolvidos = [
        p.name
        for p in _arquivos()
        if documento.detectar_fornecedor(_texto(p), CONHECIDOS) is not None
    ]
    assert len(resolvidos) >= 19, f"cobertura caiu para {len(resolvidos)}: {resolvidos}"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_samples_sem_cnpj_devolvem_none_e_nao_palpite():
    """Os 7 sem CNPJ de fornecedor caem para o degrau seguinte, nao para um default.

    Lista fechada e verificada no checkout em 2026-09-09 — sao exatamente estes
    sete, nem um a mais.
    """
    sem_cnpj = [
        "Desmembramento Authentic feet (1).xlsx",
        "Desmembramento Magic Feet.xlsx",
        "PEDIDO KALLAN K01.xlsx",
        "PEDIDO NBA 3.xlsx",
        "PEDIDO BEIRA RIO.pdf",
        "Pedido Authentic Fit.xlsx",
        "Pedido Magic Feet MF048.xlsx",
    ]
    for nome in sem_cnpj:
        p = SAMPLES / nome
        if not p.exists():
            continue
        assert documento.detectar_fornecedor(_texto(p), CONHECIDOS) is None, nome
