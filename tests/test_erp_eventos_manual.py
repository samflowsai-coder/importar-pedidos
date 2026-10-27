"""Envio manual: o clique vira pedido em memória e a thread o atende no tick."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.erp_eventos import foto, runner
from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.envio import RespostaHttp, Ritmo
from app.erp_eventos.eventos import EventoFila
from app.persistence import (
    db,
    environments_repo,
    erp_bases_repo,
    erp_estado_repo,
    router,
)
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
FORA_DA_JANELA = datetime(2026, 10, 15, 21, 0, tzinfo=UTC)  # quinta, 18:00 em São Paulo
JANELA_14H = datetime(2026, 10, 15, 17, 10, tzinfo=UTC)  # 14:10 em São Paulo


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    monkeypatch.setenv("PORTAL_ERP_VIGIA", "1")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    env = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="x",
    )
    erp_bases_repo.salvar(
        env["id"],
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=None,
        ativo=True,
        token="fpe_" + "g" * 43,
    )
    runner._FALHA_ERP.clear()
    runner._ULTIMA_RETENCAO.clear()
    runner._PEDIDO_MANUAL.clear()
    runner._RESULTADOS.clear()
    runner._EM_ANDAMENTO.clear()
    runner._ATENDIDO_EM.clear()
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    monkeypatch.setattr(runner, "_relogio", lambda: FORA_DA_JANELA)
    monkeypatch.setattr(foto, "devida", lambda *a, **k: False)
    yield erp_bases_repo.obter(env["id"])
    db.set_db_path(None)
    db.reset_init_cache()


def _evento(n: int) -> EventoFila:
    return EventoFila(
        id=f"{n:064x}",
        base="mm",
        erp_codigo=n,
        seq=1,
        tipo="status_alterado",
        unidade=f"mm:{n}:1",
        payload={"id": f"{n:064x}", "n": n},
        observado_em=f"2026-10-15T20:0{n}:00.000+00:00",
    )


def _enfileirar(cfg, *ns):
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(conn, [_evento(n) for n in ns], "2026-10-15T20:30:00.000+00:00")


class Flow:
    """Dublê do Flow: guarda cada lote recebido."""

    lotes: list[int] = []

    def __init__(self, **kw):
        pass

    def enviar_lote(self, lote_id, eventos):
        Flow.lotes.append(len(eventos))
        return RespostaHttp(
            202, {"resultados": [{"id": e["id"], "situacao": "aceito"} for e in eventos]}, None
        )

    def close(self):
        pass


@pytest.fixture
def flow(monkeypatch):
    Flow.lotes = []
    monkeypatch.setattr(runner, "ClienteErpEventos", Flow)
    return Flow


def _lotes_gravados(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        return conn.execute(
            "SELECT origem, quem, envio_id, eventos FROM erp_envio_lote ORDER BY rowid"
        ).fetchall()


def _marcar(cfg, **campos):
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo._atualizar_envio(conn, cfg.base, "2026-10-15T20:00:00.000+00:00", **campos)


def _recusa(cfg, agora=FORA_DA_JANELA):
    with pytest.raises(runner.EnvioManualRecusado) as exc:
        runner.pedir_envio_manual(cfg, "ana", agora=agora)
    return exc.value


def test_pedido_aceito_devolve_id_e_fica_pendente(cfg):
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    r = runner.resultado_manual(envio_id)
    assert r["situacao"] == "pendente" and r["terminado_em"] is None
    assert runner.checar_envio_manual(cfg, FORA_DA_JANELA).codigo == "em_andamento"


def test_pedido_recusado_vigia_desligada(cfg, monkeypatch):
    monkeypatch.setenv("PORTAL_ERP_VIGIA", "0")
    e = _recusa(cfg)
    assert e.codigo == "vigia_desligada"
    assert e.detalhe == "O envio de pedidos ao Flow está desligado neste portal."


def test_pedido_recusado_base_desligada(cfg):
    erp_bases_repo.salvar(
        cfg.environment_id,
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=None,
        ativo=False,
    )
    e = _recusa(erp_bases_repo.obter(cfg.environment_id))
    assert e.codigo == "desligada" and e.detalhe == "Envio desligado nesta empresa."


def test_pedido_recusado_sem_credencial(cfg):
    from dataclasses import replace

    e = _recusa(replace(cfg, token=None))
    assert e.codigo == "sem_credencial"
    assert e.detalhe == ("Base ligada sem credencial: salve a credencial na edição do ambiente.")


def test_pedido_recusado_bloqueada(cfg):
    _marcar(cfg, bloqueio="unidade_grande")
    e = _recusa(cfg)
    assert e.codigo == "bloqueada"
    assert e.detalhe == (
        "Envio parado: um pedido tem mais itens do que cabe num lote. Fale com o suporte."
    )


def test_pedido_recusado_recuo_mostra_hora_local(cfg):
    _marcar(cfg, proxima_tentativa_em="2026-10-15T21:30:00.000+00:00")  # 18:30 em SP
    e = _recusa(cfg)
    assert e.codigo == "recuo"
    assert e.detalhe == "O Flow pediu para esperar: nova tentativa às 18:30."


def test_recuo_vencido_nao_recusa(cfg):
    _marcar(cfg, proxima_tentativa_em="2026-10-15T20:59:00.000+00:00")
    assert runner.checar_envio_manual(cfg, FORA_DA_JANELA) is None


def test_pedido_recusado_em_andamento(cfg):
    runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    e = _recusa(cfg)
    assert e.codigo == "em_andamento"
    assert e.detalhe == "Já tem um envio manual em andamento para esta empresa."


def test_intervalo_30s_recusa_e_61s_aceita(cfg):
    runner._ATENDIDO_EM[(cfg.environment_id, cfg.base)] = FORA_DA_JANELA - timedelta(seconds=30)
    e = _recusa(cfg)
    assert e.codigo == "intervalo" and e.detalhe == "Aguarde um minuto entre envios manuais."
    runner._ATENDIDO_EM[(cfg.environment_id, cfg.base)] = FORA_DA_JANELA - timedelta(seconds=61)
    assert runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)


def test_checar_nao_grava_nada(cfg):
    assert runner.checar_envio_manual(cfg, FORA_DA_JANELA) is None
    assert runner._PEDIDO_MANUAL == {} and runner._RESULTADOS == {}


def test_dois_pedidos_concorrentes_um_aceito(cfg):
    barreira = threading.Barrier(2)
    saidas: list = []

    def clicar():
        barreira.wait()
        try:
            saidas.append(runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA))
        except runner.EnvioManualRecusado as exc:
            saidas.append(exc)

    ts = [threading.Thread(target=clicar) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ids = [s for s in saidas if isinstance(s, str)]
    recusas = [s for s in saidas if isinstance(s, runner.EnvioManualRecusado)]
    assert len(ids) == 1 and len(recusas) == 1 and recusas[0].codigo == "em_andamento"


def test_tick_atende_na_ordem(cfg, flow, monkeypatch):
    # Sem o envio automático do tick: prova os dois passos do manual (fila, vigia, fila).
    monkeypatch.setattr(runner, "_drenar_fila", lambda c: None)
    _enfileirar(cfg, 1, 2)

    def vigia(c, *a, **k):
        _enfileirar(cfg, 3)

    monkeypatch.setattr(runner, "_vigiar", vigia)
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert flow.lotes == [2, 1]
    gravados = _lotes_gravados(cfg)
    assert [tuple(g) for g in gravados] == [
        ("manual", "ana", envio_id, 2),
        ("manual", "ana", envio_id, 1),
    ]
    r = runner.resultado_manual(envio_id)
    assert r["situacao"] == "terminado" and r["vigia"] == "ok" and r["lotes"] == 2
    assert r["aceitos"] == 3 and r["iniciado_em"] and r["terminado_em"]


def test_fila_vazia_um_lote_vazio_so(cfg, flow, monkeypatch):
    def vigia(c, *a, **k):  # sem mudança: só registra a leitura; o sinal é do _atender_manual
        with router.env_connect(cfg.env_slug) as conn:
            erp_estado_repo.registrar_vigia(conn, cfg.base, "2026-10-15T21:00:00.000+00:00")

    with router.env_connect(cfg.env_slug) as conn:  # 202 recente: o auto não manda sinal
        erp_estado_repo.registrar_202(conn, cfg.base, "2026-10-15T20:50:00.000+00:00")
    monkeypatch.setattr(runner, "_vigiar", vigia)
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert flow.lotes == [0]
    assert [g["origem"] for g in _lotes_gravados(cfg)] == ["manual"]  # vazio manual não vira sinal
    assert runner.resultado_manual(envio_id)["lotes"] == 1


def test_vigia_falha_ainda_drena(cfg, flow, monkeypatch):
    _enfileirar(cfg, 1)
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert flow.lotes == [1]
    r = runner.resultado_manual(envio_id)
    assert r["vigia"] == "falhou" and r["situacao"] == "terminado"
    assert (cfg.environment_id, cfg.base) in runner._FALHA_ERP


def test_vigia_em_pausa_pulada(cfg, flow, monkeypatch):
    _enfileirar(cfg, 1)
    chamadas = []
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: chamadas.append(1))
    runner._FALHA_ERP[(cfg.environment_id, cfg.base)] = FORA_DA_JANELA - timedelta(minutes=1)
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert chamadas == [] and flow.lotes == [1]
    assert runner.resultado_manual(envio_id)["vigia"] == "pulada"


def test_vigia_normal_nao_roda_de_novo(cfg, flow, monkeypatch):
    chamadas = []
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: chamadas.append(1))
    runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert chamadas == [1]


def test_pedido_atendido_uma_vez(cfg, flow, monkeypatch):
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: None)
    _enfileirar(cfg, 1)
    runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    n = len(_lotes_gravados(cfg))
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert len(_lotes_gravados(cfg)) == n
    assert runner._PEDIDO_MANUAL == {}


def test_fila_que_o_tick_ja_levou_passo1_pulado(cfg, flow, monkeypatch):
    """O envio normal (auto) roda antes do atendimento: o manual encontra a fila vazia."""
    monkeypatch.setattr(runner, "_relogio", lambda: JANELA_14H)
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: None)
    _enfileirar(cfg, 1, 2)
    runner.pedir_envio_manual(cfg, "ana", agora=JANELA_14H)
    runner.processar_base(cfg, agora=JANELA_14H)
    origens = [g["origem"] for g in _lotes_gravados(cfg)]
    assert origens[0] == "auto"  # o envio automático levou a fila
    with router.env_connect(cfg.env_slug) as conn:
        assert erp_estado_repo.pendentes(conn, cfg.base, limite=1) == []


def test_fora_de_horario_o_auto_leva_a_fila_e_o_manual_so_o_da_vigia(cfg, flow, monkeypatch):
    _enfileirar(cfg, 1, 2)

    def vigia(c, *a, **k):
        _enfileirar(cfg, 3)

    monkeypatch.setattr(runner, "_vigiar", vigia)
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    assert flow.lotes == [2, 1]
    assert [tuple(g) for g in _lotes_gravados(cfg)] == [
        ("auto", None, None, 2),
        ("manual", "ana", envio_id, 1),
    ]


def test_resultado_expira_em_1h(cfg, monkeypatch):
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner._RESULTADOS[envio_id].update(
        situacao="terminado", terminado_em=formatar_instante(FORA_DA_JANELA)
    )
    monkeypatch.setattr(runner, "_relogio", lambda: FORA_DA_JANELA + timedelta(minutes=59))
    assert runner.resultado_manual(envio_id) is not None
    monkeypatch.setattr(runner, "_relogio", lambda: FORA_DA_JANELA + timedelta(hours=1, seconds=1))
    assert runner.resultado_manual(envio_id) is None


def test_resultado_desconhecido_none(cfg):
    assert runner.resultado_manual("nao-existe") is None


def test_pedido_vencido_nao_e_atendido_quando_a_base_volta(cfg, flow, monkeypatch):
    """Clique → base desligada → tick → base religada → tick: nada sai em nome de quem clicou."""
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: None)
    _enfileirar(cfg, 1)
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    dias_depois = FORA_DA_JANELA + timedelta(days=3)
    runner.processar_base(cfg, agora=dias_depois)  # base religada, pedido de 3 dias atrás
    assert [g["origem"] for g in _lotes_gravados(cfg) if g["origem"] == "manual"] == []
    monkeypatch.setattr(runner, "_relogio", lambda: dias_depois)
    r = runner.resultado_manual(envio_id)
    assert r["situacao"] == "terminado" and r["parou_por"] == "expirado"
    assert runner._PEDIDO_MANUAL == {} and runner._EM_ANDAMENTO == {}


def test_checar_descarta_pedido_vencido_antes_de_recusar(cfg):
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    assert runner.checar_envio_manual(cfg, FORA_DA_JANELA + timedelta(minutes=4)).codigo == (
        "em_andamento"
    )
    assert runner.checar_envio_manual(cfg, FORA_DA_JANELA + timedelta(minutes=6)) is None
    assert runner._RESULTADOS[envio_id]["parou_por"] == "expirado"


def test_resultado_pendente_vencido_expira_na_limpeza(cfg, monkeypatch):
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    monkeypatch.setattr(runner, "_relogio", lambda: FORA_DA_JANELA + timedelta(minutes=6))
    r = runner.resultado_manual(envio_id)
    assert r["situacao"] == "terminado" and r["parou_por"] == "expirado"
    assert runner._PEDIDO_MANUAL == {}


def test_excecao_na_drenagem_termina_com_erro_e_libera(cfg, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: None)
    monkeypatch.setattr(runner.drenagem, "drenar", explode)
    envio_id = runner.pedir_envio_manual(cfg, "ana", agora=FORA_DA_JANELA)
    runner.processar_base(cfg, agora=FORA_DA_JANELA)
    r = runner.resultado_manual(envio_id)
    assert r["situacao"] == "terminado" and r["parou_por"] == "erro" and r["terminado_em"]
    assert runner._EM_ANDAMENTO == {}
    depois = FORA_DA_JANELA + timedelta(seconds=61)
    assert runner.pedir_envio_manual(cfg, "ana", agora=depois)
