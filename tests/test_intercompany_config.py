"""Config do de-para de cliente intercompany (colunas + repo + rota)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


@pytest.fixture
def env(fresh_shared):
    return environments_repo.create(
        slug="mm", name="MM", watch_dir="/tmp/in", output_dir="/tmp/out", fb_path="/tmp/x.fdb"
    )


def test_default_e_desligado(env):
    assert env["intercompany_cnpj"] is None
    assert env["intercompany_env_slug"] is None


def test_grava_e_le_config(env):
    atualizado = environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    assert atualizado["intercompany_cnpj"] == "34513679000134"  # normalizado p/ dígitos
    assert atualizado["intercompany_env_slug"] == "nasmar"


def test_limpar_desliga(env):
    environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    atualizado = environments_repo.set_intercompany_config(env["id"], cnpj="", revenda_slug="")
    assert atualizado["intercompany_cnpj"] is None
    assert atualizado["intercompany_env_slug"] is None


def test_persiste_no_get(env):
    environments_repo.set_intercompany_config(
        env["id"], cnpj="34513679000134", revenda_slug="nasmar"
    )
    lido = environments_repo.get(env["id"])
    assert lido["intercompany_cnpj"] == "34513679000134"
    assert lido["intercompany_env_slug"] == "nasmar"


def test_set_intercompany_config_limpa_cache_do_resolver(env, monkeypatch):
    """Achado 5c: sem isso, trocar o Firebird da revenda (path/host) deixa o
    cache POSITIVO de `resolver_cliente_real` com o vínculo antigo pela vida
    do processo — ninguém chama `limpar_cache()` em produção."""
    import app.erp.depara_cliente as dc

    chamou = []
    monkeypatch.setattr(dc, "limpar_cache", lambda: chamou.append(1))

    environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )

    assert chamou == [1]


# ── Rota PUT /api/admin/environments/{id}/intercompany ──────────────────────


@pytest.fixture
def setup_rotas(tmp_path: Path):
    import os

    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _client():
    from app.web.server import app

    return TestClient(app)


def test_rota_grava_intercompany(setup_rotas):
    criado = (
        _client()
        .post(
            "/api/admin/environments",
            json={
                "slug": "mm",
                "name": "MM",
                "watch_dir": str(setup_rotas / "in"),
                "output_dir": str(setup_rotas / "out"),
                "fb_path": str(setup_rotas / "x.fdb"),
            },
        )
        .json()
    )
    r = _client().put(
        f"/api/admin/environments/{criado['id']}/intercompany",
        json={"cnpj": "34.513.679/0001-34", "revenda_slug": "nasmar"},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_cnpj"] == "34513679000134"
    assert r.json()["intercompany_env_slug"] == "nasmar"


def test_rota_404_em_ambiente_inexistente(setup_rotas):
    r = _client().put(
        "/api/admin/environments/nao-existe/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": "nasmar"},
    )
    assert r.status_code == 404


def test_rota_grava_slug_de_ambiente_existente(setup_rotas):
    """Trava só a metade da rota do achado da Task 2: prova que o round-trip da
    rota preserva o slug exatamente como veio (aqui, o de um ambiente real).
    NÃO cobre o <select> nem a degradação da lista — isso passaria igual com
    qualquer string, inclusive digitada à mão; quem fecha o achado de verdade é
    o guard de HTML/JS abaixo (`test_intercompany_select_preserva_slug_...`)."""
    c = _client()
    mm = c.post(
        "/api/admin/environments",
        json={
            "slug": "mm",
            "name": "MM",
            "watch_dir": str(setup_rotas / "in"),
            "output_dir": str(setup_rotas / "out"),
            "fb_path": str(setup_rotas / "x.fdb"),
        },
    ).json()
    nasmar = c.post(
        "/api/admin/environments",
        json={
            "slug": "nasmar",
            "name": "Nasmar",
            "watch_dir": str(setup_rotas / "in2"),
            "output_dir": str(setup_rotas / "out2"),
            "fb_path": str(setup_rotas / "y.fdb"),
        },
    ).json()
    r = c.put(
        f"/api/admin/environments/{mm['id']}/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": nasmar["slug"]},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_env_slug"] == nasmar["slug"] == "nasmar"


def test_rota_normaliza_case_do_slug(setup_rotas):
    """Defesa mínima na rota: mesmo que algo escape do <select> (edição manual do
    payload), o slug é normalizado pra minúsculas antes de gravar — slugs de
    ambiente são sempre lowercase (SLUG_RE)."""
    criado = (
        _client()
        .post(
            "/api/admin/environments",
            json={
                "slug": "mm",
                "name": "MM",
                "watch_dir": str(setup_rotas / "in"),
                "output_dir": str(setup_rotas / "out"),
                "fb_path": str(setup_rotas / "x.fdb"),
            },
        )
        .json()
    )
    r = _client().put(
        f"/api/admin/environments/{criado['id']}/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": "NASMAR"},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_env_slug"] == "nasmar"


# ── Guarda do <select> na tela de edição (app/web/static/admin-ambiente-edit.html) ──
#
# Precedente: tests/test_admin_ambiente_edit_clientes.py, criado depois de um
# bug real onde o payload do "Salvar FlowPCP" zerava um gate em silêncio por
# faltar um campo. Achado da revisão desta task: o mesmo tipo de bug existe
# aqui — se o fetch da lista de ambientes falhar (ou o ambiente salvo tiver
# sumido da lista), o <select> caía pra "— desligado —" sem aviso, e o próximo
# "Salvar de-para de cliente" gravava revenda_slug=null por cima de um vínculo
# funcionando. Os trechos abaixo travam a correção: falha vira `null` (nunca
# `[]`, que se confunde com "lista vazia de verdade"), o slug salvo sempre
# ganha uma <option> própria quando não está na lista, e a falha aparece no
# status — nunca em silêncio.

_HTML = (
    Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "admin-ambiente-edit.html"
).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "trecho",
    [
        # falha do fetch vira null, não [] — "não carregou" != "carregou vazia"
        "fetch('/api/admin/environments').then(r => r.ok ? r.json() : null).catch(() => null),",
        "const listaCarregou = Array.isArray(allEnvs);",
        # CRÍTICO: o slug salvo sempre vira <option> selecionável, mesmo que a
        # lista tenha falhado ou não contenha mais aquele ambiente — sem isso
        # sel.value cai em "" (selectedIndex=-1) e o próximo save zera o vínculo
        "if (slugSalvo && !lista.some(e => e.slug === slugSalvo)) {",
        "sel.value = slugSalvo;",
        # aviso visível — nunca falha em silêncio
        "Lista de ambientes indisponível",
    ],
)
def test_intercompany_select_preserva_slug_quando_lista_falha(trecho):
    assert trecho in _HTML, f"trecho ausente na tela de edição: {trecho!r}"


# ── Achado 5b da revisão: fetch do "Salvar de-para de cliente" sem catch ──
#
# Sem catch, um fetch que rejeita (offline, DNS, CORS) propaga pro handler do
# click sem tratamento: o botão reabilita (finally) mas o status nunca ganha
# texto de erro — parece que nada aconteceu. Os handlers `rodarSync*` no
# mesmo arquivo já tratam isso; o de intercompany precisa do mesmo padrão.


def test_save_intercompany_trata_fetch_rejeitado():
    # Âncora no addEventListener do botão (não na primeira ocorrência do id,
    # que é a <button> no HTML) — pega só o corpo do handler de click.
    inicio = _HTML.index("document.getElementById('btn-save-intercompany').addEventListener")
    bloco = _HTML[inicio : inicio + 1200]
    assert "} catch (err) {" in bloco
    assert "err.message" in bloco
