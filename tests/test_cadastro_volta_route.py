"""Rota GET /{env_id}/flowpcp/cadastro-volta."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import cadastro_volta_repo, db, environments_repo, router


@pytest.fixture
def setup(tmp_path: Path):
    import os

    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _get(env_id):
    from app.web.server import app

    return TestClient(app).get(f"/api/admin/environments/{env_id}/flowpcp/cadastro-volta")


def _env(setup):
    return environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )


def test_404(setup):
    assert _get("nao-existe").status_code == 404


def test_livro_vazio(setup, monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_ENVIO", raising=False)
    r = _get(_env(setup)["id"])
    assert r.status_code == 200
    assert r.json() == {
        "modo": "desligado",
        "por_estado": {},
        "ultimos": [],
        "ultimo_ciclo": None,
        "thread_desligada": False,
        "p0_ok": False,
        "inativar_ligado": False,
        "sem_volta": 0,
        "n_orfaos": 0,
        "n_incertos": 0,
        "orfaos": [],
        "incertos": [],
        "n_aguardando_recuperacao": 0,
        "aguardando_recuperacao": [],
        "sem_volta_itens": [],
    }


def test_ultimo_ciclo_e_thread_desligada(setup, monkeypatch):
    from app.integrations.flowpcp import cadastro_volta

    env = _env(setup)
    resumo = {"modo": "simular", "lidos": 3, "capacidade_ok": False, "erro": None}
    monkeypatch.setattr(
        cadastro_volta,
        "_ultimo_ciclo",
        {"mm": {"em": "2026-10-20T12:05:00+00:00", "resumo": resumo}},
    )
    monkeypatch.setenv("PORTAL_FLOWPCP_ENVIO", "0")
    body = _get(env["id"]).json()
    assert body["ultimo_ciclo"] == {"em": "2026-10-20T12:05:00+00:00", "resumo": resumo}
    assert body["thread_desligada"] is True


def _linha(conn, cid, estado, seq=None, iniciado="2026-01-01 00:00:00"):
    assert cadastro_volta_repo.reservar(
        conn, cadastro_id=cid, versao=1, produto_id=f"p-{cid}", acao="criar", tipo="simples",
        modo="criar", plano_json="{}",
    )
    conn.execute(
        "UPDATE cadastro_volta SET estado = ?, seq_reservado = ?, iniciado_em = ?, "
        "corpo_json = 'SEGREDO-CORPO', nota_operador = 'SEGREDO-NOTA' WHERE cadastro_id = ?",
        (estado, seq, iniciado, cid),
    )


def test_listas_de_atencao_e_portoes(setup, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CADASTRO_P0", "mm")
    monkeypatch.delenv("PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR", raising=False)
    env = _env(setup)
    with router.env_connect("mm") as conn:
        _linha(conn, "o1", "orfao", 3992)
        _linha(conn, "i1", "incerto")
        _linha(conn, "t1", "transitorio", 3993)  # aguarda a recuperação: conta como incerto
        _linha(conn, "t2", "transitorio")  # sem SEQ: só reentrega, não é alerta
        _linha(conn, "s1", "gravado_sem_volta", 3994)
        _linha(conn, "s2", "erro_a_enviar")
        _linha(conn, "c1", "confirmado", 3995)
    r = _get(env["id"])
    body = r.json()
    assert body["p0_ok"] is True and body["inativar_ligado"] is False
    assert body["sem_volta"] == 2 and body["n_orfaos"] == 1 and body["n_incertos"] == 2
    assert [x["cadastro_id"] for x in body["orfaos"]] == ["o1"]
    assert {x["cadastro_id"] for x in body["incertos"]} == {"i1", "t1"}
    assert {x["cadastro_id"] for x in body["sem_volta_itens"]} == {"s1", "s2"}
    for lista in ("orfaos", "incertos", "sem_volta_itens"):
        for x in body[lista]:
            assert set(x) == {"cadastro_id", "versao", "acao", "erp_produto_id",
                              "seq_reservado", "tentativas", "alerta", "atualizado_em"}
    assert "SEGREDO" not in r.text


def test_transitorio_recente_vai_para_lista_propria_e_fora_dos_incertos(setup):
    from datetime import UTC, datetime, timedelta

    agora = datetime.now(UTC)
    fmt = "%Y-%m-%d %H:%M:%S"
    env = _env(setup)
    with router.env_connect("mm") as conn:
        _linha(conn, "novo", "transitorio", 3993, iniciado=(agora - timedelta(minutes=2)).strftime(fmt))
        _linha(conn, "velho", "transitorio", 3994, iniciado=(agora - timedelta(minutes=30)).strftime(fmt))
    body = _get(env["id"]).json()
    assert body["n_incertos"] == 1 and body["n_aguardando_recuperacao"] == 1
    assert [x["cadastro_id"] for x in body["incertos"]] == ["velho"]
    assert [x["cadastro_id"] for x in body["aguardando_recuperacao"]] == ["novo"]


def test_incertos_com_mais_de_50_ficam_coerentes_com_a_contagem(setup):
    from datetime import UTC, datetime, timedelta

    agora = datetime.now(UTC)
    fmt = "%Y-%m-%d %H:%M:%S"
    env = _env(setup)
    with router.env_connect("mm") as conn:
        for i in range(55):
            _linha(conn, f"i{i:02d}", "incerto")
        for i in range(3):  # recentes: não entram na lista nem na contagem
            _linha(conn, f"r{i}", "transitorio", 100 + i,
                   iniciado=(agora - timedelta(minutes=1)).strftime(fmt))
    body = _get(env["id"]).json()
    assert body["n_incertos"] == 55 and len(body["incertos"]) == 50
    assert not any(x["cadastro_id"].startswith("r") for x in body["incertos"])
    assert body["n_aguardando_recuperacao"] == 3 and len(body["aguardando_recuperacao"]) == 3


def test_listas_de_atencao_nao_vazam_corpo_nem_nota(setup):
    env = _env(setup)
    with router.env_connect("mm") as conn:
        _linha(conn, "o1", "orfao", 3992)
    body = _get(env["id"]).json()
    assert "corpo_json" not in str(body["orfaos"]) and "SEGREDO" not in str(body["orfaos"])


def test_duas_linhas_com_plano_truncado(setup):
    env = _env(setup)
    with router.env_connect("mm") as conn:
        for i, plano in enumerate(['{"a":1}', "x" * 5000]):
            cadastro_volta_repo.registrar(
                conn,
                cadastro_id=f"id{i}",
                versao=1,
                estado="simulado",
                acao="criar",
                tipo="kit",
                erro_codigo=None,
                erro_mensagem=None,
                plano_json=plano,
            )
    body = _get(env["id"]).json()
    assert body["por_estado"] == {"simulado": 2}
    por_id = {u["cadastro_id"]: u for u in body["ultimos"]}
    assert por_id["id0"]["plano_truncado"] is False
    assert por_id["id1"]["plano_truncado"] is True and len(por_id["id1"]["plano_json"]) == 2048


def test_plano_truncado_por_bytes_com_acento(setup):
    env = _env(setup)
    with router.env_connect("mm") as conn:
        cadastro_volta_repo.registrar(
            conn,
            cadastro_id="id0",
            versao=1,
            estado="simulado",
            acao="criar",
            tipo="kit",
            erro_codigo=None,
            erro_mensagem=None,
            plano_json="ç" * 1500,
        )
    (linha,) = _get(env["id"]).json()["ultimos"]
    assert linha["plano_truncado"] is True
    assert len(linha["plano_json"].encode("utf-8")) <= 2048


def test_sem_login_e_401(setup, real_auth):
    assert _get(_env(setup)["id"]).status_code == 401


def test_operador_logado_e_403(setup, real_auth):
    from app.web.server import app

    env = _env(setup)
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    c.post(
        "/api/admin/users", json={"email": "op@x.com", "password": "operpass1", "role": "operator"}
    )
    c.post("/api/auth/logout")
    assert (
        c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"}).status_code
        == 200
    )
    assert c.get(f"/api/admin/environments/{env['id']}/flowpcp/cadastro-volta").status_code == 403
