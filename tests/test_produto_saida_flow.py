"""I2 (revisão final): em `ligado` o Flow recebe o código/EAN do CLIENTE.

O `depara_apply.aplicar` troca código e EAN dos itens `ok` pelos do produto
decidido (para a planilha/insert). Até o Flow consumir `erpProdutoId` (Task 9), o
payload do Flow tem de levar o que o cliente mandou. Em `desligado` nada muda: o
Flow segue recebendo o pedido como o de-para v1 deixou (como hoje).
"""

from __future__ import annotations

import pytest

from app.erp import product_check
from app.exporters.erp_exporter import ERPExporter
from tests.test_produto_saida import (
    _check,
    _config,
    _env_ligado,
    _ok,
    _order,
    _seed,
    _sem_depara_v1,
    isolated_sqlite,  # noqa: F401 — fixture autouse
)


@pytest.fixture
def flow(monkeypatch):
    from app.web import server as server_mod

    vistos: list = []

    def _push(order, *, import_id=None, slug=None):  # noqa: ARG001
        vistos.append(order.model_copy(deep=True))
        return True

    monkeypatch.setattr(server_mod, "push_new_order", _push)
    return vistos


def _check_ref9(o, **kw):  # noqa: ARG001
    return _check(_ok(2077, "KG02PR", codigo="REF-9", ean="7890000000017"))


def test_export_xlsx_ligado_flow_recebe_codigo_e_ean_do_cliente(monkeypatch, tmp_path, flow):
    from app.web import server as server_mod

    env = _env_ligado(tmp_path)
    _config(monkeypatch, tmp_path, "xlsx")
    import_id = _seed(_order({"product_code": "REF-9", "ean": "7891234567890"}))
    monkeypatch.setattr(product_check, "check_order", _check_ref9)
    planilha: list = []
    monkeypatch.setattr(
        ERPExporter,
        "export",
        lambda self, order, out: planilha.append(order.model_copy(deep=True)) or [],
    )
    out = server_mod._export_one_xlsx(
        import_id, {"output_dir": str(tmp_path / "out"), "export_mode": "xlsx"}, request_env=env
    )
    assert out.ok, out.detail
    # A planilha leva o produto decidido...
    assert planilha[0].items[0].product_code == "KG02PR"
    assert planilha[0].items[0].ean == "7890000000017"
    # ...e o Flow, o que o cliente mandou.
    (enviado,) = flow
    assert enviado.items[0].product_code == "REF-9"
    assert enviado.items[0].ean == "7891234567890"


def test_send_to_fire_ligado_flow_recebe_codigo_e_ean_do_cliente(monkeypatch, tmp_path, flow):
    from app.exporters import firebird_exporter
    from app.exporters.firebird_exporter import FirebirdExportResult
    from app.web import server as server_mod

    env = _env_ligado(tmp_path)
    _config(monkeypatch, tmp_path, "db")
    import_id = _seed(_order({"product_code": "REF-9", "ean": "7891234567890"}))
    monkeypatch.setattr(product_check, "check_order", _check_ref9)
    insert: list = []

    def _export(self, order, *, override_client_id=None, produtos_por_item=None):  # noqa: ARG001
        insert.append(order.model_copy(deep=True))
        return FirebirdExportResult("P1", 1, fire_codigo=5001)

    monkeypatch.setattr(firebird_exporter.FirebirdExporter, "export", _export)
    out = server_mod._send_one_to_fire(
        import_id, {"output_dir": str(tmp_path / "out"), "export_mode": "db"}, request_env=env
    )
    assert out.ok, out.detail
    assert insert[0].items[0].product_code == "KG02PR"
    (enviado,) = flow
    assert enviado.items[0].product_code == "REF-9"
    assert enviado.items[0].ean == "7891234567890"


def test_export_xlsx_desligado_flow_segue_como_hoje(monkeypatch, tmp_path, flow):
    """Desligado: o Flow recebe o pedido depois do de-para v1, como hoje."""
    from app.persistence import environments_repo
    from app.web import server as server_mod

    env = _env_ligado(tmp_path)
    environments_repo.set_produto_chave_modo(env["id"], "desligado", por="teste")
    env = environments_repo.get(env["id"])
    _config(monkeypatch, tmp_path, "xlsx")
    _sem_depara_v1(monkeypatch)
    import_id = _seed(_order({"product_code": "C1"}))
    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda o, **kw: {
            **_check(_ok(2077, "KG02PR", codigo="C1")),
            "items": [{**_ok(2077, "KG02PR", codigo="C1"), "price_status": "match"}],
        },
    )
    monkeypatch.setattr(ERPExporter, "export", lambda self, order, out: [])
    out = server_mod._export_one_xlsx(
        import_id, {"output_dir": str(tmp_path / "out"), "export_mode": "xlsx"}, request_env=env
    )
    assert out.ok, out.detail
    (enviado,) = flow
    assert enviado.items[0].product_code == "777"  # o vínculo v1, como hoje
