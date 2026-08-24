"""Runner da reconciliação: liga `_buscar_no_fire_detalhado` (Task 3) a
`list_parsed_for_reconcile` / `mark_found_in_fire` (Task 4), com trava
anti-corrida-no-mesmo-processo e lock por-ambiente (fix round 1).

A trava (`_ULTIMA_EXECUCAO`) e os locks (`_LOCKS`) são dicts de módulo —
persistem entre chamadas DENTRO do mesmo processo de teste. Sem limpá-los a
cada teste, um teste anterior armaria a trava (ou deixaria um lock
travado por uma exceção não tratada) para "mm"/"quebrado", e um teste
seguinte que espera rodar de verdade seria bloqueado silenciosamente,
dependendo da ordem de execução. `_limpa_trava` (autouse) fecha esse buraco.
"""

from __future__ import annotations

import threading
import time
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
        return {}, False

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", _busca)
    runner.reconciliar("quebrado")
    r = runner.reconciliar("mm")
    assert "mm" in chamados
    assert r.status == "ok"


def test_dois_gatilhos_concorrentes_geram_um_evento(env_db_com_import, monkeypatch):
    """O CAS da Task 4 é quem fecha isso; aqui provamos ponta a ponta."""
    from app.erp.fire_reconcile import Achado
    from app.reconcile import runner
    from app.state import events as ev

    monkeypatch.setattr(
        runner,
        "_buscar_no_fire_detalhado",
        lambda cands, *, env_slug: (
            {"com-header": Achado("com-header", 900, "PEDIDO", 2, 0)},
            False,
        ),
    )
    runner.reconciliar("mm", respeitar_trava=False)
    runner.reconciliar("mm", respeitar_trava=False)

    eventos = [e for e in ev.list_events("com-header") if e["event_type"] == "found_in_fire"]
    assert len(eventos) == 1


def test_trava_barra_a_segunda_execucao(env_db_com_import, monkeypatch):
    from app.reconcile import runner

    execucoes = []

    def _busca(cands, *, env_slug):
        execucoes.append(env_slug)
        return {}, False

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", _busca)
    runner.reconciliar("mm")
    r2 = runner.reconciliar("mm")
    assert len(execucoes) == 1
    assert r2.status == "trava_ativa"


def test_botao_ignora_a_trava(env_db_com_import, monkeypatch):
    from app.reconcile import runner

    execucoes = []

    def _busca(cands, *, env_slug):
        execucoes.append(env_slug)
        return {}, False

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", _busca)
    runner.reconciliar("mm")
    runner.reconciliar("mm", respeitar_trava=False)
    assert len(execucoes) == 2


def test_erro_de_conexao_real_e_reportado_no_resultado(env_db_com_import, monkeypatch):
    """`status="erro_conexao"` tem que vir de um sinal de verdade (falha de
    conexão), não de "achados veio vazio" — um Firebird fora e "nada casou
    porque não achou" são achados idênticos (`{}`) e a operadora precisa
    distingui-los.

    Usa o `buscar_no_fire` REAL (não mocka `runner._buscar_no_fire_detalhado`)
    contra uma conexão que falha de propósito, do mesmo jeito que
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
        assert r.status == "erro_conexao"
    finally:
        fire_reconcile.limpar_cache()


def test_erro_de_leitura_real_tambem_vira_erro_conexao_no_resultado(
    env_db_com_import, monkeypatch
):
    """Zero silencioso pela 3ª porta (achado em teste real de navegador,
    2026-08-24): a conexão funcionava, a LEITURA que falhava (`invalid
    database handle`), e mesmo assim o botão manual devolvia `status="ok"`
    com `casaram=0` — a operadora lia "consultei o Fire: nenhum pedido está
    cadastrado lá ainda", que é mentira, a consulta nem completou.

    Usa o `_buscar_no_fire_detalhado` REAL (não mocka o runner) contra uma
    conexão que CONECTA mas falha ao ler, espelhando
    `tests/test_fire_reconcile.py::test_detalhado_leitura_ruim_tem_erro_conexao_true`
    — aqui provamos que o sinal atravessa até o `Resultado` do runner e
    nunca vira `"ok"`.
    """
    from app.erp import fire_reconcile
    from app.erp.connection import FirebirdConnection
    from app.reconcile import runner

    class _FakeCursorRuim:
        def execute(self, sql, params=None):
            raise RuntimeError("invalid database handle (no active connection)")

        def close(self):
            pass

    class _FakeConnRuim:
        def cursor(self):
            return _FakeCursorRuim()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fire_reconcile.limpar_cache()
    monkeypatch.setattr(
        FirebirdConnection, "connect_with_config", lambda self, cfg: _FakeConnRuim()
    )

    try:
        r = runner.reconciliar("mm", respeitar_trava=False)
        assert r.status == "erro_conexao"
        assert r.status != "ok"
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


# ── Fix round 1 (reviewer): trava é rate-limit, não mutex — lock por-ambiente ──


def test_duas_chamadas_concorrentes_do_mesmo_ambiente_uma_so_roda(env_db_com_import, monkeypatch):
    """Duplo clique no botão (ou o botão sobrepondo a entrada do operador,
    que ignora a trava de propósito) não pode disparar dois scans completos
    do mesmo ambiente ao mesmo tempo. A segunda chamada devolve NA HORA —
    sem esperar a primeira, que seguem uma thread real presa até ser
    liberada."""
    from app.reconcile import runner

    entrou = threading.Event()
    liberar = threading.Event()

    def _busca_lenta(cands, *, env_slug):
        entrou.set()
        assert liberar.wait(timeout=2.0), "a 2ª chamada nunca liberou a 1ª"
        return {}, False

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", _busca_lenta)

    resultado_primeira: list = []

    def _primeira():
        # Threads novas não herdam a ContextVar de ambiente ativado no
        # fixture — mas `reconciliar()` resolve e ativa o ambiente pelo
        # slug ele mesmo (é exatamente a garantia provada em
        # tests/test_web_reconciliar_fire.py para o gatilho de entrada).
        resultado_primeira.append(runner.reconciliar("mm", respeitar_trava=False))

    t = threading.Thread(target=_primeira)
    t.start()
    assert entrou.wait(timeout=2.0), "a 1ª chamada nunca chegou a rodar"

    inicio = time.monotonic()
    r2 = runner.reconciliar("mm", respeitar_trava=False)
    duracao = time.monotonic() - inicio

    liberar.set()
    t.join(timeout=2.0)

    assert duracao < 1.0, "a 2ª chamada esperou a 1ª em vez de devolver na hora"
    # Coordenador, complemento fix round 1: "já em execução" é a MESMA
    # família de zero silencioso que erro_conexao existe pra evitar — sem um
    # status próprio, r2 pareceria "rodou, achou zero" quando na real não
    # rodou nada.
    assert r2 == runner.Resultado(verificados=0, casaram=0, status="em_execucao")
    assert resultado_primeira[0].status == "ok"


def test_lock_e_liberado_apos_excecao_para_a_proxima_chamada_rodar(env_db_com_import, monkeypatch):
    """Uma reconciliação que estoura exceção (já capturada e virada
    Resultado dentro de `reconciliar()`) não pode deixar o lock preso —
    senão o ambiente fica permanentemente bloqueado até reiniciar o
    processo."""
    from app.reconcile import runner

    def _explode(cands, *, env_slug):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", _explode)
    r1 = runner.reconciliar("mm", respeitar_trava=False)
    assert r1.status == "erro_conexao"

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", lambda cands, *, env_slug: ({}, False))
    r2 = runner.reconciliar("mm", respeitar_trava=False)
    assert r2.status == "ok"


# ── Fix round 1 (reviewer): guard PORTAL_RECONCILE_PERIODICO ──


def test_periodico_desligado_via_env_var_nao_lista_ambientes(monkeypatch):
    from app.reconcile import runner

    monkeypatch.setenv("PORTAL_RECONCILE_PERIODICO", "0")

    def _boom():
        raise AssertionError("não deveria listar ambientes com o periódico desligado")

    monkeypatch.setattr(runner.router, "list_env_slugs", _boom)
    runner.reconciliar_todos_os_ambientes()  # não deve levantar nem chamar list_env_slugs


def test_periodico_habilitado_por_padrao_reconcilia_todos(monkeypatch):
    from app.reconcile import runner

    monkeypatch.setenv("PORTAL_RECONCILE_PERIODICO", "1")
    chamados = []
    monkeypatch.setattr(runner, "reconciliar", lambda slug, **kw: chamados.append(slug))
    monkeypatch.setattr(runner.router, "list_env_slugs", lambda: ["mm", "nasmar"])
    runner.reconciliar_todos_os_ambientes()
    assert chamados == ["mm", "nasmar"]


def test_periodico_ausente_do_env_mantem_habilitado(monkeypatch):
    """Só "0" explícito desliga — omitir a variável não pode desligar por
    acidente em produção (só nos testes, via default do conftest)."""
    from app.reconcile import runner

    monkeypatch.delenv("PORTAL_RECONCILE_PERIODICO", raising=False)
    chamados = []
    monkeypatch.setattr(runner, "reconciliar", lambda slug, **kw: chamados.append(slug))
    monkeypatch.setattr(runner.router, "list_env_slugs", lambda: ["mm"])
    runner.reconciliar_todos_os_ambientes()
    assert chamados == ["mm"]


# ── Fix round 1 (reviewer): Gauge de observabilidade por ambiente ──


def test_gauge_last_run_ok_reflete_a_ultima_corrida_real(env_db_com_import, monkeypatch):
    from app.observability.metrics import reconcile_fire_last_run_ok
    from app.reconcile import runner

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", lambda cands, *, env_slug: ({}, False))
    runner.reconciliar("mm", respeitar_trava=False)
    assert reconcile_fire_last_run_ok.labels(environment="mm")._value.get() == 1.0

    monkeypatch.setattr(runner, "_buscar_no_fire_detalhado", lambda cands, *, env_slug: ({}, True))
    runner.reconciliar("mm", respeitar_trava=False)
    assert reconcile_fire_last_run_ok.labels(environment="mm")._value.get() == 0.0
