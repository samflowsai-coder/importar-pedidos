"""Guarda a leitura do relatório do catálogo na tela de edição de ambiente
(app/web/static/admin-ambiente-edit.html).

O Flow devolve impactoPool/impactoPoolTotal no TOPO da resposta, ao lado de
composicao e vinculoMes (spec do Flow §4). A tela lia de dentro de composicao e
não mostrava o impacto na simulação real da MM (16/10).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_HTML = (
    Path(__file__).resolve().parent.parent
    / "app" / "web" / "static" / "admin-ambiente-edit.html"
).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "trecho",
    [
        "const ip = rep.impactoPoolTotal ?? co.impactoPoolTotal;",
        "const ipLista = rep.impactoPool ?? co.impactoPool;",
        "co.bloqueadosFilhoInativo",
        "co.amostras.bloqueados",
        "vm.amostras.inexistenteNoMes",
        "vm.mantidoPeloGerente ?? 0",
    ],
)
def test_relatorio_do_catalogo_le_os_campos_do_flow(trecho):
    assert trecho in _HTML, f"trecho ausente na tela de edição: {trecho!r}"
