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
        "ultimo_ciclo",
        "Última leitura: ",
        "sem cadastros.v1",
        "nenhum cadastro",
        "Em simulação, a volta só enxerga os 50 mais antigos da fila do Flow.",
        "thread_desligada",
        "A thread de envio está desligada neste servidor (PORTAL_FLOWPCP_ENVIO=0).",
        "fp.pendencias",
        "'Por motivo: '",
        "Portão da P0",
        "Portão de inativar",
        "volta-n-orfaos",
        "volta-n-incertos",
        "volta-n-sem-volta",
        "volta-n-aguardando",
        "Órfãos: produto gravado no Fire que o Flow não vinculou. Avise a área de cadastro com o código.",
        "Incertos: cadastro interrompido sem prova do que aconteceu. Confira no Fire antes de qualquer ação.",
        "Sem volta: gravado no Fire, resultado ainda não aceito pelo Flow. O Portal reenvia sozinho.",
        "Aguardando outro modo: ",
        "Itens aprovados no Flow que este modo da volta não grava.",
        "Nenhum item pede atenção.",
        "volta-retry",
        "Não foi possível carregar a volta do Flow agora.",
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


def test_bloco_da_volta_tem_tabular_nums_foco_e_nao_cria_cor():
    css = _HTML[_HTML.index("/* Volta de cadastro:") : _HTML.index(".file-row { font-size: 13px; }")]
    assert "font-variant-numeric: tabular-nums" in css
    assert ".volta-tabela-wrap:focus-visible" in css
    assert "minmax(min(100%, 14rem), 1fr)" in css  # 320 px sem estourar
    # só tokens que a página já usa; nenhum hex novo além dos fallbacks existentes
    import re

    assert set(re.findall(r"#[0-9a-fA-F]{3,6}", css)) <= {"#ef4444", "#B45309", "#3b82f6"}
