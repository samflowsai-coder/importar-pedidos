"""`POST /api/imported/{id}/vincular-fire`: o admin liga um import `parsed` ao pedido do Fire.

Serve para o que a reconciliação por número não alcança (o número que o Portal leu não é o
`PEDIDO_CLIENTE` que o Fire guarda). Sem isso o envio ao Flow fica `retido` para sempre.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.persistence import context as env_context
from app.persistence import environments_repo, repo, router


def _entry(id_: str, **kw) -> dict:
    base = {
        "id": id_,
        "source_filename": f"{id_}.xlsx",
        "imported_at": "2026-08-27T09:00:00",
        "order_number": "TENNIS STATION LJ 180 181",
        "customer": "TENNIS",
        "customer_cnpj": "09.530.553/0001-20",
        "fire_codigo": None,
        "snapshot": {
            "header": {"order_number": "X", "customer_cnpj": "09.530.553/0001-20"},
            "items": [{"description": "KIT", "delivery_cnpj": "09.530.553/0001-20"}],
        },
        "status": "success",
        "error": None,
        "portal_status": "parsed",
    }
    base.update(kw)
    return base


def _pedido(codigo: int = 4692, cnpj: str | None = "09530553000120", status: str = "PEDIDO"):
    item = ItemErp(
        erp_item_codigo=1,
        cod_produto=3914,
        produto_produtor=None,
        descricao="KIT",
        qtd=Decimal(12),
        qtd_faturada=Decimal(0),
        cod_nf=None,
        preco_unitario=None,
        unid=None,
        dt_entrega_item=None,
    )
    return PedidoErp(
        erp_codigo=codigo,
        pedido_cliente="TSLJ180181",
        cliente_erp_codigo=77,
        cliente_erp_cnpj=cnpj,
        cliente_real_cnpj=None,
        status=status,
        data_pedido=date(2026, 8, 27),
        dt_entrega=None,
        itens=(item,),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "in"),
        output_dir=str(tmp_path / "out"),
        fb_path=str(tmp_path / "mm.fdb"),
    )
    from app.web import routes_vinculo_fire
    from app.web.server import app

    achado: dict = {"pedido": _pedido()}
    monkeypatch.setattr(
        routes_vinculo_fire, "_ler_pedido_do_fire", lambda slug, codigo: achado["pedido"]
    )
    c = TestClient(app)
    c.cookies.set("portal_env", env["id"])
    c.achado = achado
    c.env = env
    return c


def _no_env(c, fn, *a, **kw):
    with env_context.active_env(c.env["id"], c.env["slug"]):
        return fn(*a, **kw)


def _vincular(c, id_="imp-1", codigo=4692):
    return c.post(f"/api/imported/{id_}/vincular-fire", json={"fire_codigo": codigo})


def test_liga_o_import_ao_pedido_e_audita(client):
    _no_env(client, repo.insert_import, _entry("imp-1"))
    r = _vincular(client)
    assert r.status_code == 200, r.text
    e = _no_env(client, repo.get_import, "imp-1")
    assert (e["portal_status"], e["fire_codigo"]) == ("found_in_fire", 4692)
    assert e["fire_status_last_seen"] == "PEDIDO"
    tipos = [a["event_type"] for a in _no_env(client, repo.list_audit, "imp-1")]
    assert "vinculo_fire_manual" in tipos


def test_pedido_inexistente_no_fire_e_404(client):
    _no_env(client, repo.insert_import, _entry("imp-1"))
    client.achado["pedido"] = None
    assert _vincular(client).status_code == 404
    assert _no_env(client, repo.get_import, "imp-1")["portal_status"] == "parsed"


def test_cnpj_do_fire_que_nao_e_do_import_e_409(client):
    _no_env(client, repo.insert_import, _entry("imp-1"))
    client.achado["pedido"] = _pedido(cnpj="99999999000199")
    r = _vincular(client)
    assert r.status_code == 409
    assert _no_env(client, repo.get_import, "imp-1")["portal_status"] == "parsed"


def test_cnpj_de_entrega_tambem_vale(client):
    _no_env(client, repo.insert_import, _entry("imp-1", customer_cnpj="00.063.960/0223-31"))
    client.achado["pedido"] = _pedido(cnpj="09530553000120")
    assert _vincular(client).status_code == 200


def test_import_ja_vinculado_e_409(client):
    _no_env(
        client, repo.insert_import, _entry("imp-1", portal_status="found_in_fire", fire_codigo=4000)
    )
    assert _vincular(client).status_code == 409
    assert _no_env(client, repo.get_import, "imp-1")["fire_codigo"] == 4000


def test_import_inexistente_e_404(client):
    assert _vincular(client, "nao-existe").status_code == 404


def test_codigo_invalido_e_422(client):
    _no_env(client, repo.insert_import, _entry("imp-1"))
    r = client.post("/api/imported/imp-1/vincular-fire", json={"fire_codigo": 0})
    assert r.status_code == 422


def test_exige_admin(tmp_path: Path, monkeypatch, real_auth):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    from app.web.server import app

    r = TestClient(app).post("/api/imported/x/vincular-fire", json={"fire_codigo": 1})
    assert r.status_code in (401, 403)
