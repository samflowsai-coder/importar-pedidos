"""Guardas de string dos blocos de fiscal, materiais e volta na tela do ambiente
(app/web/static/admin-ambiente-edit.html). Etapa 1: material só simula (dryRun)."""

from __future__ import annotations

from pathlib import Path

import pytest

_HTML = (
    Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "admin-ambiente-edit.html"
).read_text(encoding="utf-8")

_INI = "// ── Cadastro: fiscal, materiais e volta (início)"
_FIM = "// ── Cadastro: fiscal, materiais e volta (fim)"


def _bloco_js() -> str:
    assert _INI in _HTML and _FIM in _HTML
    return _HTML[_HTML.index(_INI) : _HTML.index(_FIM)]


@pytest.mark.parametrize(
    "trecho",
    [
        "fiscal_portal",
        "capacidade_ok",
        "O Flow ignorou os campos fiscais (o Flow ainda não aceita). Tire a empresa de PORTAL_FLOWPCP_CATALOGO_FISCAL.",
        "com_pendencia",
        "por_motivo",
        "sync-materiais",
        "Simular carga de material",
        "materiais_portal",
        "cadastro-volta",
        "por_estado",
        "Volta do Flow para o Fire",
        "Desligada",
        "casados",
        "porId",
        "PORTAL_FLOWPCP_MATERIAIS",
    ],
)
def test_tela_tem_os_blocos_novos(trecho):
    assert trecho in _HTML, f"trecho ausente na tela de edição: {trecho!r}"


def test_material_so_simula_nesta_etapa():
    assert "sync-materiais?apply=1" not in _HTML
    assert "sync-materiais?apply=" not in _HTML
    assert "btn-aplicar-materiais" not in _HTML


def test_blocos_novos_usam_textcontent_e_nunca_innerhtml():
    js = _bloco_js()
    assert "textContent" in js
    assert "innerHTML" not in js
