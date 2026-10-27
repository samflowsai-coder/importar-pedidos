"""Registro de lotes de envio ao Flow (`erp_envio_lote`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.erp_eventos import runner
from app.persistence import (
    db,
    environments_repo,
    erp_bases_repo,
    router,
)
from app.persistence import erp_envio_lote_repo as repo
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
AGORA = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


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
    runner._ULTIMA_RETENCAO.clear()
    yield erp_bases_repo.obter(env["id"])
    db.set_db_path(None)
    db.reset_init_cache()


def _abrir(conn, lote_id, em="2026-10-26T12:00:00+00:00", origem="janela", eventos=1):
    repo.abrir(
        conn,
        lote_id=lote_id,
        base="mm",
        origem=origem,
        quem=None,
        envio_id=None,
        enviado_em=em,
        eventos=eventos,
    )


def test_abrir_e_fechar_lote(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        repo.abrir(
            conn,
            lote_id="L1",
            base="mm",
            origem="manual",
            quem="adm@x.com",
            envio_id="E1",
            enviado_em="2026-10-26T12:00:00+00:00",
            eventos=3,
        )
        repo.fechar(
            conn,
            "L1",
            http=202,
            aceitos=2,
            duplicados=0,
            rejeitados=1,
            respondido_em="2026-10-26T12:00:02+00:00",
        )
        linhas = repo.recentes(conn, "mm")
    assert len(linhas) == 1
    r = linhas[0]
    assert (r.lote_id, r.origem, r.quem, r.envio_id, r.eventos) == (
        "L1",
        "manual",
        "adm@x.com",
        "E1",
        3,
    )
    assert (r.http, r.aceitos, r.duplicados, r.rejeitados) == (202, 2, 0, 1)
    assert r.respondido_em == "2026-10-26T12:00:02+00:00"


def test_recentes_ordem_e_limite(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        for i in range(25):
            _abrir(conn, f"L{i:02d}", em=f"2026-10-26T12:{i:02d}:00+00:00")
        linhas = repo.recentes(conn, "mm")
    assert len(linhas) == 20
    assert linhas[0].lote_id == "L24"
    assert linhas[-1].lote_id == "L05"


def test_origens_por_lote_id(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        _abrir(conn, "A", origem="foto")
        _abrir(conn, "B", origem="manual")
        achados = repo.origens(conn, ["A", "B", "ZZ"])
        vazio = repo.origens(conn, [])
    assert set(achados) == {"A", "B"}
    assert achados["A"].origem == "foto"
    assert vazio == {}


def test_apagar_antigos(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        _abrir(conn, "velho", em="2026-09-01T00:00:00+00:00")
        _abrir(conn, "novo", em="2026-10-20T00:00:00+00:00")
        n = repo.apagar_antigos(conn, "2026-10-01T00:00:00+00:00")
        restam = [r.lote_id for r in repo.recentes(conn, "mm")]
    assert n == 1
    assert restam == ["novo"]


def test_apagar_antigos_poupa_lote_de_evento_recusado(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        _abrir(conn, "recusou", em="2026-09-01T00:00:00+00:00")
        _abrir(conn, "aceitou", em="2026-09-01T00:00:00+00:00")
        for i, (lote, sit) in enumerate([("recusou", "rejeitado"), ("aceitou", "aceito")]):
            conn.execute(
                "INSERT INTO erp_eventos_saida (id, base, erp_codigo, seq, tipo, unidade, "
                "payload_json, observado_em, situacao, lote_id, criado_em) "
                "VALUES (?, 'mm', 1, ?, 'pedido_visto', 'u', '{}', 'x', ?, ?, 'x')",
                (f"e{i}", i, sit, lote),
            )
        n = repo.apagar_antigos(conn, "2026-10-01T00:00:00+00:00")
        restam = [r.lote_id for r in repo.recentes(conn, "mm")]
    assert n == 1
    assert restam == ["recusou"]


def test_origem_invalida(cfg):
    with router.env_connect(cfg.env_slug) as conn, pytest.raises(ValueError):
        _abrir(conn, "X", origem="sei_la")


def test_retencao_do_runner_apaga_lotes(cfg):
    velho = (AGORA - timedelta(days=31)).isoformat()
    novo = (AGORA - timedelta(days=1)).isoformat()
    with router.env_connect(cfg.env_slug) as conn:
        _abrir(conn, "velho", em=velho)
        _abrir(conn, "novo", em=novo)
    runner._reter(cfg, AGORA)
    with router.env_connect(cfg.env_slug) as conn:
        restam = [r.lote_id for r in repo.recentes(conn, "mm")]
    assert restam == ["novo"]
