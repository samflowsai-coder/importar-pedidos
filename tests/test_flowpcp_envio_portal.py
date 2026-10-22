"""Envio ao FlowPCP de dentro do processo web: fila, fila acumulada, interruptor das decisões."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.integrations.flowpcp import envio_portal
from app.integrations.flowpcp.config import FlowPCPConfig
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo
from app.utils import janelas_envio

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
    envio_portal._pausa_flow.clear()
    yield
    envio_portal._pausa_erp.clear()
    envio_portal._pausa_flow.clear()


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


def test_falha_de_envio_pausa_o_ambiente_sem_queimar_a_fila(env):
    pedido("a", dias=1)
    pedido("b", dias=0.5)
    pedido("c", dias=0.2)
    fake = Fake(envio_falha={"a"})

    conta = rodar(env, fake)
    assert conta["falhas"] == 1 and fake.enviados == ["a"]
    rodar(env, fake, agora=AGORA + timedelta(minutes=4))  # pausado: nem tenta
    assert fake.enviados == ["a"]
    assert situacao("b") == ("pending", 0, None)
    rodar(env, fake, agora=AGORA + timedelta(minutes=6))
    assert fake.enviados == ["a", "b", "c"]  # "a" está em backoff próprio


def test_erp_que_nao_acha_nenhum_pedido_nao_descarta_nada(env):
    pedido("p1", dias=30, fire_codigo=101)
    pedido("p2", dias=30, fire_codigo=102)
    fake = Fake({})  # banco errado: nenhum código existe

    conta = rodar(env, fake)

    assert (conta["descartados"], conta["aguardando_erp"]) == (0, 2)
    assert situacao("p1")[0] == "pending"


def test_recusa_definitiva_morre_com_motivo_e_passageira_nao():
    from pydantic import BaseModel, ValidationError

    from app.integrations.flowpcp.client import FlowPCPClientError
    from app.worker.jobs.drain_outbox import _recusa_definitiva

    class M(BaseModel):
        x: int

    try:
        M.model_validate({"x": "a"})
    except ValidationError as exc:
        assert _recusa_definitiva(exc)
    assert _recusa_definitiva(FlowPCPClientError("x", status_code=422))
    for passageira in (401, 403, 408, 429, 500, 503, None):
        assert not _recusa_definitiva(FlowPCPClientError("x", status_code=passageira))
    assert not _recusa_definitiva(RuntimeError("rede"))


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
    # Fuso a oeste de UTC (onde o bug aparecia), fixado ANTES de enfileirar:
    # `enqueue` grava next_attempt_at no relógio local do processo.
    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    time.tzset()
    try:
        pedido("x", dias=0)
        agora = datetime.now()
        # O resto do arquivo usa AGORA (data fixa); este teste drena com o relógio real, então a
        # linha nasce "agora" de verdade. Com a data fixa ele virava `retido` (>7 dias) e quebrava.
        with db.connect() as conn:
            conn.execute(
                "UPDATE outbox SET payload_json = ?, created_at = ? WHERE import_id = 'x'",
                (json.dumps(_payload_recebimento("x")), agora.isoformat(timespec="seconds")),
            )
            conn.execute("UPDATE imports SET imported_at = ? WHERE id = 'x'", (agora.isoformat(),))
        envio_portal.drenar_ambiente(env, CFG, agora=agora, ler_status=Fake().ler)
        st, tentativas, _ = situacao("x")
        assert (st, tentativas) == ("pending", 1)
        proxima = outbox_repo.get(outbox_repo.list_for_import("x")[0].id).next_attempt_at
        assert proxima is not None and "+" not in proxima and "Z" not in proxima

        def vencidas(segundos):
            now = (agora + timedelta(seconds=segundos)).isoformat(timespec="seconds")
            return [r.import_id for r in outbox_repo.list_due("flowpcp", limit=5, now=now)]

        assert vencidas(25) == []
        assert vencidas(31) == ["x"]
    finally:
        monkeypatch.undo()
        time.tzset()


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
    monkeypatch.setattr(envio_portal, "janela_aberta", lambda agora=None: "2026-10-15 08h")
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


# ── Janelas de envio: 8h, 11h, 14h, 17h, segunda a sábado (Brasília) ─────────

SP = envio_portal.FUSO


@pytest.mark.parametrize(
    ("quando", "esperado"),
    [
        (datetime(2026, 10, 15, 8, 0, tzinfo=SP), "2026-10-15 08h"),  # quinta
        (datetime(2026, 10, 15, 8, 59, tzinfo=SP), "2026-10-15 08h"),
        (datetime(2026, 10, 15, 9, 0, tzinfo=SP), None),
        (datetime(2026, 10, 15, 7, 59, tzinfo=SP), None),
        (datetime(2026, 10, 15, 11, 30, tzinfo=SP), "2026-10-15 11h"),
        (datetime(2026, 10, 15, 14, 0, tzinfo=SP), "2026-10-15 14h"),
        (datetime(2026, 10, 15, 17, 45, tzinfo=SP), "2026-10-15 17h"),
        (datetime(2026, 10, 15, 18, 0, tzinfo=SP), None),
        (datetime(2026, 10, 17, 8, 10, tzinfo=SP), "2026-10-17 08h"),  # sábado
        (datetime(2026, 10, 18, 8, 10, tzinfo=SP), None),  # domingo
        (datetime(2026, 10, 15, 11, 0, tzinfo=janelas_envio.ZoneInfo("UTC")), "2026-10-15 08h"),
    ],
)
def test_janela_aberta(monkeypatch, quando, esperado):
    monkeypatch.delenv("PORTAL_FLOW_JANELAS", raising=False)
    assert envio_portal.janela_aberta(quando) == esperado


def test_janelas_configuraveis_e_valor_invalido_cai_no_padrao(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOW_JANELAS", "9, 15")
    assert envio_portal.janela_aberta(datetime(2026, 10, 15, 9, 5, tzinfo=SP)) == "2026-10-15 09h"
    assert envio_portal.janela_aberta(datetime(2026, 10, 15, 8, 5, tzinfo=SP)) is None
    monkeypatch.setenv("PORTAL_FLOW_JANELAS", "9,xx")  # inválido: padrão inteiro, não "9"
    assert envio_portal.janela_aberta(datetime(2026, 10, 15, 8, 5, tzinfo=SP)) == "2026-10-15 08h"
    assert envio_portal.janela_aberta(datetime(2026, 10, 15, 9, 5, tzinfo=SP)) is None


def test_janela_invalida_loga_uma_vez(monkeypatch):
    erros = []
    monkeypatch.setattr(janelas_envio.logger, "error", erros.append)
    monkeypatch.setattr(janelas_envio, "_invalida_logada", None)
    monkeypatch.setenv("PORTAL_FLOW_JANELAS", "25")
    for _ in range(4):
        envio_portal.janela_aberta(datetime(2026, 10, 15, 8, 5, tzinfo=SP))
    assert len(erros) == 1


def test_janelas_recentes_pula_domingo_e_inclui_a_aberta(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOW_JANELAS", raising=False)
    segunda_9h30 = datetime(2026, 10, 19, 9, 30, tzinfo=SP)
    rotulos = [r for r, _, _ in janelas_envio.janelas_recentes(segunda_9h30, n=6)]
    assert rotulos == [
        "2026-10-19 08h",
        "2026-10-17 17h",  # sábado; o domingo 18 fica de fora
        "2026-10-17 14h",
        "2026-10-17 11h",
        "2026-10-17 08h",
        "2026-10-16 17h",
    ]


@pytest.fixture
def _janelas_limpas(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOW_JANELAS", raising=False)
    monkeypatch.setattr(envio_portal, "_janela_corrente", None)
    monkeypatch.setattr(envio_portal, "_gauge_iniciado", False)
    monkeypatch.setattr(envio_portal, "_rotulos_no_gauge", set())


def test_fora_da_janela_nada_sai(monkeypatch, _janelas_limpas):
    chamadas = []
    monkeypatch.setattr(envio_portal, "enabled_flowpcp_envs", lambda: chamadas.append(1) or {})
    monkeypatch.setattr(envio_portal, "update_outbox_metrics", lambda: None)
    monkeypatch.setattr(envio_portal, "_atualizar_gauge", lambda *a, **k: None)
    assert envio_portal.drenar(datetime(2026, 10, 15, 10, 0, tzinfo=SP)) is None
    assert chamadas == []


def _marcar(import_id, *, enviado_em=None, evento=None, quando=None):
    with db.connect() as conn:
        if enviado_em:
            conn.execute(
                "UPDATE outbox SET status = 'sent', sent_at = ? WHERE import_id = ?",
                (enviado_em, import_id),
            )
        if evento:
            conn.execute(
                "INSERT INTO audit_log (environment_id, import_id, event_type, created_at) "
                "VALUES ('x', ?, ?, ?)",
                (import_id, evento, quando),
            )


def test_resumo_da_janela_vem_do_banco(env, monkeypatch):
    monkeypatch.setattr(envio_portal.router, "list_env_slugs", lambda: ["mm"])
    for nome in ("a", "b", "c", "d", "e", "fora"):
        pedido(nome, dias=0)
    _marcar("a", enviado_em="2026-10-15T08:00:05")
    _marcar("b", enviado_em="2026-10-15T08:59:59")
    _marcar("fora", enviado_em="2026-10-15T09:00:00")  # já é a janela seguinte
    _marcar("c", evento="flowpcp_descartado", quando="2026-10-15T08:10:00")
    _marcar("d", evento="flowpcp_retido", quando="2026-10-15T08:10:00")
    _marcar("d", evento="flowpcp_retido", quando="2026-10-15T08:20:00")  # mesmo pedido: 1
    _marcar("e", evento="flowpcp_recusado", quando="2026-10-15T08:30:00")

    resumo = envio_portal.resumo_da_janela(
        datetime(2026, 10, 15, 8, 0), datetime(2026, 10, 15, 9, 0)
    )

    assert resumo == {"mm": {"enviado": 2, "recusado": 1, "descartado": 1, "retido": 1}}


def test_recusado_pelo_flow_nao_conta_como_enviado(env):
    pedido("ruim", dias=0)

    def recusa(row, cfg):
        outbox_repo.mark_failed(row.id, error="recusado: 422", dead=True)

    conta = envio_portal.drenar_ambiente(
        env, CFG, agora=AGORA, ler_status=Fake().ler, enviar=recusa
    )
    assert (conta["enviados"], conta["recusados"], conta["falhas"]) == (0, 1, 0)


def test_janela_fecha_com_resumo_do_banco_e_gauge_poda_as_antigas(
    env, monkeypatch, _janelas_limpas
):
    from app.observability.metrics import flowpcp_janela_pedidos

    monkeypatch.setattr(envio_portal.router, "list_env_slugs", lambda: ["mm"])
    monkeypatch.setattr(envio_portal, "enabled_flowpcp_envs", lambda: {})
    monkeypatch.setattr(envio_portal, "update_outbox_metrics", lambda: None)
    pedido("a", dias=0)
    _marcar("a", enviado_em="2026-10-15T08:30:00")
    logs = []
    monkeypatch.setattr(envio_portal.logger, "info", logs.append)

    oito = datetime(2026, 10, 15, 8, 0, tzinfo=SP)
    envio_portal.drenar(oito)
    envio_portal.drenar(oito + timedelta(hours=1))  # 9h: fecha

    fechou = [linha for linha in logs if "janela 2026-10-15 08h fechada" in linha]
    assert fechou and "'enviado': 1" in fechou[0]

    def amostras():
        return {
            (a.labels["janela"], a.labels["resultado"]): a.value
            for a in flowpcp_janela_pedidos.collect()[0].samples
        }

    assert amostras()[("2026-10-15 08h", "enviado")] == 1
    envio_portal.drenar(oito + timedelta(days=5))  # mais de 8 janelas depois
    assert ("2026-10-15 08h", "enviado") not in amostras()


def test_worker_fora_da_janela_nao_drena(env, monkeypatch):
    import app.worker.jobs.drain_outbox as drain

    chamadas = []
    monkeypatch.setattr(drain, "enabled_flowpcp_envs", lambda: {"mm": CFG})
    monkeypatch.setattr(envio_portal, "janela_aberta", lambda agora=None: None)
    monkeypatch.setattr(envio_portal, "drenar_ambiente", lambda e, c: chamadas.append(1))
    drain.run_drain_outbox()
    assert chamadas == []
