"""Notas de versão: CHANGELOG.md → manifest.json.

O que estes testes protegem: o texto que a operação lê na tela
`/admin/atualizacao` sai daqui. Pegar a seção errada significa o cliente ler as
notas de outra versão e concluir que a atualização não fez nada.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.extract_notes import notes_for, parse_sections

CHANGELOG = """# Changelog

Preâmbulo que não é seção nenhuma.

---

## Não publicado

1) Coisa nova ainda sem versão.

---

## 20260824-1530

1) Coisa publicada.

2) Outra coisa publicada.

---

## 20260725-1634

1) Coisa antiga.
"""


def test_parse_sections_ignora_preambulo_e_preserva_ordem():
    secoes = parse_sections(CHANGELOG)
    assert [t for t, _ in secoes] == ["Não publicado", "20260824-1530", "20260725-1634"]


def test_parse_sections_remove_a_regua_do_fim_do_corpo():
    _, corpo = parse_sections(CHANGELOG)[0]
    assert corpo == "1) Coisa nova ainda sem versão."
    assert "---" not in corpo


def test_versao_exata_ganha_do_topo(tmp_path: Path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG, encoding="utf-8")
    texto = notes_for(cl, "20260824-1530", tmp_path / "ausente.txt")
    assert "1) Coisa publicada." in texto
    assert "2) Outra coisa publicada." in texto
    assert "Coisa nova" not in texto


def test_versao_desconhecida_cai_na_secao_do_topo(tmp_path: Path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG, encoding="utf-8")
    texto = notes_for(cl, "99999999-9999", tmp_path / "ausente.txt")
    assert texto == "1) Coisa nova ainda sem versão."


def test_sem_changelog_usa_o_legado(tmp_path: Path):
    legacy = tmp_path / "RELEASE_NOTES.txt"
    legacy.write_text("  formato antigo  \n", encoding="utf-8")
    texto = notes_for(tmp_path / "nao_existe.md", "qualquer", legacy)
    assert texto == "formato antigo"


def test_changelog_sem_secoes_cai_no_legado(tmp_path: Path):
    """Um CHANGELOG só com preâmbulo não pode engolir o fallback."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\nsó texto, nenhuma seção.\n", encoding="utf-8")
    legacy = tmp_path / "RELEASE_NOTES.txt"
    legacy.write_text("legado vale", encoding="utf-8")
    assert notes_for(cl, "v1", legacy) == "legado vale"


def test_sem_nenhuma_fonte_devolve_vazio(tmp_path: Path):
    assert notes_for(tmp_path / "a.md", "v1", tmp_path / "b.txt") == ""


def test_secao_do_topo_vazia_nao_mascara_o_legado(tmp_path: Path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## Não publicado\n\n", encoding="utf-8")
    legacy = tmp_path / "RELEASE_NOTES.txt"
    legacy.write_text("legado vale", encoding="utf-8")
    assert notes_for(cl, "v1", legacy) == "legado vale"


def test_texto_e_truncado_para_caber_no_manifest(tmp_path: Path):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## Não publicado\n\n" + ("x" * 5000), encoding="utf-8")
    saida = subprocess.run(
        [sys.executable, "tools/extract_notes.py", str(cl), "v1", "/dev/null"],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parent.parent,
    ).stdout
    assert len(json.loads(saida)) == 4000


def test_cli_devolve_json_valido_e_exige_3_argumentos():
    raiz = Path(__file__).resolve().parent.parent
    ok = subprocess.run(
        [sys.executable, "tools/extract_notes.py", "CHANGELOG.md", "x", "RELEASE_NOTES.txt"],
        capture_output=True,
        text=True,
        check=True,
        cwd=raiz,
    )
    assert isinstance(json.loads(ok.stdout), str)

    faltando = subprocess.run(
        [sys.executable, "tools/extract_notes.py", "CHANGELOG.md"],
        capture_output=True,
        text=True,
        cwd=raiz,
    )
    assert faltando.returncode == 2


@pytest.mark.parametrize("versao", ["Não publicado", "20260725-1634"])
def test_changelog_real_do_repo_tem_as_secoes_esperadas(versao: str):
    """O CHANGELOG versionado precisa continuar legível pelo extrator."""
    raiz = Path(__file__).resolve().parent.parent
    secoes = parse_sections((raiz / "CHANGELOG.md").read_text(encoding="utf-8"))
    titulos = [t for t, _ in secoes]
    assert versao in titulos
    corpo = dict(secoes)[versao]
    assert corpo.strip(), f"seção '{versao}' está vazia"
