"""Selo do de-para de cliente intercompany no preview reidratado."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.erp.depara_cliente import ResolucaoCliente
from app.persistence import context as env_context
from app.persistence import db, environments_repo, repo


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def cliente_com_pedido(tmp_path, monkeypatch):
    """TestClient com ambiente ativo + um import com snapshot. Devolve (client, import_id)."""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    from app.web.server import app

    env = environments_repo.create(
        slug="mm", name="MM", watch_dir=str(tmp_path), output_dir=str(tmp_path), fb_path=""
    )
    import_id = str(uuid.uuid4())
    # insert_import roteia pra DB do ambiente ATIVO no contexto (não pelo
    # campo environment_id do dict) — sem isso o row cai na DB do ambiente
    # "test" (ativado por db.set_db_path) e o preview 404 em vez de achar o
    # import. Mesmo padrão de tests/test_webhooks.py.
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": import_id,
                "environment_id": env["id"],
                "source_filename": "pedido.pdf",
                "imported_at": datetime.now(UTC).isoformat(),
                "order_number": "AF066",
                "customer_name": "Nasmar Comercio De Roupas Ltda",
                "customer_cnpj": "34513679000134",
                "portal_status": "parsed",
                "snapshot": {
                    "header": {
                        "order_number": "AF066",
                        "customer_name": "Nasmar Comercio De Roupas Ltda",
                        "customer_cnpj": "34513679000134",
                    },
                    "items": [{"description": "MEIA STZ", "quantity": 12}],
                },
            }
        )
    client = TestClient(app)
    client.cookies.set("portal_env", env["id"])
    yield client, import_id
    client.cookies.clear()


def test_preview_sem_intercompany_traz_none(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: None)
    r = client.get(f"/api/imported/{import_id}/preview")
    assert r.status_code == 200, r.text
    assert r.json()["depara_cliente"] is None


def test_preview_mostra_cliente_real(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET", motivo="ok")
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: res)
    r = client.get(f"/api/imported/{import_id}/preview")
    assert r.json()["depara_cliente"] == {
        "resolvido": True,
        "cnpj": "10772208000182",
        "nome": "AUTHENTIC FEET",
        "motivo": "ok",
    }


def test_preview_marca_nao_resolvido(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido
    res = ResolucaoCliente(False, motivo="nao_encontrado")
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: res)
    dp = client.get(f"/api/imported/{import_id}/preview").json()["depara_cliente"]
    assert dp == {"resolvido": False, "cnpj": None, "nome": None, "motivo": "nao_encontrado"}


def test_preview_nao_quebra_se_o_resolver_explodir(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido

    def _boom(order, *, slug):
        raise RuntimeError("firebird fora")

    monkeypatch.setattr(server, "resolucao_para", _boom)
    r = client.get(f"/api/imported/{import_id}/preview")
    assert r.status_code == 200
    assert r.json()["depara_cliente"] is None


# ---------------------------------------------------------------------------
# Guarda de front-end: o selo tem três ramos de lógica real (sem de-para,
# resolvido, não resolvido) que nenhum teste de payload cobre. Segue o padrão
# de tests/test_admin_ambiente_edit_clientes.py — lê o HTML/JS estático como
# texto e trava os trechos que não podem regredir silenciosamente.
# ---------------------------------------------------------------------------

_HTML = (
    Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "index.html"
).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "trecho",
    [
        # elemento do selo, ao lado do nome do cliente no preview
        '<span id="pvDeparaCliente" class="badge hidden"></span>',
        # renderPreview lê a chave nova do payload
        "const dp = data.depara_cliente;",
        # ramo 1: sem de-para aplicável → selo escondido e vazio
        "dpEl.classList.add('hidden');",
        # ramo 2: cliente real resolvido → selo verde com o nome
        "dpEl.className = 'badge badge-ok';",
        "dpEl.textContent = `Flow recebe: ${dp.nome}`;",
        # ramo 3: não resolvido → selo de aviso, sobe como revenda
        "dpEl.className = 'badge badge-warn';",
        "dpEl.textContent = 'Cliente real não resolvido — sobe como revenda';",
        # variantes de cor existem no CSS (tokens do arquivo, sem paleta nova)
        ".badge-ok",
        ".badge-warn",
    ],
)
def test_selo_depara_cliente_presente_no_frontend(trecho):
    assert trecho in _HTML, f"trecho ausente no front-end do preview: {trecho!r}"
