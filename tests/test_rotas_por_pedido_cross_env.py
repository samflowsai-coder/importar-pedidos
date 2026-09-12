"""Em 'ligado' sem empresa selecionada, agir num pedido funciona — na empresa dele.

O teste que protege a adocao e o de 'desligado'/'observando': 412, como hoje.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router
from app.web.server import app


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("mm", "MM Americanense"), ("nasmar", "Nasmar")):
        (tmp_path / slug / "in").mkdir(parents=True)
        (tmp_path / slug / "out").mkdir(parents=True)
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug / "in"),
            output_dir=str(tmp_path / slug / "out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    yield TestClient(app)


def _snapshot(ident: str) -> dict:
    return {
        "header": {"order_number": ident, "customer_name": "DAJU"},
        "items": [{"description": "TENIS", "quantity": 2.0, "unit_price": 89.90, "ean": "7891"}],
        "source_file": "",
    }


def _grava(slug: str, ident: str, *, snapshot: dict | None = None) -> None:
    env = environments_repo.get_by_slug(slug)
    dados = {
        "id": ident,
        "source_filename": f"{ident}.pdf",
        "imported_at": "2026-09-11T10:00:00",
        "order_number": ident,
        "customer_name": "DAJU",
        "status": "success",
        "portal_status": "parsed",
    }
    # Snapshot so' quando o teste vai exercitar uma rota que exporta: sem ele
    # `_export_one_xlsx` para em 'no_snapshot' antes de tocar qualquer amarracao.
    if snapshot is not None:
        dados["snapshot"] = snapshot
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(dados)


def test_ligado_sem_cookie_abre_pedido_da_outra_empresa(portal):
    """O caso que motivou a entrega: a caixa soma, e clicar na linha funciona."""
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.get("/api/imported/N1")
    assert r.status_code == 200
    assert r.json()["entry"]["order_number"] == "N1"


def test_ligado_o_pedido_decide_e_o_cookie_nao_opina(portal):
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    portal.cookies.set("portal_env", environments_repo.get_by_slug("mm")["id"])
    r = portal.get("/api/imported/N1")
    assert r.status_code == 200
    assert r.json()["entry"]["order_number"] == "N1"


def test_ligado_id_inexistente_da_404_nunca_um_default(portal):
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    assert portal.get("/api/imported/nao-existe").status_code == 404


@pytest.mark.parametrize("modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO])
def test_fora_de_ligado_sem_cookie_continua_412(portal, modo):
    """O teste que protege a adocao: o comportamento de hoje, intacto."""
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(modo, por="teste")
    assert portal.get("/api/imported/N1").status_code == 412


@pytest.mark.parametrize("rota", ["/api/imported/N1", "/api/imported/N1/preview"])
def test_ligado_nao_serve_pedido_a_anonimo(tmp_path, monkeypatch, rota):
    """Sem o portao acidental do cookie, so a auth separa anonimo dos dados
    das duas empresas."""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    router.reset_init_cache()
    with router.shared_connect():
        pass
    environments_repo.create(
        slug="nasmar",
        name="Nasmar",
        watch_dir=str(tmp_path / "n"),
        output_dir=str(tmp_path / "n"),
        fb_path=str(tmp_path / "n.fdb"),
    )
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    assert TestClient(app).get(rota).status_code == 401


def test_lote_agrupa_por_empresa(portal):
    """Selecao mista nao e recusada: cada pedido e processado na SUA empresa."""
    _grava("mm", "M1")
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")

    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1", "N1"]})
    assert r.status_code == 200
    por_id = {x["id"]: x for x in r.json()["results"]}
    assert por_id["M1"]["env_slug"] == "mm"
    assert por_id["M1"]["env_name"] == "MM Americanense"
    assert por_id["N1"]["env_slug"] == "nasmar"


def test_lote_com_id_orfao_falha_so_aquele_item(portal):
    _grava("mm", "M1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")

    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1", "fantasma"]})
    assert r.status_code == 200
    corpo = r.json()
    por_id = {x["id"]: x for x in corpo["results"]}
    assert por_id["fantasma"]["ok"] is False
    assert por_id["fantasma"]["reason"] == "nao_encontrado"
    assert por_id["M1"]["env_slug"] == "mm"
    assert corpo["total"] == 2
    assert corpo["failed"] >= 1


@pytest.mark.parametrize("modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO])
def test_lote_fora_de_ligado_sem_cookie_continua_412(portal, modo):
    """Sem cookie e sem 'ligado', o lote e o de hoje."""
    _grava("mm", "M1")
    roteamento_repo.set_modo(modo, por="teste")
    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1"]})
    assert r.status_code == 412


# ── Escrita por-pedido: as QUATRO amarracoes seguem o pedido ────────────────
#
# O SQLite ja seguia (contextvar). As outras tres saem de lugares diferentes e
# nao seguiam: a pasta de saida vem do `cfg`, e o Firebird / o check de preco /
# o slug do Flow vem de `request.state.environment`, que o middleware preenche
# a partir do COOKIE. Em 'ligado' "sem cookie" e o estado normal, entao o alvo
# silencioso e a config legada — outro banco, outra pasta, trava de preco off.


@pytest.fixture
def espia(monkeypatch, tmp_path):
    """Grava qual empresa chegou em cada amarracao de uma rota de escrita.

    Tudo que toca ERP ou planilha e' substituido: nenhuma conexao Firebird,
    nenhum xlsx de verdade. A pasta ainda e' exercitada no disco (o dublê do
    ERPExporter escreve onde o handler mandou), porque o defeito e justamente
    a pasta errada.

    `app_config.load` aponta pra uma pasta 'legado' em tmp_path de proposito:
    e' pra onde o bug manda o arquivo quando nao ha cookie, e um teste nao
    pode escrever no `output/` do repo pra descobrir isso.
    """
    from app import config as app_config
    from app.erp import product_check as pc_mod
    from app.exporters import erp_exporter as erp_mod
    from app.exporters import firebird_exporter as fb_mod
    from app.web import server as srv

    visto: dict[str, str | None] = {}
    legado = tmp_path / "legado"
    legado.mkdir()

    monkeypatch.setattr(
        app_config,
        "load",
        lambda: {"watch_dir": str(legado), "output_dir": str(legado), "export_mode": "xlsx"},
    )

    def _check(order, *, env=None):
        visto["check"] = (env or {}).get("slug")
        # `available: False` e o proprio sintoma do cenario B (is_blocking
        # devolve (False, ...) pra check indisponivel): nao bloqueia, entao a
        # rota segue e da' pra medir as outras amarracoes.
        return {"available": False, "items": [], "summary": {}}

    monkeypatch.setattr(pc_mod, "check_order", _check)

    def _export(self, order, output_dir):  # noqa: ARG001 — assinatura do real
        visto["pasta"] = str(output_dir)
        destino = Path(output_dir) / f"{order.header.order_number}.xlsx"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.touch()
        return [destino]

    monkeypatch.setattr(erp_mod.ERPExporter, "export", _export)

    class _FirebirdEspia:
        """Registra o env que chegou no construtor e recusa o insert."""

        def __init__(self, env=None):
            visto["firebird"] = (env or {}).get("slug")

        def export(self, order, *, override_client_id=None):  # noqa: ARG002
            return _ResultadoSkip()

    class _ResultadoSkip:
        skipped = True
        skip_reason = "ESPIA"
        fire_codigo = None
        items_inserted = 0

        def to_dict(self):
            return {"skipped": True, "skip_reason": "ESPIA"}

    monkeypatch.setattr(fb_mod, "FirebirdExporter", _FirebirdEspia)

    def _push(order, *, import_id=None, slug=None):  # noqa: ARG001
        visto["flow"] = slug
        return True

    monkeypatch.setattr(srv, "push_new_order", _push)
    return visto


def _pasta_de(slug: str) -> str:
    """Pasta de saida da empresa, na forma que o handler resolve (`.resolve()`)."""
    return str(Path(environments_repo.get_by_slug(slug)["output_dir"]).resolve())


def _sqlite_de(slug: str, ident: str) -> dict | None:
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        return repo.get_import(ident)


@pytest.mark.parametrize("cookie", [None, "mm"], ids=["sem-cookie", "cookie-da-outra-empresa"])
def test_ligado_escrita_amarra_tudo_na_empresa_do_pedido(portal, espia, cookie):
    """O pedido decide as QUATRO pontas, nao so' o SQLite.

    Um pedido da Nasmar exportado com 'MM' no filtro — ou com filtro nenhum,
    que e o default em 'ligado' — nao pode encostar em nada da MM nem na
    config legada.
    """
    _grava("nasmar", "N1", snapshot=_snapshot("N1"))
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    if cookie:
        portal.cookies.set("portal_env", environments_repo.get_by_slug(cookie)["id"])

    r = portal.post("/api/imported/N1/export-xlsx")
    assert r.status_code == 200, r.text

    # 1. check de preco / Firebird
    assert espia["check"] == "nasmar"
    # 2. pasta de saida — a config, o que a rota respondeu, e o disco
    assert espia["pasta"] == _pasta_de("nasmar")
    assert r.json()["output_files"][0]["path"].startswith(_pasta_de("nasmar"))
    assert list(Path(_pasta_de("nasmar")).glob("*.xlsx"))
    assert not list(Path(_pasta_de("mm")).glob("*.xlsx"))
    # 3. slug do FlowPCP
    assert espia["flow"] == "nasmar"
    # 4. SQLite
    gravado = _sqlite_de("nasmar", "N1")
    assert gravado is not None
    assert gravado["output_files"][0]["path"].startswith(_pasta_de("nasmar"))
    assert _sqlite_de("mm", "N1") is None


@pytest.mark.parametrize("cookie", [None, "mm"], ids=["sem-cookie", "cookie-da-outra-empresa"])
def test_ligado_send_to_fire_usa_o_firebird_da_empresa_do_pedido(portal, espia, cookie):
    """O pior caso do defeito: pedido de compra inserido no ERP da empresa errada.

    O dublê do exporter registra o env que chegou e devolve skip — o assert e'
    sobre qual banco teria recebido o insert, nunca sobre um insert real.
    """
    _grava("nasmar", "N1", snapshot=_snapshot("N1"))
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    if cookie:
        portal.cookies.set("portal_env", environments_repo.get_by_slug(cookie)["id"])

    r = portal.post("/api/imported/N1/send-to-fire")
    assert r.status_code == 409  # o skip do dublê, nao um erro de amarracao
    assert espia["firebird"] == "nasmar"
    assert espia["check"] == "nasmar"
    assert espia["pasta"] == _pasta_de("nasmar")


@pytest.mark.parametrize("modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO])
def test_fora_de_ligado_a_escrita_continua_seguindo_o_cookie(portal, espia, modo):
    """Nada muda pra quem nao ligou o roteamento: o cookie manda, como hoje."""
    _grava("mm", "M1", snapshot=_snapshot("M1"))
    roteamento_repo.set_modo(modo, por="teste")
    portal.cookies.set("portal_env", environments_repo.get_by_slug("mm")["id"])

    r = portal.post("/api/imported/M1/export-xlsx")
    assert r.status_code == 200, r.text
    assert espia["check"] == "mm"
    assert espia["flow"] == "mm"
    assert espia["pasta"] == _pasta_de("mm")


# ── As outras QUATRO amarracoes: so tinham cobertura indireta ───────────────
#
# `send-to-fire` e `export-xlsx`, acima, ja provam a amarracao ponta-a-ponta.
# Estas quatro rotas passam pelo MESMO `_request_environment`/
# `_firebird_open_for_request`, mas cada uma numa forma diferente de chegar
# no Fire, no check de preco ou no slug do Flow — vale um teste de rota por
# cada uma, nao so' a prova unitaria da dependency.


@pytest.mark.parametrize("cookie", [None, "mm"], ids=["sem-cookie", "cookie-da-outra-empresa"])
def test_ligado_override_cliente_abre_o_firebird_da_empresa_do_pedido(portal, monkeypatch, cookie):
    """`_firebird_open_for_request` tem que devolver o config de NASMAR — nunca
    o da MM (cookie) nem o singleton legado (sem cookie, que em 'ligado' e' o
    estado normal). O dublê recusa a conexao: o assert e' sobre qual config
    chegou no construtor, nunca um SELECT de verdade."""
    from contextlib import contextmanager

    from app.erp.connection import FirebirdConnection

    visto: dict[str, str | None] = {}

    @contextmanager
    def _fake_connect_with_config(self, cfg):
        visto["firebird"] = cfg.get("path")
        raise RuntimeError("espia: sem firebird de verdade")
        yield None  # pragma: no cover — nunca alcancado

    monkeypatch.setattr(FirebirdConnection, "connect_with_config", _fake_connect_with_config)

    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    if cookie:
        portal.cookies.set("portal_env", environments_repo.get_by_slug(cookie)["id"])

    r = portal.post("/api/imported/N1/override-cliente", json={"cliente_codigo": 123})
    assert r.status_code == 502, r.text  # a excecao do dublê, nao um erro de amarracao
    assert visto["firebird"] == environments_repo.get_by_slug("nasmar")["fb_path"]


@pytest.mark.parametrize("cookie", [None, "mm"], ids=["sem-cookie", "cookie-da-outra-empresa"])
def test_ligado_vincular_produto_roda_o_check_na_empresa_do_pedido(portal, espia, monkeypatch, cookie):
    """`check_order(order, env=...)` tem que rodar com o env de NASMAR — nunca
    o da MM nem `None`, que abriria a trava de preco (`env=None`) no pedido
    errado."""
    from app.persistence import catalogo_fire_repo

    monkeypatch.setattr(
        catalogo_fire_repo,
        "list_all",
        lambda conn: [{"fire_produto_id": "P1", "codigo": 111, "nome": "TENIS X", "ean": "7891"}],
    )

    _grava("nasmar", "N1", snapshot=_snapshot("N1"))
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    if cookie:
        portal.cookies.set("portal_env", environments_repo.get_by_slug(cookie)["id"])

    r = portal.post(
        "/api/imported/N1/vincular-produto", json={"item_index": 0, "fire_produto_id": "P1"}
    )
    assert r.status_code == 200, r.text
    assert espia["check"] == "nasmar"


@pytest.mark.parametrize("cookie", [None, "mm"], ids=["sem-cookie", "cookie-da-outra-empresa"])
def test_ligado_ack_sem_preco_roda_o_check_na_empresa_do_pedido(portal, espia, cookie):
    """Mesma amarracao do vincular-produto, outra rota: o check tem que rodar
    com o env de NASMAR, nunca da MM nem `None`."""
    _grava("nasmar", "N1", snapshot=_snapshot("N1"))
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    if cookie:
        portal.cookies.set("portal_env", environments_repo.get_by_slug(cookie)["id"])

    r = portal.post("/api/imported/N1/ack-sem-preco")
    assert r.status_code == 503, r.text  # o dublê devolve available=False
    assert espia["check"] == "nasmar"


@pytest.mark.parametrize("cookie", [None, "mm"], ids=["sem-cookie", "cookie-da-outra-empresa"])
def test_ligado_rehydrate_preview_le_o_slug_da_empresa_do_pedido(portal, monkeypatch, cookie):
    """O de-para de cliente intercompany (`resolucao_para`) tem que receber o
    slug de NASMAR — nunca o da MM nem `None`, que apagaria o bloco inteiro do
    payload em silencio."""
    from app.web import server as srv

    visto: dict[str, str | None] = {}

    def _resolucao(order, *, slug=None):
        visto["slug"] = slug
        return None

    monkeypatch.setattr(srv, "resolucao_para", _resolucao)

    _grava("nasmar", "N1", snapshot=_snapshot("N1"))
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    if cookie:
        portal.cookies.set("portal_env", environments_repo.get_by_slug(cookie)["id"])

    r = portal.get("/api/imported/N1/preview")
    assert r.status_code == 200, r.text
    assert visto["slug"] == "nasmar"
