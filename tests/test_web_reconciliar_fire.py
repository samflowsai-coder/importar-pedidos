"""Botão manual de reconciliação: `POST /api/imported/reconciliar-fire`.

Exige sessão (`require_user`) e ambiente ativo (cookie `portal_env`) — o
botão só existe na UI depois que o operador já selecionou um ambiente.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo


@pytest.fixture
def client(tmp_path: Path):
    """TestClient com bypass de auth (padrão do arquivo) + ambiente ativo."""
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()

    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "watch"),
        output_dir=str(tmp_path / "out"),
        fb_path="",
    )

    from app.web.server import app

    c = TestClient(app)
    c.cookies.set("portal_env", env["id"])
    yield c

    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def client_sem_auth(tmp_path: Path, real_auth):
    """TestClient sem sessão — exercita `require_user` de verdade (401/403)."""
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()

    from app.web.server import app

    c = TestClient(app)
    yield c

    db.set_db_path(None)
    db.reset_init_cache()


def test_rota_exige_autenticacao(client_sem_auth):
    r = client_sem_auth.post("/api/imported/reconciliar-fire")
    assert r.status_code in (401, 403)


def test_rota_devolve_o_resultado(client, monkeypatch):
    from app.reconcile.runner import Resultado
    from app.web import server

    monkeypatch.setattr(server, "reconciliar", lambda slug, **kw: Resultado(12, 5, False))
    body = client.post("/api/imported/reconciliar-fire").json()
    assert body == {"verificados": 12, "casaram": 5, "erro_conexao": False}


def test_firebird_fora_nao_vira_zero_silencioso(client, monkeypatch):
    """Sem isto a Grazi vê '0 encontrados' e conclui que quebrou."""
    from app.reconcile.runner import Resultado
    from app.web import server

    monkeypatch.setattr(server, "reconciliar", lambda slug, **kw: Resultado(12, 0, True))
    body = client.post("/api/imported/reconciliar-fire").json()
    assert body["erro_conexao"] is True


def test_startup_dispara_thread_daemon_do_loop_periodico(monkeypatch):
    """`scripts/setup-service.ps1` só registra `ui.py` no Windows do cliente
    — o worker nunca roda lá. Sem essa thread no startup do processo web, o
    gatilho periódico nunca dispararia em produção. Substitui
    `loop_periodico` por um stub (a versão real dorme para sempre) só para
    confirmar que o startup dispara *alguma* thread daemon com esse alvo —
    não deixa o loop de verdade rodar durante o teste."""
    import threading

    from app.web import server

    chamado = threading.Event()
    monkeypatch.setattr(server, "loop_periodico", chamado.set)

    with TestClient(server.app):
        assert chamado.wait(timeout=2.0)


def test_entrada_do_operador_reconcilia_o_ambiente_novo_nao_o_antigo(tmp_path: Path, monkeypatch):
    """`POST /api/env/select` dispara a reconciliação em background para o
    ambiente RECÉM-selecionado — não para o que estava ativo no cookie da
    requisição. O contexto do request ainda aponta pro ambiente anterior
    quando a tarefa de fundo roda; só resolver pelo slug passado (em vez de
    herdar o contexto ambiente) evita reconciliar a empresa errada."""
    from app.web import routes_env_select
    from app.web.server import app

    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    try:
        antigo = environments_repo.create(
            slug="antigo",
            name="Antigo",
            watch_dir=str(tmp_path / "w1"),
            output_dir=str(tmp_path / "o1"),
            fb_path="",
        )
        novo = environments_repo.create(
            slug="novo",
            name="Novo",
            watch_dir=str(tmp_path / "w2"),
            output_dir=str(tmp_path / "o2"),
            fb_path="",
        )

        chamados = []
        monkeypatch.setattr(
            routes_env_select, "reconciliar", lambda slug, **kw: chamados.append(slug)
        )

        c = TestClient(app)
        c.cookies.set("portal_env", antigo["id"])  # ambiente ativo ANTES da troca
        r = c.post("/api/env/select", json={"environment_id": novo["id"]})

        assert r.status_code == 200
        assert chamados == ["novo"]
    finally:
        db.set_db_path(None)
        db.reset_init_cache()
