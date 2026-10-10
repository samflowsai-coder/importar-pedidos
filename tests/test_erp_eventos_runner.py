"""Runner: uma etapa que falha não derruba as outras (revisão C1); foto inviável vira estado."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.erp_eventos import drenagem, foto, runner
from app.erp_eventos.envio import RespostaHttp, Ritmo
from app.erp_eventos.leitura import SEM_STATUS
from app.persistence import (
    db,
    environments_repo,
    erp_bases_repo,
    erp_estado_repo,
    erp_foto_repo,
    router,
)
from app.security import secret_store
from tests.test_erp_eventos_origem import pedidos_foto

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
NOITE = datetime(2026, 10, 10, 2, 30, tzinfo=UTC)  # 23:30 em São Paulo


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
    monkeypatch.setattr(foto, "enviar", lambda *a, **k: chamadas.append("foto"))
    runner.processar_base(cfg, agora=NOITE)
    assert chamadas == ["drenar", "foto"]
    assert (cfg.environment_id, cfg.base) in runner._FALHA_ERP  # próxima tentativa só em 5 min


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
