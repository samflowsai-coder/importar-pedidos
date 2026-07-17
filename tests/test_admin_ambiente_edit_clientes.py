"""Guarda os controles de 'clientes' na tela de edição de ambiente
(app/web/static/admin-ambiente-edit.html).

O trap do gate-reset (review final): se o payload do 'Salvar FlowPCP' NÃO
incluir clientes_push, o backend (default False) zera o gate a cada save. Este
teste trava que o campo está no checkbox, no load do form E no payload do PUT —
além do botão de carga e do wiring da rota sync-clientes.
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
        # checkbox do gate
        'name="flowpcp_clientes_push"',
        # load: env → checkbox
        "fv('flowpcp_clientes_push').checked = !!env.flowpcp_clientes_push;",
        # CRÍTICO: o gate entra no payload do PUT (senão o save zera o gate)
        "clientes_push: fv('flowpcp_clientes_push').checked,",
        # botões de carga + wiring da rota
        'id="btn-sync-clientes"',
        'id="btn-promover-clientes"',
        "/flowpcp/sync-clientes",
        "rodarSyncClientes(false)",
        "rodarSyncClientes(true)",
    ],
)
def test_controles_de_clientes_presentes(trecho):
    assert trecho in _HTML, f"trecho ausente na tela de edição: {trecho!r}"
