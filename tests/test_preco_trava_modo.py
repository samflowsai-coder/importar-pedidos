"""Interruptor da trava de preço por ambiente (desligado | observando | ligado)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield tmp_path


@pytest.fixture
def env(fresh_shared):
    return environments_repo.create(
        slug="mm", name="MM", watch_dir="/tmp/in", output_dir="/tmp/out", fb_path="/tmp/x.fdb"
    )


def test_ambiente_novo_nasce_desligado(env):
    assert env["preco_trava_modo"] == "desligado"
    assert env["preco_fonte_slug"] is None


def test_set_modo_invalido_recusa_sem_mudar(env):
    with pytest.raises(ValueError):
        environments_repo.set_preco_trava_modo(env["id"], "talvez", por="x")
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "desligado"


def test_set_modo_ligado_aparece_no_get_by_slug(env):
    environments_repo.set_preco_trava_modo(env["id"], "ligado", por="admin@x.com")
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "ligado"
    environments_repo.set_preco_trava_modo(env["id"], "observando", por="admin@x.com")
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "observando"


def test_banco_antigo_migra_coluna(env):
    # Simula banco anterior à feature: tira as colunas novas e força nova migração.
    with router.shared_connect() as conn:
        conn.execute("ALTER TABLE environments DROP COLUMN preco_trava_modo")
        conn.execute("ALTER TABLE environments DROP COLUMN preco_fonte_slug")
    router.reset_init_cache()

    lido = environments_repo.get_by_slug("mm")
    assert lido["preco_trava_modo"] == "desligado"
    assert lido["preco_fonte_slug"] is None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()


def _novo_env(setup):
    return environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )


def test_rota_admin_troca_modo(setup):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "observando"})
    assert r.status_code == 200
    assert r.json()["preco_trava_modo"] == "observando"
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "observando"

    assert (
        c.patch(
            f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "talvez"}
        ).status_code
        == 422
    )
    assert (
        c.patch(
            "/api/admin/environments/nao-existe/preco-trava", json={"modo": "ligado"}
        ).status_code
        == 404
    )


def test_rota_operador_e_403(setup, real_auth):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    c.post(
        "/api/admin/users", json={"email": "op@x.com", "password": "operpass1", "role": "operator"}
    )
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "ligado"})
    assert r.status_code == 403
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "desligado"


def _sync_ok(slug):
    from datetime import UTC, datetime

    from app.persistence import precos_repo

    with router.env_connect(slug) as conn:
        precos_repo.aplicar_pagina(
            conn,
            {
                "schema": "precos.tabela.v1",
                "capacidades": ["precos.tabela.v1", "precos.aprovacao.v1"],
                "geradoEm": datetime.now(UTC).isoformat(),
                "empresaFaturadoraCnpj": "11111111000191",
                "reiniciar": False,
                "mudancas": [],
                "cursor": "c1",
                "temMais": False,
            },
            empresa_cnpj="11111111000191",
        )


def test_rota_recusa_ligar_sem_tabela_sincronizada(setup):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "ligado"})

    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Não dá para ligar: a tabela de preço desta empresa ainda não foi sincronizada com o Flow."
    )
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "desligado"


def test_rota_liga_depois_da_primeira_sync(setup):
    from app.web.server import app

    e = _novo_env(setup)
    _sync_ok("mm")
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "ligado"})

    assert r.status_code == 200, r.text
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "ligado"


@pytest.mark.parametrize("modo", ["observando", "desligado"])
def test_rota_observando_e_desligado_sem_sync(setup, modo):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": modo})

    assert r.status_code == 200
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == modo


def test_hint_do_observando_avisa_da_planilha_adiada():
    from pathlib import Path

    html = (
        Path(__file__).parent.parent / "app" / "web" / "static" / "admin-ambiente-edit.html"
    ).read_text(encoding="utf-8")
    assert (
        "Observando: avalia e mostra; não bloqueia o envio, mas a planilha da importação "
        "passa a sair só pela revisão."
    ) in html


# ── Task 7.5: ambiente fonte da chave do produto (preco_fonte_slug) ──────────


def _dois_ambientes(setup, *, flow_na_mm=True):
    mm = _novo_env(setup)
    nasmar = environments_repo.create(
        slug="nasmar",
        name="Nasmar",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "n.fdb"),
    )
    if flow_na_mm:
        environments_repo.set_flowpcp_config(
            mm["id"],
            enabled=True,
            base_url="https://flow.exemplo",
            tenant_id="t1",
            service_token="tok",
        )
    return mm, nasmar


def test_set_fonte_aponta_para_ambiente_com_flow(setup):
    _, nasmar = _dois_ambientes(setup)
    environments_repo.set_preco_fonte_slug(nasmar["id"], " MM ", por="admin@x.com")
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] == "mm"


@pytest.mark.parametrize("valor", [None, "", "  ", "nasmar"])
def test_set_fonte_vazio_ou_o_proprio_vira_none(setup, valor):
    _, nasmar = _dois_ambientes(setup)
    environments_repo.set_preco_fonte_slug(nasmar["id"], "mm", por="x")
    environments_repo.set_preco_fonte_slug(nasmar["id"], valor, por="x")
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] is None


def test_set_fonte_inexistente_recusa_sem_mudar(setup):
    _, nasmar = _dois_ambientes(setup)
    with pytest.raises(ValueError):
        environments_repo.set_preco_fonte_slug(nasmar["id"], "nao-existe", por="x")
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] is None


def test_set_fonte_sem_config_do_flow_recusa(setup):
    _, nasmar = _dois_ambientes(setup, flow_na_mm=False)
    with pytest.raises(ValueError):
        environments_repo.set_preco_fonte_slug(nasmar["id"], "mm", por="x")
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] is None


def test_rota_grava_a_fonte_junto_com_o_modo(setup):
    from app.web.server import app

    _, nasmar = _dois_ambientes(setup)
    c = TestClient(app)
    url = f"/api/admin/environments/{nasmar['id']}/preco-trava"

    r = c.patch(url, json={"modo": "observando", "preco_fonte_slug": "mm"})
    assert r.status_code == 200, r.text
    assert r.json()["preco_fonte_slug"] == "mm"

    # Campo ausente: a fonte fica como está.
    assert c.patch(url, json={"modo": "desligado"}).status_code == 200
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] == "mm"

    # Inválida: 422 e nada muda (nem o modo).
    r = c.patch(url, json={"modo": "observando", "preco_fonte_slug": "nao-existe"})
    assert r.status_code == 422
    lido = environments_repo.get_by_slug("nasmar")
    assert (lido["preco_trava_modo"], lido["preco_fonte_slug"]) == ("desligado", "mm")

    r = c.patch(url, json={"modo": "desligado", "preco_fonte_slug": None})
    assert r.status_code == 200
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] is None


def test_cartao_da_trava_tem_o_select_da_fonte():
    from pathlib import Path

    html = (
        Path(__file__).parent.parent / "app" / "web" / "static" / "admin-ambiente-edit.html"
    ).read_text(encoding="utf-8")
    assert 'id="preco-fonte-slug"' in html
    assert "preco_fonte_slug:" in html


# ── Task 7.5 fix 1 ───────────────────────────────────────────────────────────


def test_desligar_com_fonte_salva_que_perdeu_o_flow_passa(setup):
    from app.web.server import app

    mm, nasmar = _dois_ambientes(setup)
    environments_repo.set_preco_fonte_slug(nasmar["id"], "mm", por="x")
    environments_repo.set_flowpcp_config(mm["id"], enabled=False, base_url=None, tenant_id=None)
    c = TestClient(app)

    r = c.patch(
        f"/api/admin/environments/{nasmar['id']}/preco-trava",
        json={"modo": "desligado", "preco_fonte_slug": "mm"},
    )

    assert r.status_code == 200, r.text
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] == "mm"


def test_set_fonte_inativa_recusa(setup):
    mm, nasmar = _dois_ambientes(setup)
    environments_repo.soft_delete(mm["id"])
    with pytest.raises(ValueError):
        environments_repo.set_preco_fonte_slug(nasmar["id"], "mm", por="x")
    assert environments_repo.get_by_slug("nasmar")["preco_fonte_slug"] is None


def test_set_fonte_que_tem_fonte_propria_recusa(setup):
    mm, nasmar = _dois_ambientes(setup)
    environments_repo.set_flowpcp_config(
        nasmar["id"], enabled=True, base_url="https://f", tenant_id="t2", service_token="t"
    )
    environments_repo.set_preco_fonte_slug(nasmar["id"], "mm", por="x")
    terceiro = environments_repo.create(
        slug="terceiro", name="T", watch_dir=str(setup), output_dir=str(setup), fb_path=""
    )
    with pytest.raises(ValueError):
        environments_repo.set_preco_fonte_slug(terceiro["id"], "nasmar", por="x")
    assert environments_repo.get_by_slug("terceiro")["preco_fonte_slug"] is None


# ── Reenviar bloqueados localmente (pendência 2) ────────────────────────────


def _revisao_bloqueada(slug, imp):
    from datetime import UTC, datetime

    from app.persistence import preco_revisao_repo as revs

    agora = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    with router.env_connect(slug) as conn:
        revs.garantir_revisao(conn, imp, {"externalId": imp}, f"sha256:{imp}", agora=agora)
        revs.marcar_bloqueio_local(conn, imp, 1, "pedido grande demais", em=agora)


def test_get_do_ambiente_traz_a_contagem_de_bloqueadas_localmente(setup):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    assert c.get(f"/api/admin/environments/{e['id']}").json()["preco_bloqueadas_local"] == 0
    _revisao_bloqueada("mm", "imp-a")
    _revisao_bloqueada("mm", "imp-b")
    assert c.get(f"/api/admin/environments/{e['id']}").json()["preco_bloqueadas_local"] == 2


def test_rota_desbloquear_limpa_e_conta(setup):
    from app.persistence import preco_revisao_repo as revs
    from app.utils.logger import logger
    from app.web.server import app

    e = _novo_env(setup)
    _revisao_bloqueada("mm", "imp-a")
    _revisao_bloqueada("mm", "imp-b")
    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="INFO")
    try:
        c = TestClient(app)
        r = c.post(f"/api/admin/environments/{e['id']}/preco-trava/desbloquear")
    finally:
        logger.remove(sink)
    assert r.status_code == 200, r.text
    assert r.json() == {"desbloqueadas": 2}
    with router.env_connect("mm") as conn:
        assert len(revs.pendentes_envio(conn, 10)) == 2
    assert any("desbloque" in m and "env=mm" in m and "por=" in m for m in msgs)
    assert c.post(f"/api/admin/environments/{e['id']}/preco-trava/desbloquear").json() == {
        "desbloqueadas": 0
    }
    assert c.post("/api/admin/environments/nao-existe/preco-trava/desbloquear").status_code == 404


def test_rota_desbloquear_operador_e_403(setup, real_auth):
    from app.web.server import app

    e = _novo_env(setup)
    _revisao_bloqueada("mm", "imp-a")
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    c.post(
        "/api/admin/users", json={"email": "op@x.com", "password": "operpass1", "role": "operator"}
    )
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    r = c.post(f"/api/admin/environments/{e['id']}/preco-trava/desbloquear")
    assert r.status_code == 403
    with router.env_connect("mm") as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM preco_revisao WHERE bloqueio_local IS NOT NULL"
        ).fetchone()[0]
    assert n == 1


def test_cartao_da_trava_tem_o_reenviar_bloqueados():
    from pathlib import Path

    html = (
        Path(__file__).parent.parent / "app" / "web" / "static" / "admin-ambiente-edit.html"
    ).read_text(encoding="utf-8")
    assert 'id="btn-preco-desbloquear"' in html
    assert "/preco-trava/desbloquear" in html
    assert "preco_bloqueadas_local" in html
