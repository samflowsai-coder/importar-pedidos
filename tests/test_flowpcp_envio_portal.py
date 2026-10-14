"""Envio ao FlowPCP de dentro do processo web: fila, fila acumulada, interruptor das decisões."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.integrations.flowpcp import envio_portal
from app.integrations.flowpcp.config import FlowPCPConfig
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo

AGORA = datetime(2026, 10, 15, 10, 0, 0)


def _payload_recebimento(import_id: str) -> dict:
    from app.integrations.flowpcp.schema import (
        ClienteRecebimento,
        ItemRecebimento,
        OrigemRecebimento,
        RecebimentoRequest,
    )

    return RecebimentoRequest(
        externalId=import_id,
        fornecedor="MM",
        pedidoNumero="AW097",
        emitidoEm="2026-10-15T00:00:00.000Z",
        cliente=ClienteRecebimento(nome="MM", cnpj="123"),
        itens=[ItemRecebimento(descricao="meia", quantidade=10)],
        origem=OrigemRecebimento(
            importadorVersao="1.0.0",
            arquivoOriginal="p.pdf",
            parserUsado="t",
            confiancaParser="alta",
        ),
    ).model_dump(by_alias=True)


CFG = FlowPCPConfig(enabled=True, base_url="https://flow.test", service_token="t", tenant_id="mm")


@pytest.fixture
def env(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    e = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="x",
    )
    with env_context.active_env(e["id"], e["slug"]):
        yield e
    db.set_db_path(None)
    db.reset_init_cache()


def pedido(import_id: str, *, dias: float, fire_codigo: int | None = None) -> None:
    """Import + linha da fila criada `dias` antes de AGORA."""
    repo.insert_import(
        {
            "id": import_id,
            "source_filename": f"{import_id}.pdf",
            "imported_at": AGORA.isoformat(),
            "fire_codigo": fire_codigo,
        }
    )
    row = outbox_repo.enqueue(
        import_id=import_id,
        target="flowpcp",
        endpoint="/api/portal-pedidos/recebimento",
        payload={"externalId": import_id},
        idempotency_key=f"send-{import_id}",
    )
    criada = (AGORA - timedelta(days=dias)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE outbox SET created_at = ? WHERE id = ?", (criada, row.id))


class Fake:
    def __init__(self, status=None, falha=False, envio_falha=()):
        self.enviados: list[str] = []
        self.lidos: list[list[int]] = []
        self._status = status or {}
        self._falha = falha
        self._envio_falha = set(envio_falha)

    def enviar(self, row, cfg):
        self.enviados.append(row.import_id)
        if row.import_id in self._envio_falha:
            outbox_repo.mark_failed(
                row.id, error="Flow fora", next_attempt_at="2999-01-01T00:00:00"
            )
        else:
            outbox_repo.mark_sent(row.id, response={"ok": True})

    def ler(self, env, codigos):
        self.lidos.append(codigos)
        if self._falha:
            raise RuntimeError("ERP fora do ar")
        return {c: s for c, s in self._status.items() if c in codigos}


@pytest.fixture(autouse=True)
def _sem_pausa():
    envio_portal._pausa_erp.clear()
    yield
    envio_portal._pausa_erp.clear()


def rodar(env, fake, agora=AGORA):
    return envio_portal.drenar_ambiente(
        env, CFG, agora=agora, ler_status=fake.ler, enviar=fake.enviar
    )


def situacao(import_id):
    with db.connect() as conn:
        r = conn.execute(
            "SELECT status, attempts, last_error FROM outbox WHERE import_id = ?", (import_id,)
        ).fetchone()
    return r["status"], r["attempts"], r["last_error"]


def eventos(import_id):
    return [a["event_type"] for a in repo.list_audit(import_id)]


def test_pedido_recente_sem_codigo_vai_sem_consultar_o_erp(env):
    pedido("novo", dias=1)
    fake = Fake()
    conta = rodar(env, fake)
    assert conta["enviados"] == 1 and fake.lidos == []


def test_pedido_recente_ja_faturado_nao_vai(env):
    pedido("recente_faturado", dias=1, fire_codigo=103)
    pedido("recente_aberto", dias=1, fire_codigo=101)
    fake = Fake({101: "PEDIDO", 103: "FATURADO"})
    rodar(env, fake)
    assert fake.enviados == ["recente_aberto"]
    assert situacao("recente_faturado")[2] == "descartado: pedido FATURADO no ERP"


def test_fila_acumulada_so_manda_o_que_segue_aberto_no_erp(env):
    pedido("aberto", dias=60, fire_codigo=101)
    pedido("analise", dias=60, fire_codigo=102)
    pedido("faturado", dias=60, fire_codigo=103)
    pedido("cancelado", dias=60, fire_codigo=104)
    pedido("apagado", dias=60, fire_codigo=105)
    fake = Fake({101: "PEDIDO", 102: "EM ANÁLISE", 103: "FATURADO", 104: "CANCELADO"})

    conta = rodar(env, fake)

    assert (conta["enviados"], conta["descartados"]) == (2, 3)
    assert fake.enviados == ["aberto", "analise"]
    assert fake.lidos == [[101, 102, 103, 104, 105]]  # uma leitura só, em lote
    st, tentativas, motivo = situacao("faturado")
    assert (st, tentativas, motivo) == ("dead", 0, "descartado: pedido FATURADO no ERP")
    assert situacao("apagado")[2] == "descartado: pedido apagado no ERP"
    assert eventos("cancelado") == ["flowpcp_descartado"]


def test_fila_acumulada_sem_codigo_fica_retida_e_volta_quando_a_reconciliacao_acha(env):
    pedido("sem_codigo", dias=30)
    fake = Fake({777: "PEDIDO"})

    assert rodar(env, fake)["retidos"] == 1
    assert situacao("sem_codigo")[0] == "retido"
    assert fake.enviados == [] and fake.lidos == []
    rodar(env, fake)  # retida não volta a ser tratada
    assert eventos("sem_codigo") == ["flowpcp_retido"]

    with db.connect() as conn:  # a reconciliação achou o pedido no ERP
        conn.execute("UPDATE imports SET fire_codigo = 777 WHERE id = 'sem_codigo'")
    conta = rodar(env, fake)

    assert (conta["liberados"], conta["enviados"]) == (1, 1)
    assert fake.enviados == ["sem_codigo"]


def test_erp_fora_segura_quem_tem_codigo_manda_o_resto_e_pausa(env):
    pedido("velho", dias=30, fire_codigo=101)
    pedido("novo_com_codigo", dias=0.2, fire_codigo=102)
    pedido("novo", dias=0.1)
    fake = Fake(falha=True)

    conta = rodar(env, fake)

    assert (conta["enviados"], conta["aguardando_erp"]) == (1, 2)
    assert fake.enviados == ["novo"]
    assert situacao("velho") == ("pending", 0, None)
    rodar(env, fake, agora=AGORA + timedelta(minutes=4))
    assert len(fake.lidos) == 1  # pausa: não insiste no ERP fora
    rodar(env, fake, agora=AGORA + timedelta(minutes=6))
    assert len(fake.lidos) == 2


def test_falha_de_envio_para_o_ambiente_sem_queimar_o_lote(env):
    pedido("a", dias=1)
    pedido("b", dias=0.5)
    pedido("c", dias=0.2)
    fake = Fake(envio_falha={"a"})

    conta = rodar(env, fake)

    assert conta["falhas"] == 1 and fake.enviados == ["a"]
    assert situacao("b") == ("pending", 0, None)


def test_fila_acumulada_grande_nao_atrasa_pedido_novo(env):
    for i in range(envio_portal.LOTE + 5):
        pedido(f"velho{i}", dias=40, fire_codigo=1000 + i)
    pedido("novo", dias=0)
    fake = Fake({1000 + i: "PEDIDO" for i in range(envio_portal.LOTE + 5)})

    rodar(env, fake)

    assert fake.enviados[0] == "novo"
    assert len(fake.enviados) == envio_portal.LOTE


def test_linha_ja_tratada_nao_volta(env):
    pedido("faturado", dias=60, fire_codigo=103)
    fake = Fake({103: "FATURADO"})
    rodar(env, fake)
    conta = rodar(env, fake)
    assert not any(conta.values())


def test_falha_real_de_envio_volta_para_a_fila_no_relogio_local(env, monkeypatch):
    """O retry é gravado no mesmo relógio de `list_due` (antes: UTC vs local = +3 h)."""
    import app.worker.jobs.drain_outbox as drain

    class FlowFora:
        def __init__(self, **kw):
            pass

        def send_order(self, *a, **kw):
            raise RuntimeError("Flow fora")

        def close(self):
            pass

    monkeypatch.setattr(drain, "FlowPCPClient", FlowFora)
    pedido("x", dias=0)
    with db.connect() as conn:
        conn.execute(
            "UPDATE outbox SET payload_json = ? WHERE import_id = 'x'",
            (json.dumps(_payload_recebimento("x")),),
        )
    agora = datetime.now()
    envio_portal.drenar_ambiente(env, CFG, agora=agora, ler_status=Fake().ler)
    st, tentativas, _ = situacao("x")
    assert (st, tentativas) == ("pending", 1)
    depois = (agora + timedelta(seconds=31)).isoformat(timespec="seconds")
    assert [r.import_id for r in outbox_repo.list_due("flowpcp", limit=5, now=depois)] == ["x"]


def test_linha_flowpcp_nunca_morre_por_falha_de_envio(env, monkeypatch):
    import app.worker.jobs.drain_outbox as drain

    pedido("x", dias=0)
    row = outbox_repo.list_due("flowpcp", limit=1)[0]
    drain._handle_flowpcp_failure(replace(row, attempts=99), "Flow fora")
    assert situacao("x")[0] == "pending"


def test_list_due_respeita_janela_e_mark_discarded_so_mexe_em_pendente(env):
    pedido("velho", dias=10)
    pedido("novo", dias=1)
    corte = (AGORA - timedelta(days=7)).isoformat(timespec="seconds")
    assert [r.import_id for r in outbox_repo.list_due("flowpcp", limit=9, criado_ate=corte)] == [
        "velho"
    ]
    assert [r.import_id for r in outbox_repo.list_due("flowpcp", limit=9, criado_depois=corte)] == [
        "novo"
    ]
    rid = outbox_repo.list_due("flowpcp", limit=9, criado_depois=corte)[0].id
    outbox_repo.mark_sent(rid)
    outbox_repo.mark_discarded(rid, reason="descartado: x")
    assert situacao("novo")[0] == "sent"


def test_worker_usa_a_mesma_regra(env, monkeypatch):
    import app.worker.jobs.drain_outbox as drain

    chamadas = []
    monkeypatch.setattr(drain, "enabled_flowpcp_envs", lambda: {"mm": CFG})
    monkeypatch.setattr(
        envio_portal, "drenar_ambiente", lambda e, c: chamadas.append((e["slug"], c))
    )
    drain.run_drain_outbox()
    assert chamadas == [("mm", CFG)]


def test_poll_do_worker_respeita_o_interruptor(monkeypatch):
    import app.worker.jobs.poll_flowpcp as job

    monkeypatch.delenv("PORTAL_FLOWPCP_DECISOES", raising=False)
    monkeypatch.setattr(job, "_list_flowpcp_envs", lambda: [("mm", CFG)])
    chamado = []
    monkeypatch.setattr(job, "poll_decisoes_once", lambda **kw: chamado.append(1))
    job.run_poll_flowpcp()
    assert chamado == []


def test_decisoes_desligadas_por_padrao(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_DECISOES", raising=False)
    monkeypatch.setattr(envio_portal, "drenar", lambda: None)
    chamadas = []
    monkeypatch.setattr(envio_portal, "run_poll_flowpcp", lambda: chamadas.append(1))
    assert envio_portal.ciclo(datetime.min, AGORA) == datetime.min
    assert chamadas == []


def test_decisoes_ligadas_respeitam_o_intervalo(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_DECISOES", "1")
    monkeypatch.setattr(envio_portal, "drenar", lambda: None)
    chamadas = []
    monkeypatch.setattr(envio_portal, "run_poll_flowpcp", lambda: chamadas.append(1))
    ultima = envio_portal.ciclo(datetime.min, AGORA)
    assert envio_portal.ciclo(ultima, AGORA + timedelta(seconds=15)) == AGORA
    envio_portal.ciclo(ultima, AGORA + timedelta(seconds=31))
    assert len(chamadas) == 2


def test_falha_na_drenagem_nao_mata_a_thread(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_DECISOES", raising=False)

    def explode():
        raise RuntimeError("banco travado")

    monkeypatch.setattr(envio_portal, "drenar", explode)
    envio_portal.ciclo(datetime.min, AGORA)  # não levanta


def test_interruptor_da_thread(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_ENVIO", raising=False)
    assert envio_portal.envio_habilitado()
    monkeypatch.setenv("PORTAL_FLOWPCP_ENVIO", "0")
    assert not envio_portal.envio_habilitado()
