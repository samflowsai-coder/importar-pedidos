"""POST /api/imported/{id}/reenviar-flow-por-loja: reenfileira um import já enviado ao Flow COMBINADO.

O piloto da Riachuelo sai pela fila do Portal (que tem o token). A rota monta um recebimento por loja
(mesmas regras do envio novo), mas só enfileira a loja cujo pedido no Fire ainda está aberto.
Dry-run por padrão.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import context as env_context
from app.persistence import environments_repo, outbox_repo, repo, router
from app.web import routes_reenvio_loja as rrl

A = "33.200.056/0313-70"
B = "33.200.056/0343-96"
C = "33.200.056/0360-97"
A14, B14, C14 = "33200056031370", "33200056034396", "33200056036097"


def _snapshot(cnpj_header=None):
    return {
        "header": {
            "order_number": "6702663705",
            "issue_date": "28/09/2026",
            "customer_name": "LOJAS RIACHUELO SA",
            "customer_cnpj": cnpj_header,
        },
        "items": [
            {
                "description": "MEIA 0",
                "product_code": "P0",
                "quantity": 7.0,
                "unit_price": 3.0,
                "delivery_cnpj": A,
            },
            {
                "description": "MEIA 1",
                "product_code": "P1",
                "quantity": 12.0,
                "unit_price": 3.0,
                "delivery_cnpj": B,
            },
            {
                "description": "MEIA 2",
                "product_code": "P2",
                "quantity": 12.0,
                "unit_price": 3.0,
                "delivery_cnpj": B,
            },
            {
                "description": "MEIA 3",
                "product_code": "P3",
                "quantity": 12.0,
                "unit_price": 3.0,
                "delivery_cnpj": C,
            },
        ],
    }


def _check(seqs):
    return {
        "items": [
            {"match": s is not None, "fire_product_id": s, "product_code": f"P{i}"}
            for i, s in enumerate(seqs)
        ]
    }


def _linha(codigo, cnpj, status="PEDIDO", data=date(2026, 9, 28)):
    # (PEDIDO_CLIENTE, V.CODIGO, V.STATUS, V.DATA_PEDIDO, C.CODIGO, C.CPF_CNPJ)
    return ("6702663705", codigo, status, data, 77, cnpj)


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
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
    from app.web.server import app

    fire = {
        "linhas": [_linha(4968, A14), _linha(4970, B14), _linha(4972, C14)],
    }
    monkeypatch.setattr(rrl, "_ler_pedidos_do_fire", lambda slug, numero: fire["linhas"])
    c = TestClient(app)
    c.cookies.set("portal_env", env["id"])
    c.env, c.fire = env, fire
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": "imp-1",
                "source_filename": "r.xlsx",
                "imported_at": "2026-09-28T09:00:00",
                "order_number": "6702663705",
                "customer": "LOJAS RIACHUELO SA",
                "customer_cnpj": None,
                "fire_codigo": 4968,
                "snapshot": _snapshot(),
                "check": _check([3914, 3915, 3916, None]),
                "status": "success",
                "error": None,
                "portal_status": "found_in_fire",
            }
        )
    return c


def _no_env(c, fn, *a, **kw):
    with env_context.active_env(c.env["id"], c.env["slug"]):
        return fn(*a, **kw)


def _post(c, id_="imp-1", aplicar=False):
    return c.post(
        f"/api/imported/{id_}/reenviar-flow-por-loja", params={"aplicar": str(aplicar).lower()}
    )


# ── plano (puro) ──────────────────────────────────────────────────────────────


def test_planejar_loja_aberta_vai_faturada_e_sem_pedido_nao():
    linhas = [_linha(1, A14, "PEDIDO"), _linha(2, B14, "FATURADO")]
    plano = rrl.planejar([A14, B14, C14], linhas, date(2026, 9, 28))
    por = {p["cnpj"]: p for p in plano}
    assert (por[A14]["enfileirar"], por[A14]["pedido_fire"]) == (True, 1)
    assert (por[B14]["enfileirar"], por[B14]["motivo"]) == (False, "faturado")
    assert (por[C14]["enfileirar"], por[C14]["motivo"]) == (False, "sem_pedido_no_fire")


def test_planejar_ignora_cancelado_e_pega_o_mais_proximo_da_data():
    linhas = [
        _linha(10, A14, "CANCELADO", date(2026, 9, 28)),
        _linha(11, A14, "PEDIDO", date(2026, 1, 5)),
        _linha(12, A14, "PEDIDO", date(2026, 9, 27)),
    ]
    p = rrl.planejar([A14], linhas, date(2026, 9, 28))[0]
    assert (p["pedido_fire"], p["enfileirar"]) == (12, True)


def test_planejar_cnpj_com_mascara_no_fire_casa():
    p = rrl.planejar([A14], [_linha(1, "33.200.056/0313-70")], date(2026, 9, 28))[0]
    assert p["enfileirar"] is True


# ── rota ──────────────────────────────────────────────────────────────────────


def test_dry_run_mostra_o_plano_e_nao_enfileira(client):
    r = _post(client)
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["aplicado"] is False
    assert [(x["cnpj"], x["indices"], x["enfileirar"]) for x in corpo["lojas"]] == [
        (A14, [0], True),
        (B14, [1, 2], True),
        (C14, [3], True),
    ]
    assert _no_env(client, outbox_repo.list_for_import, "imp-1") == []


def test_aplicar_enfileira_uma_linha_por_loja_aberta(client):
    client.fire["linhas"] = [_linha(4968, A14), _linha(4970, B14, "FATURADO"), _linha(4972, C14)]
    r = _post(client, aplicar=True)
    assert r.status_code == 200, r.text
    linhas = sorted(
        _no_env(client, outbox_repo.list_for_import, "imp-1"), key=lambda x: x.idempotency_key
    )
    assert [x.idempotency_key for x in linhas] == [f"send-imp-1-{A14}", f"send-imp-1-{C14}"]
    assert {x.payload["externalId"] for x in linhas} == {"imp-1"}
    a = linhas[0].payload
    assert a["cliente"]["cnpj"] == A14
    assert [i["indiceOriginal"] for i in a["itens"]] == [0]


def test_aplicar_leva_erp_produto_id_do_check_salvo(client):
    _post(client, aplicar=True)
    linhas = _no_env(client, outbox_repo.list_for_import, "imp-1")
    por_loja = {x.payload["cliente"]["cnpj"]: x.payload for x in linhas}
    assert [i.get("erpProdutoId") for i in por_loja[B14]["itens"]] == ["3915", "3916"]
    assert "erpProdutoId" not in por_loja[C14]["itens"][0]  # item sem produto casado


def test_aplicar_de_novo_nao_duplica(client):
    _post(client, aplicar=True)
    _post(client, aplicar=True)
    assert len(_no_env(client, outbox_repo.list_for_import, "imp-1")) == 3


def test_aplicar_exige_interruptor_ligado(client, monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", raising=False)
    assert _post(client, aplicar=True).status_code == 409
    assert _no_env(client, outbox_repo.list_for_import, "imp-1") == []
    assert _post(client, aplicar=False).status_code == 200  # ver o plano pode


def test_import_com_cnpj_no_header_nao_divide_409(client):
    snap = _snapshot(cnpj_header="12.345.678/0001-95")
    with env_context.active_env(client.env["id"], client.env["slug"]):
        repo.insert_import(
            {
                "id": "imp-2",
                "source_filename": "n.xlsx",
                "imported_at": "2026-09-28T09:00:00",
                "order_number": "X1",
                "customer_cnpj": "12.345.678/0001-95",
                "fire_codigo": 5,
                "snapshot": snap,
                "status": "success",
                "portal_status": "found_in_fire",
            }
        )
    assert _post(client, "imp-2").status_code == 409


def test_import_sem_fire_codigo_409(client):
    with env_context.active_env(client.env["id"], client.env["slug"]):
        repo.insert_import(
            {
                "id": "imp-3",
                "source_filename": "p.xlsx",
                "imported_at": "2026-09-28T09:00:00",
                "order_number": "6702663705",
                "snapshot": _snapshot(),
                "status": "success",
                "portal_status": "parsed",
            }
        )
    assert _post(client, "imp-3").status_code == 409


def test_import_inexistente_404(client):
    assert _post(client, "nao-existe").status_code == 404


def test_nenhuma_loja_aberta_nao_enfileira_nada(client):
    client.fire["linhas"] = [
        _linha(1, A14, "FATURADO"),
        _linha(2, B14, "FATURADO"),
        _linha(3, C14, "FATURADO"),
    ]
    r = _post(client, aplicar=True)
    assert r.status_code == 200
    assert r.json()["enfileiradas"] == 0
    assert _no_env(client, outbox_repo.list_for_import, "imp-1") == []


def test_exige_admin(tmp_path: Path, monkeypatch, real_auth):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    from app.web.server import app

    r = TestClient(app).post("/api/imported/x/reenviar-flow-por-loja")
    assert r.status_code in (401, 403)


def test_fire_codigo_do_import_encerrado_recusa_aplicar(client):
    """A fila decide descartar pelo pedido do `fire_codigo` do import: encerrado, ela descartaria
    também as linhas das lojas abertas. Melhor recusar a enfileirar algo que some sem aviso."""
    client.fire["linhas"] = [_linha(4968, A14, "FATURADO"), _linha(4970, B14), _linha(4972, C14)]
    r = _post(client, aplicar=True)
    assert r.status_code == 409
    assert "4968" in r.json()["detail"]
    assert _no_env(client, outbox_repo.list_for_import, "imp-1") == []
    # o plano (dry-run) continua disponível e avisa
    corpo = _post(client).json()
    assert corpo["fire_codigo_estado"] == "FATURADO"
