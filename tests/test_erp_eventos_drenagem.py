"""Drenagem com o Flow dublado: lote vazio, corte por unidade, reenvio, resposta por id."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.erp_eventos import drenagem
from app.erp_eventos.envio import ClienteErpEventos, RespostaHttp, Ritmo
from app.erp_eventos.eventos import EventoFila
from app.persistence import (
    db,
    environments_repo,
    erp_bases_repo,
    erp_envio_lote_repo,
    erp_estado_repo,
    router,
)
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "fpe_" + "b" * 43
T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


class Flow:
    """Dublê do POST /eventos: grava cada lote e responde o que o teste mandar."""

    def __init__(self, respostas=None):
        self.lotes: list[list[dict]] = []
        self.respostas = list(respostas or [])

    def enviar_lote(self, lote_id, eventos):
        self.lotes.append(eventos)
        if self.respostas:
            r = self.respostas.pop(0)
            return r(eventos) if callable(r) else r
        return aceita_tudo(eventos)


def aceita_tudo(eventos):
    return RespostaHttp(
        202, {"resultados": [{"id": e["id"], "situacao": "aceito"} for e in eventos]}, None
    )


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="",
    )
    erp_bases_repo.salvar(
        env["id"],
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=None,
        ativo=True,
        token=TOKEN,
    )
    yield erp_bases_repo.obter(env["id"])
    db.set_db_path(None)
    db.reset_init_cache()


def enfileirar(unidades: list[int]):
    """Uma unidade (pedido) por número, com aquele tanto de eventos."""
    evs, n = [], 0
    for u, tamanho in enumerate(unidades):
        for k in range(tamanho):
            n += 1
            evs.append(
                EventoFila(
                    id=f"{n:064x}",
                    base="mm",
                    erp_codigo=u,
                    seq=k + 1,
                    tipo="x",
                    unidade=f"mm:{u}:1",
                    payload={"id": f"{n:064x}", "n": n},
                    observado_em="2026-10-09T13:00:00.000+00:00",
                )
            )
    with router.env_connect("mm") as conn:
        erp_estado_repo.enfileirar(conn, evs, "2026-10-09T13:00:00.000+00:00")


def rodar(cfg, flow, agora=T0):
    return drenagem.drenar(cfg, cliente=flow, ritmo=Ritmo(0), agora_fn=lambda: agora)


def envio(cfg):
    with router.env_connect("mm") as conn:
        return erp_estado_repo.obter_envio(conn, cfg.base)


def test_sem_pendente_com_sinal_devido_manda_lote_vazio(cfg):
    with router.env_connect("mm") as conn:
        erp_estado_repo.registrar_vigia(conn, "mm", "2026-10-09T13:00:00.000+00:00")
    flow = Flow()
    rodar(cfg, flow)
    assert flow.lotes == [[]]
    assert envio(cfg).ultimo_202_em is not None and not envio(cfg).sinal_pendente
    rodar(cfg, flow)  # sinal já dado: não manda outro
    assert len(flow.lotes) == 1


def test_lote_nunca_corta_um_pedido_ao_meio(cfg):
    enfileirar([300, 250, 10])
    flow = Flow()
    rodar(cfg, flow)
    assert [len(lote) for lote in flow.lotes] == [300, 260]
    assert {e["n"] for e in flow.lotes[0]} == set(range(1, 301))


def test_id_sem_resultado_continua_pendente_e_reenvio_repete_o_mesmo_evento(cfg):
    enfileirar([3])
    so_o_primeiro = lambda evs: RespostaHttp(  # noqa: E731
        202, {"resultados": [{"id": evs[0]["id"], "situacao": "aceito"}]}, None
    )
    flow = Flow([so_o_primeiro, aceita_tudo])
    rodar(cfg, flow)
    assert flow.lotes[1] == flow.lotes[0][1:]  # mesmos bytes de evento
    with router.env_connect("mm") as conn:
        assert erp_estado_repo.contar(conn, "mm") == {"aceito": 3}


def test_resposta_casada_por_id_mesmo_fora_de_ordem(cfg):
    enfileirar([3])

    def embaralhada(evs):
        return RespostaHttp(
            202,
            {
                "resultados": [
                    {"id": evs[2]["id"], "situacao": "rejeitado", "motivo": "item.qtd"},
                    {"id": evs[0]["id"], "situacao": "duplicado"},
                    {"id": evs[1]["id"], "situacao": "aceito"},
                ]
            },
            None,
        )

    r = rodar(cfg, Flow([embaralhada]))
    assert (r.aceitos, r.duplicados, r.rejeitados) == (1, 1, 1)
    with router.env_connect("mm") as conn:
        rows = conn.execute(
            "SELECT seq, situacao, motivo FROM erp_eventos_saida ORDER BY ordem"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "duplicado", None),
        (2, "aceito", None),
        (3, "rejeitado", "item.qtd"),
    ]


def test_401_bloqueia_e_nao_reenvia(cfg):
    enfileirar([2])
    flow = Flow([RespostaHttp(401, None, None, erro="unauthorized")])
    assert rodar(cfg, flow).parou_por == "bloqueada"
    assert envio(cfg).bloqueio == "credencial"
    assert rodar(cfg, flow).parou_por == "bloqueada"
    assert len(flow.lotes) == 1


def test_429_espera_o_retry_after_antes_de_tentar_de_novo(cfg):
    enfileirar([2])
    flow = Flow([RespostaHttp(429, None, "120")])
    assert rodar(cfg, flow).parou_por == "esperando"
    assert rodar(cfg, flow, agora=T0 + timedelta(seconds=60)).parou_por == "esperando"
    assert len(flow.lotes) == 1
    rodar(cfg, flow, agora=T0 + timedelta(seconds=121))
    assert len(flow.lotes) == 2 and envio(cfg).falhas_seguidas == 0


def test_413_divide_sem_partir_unidade(cfg):
    enfileirar([4, 4, 4, 4])
    flow = Flow([RespostaHttp(413, None, None)])
    rodar(cfg, flow)
    assert [len(lote) for lote in flow.lotes] == [16, 8, 8]


def lotes_registrados():
    with router.env_connect("mm") as conn:
        return erp_envio_lote_repo.recentes(conn, "mm")


def test_lote_aceito_grava_registro_janela(cfg):
    enfileirar([3])
    rodar(cfg, Flow())
    (reg,) = lotes_registrados()
    assert (reg.origem, reg.eventos, reg.http, reg.aceitos) == ("janela", 3, 202, 3)
    assert (reg.duplicados, reg.rejeitados) == (0, 0)
    assert reg.quem is None and reg.envio_id is None and reg.respondido_em


def test_lote_vazio_de_sinal_grava_registro(cfg):
    with router.env_connect("mm") as conn:
        erp_estado_repo.registrar_vigia(conn, "mm", "2026-10-09T13:00:00.000+00:00")
    rodar(cfg, Flow())
    (reg,) = lotes_registrados()
    assert (reg.eventos, reg.http, reg.aceitos) == (0, 202, 0)


def test_lote_manual_grava_quem_e_envio_id(cfg):
    enfileirar([2])
    drenagem.drenar(
        cfg,
        cliente=Flow(),
        ritmo=Ritmo(0),
        agora_fn=lambda: T0,
        origem="manual",
        quem="samuel",
        envio_id="env-1",
    )
    (reg,) = lotes_registrados()
    assert (reg.origem, reg.quem, reg.envio_id) == ("manual", "samuel", "env-1")


def test_lote_com_erro_5xx_fecha_com_http_e_zero_aceitos(cfg):
    enfileirar([2])
    rodar(cfg, Flow([RespostaHttp(503, None, None, erro="x")]))
    (reg,) = lotes_registrados()
    assert (reg.http, reg.aceitos, reg.eventos) == (503, 0, 2) and reg.respondido_em


def test_lote_400_bloqueia_e_fecha_registro(cfg):
    enfileirar([2])
    rodar(cfg, Flow([RespostaHttp(400, None, None, erro="ruim")]))
    (reg,) = lotes_registrados()
    assert (reg.http, reg.aceitos) == (400, 0) and reg.respondido_em
    assert envio(cfg).bloqueio


def test_413_fecha_o_lote_recusado_e_registra_o_seguinte(cfg):
    enfileirar([4, 4])
    rodar(cfg, Flow([RespostaHttp(413, None, None)]))
    regs = sorted(lotes_registrados(), key=lambda r: r.enviado_em)
    assert sorted((r.http, r.eventos) for r in regs) == [(202, 4), (202, 4), (413, 8)]


def test_origem_invalida_falha_antes_de_enviar(cfg):
    enfileirar([1])
    flow = Flow()
    with pytest.raises(ValueError):
        drenagem.drenar(cfg, cliente=flow, ritmo=Ritmo(0), agora_fn=lambda: T0, origem="xis")
    assert flow.lotes == []


def test_cliente_http_manda_bearer_tenant_e_envelope():
    vistos = {}

    def handler(req: httpx.Request):
        vistos["auth"] = req.headers["authorization"]
        vistos["tenant"] = req.headers["x-tenant-id"]
        vistos["corpo"] = json.loads(req.content)
        return httpx.Response(202, json={"loteId": "x", "resultados": []})

    c = ClienteErpEventos(
        destino_url="https://flow.test",
        tenant_id=TENANT,
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    r = c.enviar_lote("lote-1", [])
    assert r.status == 202
    assert vistos == {
        "auth": f"Bearer {TOKEN}",
        "tenant": TENANT,
        "corpo": {"schema": "pedido.evento_erp.v1", "loteId": "lote-1", "eventos": []},
    }


def test_cliente_http_rede_fora_vira_status_nulo():
    def handler(req):
        raise httpx.ConnectError("recusado", request=req)

    c = ClienteErpEventos(
        destino_url="https://flow.test",
        tenant_id=TENANT,
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    assert c.enviar_lote("l", []).status is None


def test_ritmo_espacamento_por_tenant_e_rota():
    agora = [0.0]
    dormidas = []
    r = Ritmo(3.0, relogio=lambda: agora[0], dormir=lambda s: dormidas.append(s))
    r.aguardar("t", "eventos")
    r.aguardar("t", "eventos")
    r.aguardar("t", "snapshot-erp")
    assert dormidas == [3.0]


def test_413_com_unidades_de_tamanhos_diferentes_nao_bloqueia(cfg):
    # revisão I1: unidade de 12 + unidade de 4; cortar pela metade em eventos (8)
    # bloquearia a de 12 como "grande demais" sem ela ser.
    enfileirar([12, 4])
    flow = Flow([RespostaHttp(413, None, None)])
    rodar(cfg, flow)
    assert [len(lote) for lote in flow.lotes] == [16, 12, 4]
    assert envio(cfg).bloqueio is None


def test_202_sem_nenhum_resultado_casado_espera_em_vez_de_reenviar_em_laco(cfg):
    # revisão I2
    enfileirar([3])
    flow = Flow([RespostaHttp(202, {"resultados": []}, None)])
    assert rodar(cfg, flow).parou_por == "esperando"
    assert len(flow.lotes) == 1
    assert envio(cfg).falhas_seguidas == 1 and envio(cfg).proxima_tentativa_em
    with router.env_connect("mm") as conn:
        assert erp_estado_repo.contar(conn, "mm") == {"pendente": 3}
