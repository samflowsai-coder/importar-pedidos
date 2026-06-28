from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.flowpcp import poll_decisoes
from app.integrations.flowpcp.config import FlowPCPConfig
from app.integrations.flowpcp.poll_decisoes import poll_decisoes_once, processar_decisao
from app.integrations.flowpcp.schema import DecisaoFlowPCP, DecisoesResponse
from app.persistence import flowpcp_repo
from app.persistence.schema_env import TABLES_SQL

CFG = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm")


def _decisao(**over):
    base = dict(
        id="dec-1",
        pedido_erp="AW097",
        cliente_cnpj="123",
        nome_cliente="MM",
        prazo_entrega_original="2026-07-10T03:00:00.000Z",
        prazo_pactuado="2026-07-17T03:00:00.000Z",
        status="em_pool",
        motivo_decisao="negociado",
        atualizado_em="2026-06-22T14:00:00.000Z",
    )
    base.update(over)
    return DecisaoFlowPCP(**base)


def test_rejeitado_confirma_cancelamento_sem_tocar_fire(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    fire = MagicMock()
    processar_decisao(
        _decisao(status="rejeitado", prazo_pactuado=None),
        client=client,
        fire_conn=fire,
        conn=tmp_env_db,
        config=CFG,
    )
    acao = client.confirmar_reconciliacao.call_args.args[1].acao.value
    assert acao == "cancelamento_pendente_manual"
    fire.cursor.assert_not_called()


def test_sem_mudanca_confirma_sem_acao(tmp_env_db):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    processar_decisao(
        _decisao(prazo_pactuado="2026-07-10T03:00:00.000Z"),
        client=client,
        fire_conn=MagicMock(),
        conn=tmp_env_db,
        config=CFG,
    )
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "sem_acao_necessaria"


def test_data_nova_atualiza_fire_e_confirma(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    monkeypatch.setattr(
        "app.integrations.flowpcp.poll_decisoes.update_dt_entrega",
        lambda *a, **k: 1,
    )
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "data_atualizada"


def test_dry_run_nao_chama_update(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    calls = {"n": 0}
    monkeypatch.setattr(
        "app.integrations.flowpcp.poll_decisoes.update_dt_entrega",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or 1,
    )
    dry = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm", dry_run=True)
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=dry)
    assert calls["n"] == 0
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "data_atualizada"


def test_poll_nao_avanca_cursor_se_decisao_do_meio_falha(tmp_env_db, monkeypatch):
    """Bug do cursor skip: se uma decisão NÃO-última do lote não confirma, avançar
    o cursor para proximo_cursor a deixaria atrás do watermark `atualizado_em >=`
    do Flow para sempre — perda silenciosa. O cursor não pode avançar."""
    tmp_env_db.executescript(TABLES_SQL)
    d_falha = _decisao(id="dec-falha", pedido_erp="FAIL", atualizado_em="2026-06-22T14:00:00.000Z")
    d_ok = _decisao(id="dec-ok", pedido_erp="OK", atualizado_em="2026-06-22T14:05:00.000Z")
    client = MagicMock()
    client.list_decisoes.return_value = DecisoesResponse(
        decisoes=[d_falha, d_ok], proximo_cursor="2026-06-22T14:05:00.000Z"
    )

    # FAIL → 0 rows (não encontrado, não confirma ainda); OK → 1 row (confirma).
    monkeypatch.setattr(
        poll_decisoes,
        "update_dt_entrega",
        lambda *a, **k: 1 if k.get("pedido_cliente") == "OK" else 0,
    )

    poll_decisoes_once(client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)

    assert flowpcp_repo.get_last_cursor(tmp_env_db) is None


def test_poll_avanca_cursor_quando_todas_confirmadas(tmp_env_db, monkeypatch):
    """Contrapartida: com o lote inteiro confirmado, o cursor avança normalmente."""
    tmp_env_db.executescript(TABLES_SQL)
    d1 = _decisao(id="d1", pedido_erp="A", atualizado_em="2026-06-22T14:00:00.000Z")
    d2 = _decisao(id="d2", pedido_erp="B", atualizado_em="2026-06-22T14:05:00.000Z")
    client = MagicMock()
    client.list_decisoes.return_value = DecisoesResponse(
        decisoes=[d1, d2], proximo_cursor="2026-06-22T14:05:00.000Z"
    )
    monkeypatch.setattr(poll_decisoes, "update_dt_entrega", lambda *a, **k: 1)

    poll_decisoes_once(client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)

    assert flowpcp_repo.get_last_cursor(tmp_env_db) == "2026-06-22T14:05:00.000Z"


def test_nao_encontrado_incrementa_e_confirma_apos_5(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    monkeypatch.setattr(
        "app.integrations.flowpcp.poll_decisoes.update_dt_entrega",
        lambda *a, **k: 0,
    )
    for _ in range(4):
        processar_decisao(
            _decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG
        )
    assert client.confirmar_reconciliacao.call_count == 0  # ainda tentando
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)
    assert (
        client.confirmar_reconciliacao.call_args.args[1].acao.value
        == "pedido_nao_encontrado_no_fire"
    )
