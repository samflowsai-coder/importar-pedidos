"""Candidatos e gravação da reconciliação.

O ponto sensível é a idempotência: web e worker são processos distintos e
`transition()` lê o estado FORA da transação de escrita. Sem compare-and-set,
dois gatilhos simultâneos gravam o evento duas vezes no log canônico.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.persistence import context as env_context
from app.persistence import db, repo


def _import_entry(
    *,
    id: str,
    order_number: str,
    portal_status: str,
    customer_cnpj: str | None = None,
    fire_codigo: int | None = None,
    items: list[dict] | None = None,
) -> dict:
    return {
        "id": id,
        "source_filename": f"{id}.pdf",
        "imported_at": "2026-08-24T09:00:00",
        "order_number": order_number,
        "customer": "CLIENTE TESTE",
        "customer_cnpj": customer_cnpj,
        "fire_codigo": fire_codigo,
        "snapshot": {
            "header": {"order_number": order_number, "customer_cnpj": customer_cnpj},
            "items": items or [],
        },
        "status": "success",
        "error": None,
        "portal_status": portal_status,
    }


@pytest.fixture
def env_db_com_import(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()

    repo.insert_import(
        _import_entry(
            id="com-header",
            order_number="1001",
            portal_status="parsed",
            customer_cnpj="00000000000100",
        )
    )
    repo.insert_import(
        _import_entry(
            id="riachuelo",
            order_number="6702645869",
            portal_status="parsed",
            customer_cnpj=None,
            items=[
                {"description": "ITEM 1", "delivery_cnpj": "11111111000111"},
                {"description": "ITEM 2", "delivery_cnpj": "22222222000122"},
                {"description": "ITEM 3", "delivery_cnpj": "33333333000133"},
            ],
        )
    )
    repo.insert_import(
        _import_entry(
            id="sem-identidade",
            order_number="1003",
            portal_status="parsed",
            customer_cnpj=None,
            items=[{"description": "ITEM SEM CNPJ"}],
        )
    )
    repo.insert_import(
        _import_entry(
            id="ja-no-fire",
            order_number="1004",
            portal_status="sent_to_fire",
            customer_cnpj="00000000000199",
            fire_codigo=555,
        )
    )

    yield
    db.set_db_path(None)
    db.reset_init_cache()


def test_candidato_com_cnpj_de_header_e_elegivel(env_db_com_import):
    cands = repo.list_parsed_for_reconcile()
    ids = {c.import_id for c in cands}
    assert "com-header" in ids


def test_candidato_riachuelo_sem_header_e_elegivel_pelos_cnpjs_de_entrega(
    env_db_com_import,
):
    """Este é o caso dos 308: sem CNPJ no header, com CNPJ por loja nos itens."""
    cand = next(c for c in repo.list_parsed_for_reconcile() if c.import_id == "riachuelo")
    assert cand.cnpj_header is None
    assert len(cand.cnpjs_entrega) == 3


def test_pedido_sem_nenhuma_identidade_nao_e_candidato(env_db_com_import):
    ids = {c.import_id for c in repo.list_parsed_for_reconcile()}
    assert "sem-identidade" not in ids


def test_pedido_ja_no_fire_nao_e_candidato(env_db_com_import):
    ids = {c.import_id for c in repo.list_parsed_for_reconcile()}
    assert "ja-no-fire" not in ids


def test_marca_e_grava_as_quatro_colunas(env_db_com_import):
    ok = repo.mark_found_in_fire(
        "com-header",
        fire_codigo=900,
        fire_status="PEDIDO",
        caminho=2,
        lojas_casadas=0,
        at="2026-08-24T12:00:00Z",
    )
    assert ok is True
    row = repo.get_import("com-header")
    assert row["portal_status"] == "found_in_fire"
    assert row["fire_codigo"] == 900
    assert row["fire_status_last_seen"] == "PEDIDO"


def test_evento_found_in_fire_carrega_pedido_cliente_e_caminho_match(env_db_com_import):
    """Spec (Modelo de dados): o payload do evento tem que carregar
    `pedido_cliente` (o número do pedido, não o `import_id` interno) e
    `caminho_match` — sem o número, auditar o log exige join com `imports`."""
    from app.state import events as ev

    repo.mark_found_in_fire(
        "com-header",
        fire_codigo=900,
        fire_status="PEDIDO",
        caminho=2,
        lojas_casadas=0,
        at="2026-08-24T12:00:00Z",
    )

    eventos = [e for e in ev.list_events("com-header") if e["event_type"] == "found_in_fire"]
    assert len(eventos) == 1
    payload = eventos[0]["payload"]
    assert payload["pedido_cliente"] == "1001"  # order_number de "com-header" na fixture
    assert payload["caminho_match"] == 2
    assert "caminho" not in payload  # renomeado, não duplicado
    assert payload["fire_codigo"] == 900
    assert payload["fire_status"] == "PEDIDO"
    assert payload["lojas_casadas"] == 0


def test_marca_incrementa_state_version_e_derruba_expected_stale(env_db_com_import):
    """`mark_found_in_fire` muda `portal_status` por fora do `transition()`.
    Sem bumpar `state_version` na MESMA UPDATE do compare-and-set, um worker
    que leu a versão antes da reconciliação (ex.: o poll de status do Fire,
    que faz `transition(..., expected_state_version=...)`) não teria como
    perceber que o estado mudou por baixo dele — a próxima escrita dele
    aplicaria sobre um pressuposto errado ("ainda parsed") em vez de ser
    rejeitada.
    """
    from app.state.events import StaleStateError, transition
    from app.state.machine import EventSource, LifecycleEvent

    versao_antes = repo.get_import("com-header")["state_version"]

    ok = repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:00Z",
    )
    assert ok is True

    versao_depois = repo.get_import("com-header")["state_version"]
    assert versao_depois > versao_antes

    # Cenário natural: o poll de status do Fire leu a versão ANTES da
    # reconciliação e só agora tenta gravar um FIRE_STATUS_CHANGED — tem que
    # ser rejeitado, não aplicado silenciosamente sobre estado obsoleto.
    with pytest.raises(StaleStateError):
        transition(
            "com-header",
            LifecycleEvent.FIRE_STATUS_CHANGED,
            source=EventSource.FIRE,
            expected_state_version=versao_antes,
        )

    # E a versão atual (pós-reconciliação) segue funcionando normalmente.
    transition(
        "com-header",
        LifecycleEvent.FIRE_STATUS_CHANGED,
        source=EventSource.FIRE,
        expected_state_version=versao_depois,
    )


def test_segunda_marcacao_perde_a_corrida_e_nao_duplica_evento(env_db_com_import):
    """Chamada sequencial: NÃO prova ausência de corrida real entre threads/processos —
    só prova que a segunda chamada, feita depois que a primeira já comitou, encontra
    `portal_status != 'parsed'` e desiste sem gravar evento. A prova de corrida de
    verdade está em `test_concorrencia_real_duas_threads_uma_so_grava_evento` abaixo.
    """
    primeira = repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:00Z",
    )
    segunda = repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:05Z",
    )
    assert primeira is True
    assert segunda is False
    from app.state import events as ev

    eventos = [e for e in ev.list_events("com-header") if e["event_type"] == "found_in_fire"]
    assert len(eventos) == 1


def test_concorrencia_real_duas_threads_uma_so_grava_evento(env_db_com_import):
    """Prova de verdade: duas threads de SO, cada uma com sua própria conexão
    sqlite3 (via `db.connect()`), chamando `mark_found_in_fire` para o MESMO
    `import_id` ao mesmo tempo, sincronizadas por uma barreira para maximizar a
    chance de as duas UPDATEs concorrerem pelo write-lock do SQLite.

    Isto é diferente do teste sequencial acima: aqui as duas chamadas competem
    de verdade (thread real, conexão real, banco em disco real, sem mock de
    nenhuma camada) — não é a mesma chamada invocada duas vezes em ordem. O
    `UPDATE ... WHERE portal_status = 'parsed'` é quem serializa: o SQLite só
    deixa uma transação de escrita prosseguir por vez, e quando a segunda
    finalmente escreve, a condição do WHERE já não bate mais (a primeira já
    comitou 'found_in_fire'), então `rowcount == 0` para ela.

    ContextVar não atravessa thread nova sozinha — cada thread precisa ativar
    o ambiente de novo antes de chamar o repo, senão cai em
    `NoActiveEnvironmentError`.
    """
    ambiente = env_context.current()
    barreira = threading.Barrier(2)
    resultados: list[bool] = []
    erros: list[BaseException] = []
    lock = threading.Lock()

    def _tentar(at: str) -> None:
        try:
            env_context.set_active_env(ambiente["id"], ambiente["slug"])
            barreira.wait(timeout=5)
            ok = repo.mark_found_in_fire(
                "com-header",
                fire_codigo=900,
                fire_status="PEDIDO",
                caminho=2,
                lojas_casadas=0,
                at=at,
            )
            with lock:
                resultados.append(ok)
        except BaseException as exc:  # noqa: BLE001 — thread: repassa pro teste
            with lock:
                erros.append(exc)

    t1 = threading.Thread(target=_tentar, args=("2026-08-24T12:00:00Z",))
    t2 = threading.Thread(target=_tentar, args=("2026-08-24T12:00:01Z",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not erros, f"thread(s) levantaram: {erros}"
    assert sorted(resultados) == [False, True]

    row = repo.get_import("com-header")
    assert row["portal_status"] == "found_in_fire"

    from app.state import events as ev

    eventos = [e for e in ev.list_events("com-header") if e["event_type"] == "found_in_fire"]
    assert len(eventos) == 1


def test_janela_do_poll_ancora_na_reconciliacao_nao_no_ultimo_poll(env_db_com_import):
    """Regressão: `update_fire_poll_result` recarimba `fire_status_polled_at`
    a CADA poll (mudando de status ou não), e a janela usava esse carimbo
    como âncora (`COALESCE(fire_status_polled_at, imported_at)`) — uma linha
    reconciliada uma vez nunca mais saía da janela de 7 dias, porque o
    próprio poll renovava a âncora que deveria fazê-la expirar. A âncora
    certa é o momento fixo da reconciliação: `reconciled_at`, gravado uma
    única vez em `mark_found_in_fire` e nunca tocado por `update_fire_poll_result`.
    """
    reconciliado_em = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    ok = repo.mark_found_in_fire(
        "com-header",
        fire_codigo=900,
        fire_status="PEDIDO",
        caminho=2,
        lojas_casadas=0,
        at=reconciliado_em,
    )
    assert ok is True

    # Poll recente, sem mudança de status — se a âncora fosse
    # `fire_status_polled_at`, a linha continuaria (erradamente) dentro da
    # janela para sempre, não importa há quanto tempo foi reconciliada.
    repo.update_fire_poll_result("com-header", "PEDIDO", datetime.now(UTC).isoformat())

    pendentes = {e["id"] for e in repo.list_pending_for_fire_poll(window_days=7)}
    assert "com-header" not in pendentes


def test_filtro_aceita_lista_de_status(env_db_com_import):
    repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:00Z",
    )
    linhas = repo.list_imports(portal_status=["sent_to_fire", "found_in_fire"])
    ids = {r["id"] for r in linhas}
    assert {"com-header", "ja-no-fire"} <= ids
