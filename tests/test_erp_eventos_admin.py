"""Rotas admin da SP1: /api/admin/environments/{id}/erp-base."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, erp_bases_repo, erp_estado_repo, router
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "fpe_" + "d" * 43


@pytest.fixture
def env_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="",
    )
    yield env["id"]
    db.set_db_path(None)
    db.reset_init_cache()


def _client():
    from app.web.server import app

    return TestClient(app)


def _payload(**kw):
    p = {
        "base": "mm",
        "papel": "produtora",
        "tenant_id": TENANT,
        "destino_url": "https://flow.test",
        "foto_hora": 23,
        "ativo": True,
        "token": TOKEN,
    }
    p.update(kw)
    return p


def test_salva_e_nunca_devolve_a_credencial(env_id):
    c = _client()
    r = c.put(f"/api/admin/environments/{env_id}/erp-base", json=_payload())
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
    corpo = c.get(f"/api/admin/environments/{env_id}/erp-base").json()
    assert corpo["config"]["tem_token"] is True and corpo["config"]["base"] == "mm"
    assert TOKEN not in str(corpo)


def test_config_invalida_vira_422(env_id):
    r = _client().put(f"/api/admin/environments/{env_id}/erp-base", json=_payload(papel="dona"))
    assert r.status_code == 422


def test_credencial_nova_tira_o_bloqueio_de_401_e_desbloquear_tira_os_outros(env_id):
    c = _client()
    c.put(f"/api/admin/environments/{env_id}/erp-base", json=_payload())
    with router.env_connect("mm") as conn:
        erp_estado_repo.bloquear(
            conn, "mm", "2026-10-09T13:00:00.000+00:00", motivo="credencial", http=401, erro=""
        )
    corpo = c.put(
        f"/api/admin/environments/{env_id}/erp-base", json=_payload(token="fpe_" + "e" * 43)
    ).json()
    assert corpo["envio"]["bloqueio"] is None
    with router.env_connect("mm") as conn:
        erp_estado_repo.bloquear(
            conn, "mm", "2026-10-09T13:00:00.000+00:00", motivo="lote_invalido", http=400, erro=""
        )
    corpo = c.post(f"/api/admin/environments/{env_id}/erp-base/desbloquear").json()
    assert corpo["envio"]["bloqueio"] is None


def test_ambiente_inexistente_404(env_id):
    assert _client().get("/api/admin/environments/nao-existe/erp-base").status_code == 404


# ── base_atendida: o predicado único de "a thread atende esta base" ───────────


def _salvar(env_id, **kw):
    args = dict(
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=23,
        ativo=True,
        token=TOKEN,
    )
    args.update(kw)
    erp_bases_repo.salvar(env_id, **args)
    return erp_bases_repo.obter(env_id)


def test_base_atendida_ok(env_id):
    assert erp_bases_repo.base_atendida(_salvar(env_id)) is None


def test_base_desligada(env_id):
    assert erp_bases_repo.base_atendida(_salvar(env_id, ativo=False)) == "desligada"


def test_ambiente_inativo(env_id):
    cfg = _salvar(env_id)
    environments_repo.soft_delete(env_id)
    assert erp_bases_repo.base_atendida(erp_bases_repo.obter(env_id)) == "desligada"
    assert cfg.ambiente_ativo is True


def test_sem_credencial(env_id):
    assert erp_bases_repo.base_atendida(_salvar(env_id, token="")) == "sem_credencial"


def test_sem_config_nao_e_atendida():
    assert erp_bases_repo.base_atendida(None) == "desligada"


def test_listar_ativas_concorda_com_base_atendida(env_id):
    # (base ativa, token, ambiente ativo): ok, desligada, sem credencial, ambiente inativo
    for ativo, token, amb_ativo in [
        (True, TOKEN, 1),
        (False, TOKEN, 1),
        (True, "", 1),
        (True, TOKEN, 0),
    ]:
        with router.shared_connect() as conn:
            conn.execute("UPDATE environments SET is_active = ? WHERE id = ?", (amb_ativo, env_id))
        cfg = _salvar(env_id, ativo=ativo, token=token)
        assert (cfg in erp_bases_repo.listar_ativas()) == (
            erp_bases_repo.base_atendida(cfg) is None
        )


# ── Task 7: consulta por pedido, painel e Enviar agora ───────────────────────

import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from app.erp_eventos import runner  # noqa: E402
from app.erp_eventos.canonico import formatar_instante  # noqa: E402

_ERRO_MANUAL = "pedido não encontrado (o portal pode ter reiniciado); veja Últimos envios"


@pytest.fixture
def manual_limpo(monkeypatch):
    monkeypatch.delenv("PORTAL_ERP_VIGIA", raising=False)  # a suíte desliga; aqui liga
    for d in (runner._PEDIDO_MANUAL, runner._EM_ANDAMENTO, runner._ATENDIDO_EM, runner._RESULTADOS):
        d.clear()
    yield
    for d in (runner._PEDIDO_MANUAL, runner._EM_ANDAMENTO, runner._ATENDIDO_EM, runner._RESULTADOS):
        d.clear()


def _url(env_id, resto=""):
    return f"/api/admin/environments/{env_id}/erp-base{resto}"


def _estado(codigo, pc="UY1"):
    js = {
        "erp_codigo": codigo,
        "pedido_cliente": pc,
        "cliente_erp_codigo": 7,
        "cliente_erp_cnpj": "11222333000144",
        "status": "PEDIDO",
        "itens": [{"erp_item_codigo": 1}],
    }
    with router.env_connect("mm") as conn:
        conn.execute(
            "INSERT INTO erp_pedido_estado (base, erp_codigo, situacao, estado_json, visto_em, "
            "atualizado_em) VALUES ('mm', ?, 'aberto', ?, '2026-10-20T10:00:00.000+00:00', "
            "'2026-10-20T10:00:00.000+00:00')",
            (codigo, json.dumps(js)),
        )


def _config(c, env_id, **kw):
    r = c.put(_url(env_id), json=_payload(**kw))
    assert r.status_code == 200, r.text


ROTAS = [
    ("get", "/pedidos?q=UY1"),
    ("get", "/pedidos/10"),
    ("get", "/painel"),
    ("post", "/enviar-agora"),
    ("get", "/envio-manual/x"),
]


@pytest.mark.parametrize(("metodo", "resto"), ROTAS)
def test_rotas_novas_exigem_admin(env_id, monkeypatch, metodo, resto):
    from app.persistence.users_repo import User
    from app.web import auth

    fake = User(1, "op@x.test", "operator", True, "2026-01-01", None, "h")
    monkeypatch.setattr(auth, "require_user", lambda *a, **k: fake)
    r = getattr(_client(), metodo)(_url(env_id, resto))
    assert r.status_code == 403


@pytest.mark.parametrize(("metodo", "resto"), ROTAS)
def test_rotas_novas_404_ambiente_e_base(env_id, metodo, resto):
    c = _client()
    assert getattr(c, metodo)(_url("nao-existe", resto)).status_code == 404
    # ambiente existe, mas sem base configurada
    assert getattr(c, metodo)(_url(env_id, resto)).status_code == 404


@pytest.mark.parametrize("q", ["", "   ", "x" * 41])
def test_busca_q_invalido_422(env_id, q):
    c = _client()
    _config(c, env_id)
    assert c.get(_url(env_id, "/pedidos"), params={"q": q}).status_code == 422
    assert c.get(_url(env_id, "/pedidos")).status_code == 422


def test_busca_um_resultado_traz_detalhe_achatado(env_id):
    c = _client()
    _config(c, env_id)
    _estado(10, pc="UY1")
    corpo = c.get(_url(env_id, "/pedidos"), params={"q": " UY1 "}).json()
    assert corpo["limitado"] is False and len(corpo["pedidos"]) == 1
    d = corpo["detalhe"]
    assert d["erp_codigo"] == 10 and d["pedido_cliente"] == "UY1"  # resumo no topo
    assert "resumo" not in d and d["eventos"] == [] and d["retidos"] is True


def test_busca_dois_resultados_sem_detalhe(env_id):
    c = _client()
    _config(c, env_id)
    _estado(10, pc="UY1")
    _estado(11, pc="UY1")
    corpo = c.get(_url(env_id, "/pedidos"), params={"q": "UY1"}).json()
    assert len(corpo["pedidos"]) == 2 and "detalhe" not in corpo


def test_detalhe_do_pedido_e_404_sem_estado(env_id):
    c = _client()
    _config(c, env_id)
    _estado(10)
    d = c.get(_url(env_id, "/pedidos/10")).json()
    assert d["erp_codigo"] == 10 and d["frase"] == "" and d["origem_pedido"] == "fire"
    assert c.get(_url(env_id, "/pedidos/99")).status_code == 404


def test_painel_sem_credencial_nao_pode(env_id, manual_limpo):
    c = _client()
    _config(c, env_id, token="")
    p = c.get(_url(env_id, "/painel")).json()
    assert p["envio_manual"] == {
        "pode": False,
        "motivo": "Base ligada sem credencial: salve a credencial na edição do ambiente.",
    }
    assert p["envios"] == [] and p["foto_hoje"] is None and p["bloqueio"] is None
    assert p["bloqueio_texto"] is None and p["recusados_30d"] == 0 and p["fila_pendente"] == 0
    assert p["proxima_janela"]


def test_painel_com_bloqueio_recusados_e_fila(env_id, manual_limpo):
    c = _client()
    _config(c, env_id)
    agora = datetime.now(UTC)
    with router.env_connect("mm") as conn:
        erp_estado_repo.bloquear(
            conn, "mm", formatar_instante(agora), motivo="lote_invalido", http=400, erro=""
        )
        ins = (
            "INSERT INTO erp_eventos_saida (id, base, erp_codigo, seq, tipo, unidade, payload_json,"
            " observado_em, situacao, respondido_em, criado_em) "
            "VALUES (?, 'mm', 1, ?, 'pedido_visto', 'u', '{}', ?, ?, ?, ?)"
        )
        t = formatar_instante(agora)
        velho = formatar_instante(agora - timedelta(days=31))
        conn.execute(ins, ("a", 1, t, "rejeitado", t, t))
        conn.execute(ins, ("b", 2, t, "rejeitado", velho, t))
        conn.execute(ins, ("c", 3, t, "pendente", None, t))
    p = c.get(_url(env_id, "/painel")).json()
    assert p["recusados_30d"] == 1 and p["fila_pendente"] == 1
    assert p["bloqueio"] == "lote_invalido"
    assert p["bloqueio_texto"].startswith("o Flow recusou um lote inteiro")
    assert p["envio_manual"]["pode"] is False and "Envio parado" in p["envio_manual"]["motivo"]


def test_painel_bloqueio_desconhecido_cai_no_http(env_id, manual_limpo):
    c = _client()
    _config(c, env_id)
    with router.env_connect("mm") as conn:
        erp_estado_repo.bloquear(
            conn, "mm", "2026-10-26T10:00:00.000+00:00", motivo="outro", http=418, erro=""
        )
    assert c.get(_url(env_id, "/painel")).json()["bloqueio_texto"] == "HTTP 418."


def test_painel_pode_enviar(env_id, manual_limpo):
    c = _client()
    _config(c, env_id)
    assert c.get(_url(env_id, "/painel")).json()["envio_manual"] == {"pode": True, "motivo": None}


def test_enviar_agora_202_e_acompanha(env_id, manual_limpo, monkeypatch):
    from app.erp import connection
    from app.erp_eventos import envio

    def explode(*a, **k):
        raise AssertionError("o POST não pode abrir conexão")

    monkeypatch.setattr(connection, "FirebirdConnection", explode)
    monkeypatch.setattr(runner, "FirebirdConnection", explode)
    monkeypatch.setattr(envio, "ClienteErpEventos", explode)
    monkeypatch.setattr(runner, "ClienteErpEventos", explode)
    c = _client()
    _config(c, env_id)
    r = c.post(_url(env_id, "/enviar-agora"))
    assert r.status_code == 202, r.text
    envio_id = r.json()["envio_id"]
    g = c.get(_url(env_id, f"/envio-manual/{envio_id}"))
    assert g.status_code == 200
    assert g.json()["situacao"] == "pendente" and g.json()["quem"]  # email do admin


def test_enviar_agora_409_sem_credencial(env_id, manual_limpo):
    c = _client()
    _config(c, env_id, token="")
    r = c.post(_url(env_id, "/enviar-agora"))
    assert r.status_code == 409
    assert r.json() == {
        "detail": "Base ligada sem credencial: salve a credencial na edição do ambiente.",
        "codigo": "sem_credencial",
    }


def test_envio_manual_desconhecido_404_com_texto_exato(env_id, manual_limpo):
    c = _client()
    _config(c, env_id)
    r = c.get(_url(env_id, "/envio-manual/nao-existe"))
    assert r.status_code == 404 and r.json() == {"detail": _ERRO_MANUAL}


def test_repo_rejeitados_desde(env_id):
    with router.env_connect("mm") as conn:
        for i, (sit, resp) in enumerate(
            [
                ("rejeitado", "2026-10-20T00:00:00.000+00:00"),
                ("rejeitado", "2026-09-01T00:00:00.000+00:00"),
                ("aceito", "2026-10-20T00:00:00.000+00:00"),
            ]
        ):
            conn.execute(
                "INSERT INTO erp_eventos_saida (id, base, erp_codigo, seq, tipo, unidade, payload_json, "
                "observado_em, situacao, respondido_em, criado_em) "
                "VALUES (?, 'mm', 1, ?, 'pedido_visto', 'u', '{}', 'x', ?, ?, 'x')",
                (f"i{i}", i, sit, resp),
            )
        assert erp_estado_repo.rejeitados_desde(conn, "mm", "2026-10-01T00:00:00.000+00:00") == 1


def test_pedido_com_codigo_gigante_422(env_id):
    c = _client()
    _config(c, env_id)
    assert c.get(_url(env_id, "/pedidos/99999999999999999999")).status_code == 422


def test_envio_de_outro_ambiente_da_404(env_id, manual_limpo, tmp_path):
    c = _client()
    _config(c, env_id)
    b = environments_repo.create(
        slug="outra",
        name="Outra",
        watch_dir=str(tmp_path / "w2"),
        output_dir=str(tmp_path / "o2"),
        fb_path="",
    )["id"]
    c.put(_url(b), json=_payload(base="outra"))
    envio_id = c.post(_url(b, "/enviar-agora")).json()["envio_id"]
    ok = c.get(_url(b, f"/envio-manual/{envio_id}"))
    assert ok.status_code == 200 and "environment_id" not in ok.json()
    r = c.get(_url(env_id, f"/envio-manual/{envio_id}"))
    assert r.status_code == 404 and r.json() == {"detail": _ERRO_MANUAL}
