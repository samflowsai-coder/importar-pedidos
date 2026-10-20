"""I1 (revisão final): pedido em revisão com check salvo antes do pacote (itens sem
`produto`) não pode virar beco sem saída em `ligado`.

- `POST /api/imported/{id}/reconferir` roda o check de novo (com as escolhas
  pontuais do import), grava o check e devolve check + produto_trava.
- `_produto_trava_preview` com check onde nenhum item tem `produto` devolve um
  estado que não trava a tela (`check_antigo`); o portão do envio segue fechado.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.erp import product_check
from app.persistence import db, repo
from app.web import server
from app.web.server import app

client = TestClient(app, raise_server_exceptions=True)

LIGADO = {"id": "test-env-id", "slug": "test", "produto_chave_modo": "ligado"}


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _check_antigo() -> dict:
    """Check como o pacote anterior gravava: sem `produto` no item."""
    return {
        "available": True,
        "items": [{"product_code": "ABC", "ean": None, "match": {"fire_produto_id": 7}}],
        "summary": {},
    }


def _check_novo(estado="ok") -> dict:
    return {
        "available": True,
        "produto_conferencia_incompleta": False,
        "items": [
            {
                "product_code": "ABC",
                "ean": None,
                "produto": {
                    "estado": estado,
                    "fire_seq": 7 if estado == "ok" else None,
                    "fonte": "codprod_altern" if estado == "ok" else None,
                    "motivo": None,
                    "candidatos": [{"seq": 7, "codprod_altern": "ABC", "descricao": "TENIS"}]
                    if estado == "ok"
                    else [],
                    "troca_no_fire": None,
                },
            }
        ],
        "summary": {},
    }


def _seed(*, portal_status="parsed", check=None, escolhido=None) -> str:
    entry_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": entry_id,
            "source_filename": "R-1.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": "R-1",
            "customer": "RIACHUELO",
            "status": "success",
            "portal_status": portal_status,
            "snapshot": {
                "header": {"order_number": "R-1", "customer_name": "RIACHUELO"},
                "items": [{"description": "TENIS", "quantity": 1.0, "product_code": "ABC"}],
                "source_file": "",
            },
            "check": check if check is not None else _check_antigo(),
        }
    )
    if escolhido is not None:
        repo.set_produto_escolha(
            entry_id, 0, fire_seq=escolhido, por="t@x", descricao="TENIS", codigo="ABC", ean=None
        )
    return entry_id


@pytest.fixture
def ligado(monkeypatch):
    from app.erp import preco_trava

    monkeypatch.setattr(server, "_request_environment", lambda request: LIGADO)
    monkeypatch.setattr(server, "_export_mode_atual", lambda: "db")
    monkeypatch.setattr(preco_trava, "check_preco_para_gravar", lambda *a, **k: None)


def test_reconferir_roda_check_com_escolhas_grava_e_devolve_trava(ligado, monkeypatch):
    visto: dict = {}

    def fake(order, *, env=None, escolhas=None):
        visto["env"] = env
        visto["escolhas"] = escolhas
        return _check_novo("ok")

    monkeypatch.setattr(product_check, "check_order", fake)
    entry_id = _seed(escolhido=7)

    r = client.post(f"/api/imported/{entry_id}/reconferir")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["check"]["items"][0]["produto"]["estado"] == "ok"
    assert body["produto_trava"]["bloqueado"] is False
    assert body["produto_trava"]["reason"] is None
    assert visto["escolhas"] == {0: 7}
    assert visto["env"]["slug"] == "test"
    # Persistiu: o re-open lê o check novo.
    salvo = repo.get_import(entry_id)["check"]
    assert salvo["items"][0]["produto"]["estado"] == "ok"


def test_reconferir_com_fire_fora_nao_sobrescreve_o_check_salvo(ligado, monkeypatch):
    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda order, *, env=None, escolhas=None: {"available": False, "items": [], "summary": {}},
    )
    entry_id = _seed()

    r = client.post(f"/api/imported/{entry_id}/reconferir")

    assert r.status_code == 200, r.text
    assert r.json()["check"]["available"] is False
    assert repo.get_import(entry_id)["check"] == _check_antigo()


@pytest.mark.parametrize("status", ["sent_to_fire", "cancelled"])
def test_reconferir_fora_de_revisao_409(ligado, monkeypatch, status):
    monkeypatch.setattr(
        product_check, "check_order", lambda *a, **k: pytest.fail("não deveria conferir")
    )
    entry_id = _seed(portal_status=status)
    r = client.post(f"/api/imported/{entry_id}/reconferir")
    assert r.status_code == 409


def test_reconferir_inexistente_404(ligado):
    assert client.post("/api/imported/nao-existe/reconferir").status_code == 404


# ── estado check_antigo da trava da tela ─────────────────────────────────────


def test_trava_preview_check_antigo_nao_trava_a_tela():
    t = server._produto_trava_preview(_check_antigo(), LIGADO)
    assert t["bloqueado"] is False
    assert t["reason"] == "check_antigo"
    assert t["itens"] == []


def test_trava_preview_check_antigo_observando():
    env = {**LIGADO, "produto_chave_modo": "observando"}
    t = server._produto_trava_preview(_check_antigo(), env)
    assert t["bloqueado"] is False
    assert t["reason"] == "check_antigo"


def test_trava_preview_check_antigo_desligado_igual_a_hoje():
    env = {**LIGADO, "produto_chave_modo": "desligado"}
    t = server._produto_trava_preview(_check_antigo(), env)
    assert t["bloqueado"] is False
    assert t["reason"] is None


def test_trava_preview_check_novo_com_item_pendente_segue_travando():
    t = server._produto_trava_preview(_check_novo("a_identificar"), LIGADO)
    assert t["bloqueado"] is True
    assert t["reason"] == "produto_a_identificar"


def test_portao_do_envio_segue_fechado_com_check_antigo():
    """O backend não usa `check_antigo`: item sem `produto` trava em ligado."""
    from app.erp import produto_trava

    tp = produto_trava.avaliar(_check_antigo(), env=LIGADO, export_mode="db")
    assert tp.bloqueado is True
    assert tp.reason == "produto_a_identificar"


def test_preview_reidratado_de_check_antigo_expoe_check_antigo(ligado):
    entry_id = _seed()
    r = client.get(f"/api/imported/{entry_id}/preview")
    assert r.status_code == 200, r.text
    assert r.json()["produto_trava"]["reason"] == "check_antigo"
