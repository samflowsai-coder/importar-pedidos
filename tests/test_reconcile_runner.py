"""Runner da reconciliação: liga `buscar_no_fire` (Task 3) a `list_parsed_for_reconcile`
/ `mark_found_in_fire` (Task 4), com trava anti-corrida-no-mesmo-processo.

A trava (`_ULTIMA_EXECUCAO`) é um dict de módulo — persiste entre chamadas
DENTRO do mesmo processo de teste. Sem limpá-la a cada teste, um teste
anterior armaria a trava para "mm"/"quebrado" e um teste seguinte que espera
rodar de verdade seria bloqueado silenciosamente, dependendo da ordem de
execução. `_limpa_trava` (autouse) fecha esse buraco.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import context as env_context
from app.persistence import db, environments_repo, repo


def _entry(
    *,
    id: str,
    order_number: str,
    customer_cnpj: str | None = None,
) -> dict:
    return {
        "id": id,
        "source_filename": f"{id}.pdf",
        "imported_at": "2026-08-24T09:00:00",
        "order_number": order_number,
        "customer": "CLIENTE TESTE",
        "customer_cnpj": customer_cnpj,
        "snapshot": {
            "header": {"order_number": order_number, "customer_cnpj": customer_cnpj},
            "items": [],
        },
        "status": "success",
        "error": None,
        "portal_status": "parsed",
    }


@pytest.fixture(autouse=True)
def _limpa_trava():
    from app.reconcile import runner

    runner.limpar_trava()
    yield
    runner.limpar_trava()


@pytest.fixture
def env_db_com_import(tmp_path: Path):
    """Um ambiente ("mm") com um candidato elegível ("com-header")."""
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
    # Ambiente ativo passa a ser "mm" (não o "test" default de set_db_path) —
    # o corpo do teste lê/escreve no banco de "mm" sem precisar embrulhar
    # cada chamada num `with active_env(...)` próprio.
    env_context.set_active_env(env["id"], "mm")

    repo.insert_import(_entry(id="com-header", order_number="1001", customer_cnpj="00000000000100"))

    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def dois_ambientes(tmp_path: Path):
    """Dois ambientes, cada um com um candidato elegível — "quebrado" e "mm"."""
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()

    quebrado = environments_repo.create(
        slug="quebrado",
        name="Quebrado",
        watch_dir=str(tmp_path / "watch1"),
        output_dir=str(tmp_path / "out1"),
        fb_path="/nao/existe.fdb",
    )
    mm = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "watch2"),
        output_dir=str(tmp_path / "out2"),
        fb_path="/tmp/mm.fdb",
    )

    with env_context.active_env(quebrado["id"], "quebrado"):
        repo.insert_import(
            _entry(id="pend-quebrado", order_number="Q1", customer_cnpj="00000000000100")
        )
    with env_context.active_env(mm["id"], "mm"):
        repo.insert_import(_entry(id="pend-mm", order_number="M1", customer_cnpj="00000000000200"))

    yield
    db.set_db_path(None)
    db.reset_init_cache()


def test_ambiente_com_firebird_fora_nao_impede_os_outros(monkeypatch, dois_ambientes):
    """Um Firebird inalcançável não pode cancelar a varredura dos demais."""
    from app.reconcile import runner

    chamados = []

    def _busca(cands, *, env_slug):
        chamados.append(env_slug)
        if env_slug == "quebrado":
            raise RuntimeError("nunca deveria vazar até aqui")
        return {}

    monkeypatch.setattr(runner, "buscar_no_fire", _busca)
    runner.reconciliar("quebrado")
    r = runner.reconciliar("mm")
    assert "mm" in chamados
    assert r.erro_conexao is False


def test_dois_gatilhos_concorrentes_geram_um_evento(env_db_com_import, monkeypatch):
    """O CAS da Task 4 é quem fecha isso; aqui provamos ponta a ponta."""
    from app.erp.fire_reconcile import Achado
    from app.reconcile import runner
    from app.state import events as ev

    monkeypatch.setattr(
        runner,
        "buscar_no_fire",
        lambda cands, *, env_slug: {"com-header": Achado("com-header", 900, "PEDIDO", 2, 0)},
    )
    runner.reconciliar("mm", respeitar_trava=False)
    runner.reconciliar("mm", respeitar_trava=False)

    eventos = [e for e in ev.list_events("com-header") if e["event_type"] == "found_in_fire"]
    assert len(eventos) == 1


def test_trava_barra_a_segunda_execucao(env_db_com_import, monkeypatch):
    from app.reconcile import runner

    execucoes = []
    monkeypatch.setattr(
        runner,
        "buscar_no_fire",
        lambda cands, *, env_slug: execucoes.append(env_slug) or {},
    )
    runner.reconciliar("mm")
    runner.reconciliar("mm")
    assert len(execucoes) == 1


def test_botao_ignora_a_trava(env_db_com_import, monkeypatch):
    from app.reconcile import runner

    execucoes = []
    monkeypatch.setattr(
        runner,
        "buscar_no_fire",
        lambda cands, *, env_slug: execucoes.append(env_slug) or {},
    )
    runner.reconciliar("mm")
    runner.reconciliar("mm", respeitar_trava=False)
    assert len(execucoes) == 2


def test_erro_de_conexao_real_e_reportado_no_resultado(env_db_com_import, monkeypatch):
    """`erro_conexao` tem que vir de um sinal de verdade (falha de conexão),
    não de "achados veio vazio" — um Firebird fora e "nada casou porque não
    achou" são achados idênticos (`{}`) e a operadora precisa distingui-los.

    Usa o `buscar_no_fire` REAL (não mocka `runner.buscar_no_fire`) contra uma
    conexão que falha de propósito, do mesmo jeito que
    `tests/test_fire_reconcile.py::test_firebird_fora_devolve_vazio_sem_levantar`
    prova no nível de baixo — aqui provamos que o sinal atravessa até o
    `Resultado` do runner.
    """
    from app.erp import fire_reconcile
    from app.erp.connection import FirebirdConnection
    from app.reconcile import runner

    fire_reconcile.limpar_cache()

    def _connect_falha(self, cfg):
        raise RuntimeError("host inalcançável")

    monkeypatch.setattr(FirebirdConnection, "connect_with_config", _connect_falha)

    try:
        r = runner.reconciliar("mm")
        assert r.erro_conexao is True
    finally:
        fire_reconcile.limpar_cache()


# ── Gatilho periódico (07h/12h/18h local) — só a parte pura, testável ──


def test_proxima_janela_mesmo_dia():
    from datetime import datetime

    from app.reconcile.runner import proxima_janela

    agora = datetime(2026, 8, 24, 9, 30)
    assert proxima_janela(agora) == datetime(2026, 8, 24, 12, 0)


def test_proxima_janela_vira_o_dia():
    from datetime import datetime

    from app.reconcile.runner import proxima_janela

    agora = datetime(2026, 8, 24, 19, 0)
    assert proxima_janela(agora) == datetime(2026, 8, 25, 7, 0)


def test_proxima_janela_no_minuto_exato_nao_repete():
    """`agora` == 12h00 em ponto não pode devolver 12h00 de novo (senão o
    loop periódico dispararia duas vezes seguidas sem dormir)."""
    from datetime import datetime

    from app.reconcile.runner import proxima_janela

    agora = datetime(2026, 8, 24, 12, 0)
    assert proxima_janela(agora) == datetime(2026, 8, 24, 18, 0)
