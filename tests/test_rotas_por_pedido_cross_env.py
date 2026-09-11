"""Em 'ligado' sem empresa selecionada, agir num pedido funciona — na empresa dele.

O teste que protege a adocao e o de 'desligado'/'observando': 412, como hoje.
"""

from __future__ import annotations

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


def _grava(slug: str, ident: str) -> None:
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": ident,
                "source_filename": f"{ident}.pdf",
                "imported_at": "2026-09-11T10:00:00",
                "order_number": ident,
                "customer_name": "DAJU",
                "status": "success",
                "portal_status": "parsed",
            }
        )


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


def test_lote_fora_de_ligado_sem_cookie_continua_412(portal):
    """Sem cookie e sem 'ligado', o lote e o de hoje."""
    _grava("mm", "M1")
    roteamento_repo.set_modo(roteamento_repo.DESLIGADO, por="teste")
    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1"]})
    assert r.status_code == 412
