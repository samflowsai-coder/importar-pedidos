"""Envio ao FlowPCP de dentro do processo web: fila, fila acumulada, interruptor das decisões."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.integrations.flowpcp import envio_portal
from app.integrations.flowpcp.config import FlowPCPConfig
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo

AGORA = datetime(2026, 10, 15, 10, 0, 0)
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
    def __init__(self, status=None, falha=False):
        self.enviados: list[str] = []
        self.lidos: list[list[int]] = []
        self._status = status or {}
        self._falha = falha

    def enviar(self, row, cfg):
        self.enviados.append(row.import_id)
        outbox_repo.mark_sent(row.id, response={"ok": True})

    def ler(self, env, codigos):
        self.lidos.append(codigos)
        if self._falha:
            raise RuntimeError("ERP fora do ar")
        return {c: s for c, s in self._status.items() if c in codigos}


def rodar(env, fake):
    return envio_portal.drenar_ambiente(
        env, CFG, agora=AGORA, ler_status=fake.ler, enviar=fake.enviar
    )


def situacao(import_id):
    with db.connect() as conn:
        r = conn.execute(
            "SELECT status, attempts, last_error FROM outbox WHERE import_id = ?", (import_id,)
        ).fetchone()
    return r["status"], r["attempts"], r["last_error"]


def test_pedido_recente_vai_sem_consultar_o_erp(env):
    pedido("novo", dias=1)
    fake = Fake()
    assert rodar(env, fake) == {"enviados": 1, "descartados": 0, "aguardando_erp": 0}
    assert fake.enviados == ["novo"] and fake.lidos == []


def test_fila_acumulada_so_manda_o_que_segue_aberto_no_erp(env):
    pedido("aberto", dias=60, fire_codigo=101)
    pedido("analise", dias=60, fire_codigo=102)
    pedido("faturado", dias=60, fire_codigo=103)
    pedido("cancelado", dias=60, fire_codigo=104)
    pedido("apagado", dias=60, fire_codigo=105)
    fake = Fake({101: "PEDIDO", 102: "EM ANÁLISE", 103: "FATURADO", 104: "CANCELADO"})

    conta = rodar(env, fake)

    assert conta == {"enviados": 2, "descartados": 3, "aguardando_erp": 0}
    assert fake.enviados == ["aberto", "analise"]
    assert fake.lidos == [[101, 102, 103, 104, 105]]  # uma leitura só, em lote
    st, tentativas, motivo = situacao("faturado")
    assert (st, tentativas) == ("dead", 0)
    assert motivo == "descartado: fila acumulada, pedido FATURADO no ERP"
    assert situacao("apagado")[2] == "descartado: fila acumulada, pedido apagado no ERP"
    audit = repo.list_audit("cancelado")
    assert audit[0]["event_type"] == "flowpcp_descartado"


def test_fila_acumulada_sem_codigo_do_erp_sai_sem_consultar(env):
    pedido("sem_codigo", dias=30)
    fake = Fake()
    assert rodar(env, fake)["descartados"] == 1
    assert fake.lidos == [] and fake.enviados == []
    assert situacao("sem_codigo")[0] == "dead"


def test_erp_fora_do_ar_segura_a_fila_acumulada_e_manda_as_recentes(env):
    pedido("velho", dias=30, fire_codigo=101)
    pedido("novo", dias=0.1)
    fake = Fake(falha=True)

    conta = rodar(env, fake)

    assert conta == {"enviados": 1, "descartados": 0, "aguardando_erp": 1}
    assert fake.enviados == ["novo"]
    assert situacao("velho") == ("pending", 0, None)  # tenta de novo no próximo ciclo


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
    assert rodar(env, fake) == {"enviados": 0, "descartados": 0, "aguardando_erp": 0}


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
