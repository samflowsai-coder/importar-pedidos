from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.flowpcp.config import FlowPCPConfig
from app.integrations.flowpcp.poll_decisoes import processar_decisao
from app.integrations.flowpcp.schema import DecisaoFlowPCP
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
    dry = FlowPCPConfig(
        enabled=True, base_url="x", service_token="t", tenant_id="mm", dry_run=True
    )
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=dry)
    assert calls["n"] == 0
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "data_atualizada"


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
