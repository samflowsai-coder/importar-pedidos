"""Interruptor `produto_chave_modo` (desligado | observando | ligado) e o portão `avaliar`."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.erp import produto_trava
from app.erp.produto_trava import TravaProduto, avaliar, modo
from app.persistence import db, environments_repo, router


def _item(estado: str = "ok", motivo: str | None = None) -> dict:
    candidatos = [{"seq": 1, "codprod_altern": "C1"}] if estado == "ok" else []
    return {
        "produto": {
            "estado": estado,
            "motivo": motivo,
            "fire_seq": 1,
            "candidatos": candidatos,
            "troca_no_fire": None,
        }
    }


def _check(*estados, available: bool = True, incompleta: bool = False) -> dict:
    itens = [_item(e, None if e == "ok" else f"motivo-{e}") for e in estados]
    out = {"available": available, "items": itens}
    if incompleta:
        out["produto_conferencia_incompleta"] = True
    return out


def _env(m: str | None) -> dict:
    return {"slug": "mm", "produto_chave_modo": m}


# --- modo ---------------------------------------------------------------


@pytest.mark.parametrize("env", [None, {}, {"produto_chave_modo": None}, _env("talvez")])
def test_modo_padrao_desligado(env):
    assert modo(env) == "desligado"


@pytest.mark.parametrize("m", ["desligado", "observando", "ligado"])
def test_modo_valido_passa(m):
    assert modo(_env(m)) == m


# --- avaliar ------------------------------------------------------------


def test_desligado_nunca_trava_nem_lista():
    r = avaliar(_check("ausente", available=False), env=_env("desligado"), export_mode="db")
    assert r == TravaProduto(bloqueado=False, reason=None, itens=[])
    assert avaliar(_check("ausente"), env=None, export_mode="db").bloqueado is False


def test_ligado_trava_item_pendente():
    r = avaliar(_check("ok", "ambiguo", "ausente"), env=_env("ligado"), export_mode="db")
    assert r.bloqueado is True
    assert r.reason == "produto_a_identificar"
    assert r.itens == [
        {"index": 1, "estado": "ambiguo", "motivo": "motivo-ambiguo"},
        {"index": 2, "estado": "ausente", "motivo": "motivo-ausente"},
    ]


def test_ligado_item_sem_produto_conta_como_nao_ok():
    check = {"available": True, "items": [_item("ok"), {"descricao": "sem decisão"}]}
    r = avaliar(check, env=_env("ligado"), export_mode="db")
    assert r.bloqueado is True and r.reason == "produto_a_identificar"
    assert [i["index"] for i in r.itens] == [1]
    assert r.itens[0]["estado"] != "ok"


def test_ligado_check_sem_available_e_fail_closed():
    r = avaliar({"items": [_item("ok")]}, env=_env("ligado"), export_mode="db")
    assert r.bloqueado is True and r.reason == "fire_indisponivel"


def test_ligado_tudo_ok_libera():
    r = avaliar(_check("ok", "ok"), env=_env("ligado"), export_mode="db")
    assert r == TravaProduto(bloqueado=False, reason=None, itens=[])


def test_ligado_trava_fire_indisponivel():
    r = avaliar(
        _check("fire_indisponivel", "fire_indisponivel", available=False),
        env=_env("ligado"),
        export_mode="db",
    )
    assert r.bloqueado is True
    assert r.reason == "fire_indisponivel"
    assert len(r.itens) == 2


def test_ligado_trava_conferencia_incompleta_mesmo_com_itens_ok():
    r = avaliar(_check("ok", incompleta=True), env=_env("ligado"), export_mode="db")
    assert r.bloqueado is True
    assert r.reason == "fire_indisponivel"


def test_conferencia_incompleta_tem_precedencia_sobre_produto_a_identificar():
    r = avaliar(_check("ausente", incompleta=True), env=_env("ligado"), export_mode="db")
    assert r.reason == "fire_indisponivel"
    assert len(r.itens) == 1


def test_observando_nao_trava_mas_lista():
    r = avaliar(_check("ok", "ausente"), env=_env("observando"), export_mode="db")
    assert r.bloqueado is False
    assert r.reason is None
    assert r.itens == [{"index": 1, "estado": "ausente", "motivo": "motivo-ausente"}]


def test_observando_fire_indisponivel_nao_trava():
    r = avaliar(
        _check("fire_indisponivel", available=False), env=_env("observando"), export_mode="xlsx"
    )
    assert r.bloqueado is False
    assert len(r.itens) == 1


def test_export_mode_e_aceito():
    for em in ("xlsx", "db", "both"):
        assert avaliar(_check("ok"), env=_env("ligado"), export_mode=em).bloqueado is False


def test_modulo_exporta_interface():
    assert hasattr(produto_trava, "avaliar") and hasattr(produto_trava, "TravaProduto")


# --- repo ---------------------------------------------------------------


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
    assert env["produto_chave_modo"] == "desligado"


def test_set_modo_invalido_recusa_sem_mudar(env):
    with pytest.raises(ValueError):
        environments_repo.set_produto_chave_modo(env["id"], "talvez", por="x")
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "desligado"


def test_set_modo_aparece_no_get_get_by_slug_e_list(env):
    environments_repo.set_produto_chave_modo(env["id"], "ligado", por="admin@x.com")
    assert environments_repo.get(env["id"])["produto_chave_modo"] == "ligado"
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "ligado"
    assert environments_repo.list_all()[0]["produto_chave_modo"] == "ligado"
    environments_repo.set_produto_chave_modo(env["id"], "observando", por="admin@x.com")
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "observando"


def _audit_modo():
    with router.shared_connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT environment_id, campo, valor_antigo, valor_novo, alterado_por, "
                "alterado_em IS NOT NULL FROM ambiente_modo_audit ORDER BY id"
            ).fetchall()
        ]


def test_troca_de_modo_grava_audit_com_antes_depois_quem_e_quando(env):
    """M5: a troca do interruptor é auditável (não só log)."""
    environments_repo.set_produto_chave_modo(env["id"], "observando", por="admin@x.com")
    environments_repo.set_produto_chave_modo(env["id"], "ligado", por="outro@x.com")
    assert _audit_modo() == [
        (env["id"], "produto_chave_modo", "desligado", "observando", "admin@x.com", 1),
        (env["id"], "produto_chave_modo", "observando", "ligado", "outro@x.com", 1),
    ]


def test_modo_invalido_nao_grava_audit(env):
    with pytest.raises(ValueError):
        environments_repo.set_produto_chave_modo(env["id"], "talvez", por="x")
    assert _audit_modo() == []


def test_banco_antigo_migra_coluna(env):
    with router.shared_connect() as conn:
        conn.execute("ALTER TABLE environments DROP COLUMN produto_chave_modo")
    router.reset_init_cache()
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "desligado"


def test_modo_do_env_do_repo_alimenta_avaliar(env):
    environments_repo.set_produto_chave_modo(env["id"], "ligado", por="t")
    e = environments_repo.get_by_slug("mm")
    assert avaliar(_check("ausente"), env=e, export_mode="db").bloqueado is True


# --- rota ---------------------------------------------------------------


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


def test_patch_produto_chave_admin_troca_modo(setup):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/produto-chave", json={"modo": "observando"})
    assert r.status_code == 200
    assert r.json()["produto_chave_modo"] == "observando"
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "observando"
    assert (
        c.patch("/api/admin/environments/nao-existe/produto-chave", json={"modo": "ligado"})
    ).status_code == 404


def test_patch_produto_chave_valor_invalido_422(setup):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/produto-chave", json={"modo": "talvez"})
    assert r.status_code == 422
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "desligado"


def test_patch_produto_chave_admin_only(setup, real_auth):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    c.post(
        "/api/admin/users", json={"email": "op@x.com", "password": "operpass1", "role": "operator"}
    )
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    r = c.patch(f"/api/admin/environments/{e['id']}/produto-chave", json={"modo": "ligado"})
    assert r.status_code == 403
    assert environments_repo.get_by_slug("mm")["produto_chave_modo"] == "desligado"
