"""Runner: uma etapa que falha não derruba as outras (revisão C1); foto inviável vira estado."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.erp_eventos import drenagem, foto, runner
from app.erp_eventos.envio import RespostaHttp, Ritmo
from app.erp_eventos.leitura import SEM_STATUS
from app.persistence import (
    db,
    environments_repo,
    erp_bases_repo,
    erp_envio_lote_repo,
    erp_estado_repo,
    erp_foto_repo,
    router,
)
from app.security import secret_store
from tests.test_erp_eventos_origem import pedidos_foto

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
NOITE = datetime(2026, 10, 10, 2, 30, tzinfo=UTC)  # 23:30 em São Paulo
JANELA_14H = datetime(2026, 10, 15, 17, 10, tzinfo=UTC)  # quinta, 14:10 em São Paulo
DOMINGO_18H_SP = datetime(2026, 10, 25, 21, 0, tzinfo=UTC)  # domingo, 18:00 em São Paulo


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
        fb_path="x",
    )
    erp_bases_repo.salvar(
        env["id"],
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=23,
        ativo=True,
        token="fpe_" + "g" * 43,
    )
    runner._FALHA_ERP.clear()
    runner._ULTIMA_RETENCAO.clear()
    yield erp_bases_repo.obter(env["id"])
    db.set_db_path(None)
    db.reset_init_cache()


def test_vigia_e_foto_quebradas_nao_impedem_a_drenagem(cfg, monkeypatch):
    chamadas = []

    def quebra(*a, **k):
        raise RuntimeError("ERP fora do ar")

    monkeypatch.setattr(runner, "_vigiar", quebra)
    monkeypatch.setattr(runner, "_criar_foto", quebra)
    monkeypatch.setattr(drenagem, "drenar", lambda *a, **k: chamadas.append("drenar"))
    monkeypatch.setattr(runner, "_enviar_foto", lambda c: chamadas.append("foto"))
    monkeypatch.setattr(runner, "_relogio", lambda: JANELA_14H)
    runner.processar_base(cfg, agora=JANELA_14H)
    assert chamadas == ["drenar", "foto"]
    assert (cfg.environment_id, cfg.base) in runner._FALHA_ERP  # próxima tentativa só em 5 min


def test_fora_de_horario_a_fila_sai_e_a_foto_segue(cfg, monkeypatch):
    chamadas = []
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_criar_foto", lambda *a, **k: None)
    monkeypatch.setattr(drenagem, "drenar", lambda *a, **k: chamadas.append("drenar"))
    monkeypatch.setattr(runner, "_enviar_foto", lambda c: chamadas.append("foto"))
    monkeypatch.setattr(runner, "_relogio", lambda: NOITE)
    runner.processar_base(cfg, agora=NOITE)  # 23:30: não há mais janela
    assert chamadas == ["drenar", "foto"]


def _envio(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        return erp_estado_repo.obter_envio(conn, cfg.base)


class FlowLotes:
    """Dublê do POST /eventos: guarda os eventos de cada lote recebido."""

    lotes: list[list[dict]] = []

    def __init__(self, **kw):
        pass

    def enviar_lote(self, lote_id, eventos):
        FlowLotes.lotes.append(eventos)
        return RespostaHttp(
            202, {"resultados": [{"id": e["id"], "situacao": "aceito"} for e in eventos]}, None
        )

    def close(self):
        pass


@pytest.fixture
def flow_lotes(monkeypatch):
    FlowLotes.lotes = []
    monkeypatch.setattr(runner, "ClienteErpEventos", FlowLotes)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    monkeypatch.setattr(foto, "enviar", lambda *a, **k: None)
    monkeypatch.setattr(foto, "devida", lambda *a, **k: False)
    return FlowLotes


def _origens(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        linhas = conn.execute("SELECT origem, eventos FROM erp_envio_lote ORDER BY rowid")
        return [tuple(r) for r in linhas]


def _tick(cfg, monkeypatch, agora, vigia=lambda *a, **k: None):
    monkeypatch.setattr(runner, "_vigiar", vigia)
    monkeypatch.setattr(runner, "_relogio", lambda: agora)
    runner.processar_base(cfg, agora=agora)


def _ultimo_202(cfg, instante: datetime):
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.registrar_202(conn, cfg.base, runner.formatar_instante(instante))


def test_sem_202_ha_mais_de_55_min_pede_o_sinal_de_vida(cfg):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=56))
    runner._garantir_sinal_de_vida(cfg, DOMINGO_18H_SP)
    assert _envio(cfg).sinal_pendente


def test_202_recente_nao_pede_sinal_de_vida(cfg):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=54))
    runner._garantir_sinal_de_vida(cfg, DOMINGO_18H_SP)
    assert not _envio(cfg).sinal_pendente


def test_domingo_a_noite_a_fila_sai_com_origem_auto(cfg, flow_lotes, monkeypatch):
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(
            conn,
            [_evento(n, "2026-10-25T20:30:00.000+00:00") for n in (1, 2, 3)],
            "2026-10-25T20:30:00.000+00:00",
        )
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=5))
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)
    assert [len(e) for e in flow_lotes.lotes] == [3]
    assert _origens(cfg) == [("auto", 3)]


def test_fila_vazia_com_56_min_de_silencio_manda_um_lote_de_sinal(cfg, flow_lotes, monkeypatch):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=56))
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)
    assert flow_lotes.lotes == [[]]
    assert _origens(cfg) == [("sinal", 0)]


def test_fila_vazia_com_54_min_de_silencio_nao_manda_nada(cfg, flow_lotes, monkeypatch):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=54))
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)
    assert flow_lotes.lotes == []


def test_dois_ticks_logo_apos_um_202_nao_mandam_sinal(cfg, flow_lotes, monkeypatch):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=56))
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)  # o sinal sai e o 202 zera a contagem
    _tick(cfg, monkeypatch, DOMINGO_18H_SP + timedelta(seconds=30))
    _tick(cfg, monkeypatch, DOMINGO_18H_SP + timedelta(seconds=60))
    assert flow_lotes.lotes == [[]]


def test_vigia_real_com_202_recente_nao_gera_lote_vazio(cfg, flow_lotes, monkeypatch):
    """A vigia (a cada 15 min) não liga o sinal de vida: 202 há 5 min = nenhum POST."""
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=5))

    def vigia_real(*a, **k):
        with router.env_connect(cfg.env_slug) as conn:
            erp_estado_repo.registrar_vigia(
                conn, cfg.base, runner.formatar_instante(DOMINGO_18H_SP)
            )

    _tick(cfg, monkeypatch, DOMINGO_18H_SP, vigia=vigia_real)
    _tick(cfg, monkeypatch, DOMINGO_18H_SP + timedelta(seconds=30), vigia=vigia_real)
    assert flow_lotes.lotes == []


def test_vigia_que_lanca_nao_segura_o_sinal_de_vida(cfg, flow_lotes, monkeypatch):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=56))

    def quebra(*a, **k):
        raise RuntimeError("Fire fora do ar")

    _tick(cfg, monkeypatch, DOMINGO_18H_SP, vigia=quebra)
    assert flow_lotes.lotes == [[]]
    assert (cfg.environment_id, cfg.base) in runner._FALHA_ERP


def test_recuo_pedido_pelo_flow_segura_a_fila_cheia(cfg, flow_lotes, monkeypatch):
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(
            conn, [_evento(1, "2026-10-25T20:30:00.000+00:00")], "2026-10-25T20:30:00.000+00:00"
        )
        erp_estado_repo._atualizar_envio(
            conn,
            cfg.base,
            "2026-10-25T20:59:00.000+00:00",
            proxima_tentativa_em=runner.formatar_instante(DOMINGO_18H_SP + timedelta(minutes=5)),
        )
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)
    assert flow_lotes.lotes == []


def test_base_bloqueada_nao_manda_lote_nem_sinal(cfg, flow_lotes, monkeypatch):
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=120))
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.bloquear(
            conn, cfg.base, "2026-10-25T20:00:00.000+00:00", motivo="auth", http=401, erro="x"
        )
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)
    assert flow_lotes.lotes == []


def test_envio_sai_antes_da_vigia(cfg, monkeypatch):
    """Vigia travada no ERP não pode segurar o envio nem o sinal de vida."""
    ordem = []
    monkeypatch.setattr(runner, "_relogio", lambda: JANELA_14H)
    monkeypatch.setattr(runner, "_drenar_fila", lambda c: ordem.append("envio"))
    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: ordem.append("vigia"))
    monkeypatch.setattr(foto, "enviar", lambda *a, **k: None)
    runner.processar_base(cfg, agora=JANELA_14H)
    assert ordem == ["envio", "vigia"]


def test_vigia_que_falhou_espera_5_minutos(cfg, monkeypatch):
    tentativas = []
    monkeypatch.setattr(
        runner,
        "_vigiar",
        lambda *a, **k: tentativas.append(1) or (_ for _ in ()).throw(RuntimeError("x")),
    )
    monkeypatch.setattr(drenagem, "drenar", lambda *a, **k: None)
    monkeypatch.setattr(foto, "enviar", lambda *a, **k: None)
    monkeypatch.setattr(foto, "devida", lambda *a, **k: False)
    runner.processar_base(cfg, agora=NOITE)
    runner.processar_base(cfg, agora=NOITE.replace(minute=32))
    runner.processar_base(cfg, agora=NOITE.replace(minute=36))
    assert len(tentativas) == 2


def test_foto_com_pedido_fora_do_contrato_fica_registrada_e_nao_repete(cfg):
    ruim = replace(pedidos_foto(1)[0], status="X" * 41)
    assert foto.criar(cfg, corte=NOITE, pedidos=[ruim]) is None
    with router.env_connect("mm") as conn:
        f = erp_foto_repo.foto_do_dia(conn, "mm", foto.dia_local(NOITE))
    assert f.situacao == "nao_criada" and "status" in f.motivo
    assert not foto.devida(cfg, NOITE, ja_existe=f is not None)


def test_foto_respeita_a_espera_da_base(cfg):
    foto.criar(cfg, corte=NOITE, pedidos=pedidos_foto(2))
    with router.env_connect("mm") as conn:
        erp_estado_repo.registrar_espera(
            conn,
            "mm",
            "2026-10-10T02:30:00.000+00:00",
            http=429,
            erro="",
            ate="2026-10-10T02:40:00.000+00:00",
        )

    class Flow:
        def enviar_pagina(self, pagina):
            raise AssertionError("não devia enviar durante a espera")

    assert foto.enviar(cfg, cliente=Flow(), ritmo=Ritmo(0), agora_fn=lambda: NOITE) == "esperando"


def test_foto_envia_paginas_em_serie_e_completa(cfg):
    foto.criar(cfg, corte=NOITE, pedidos=pedidos_foto(450))
    vistas = []

    class Flow:
        def enviar_pagina(self, pagina):
            vistas.append((pagina["pagina"], pagina["totalPaginas"]))
            return RespostaHttp(202, {"completa": False}, None)

    assert foto.enviar(cfg, cliente=Flow(), ritmo=Ritmo(0), agora_fn=lambda: NOITE) == "completa"
    assert vistas == [(1, 3), (2, 3), (3, 3)]


def test_base_repetida_no_mesmo_tenant_e_recusada(cfg, tmp_path):
    outro = environments_repo.create(
        slug="mm2",
        name="MM2",
        watch_dir=str(tmp_path / "w2"),
        output_dir=str(tmp_path / "o2"),
        fb_path="x",
    )
    with pytest.raises(erp_bases_repo.ConfigErpInvalidaError, match="já é usada"):
        erp_bases_repo.salvar(
            outro["id"],
            base="mm",
            papel="produtora",
            tenant_id=TENANT,
            destino_url="https://flow.test",
            foto_hora=None,
            ativo=True,
        )


def test_status_vazio_no_erp_vira_marca_explicita():
    from app.erp_eventos.leitura import LeitorFirebird
    from tests.test_erp_eventos_origem import ConnFake

    conn = ConnFake({"CAB": [(1, "P", 1, None, None, None, None)], "ITENS": [], "NFS": []})
    assert LeitorFirebird(conn).abertos()[0].status == SEM_STATUS


def test_foto_tambem_pausa_quando_o_erp_esta_fora(cfg, monkeypatch):
    tentativas = []

    def quebra(*a, **k):
        tentativas.append(1)
        raise RuntimeError("ERP fora do ar")

    monkeypatch.setattr(runner, "_vigiar", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_criar_foto", quebra)
    monkeypatch.setattr(drenagem, "drenar", lambda *a, **k: None)
    monkeypatch.setattr(foto, "enviar", lambda *a, **k: None)
    runner.processar_base(cfg, agora=NOITE)
    runner.processar_base(cfg, agora=NOITE.replace(minute=31))
    assert len(tentativas) == 1


def _evento(n: int, observado_em: str):
    from app.erp_eventos.eventos import EventoFila

    return EventoFila(
        id=f"{n:064x}",
        base="mm",
        erp_codigo=n,
        seq=1,
        tipo="status_alterado",
        unidade=f"mm:{n}:1",
        payload={"id": f"{n:064x}", "n": n},
        observado_em=observado_em,
    )


class FlowGravando:
    """Dublê das duas rotas: guarda a ordem do que chegou."""

    ordem: list[str] = []

    def __init__(self, **kw):
        pass

    def enviar_lote(self, lote_id, eventos):
        FlowGravando.ordem.append(f"lote:{len(eventos)}")
        return RespostaHttp(
            202, {"resultados": [{"id": e["id"], "situacao": "aceito"} for e in eventos]}, None
        )

    def enviar_pagina(self, pagina):
        FlowGravando.ordem.append(f"pagina:{pagina['pagina']}")
        return RespostaHttp(202, {"ok": True}, None)

    def close(self):
        pass


def test_foto_espera_e_manda_antes_a_fila_anterior_ao_corte(cfg, monkeypatch):
    """Pedido faturado às 19h (fora da janela) chega ao Flow ANTES da foto das 5h."""
    FlowGravando.ordem = []
    monkeypatch.setattr(runner, "ClienteErpEventos", FlowGravando)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(
            conn,
            [
                _evento(1, "2026-10-15T22:00:00.000+00:00"),
                _evento(2, "2026-10-15T23:00:00.000+00:00"),
            ],
            "2026-10-15T23:00:00.000+00:00",
        )
    foto.criar(cfg, corte=datetime(2026, 10, 16, 8, 0, tzinfo=UTC), pedidos=pedidos_foto(1))

    runner._enviar_foto(cfg)

    assert FlowGravando.ordem == ["lote:2", "pagina:1"]


def test_evento_depois_do_corte_nao_segura_a_foto(cfg, monkeypatch):
    FlowGravando.ordem = []
    monkeypatch.setattr(runner, "ClienteErpEventos", FlowGravando)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    foto.criar(cfg, corte=datetime(2026, 10, 16, 8, 0, tzinfo=UTC), pedidos=pedidos_foto(1))
    with router.env_connect(cfg.env_slug) as conn:  # observado depois do corte: espera a janela
        erp_estado_repo.enfileirar(
            conn, [_evento(3, "2026-10-16T08:05:00.000+00:00")], "2026-10-16T08:05:00.000+00:00"
        )

    runner._enviar_foto(cfg)

    assert FlowGravando.ordem == ["pagina:1"]


def test_foto_nao_sai_se_a_fila_anterior_nao_entrou(cfg, monkeypatch):
    """Flow fora para os lotes: a foto não vai na frente dos eventos dela."""
    FlowGravando.ordem = []

    class FlowForaNosLotes(FlowGravando):
        def enviar_lote(self, lote_id, eventos):
            FlowGravando.ordem.append("lote:falhou")
            return RespostaHttp(503, None, None, erro="fora")

    monkeypatch.setattr(runner, "ClienteErpEventos", FlowForaNosLotes)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(
            conn, [_evento(1, "2026-10-15T22:00:00.000+00:00")], "2026-10-15T22:00:00.000+00:00"
        )
    foto.criar(cfg, corte=datetime(2026, 10, 16, 8, 0, tzinfo=UTC), pedidos=pedidos_foto(1))

    runner._enviar_foto(cfg)

    assert FlowGravando.ordem == ["lote:falhou"]


def test_foto_manda_so_o_anterior_ao_corte(cfg, monkeypatch):
    FlowGravando.ordem = []
    monkeypatch.setattr(runner, "ClienteErpEventos", FlowGravando)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(
            conn,
            [
                _evento(1, "2026-10-16T07:00:00.000+00:00"),
                _evento(9, "2026-10-16T09:00:00.000+00:00"),
            ],
            "2026-10-16T09:00:00.000+00:00",
        )
    foto.criar(cfg, corte=datetime(2026, 10, 16, 8, 0, tzinfo=UTC), pedidos=pedidos_foto(1))

    runner._enviar_foto(cfg)

    assert FlowGravando.ordem == ["lote:1", "pagina:1"]  # o evento das 9h espera a janela
    with router.env_connect(cfg.env_slug) as conn:
        assert erp_estado_repo.contar(conn, "mm").get("pendente") == 1


def test_foto_vencida_expira_mesmo_esperando_a_fila(cfg, monkeypatch):
    """Evento anterior ao corte que nunca entra não segura a foto em 'enviando' para sempre."""

    class FlowFora(FlowGravando):
        def enviar_lote(self, lote_id, eventos):
            return RespostaHttp(503, None, None, erro="fora")

    monkeypatch.setattr(runner, "ClienteErpEventos", FlowFora)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    with router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.enfileirar(
            conn, [_evento(1, "2026-10-15T22:00:00.000+00:00")], "2026-10-15T22:00:00.000+00:00"
        )
    foto_id = foto.criar(
        cfg, corte=datetime(2026, 10, 16, 8, 0, tzinfo=UTC), pedidos=pedidos_foto(1)
    )
    with router.env_connect(cfg.env_slug) as conn:
        conn.execute(
            "UPDATE erp_foto SET criada_em = '2020-01-01T00:00:00.000+00:00' WHERE foto_id = ?",
            (foto_id,),
        )

    runner._enviar_foto(cfg)

    with router.env_connect(cfg.env_slug) as conn:
        assert erp_foto_repo.em_envio(conn, "mm") is None
        situacao = conn.execute(
            "SELECT situacao FROM erp_foto WHERE foto_id = ?", (foto_id,)
        ).fetchone()[0]
    assert situacao == "expirada"


def test_enviar_foto_registra_origem_foto(cfg, monkeypatch):
    with router.env_connect("mm") as conn:
        erp_estado_repo.enfileirar(
            conn, [_evento(1, "2026-10-10T01:00:00.000+00:00")], "2026-10-10T01:00:00.000+00:00"
        )
    foto.criar(cfg, corte=NOITE, pedidos=pedidos_foto(2))

    class Flow:
        def __init__(self, **kw):
            pass

        def enviar_lote(self, lote_id, eventos):
            return RespostaHttp(
                202, {"resultados": [{"id": e["id"], "situacao": "aceito"} for e in eventos]}, None
            )

        def enviar_pagina(self, pagina):
            return RespostaHttp(202, {"completa": False}, None)

        def close(self):
            pass

    monkeypatch.setattr(runner, "ClienteErpEventos", Flow)
    monkeypatch.setattr(runner, "_ritmo", Ritmo(0))
    monkeypatch.setattr(runner, "_relogio", lambda: NOITE)
    runner._enviar_foto(cfg)
    with router.env_connect("mm") as conn:
        regs = erp_envio_lote_repo.recentes(conn, "mm")
    assert [(r.origem, r.eventos, r.aceitos) for r in regs] == [("foto", 1, 1)]


def test_tick_com_fila_vazia_e_sem_sinal_nao_cria_o_cliente_http(cfg, flow_lotes, monkeypatch):
    criados = []
    monkeypatch.setattr(runner, "ClienteErpEventos", lambda **k: criados.append(k))
    _ultimo_202(cfg, DOMINGO_18H_SP - timedelta(minutes=5))
    _tick(cfg, monkeypatch, DOMINGO_18H_SP)
    assert criados == []
