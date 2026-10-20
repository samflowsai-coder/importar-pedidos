"""Trava de produto (`produto_chave_modo`) nos caminhos que saem para o Fire/XLSX
fora do `decidir_envio`: importação (`_run_exporters`), `/api/process` e o CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import db
from app.web.server import app

client = TestClient(app, raise_server_exceptions=False)

SAMPLE_PDF = Path(__file__).parent.parent / "samples" / "2600009562-2026-02-25.pdf"


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _env(tmp_path, *, produto="ligado", preco="desligado"):
    """Ambiente slug "test" (mesmo SQLite dos testes) com os dois interruptores."""
    from app.persistence import environments_repo

    env = environments_repo.create(
        slug="test",
        name="Teste",
        watch_dir=str(tmp_path / "in"),
        output_dir=str(tmp_path / "out"),
        fb_path=str(tmp_path / "x.fdb"),
    )
    environments_repo.set_produto_chave_modo(env["id"], produto, por="teste")
    environments_repo.set_preco_trava_modo(env["id"], preco, por="teste")
    return environments_repo.get(env["id"])


def _config(monkeypatch, tmp_path, export_mode="xlsx"):
    from app import config as app_config

    monkeypatch.setattr(
        app_config,
        "load",
        lambda: {
            "watch_dir": str(tmp_path / "in"),
            "output_dir": str(tmp_path / "out"),
            "export_mode": export_mode,
        },
    )


def _order(numero="A1"):
    return Order(
        header=OrderHeader(order_number=numero),
        items=[OrderItem(description="x", product_code="C1", quantity=1, unit_price=10.0)],
    )


# ── _run_exporters ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("preco", [None, "desligado", "observando", "ligado"])
def test_run_exporters_produto_ligado_adia_antes_do_preco(monkeypatch, tmp_path, preco):
    from app.web import server as server_mod

    _config(monkeypatch, tmp_path)
    env = {"slug": "mm", "produto_chave_modo": "ligado"}
    if preco is not None:
        env["preco_trava_modo"] = preco

    out = server_mod._run_exporters(_order(), tmp_path, env=env)

    assert out["output_files"] == []
    assert out["xlsx_adiado"] == "produto_a_identificar"
    assert list(tmp_path.glob("*.xlsx")) == []


@pytest.mark.parametrize("modo", ["desligado", "observando"])
def test_run_exporters_produto_fora_de_ligado_gera_como_hoje(monkeypatch, tmp_path, modo):
    from app.web import server as server_mod

    _config(monkeypatch, tmp_path)
    env = {"slug": "mm", "produto_chave_modo": modo, "preco_trava_modo": "desligado"}

    out = server_mod._run_exporters(_order(), tmp_path, env=env)

    assert len(out["output_files"]) == 1
    assert "xlsx_adiado" not in out


def test_run_exporters_produto_ligado_em_db_nao_marca_xlsx(monkeypatch, tmp_path):
    from app.web import server as server_mod

    _config(monkeypatch, tmp_path, export_mode="db")
    out = server_mod._run_exporters(
        _order(), tmp_path, env={"slug": "mm", "produto_chave_modo": "ligado"}
    )
    assert out["db_result"] == {"skipped": True, "skip_reason": "USE_PREVIEW"}
    assert "xlsx_adiado" not in out


def test_run_exporters_adia_com_produto_ligado(monkeypatch, tmp_path):
    """`/api/import` com o ambiente em `ligado`: nada em output/, pedido em revisão."""
    from app.persistence import repo

    if not SAMPLE_PDF.exists():
        pytest.skip("Sample PDF not available")
    env = _env(tmp_path)
    _config(monkeypatch, tmp_path)
    (tmp_path / "in").mkdir()
    shutil.copy(SAMPLE_PDF, tmp_path / "in" / SAMPLE_PDF.name)

    client.cookies.set("portal_env", env["id"])
    try:
        r = client.post("/api/import", json={"files": [SAMPLE_PDF.name]})
    finally:
        client.cookies.clear()

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [], body
    (res,) = body["results"]
    assert res["files"] == []
    assert res["xlsx_adiado"] == "produto_a_identificar"
    assert list((tmp_path / "out").glob("*.xlsx")) == []
    assert repo.get_import(res["entry_id"])["portal_status"] == "parsed"


# ── /api/process (caminho legado de XLSX) ────────────────────────────────────


def test_process_409_com_produto_ligado(tmp_path):
    env = _env(tmp_path)

    client.cookies.set("portal_env", env["id"])
    try:
        r = client.post(
            "/api/process",
            data={"output_dir": str(tmp_path)},
            files=[("files", ("p.pdf", b"%PDF-1.4", "application/pdf"))],
        )
    finally:
        client.cookies.clear()

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Trava de produto ligada: use a revisão de pedidos."
    assert list(tmp_path.glob("*.xlsx")) == []


@pytest.mark.parametrize("modo", ["desligado", "observando"])
def test_process_produto_fora_de_ligado_segue_como_hoje(tmp_path, modo):
    env = _env(tmp_path, produto=modo)

    client.cookies.set("portal_env", env["id"])
    try:
        r = client.post(
            "/api/process",
            data={"output_dir": str(tmp_path)},
            files=[("files", ("malware.exe", b"MZ", "application/octet-stream"))],
        )
    finally:
        client.cookies.clear()

    assert r.status_code == 200
    assert any("suportado" in e["error"].lower() for e in r.json()["errors"])


# ── CLI (main.py) ────────────────────────────────────────────────────────────


def _cli(monkeypatch, tmp_path, orders, checks, *, trava=None):
    """Roda o CLI com os pedidos dados; devolve (exportados, mensagens WARNING, chamadas
    ao check). `checks[numero]` é o check ou uma exceção a levantar."""
    import main as cli
    from app.exporters.erp_exporter import ERPExporter
    from app.utils.logger import logger

    if trava is None:
        monkeypatch.delenv("PORTAL_PRODUTO_CHAVE_CLI", raising=False)
    else:
        monkeypatch.setenv("PORTAL_PRODUTO_CHAVE_CLI", trava)
    monkeypatch.setattr(
        cli.app_config, "load", lambda: {"export_mode": "xlsx", "output_dir": str(tmp_path)}
    )
    monkeypatch.setattr(cli.FileLoader, "load_files", lambda self, d: list(orders))
    monkeypatch.setattr(cli, "process", lambda f: f)
    chamadas: list[str] = []

    def _check(order, **kw):
        chamadas.append(order.header.order_number)
        valor = checks[order.header.order_number]
        if isinstance(valor, Exception):
            raise valor
        return valor

    monkeypatch.setattr(cli, "check_order", _check)
    exportados: list[str] = []
    monkeypatch.setattr(
        ERPExporter,
        "export",
        lambda self, order, out: exportados.append(order.header.order_number) or [],
    )
    mensagens: list[str] = []
    sink = logger.add(lambda m: mensagens.append(m.record["message"]), level="WARNING")
    try:
        cli.main()
    finally:
        logger.remove(sink)
    return exportados, mensagens, chamadas


def _item(estado, motivo=""):
    return {"produto": {"estado": estado, "motivo": motivo}}


def _ok(*estados, available=True, incompleta=False):
    return {
        "available": available,
        "produto_conferencia_incompleta": incompleta,
        "items": [_item(e, "código C9 fora do Fire") for e in estados],
    }


@pytest.mark.parametrize("trava", [None, "", "desligado", "observando", "LIGADO"])
def test_cli_sem_a_env_var_ligado_e_igual_a_hoje(monkeypatch, tmp_path, trava):
    exportados, _, chamadas = _cli(
        monkeypatch, tmp_path, [_order("P1")], {"P1": _ok("a_identificar")}, trava=trava
    )
    assert exportados == ["P1"]
    assert chamadas == []  # nem consulta o Fire


def test_cli_pula_pedido_com_item_pendente(monkeypatch, tmp_path):
    exportados, mensagens, _ = _cli(
        monkeypatch,
        tmp_path,
        [_order("P1"), _order("P2")],
        {"P1": _ok("ok", "a_identificar"), "P2": _ok("ok")},
        trava="ligado",
    )
    assert exportados == ["P2"]
    assert (
        "Pedido P1 não exportado: item 2 sem produto exato (código C9 fora do Fire)." in mensagens
    )


@pytest.mark.parametrize(
    "check",
    [_ok("fire_indisponivel", available=False), _ok("ok", incompleta=True)],
    ids=["fire_fora", "conferencia_incompleta"],
)
def test_cli_ligado_pula_sem_conferencia(monkeypatch, tmp_path, check):
    exportados, mensagens, _ = _cli(
        monkeypatch,
        tmp_path,
        [_order("P1"), _order("P2")],
        {"P1": check, "P2": _ok("ok")},
        trava="ligado",
    )
    assert exportados == ["P2"]
    assert any(m.startswith("Pedido P1 não exportado: o produto dos itens") for m in mensagens)


def test_cli_ligado_erro_no_check_pula_so_o_pedido(monkeypatch, tmp_path):
    exportados, mensagens, _ = _cli(
        monkeypatch,
        tmp_path,
        [_order("P1"), _order("P2")],
        {"P1": RuntimeError("fire caiu"), "P2": _ok("ok")},
        trava="ligado",
    )
    assert exportados == ["P2"]
    assert "Pedido P1 não exportado: falha ao conferir o produto (RuntimeError)." in mensagens


def test_cli_motivo_com_ponto_nao_duplica_pontuacao(monkeypatch, tmp_path):
    check = {"available": True, "items": [_item("ambiguo", "O EAN 1 está em 2 produtos.")]}
    _, mensagens, _ = _cli(monkeypatch, tmp_path, [_order("P1")], {"P1": check}, trava="ligado")
    assert "Pedido P1 não exportado: item 1 sem produto exato (O EAN 1 está em 2 produtos)." in (
        mensagens
    )


# ── Sem ambiente (sem cookie): vale o mais restrito ──────────────────────────


def test_run_exporters_sem_ambiente_com_algum_ligado_adia(monkeypatch, tmp_path):
    from app.web import server as server_mod

    _env(tmp_path)
    _config(monkeypatch, tmp_path)
    out = server_mod._run_exporters(_order(), tmp_path, env=None)
    assert out["xlsx_adiado"] == "produto_a_identificar"
    assert list(tmp_path.glob("*.xlsx")) == []


def test_process_sem_cookie_com_algum_ligado_409(tmp_path):
    _env(tmp_path)
    r = client.post(
        "/api/process",
        data={"output_dir": str(tmp_path)},
        files=[("files", ("p.pdf", b"%PDF-1.4", "application/pdf"))],
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Trava de produto ligada: use a revisão de pedidos."


def test_decidir_envio_sem_ambiente_com_algum_ligado_bloqueia(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    import app.erp.product_check as pc
    from app.erp import preco_trava

    _env(tmp_path)
    monkeypatch.setattr(
        pc,
        "check_order",
        lambda order, **kw: {"available": True, "items": [_item("a_identificar", "x.")]},
    )
    d = preco_trava.decidir_envio("id", _order(), {}, env=None, agora=datetime.now(UTC))
    assert (d.bloqueado, d.reason) == (True, "produto_a_identificar")


# ── Rotas que passam pelo decidir_envio ──────────────────────────────────────


def _seed_parsed():
    import uuid
    from datetime import datetime

    from app.persistence import repo

    import_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": import_id,
            "source_filename": "p.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": "A1",
            "status": "success",
            "portal_status": "parsed",
            "snapshot": _order().model_dump(),
        }
    )
    return import_id


@pytest.mark.parametrize(
    ("rota", "evento"),
    [("send-to-fire", "send_to_fire_blocked"), ("export-xlsx", "xlsx_export_blocked")],
)
def test_rota_com_produto_ligado_responde_409_e_nao_exporta(monkeypatch, tmp_path, rota, evento):
    import app.erp.product_check as pc
    from app.exporters import erp_exporter, firebird_exporter
    from app.observability import metrics
    from app.persistence import repo

    env = _env(tmp_path)
    _config(monkeypatch, tmp_path)
    import_id = _seed_parsed()
    motivo = "O cliente mandou o código C1, que não está cadastrado no Fire."
    monkeypatch.setattr(
        pc,
        "check_order",
        lambda order, **kw: {"available": True, "items": [_item("a_identificar", motivo)]},
    )

    def _nunca(*a, **kw):
        raise AssertionError("exportou com a trava de produto ligada")

    monkeypatch.setattr(erp_exporter.ERPExporter, "export", _nunca)
    monkeypatch.setattr(firebird_exporter.FirebirdExporter, "export", _nunca)
    contador = metrics.price_check_blocks_total.labels(reason="produto")
    antes = contador._value.get()

    client.cookies.set("portal_env", env["id"])
    try:
        r = client.post(f"/api/imported/{import_id}/{rota}")
    finally:
        client.cookies.clear()

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == f"Item 1: {motivo}"
    (bloqueio,) = [a for a in repo.list_audit(import_id) if a["event_type"] == evento]
    assert bloqueio["detail"] == {
        "produto": [{"index": 0, "estado": "a_identificar", "motivo": motivo}]
    }
    assert contador._value.get() - antes == 1
    assert repo.get_import(import_id)["portal_status"] == "parsed"
