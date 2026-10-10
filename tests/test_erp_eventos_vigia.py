"""Vigia + estado + seq + fila, com o ERP dublado (nenhum Firebird real)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.erp_eventos import eco, lote_fiscal, vigia
from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.persistence import db, environments_repo, erp_bases_repo, router
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "fpe_" + "a" * 43
NASMAR = "34513679000134"
T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


def item(codigo, qtd="10", fat="0", cod_nf=None, preco="3.15"):
    return ItemErp(
        erp_item_codigo=codigo,
        cod_produto=4410,
        produto_produtor=None,
        descricao="MEIA X",
        qtd=Decimal(qtd),
        qtd_faturada=Decimal(fat),
        cod_nf=cod_nf,
        preco_unitario=Decimal(preco),
        unid="PR",
        dt_entrega_item=None,
    )


def pedido(codigo=88213, *itens, status="PEDIDO", dt=date(2026, 11, 5), cnpj="11222333000144"):
    return PedidoErp(
        erp_codigo=codigo,
        pedido_cliente="PC-4471",
        cliente_erp_codigo=512,
        cliente_erp_cnpj=cnpj,
        cliente_real_cnpj=None,
        status=status,
        data_pedido=date(2026, 10, 1),
        dt_entrega=dt,
        itens=tuple(itens) or (item(1), item(2)),
    )


class Leitor:
    def __init__(self, abertos=(), outros=None):
        self._abertos = list(abertos)
        self._outros = outros or {}

    def abertos(self):
        return self._abertos

    def por_codigo(self, codigos):
        return {c: self._outros[c] for c in codigos if c in self._outros}


@pytest.fixture
def bases(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    saida = {}
    for slug, papel, inter in (("mm", "produtora", NASMAR), ("nasmar", "intermediaria", None)):
        env = environments_repo.create(
            slug=slug,
            name=slug.upper(),
            watch_dir=str(tmp_path / slug / "w"),
            output_dir=str(tmp_path / slug / "o"),
            fb_path="",
        )
        if inter:
            environments_repo.set_intercompany_config(env["id"], cnpj=inter, revenda_slug="nasmar")
        erp_bases_repo.salvar(
            env["id"],
            base=slug,
            papel=papel,
            tenant_id=TENANT,
            destino_url="https://flow.test",
            foto_hora=23,
            ativo=True,
            token=TOKEN,
        )
        saida[slug] = erp_bases_repo.obter(env["id"])
    yield saida
    db.set_db_path(None)
    db.reset_init_cache()


def rodar(cfg, leitor, agora=T0):
    return vigia.vigiar(
        cfg,
        leitor=leitor,
        agora=agora,
        resolver_clientes=lambda ps: {p.erp_codigo: p.cliente_erp_cnpj for p in ps},
        resolver_produtores=lambda ps: {
            i.erp_item_codigo: i.cod_produto for p in ps for i in p.itens
        },
        casar_vinculos=lambda novos: None,
    )


def fila(cfg):
    with router.env_connect(cfg.env_slug) as conn:
        rows = conn.execute(
            "SELECT tipo, seq, erp_codigo, unidade, payload_json FROM erp_eventos_saida ORDER BY ordem"
        ).fetchall()
    return [
        (r["tipo"], r["seq"], r["erp_codigo"], r["unidade"], json.loads(r["payload_json"]))
        for r in rows
    ]


def test_primeira_vigia_manda_visto_e_itens_na_mesma_unidade(bases):
    r = rodar(bases["mm"], Leitor([pedido()]))
    evs = fila(bases["mm"])
    assert r.eventos == 3
    assert [(t, s) for t, s, *_ in evs] == [
        ("pedido_visto", 1),
        ("item_incluido", 2),
        ("item_incluido", 3),
    ]
    assert len({u for *_, u, _ in evs}) == 1
    assert evs[1][4]["item"]["produtoProdutor"] == 4410  # base produtora: o próprio produto


def test_vigia_sem_mudanca_nao_gera_evento_mas_marca_sinal_de_vida(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    r = rodar(bases["mm"], Leitor([pedido()]), agora=T0 + timedelta(minutes=15))
    assert r.eventos == 0
    with router.env_connect("mm") as conn:
        assert conn.execute("SELECT sinal_pendente FROM erp_envio_estado").fetchone()[0] == 1


def test_mudanca_de_quantidade_continua_o_seq(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    rodar(
        bases["mm"],
        Leitor([pedido(88213, item(1, qtd="12"), item(2))]),
        agora=T0 + timedelta(minutes=15),
    )
    ultimo = fila(bases["mm"])[-1]
    assert (ultimo[0], ultimo[1], ultimo[4]["antes"], ultimo[4]["depois"]) == (
        "quantidade_alterada",
        4,
        10,
        12,
    )


def test_seq_perene_depois_de_recriar_o_estado(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    with router.env_connect("mm") as conn:
        conn.execute("DELETE FROM erp_pedido_estado")
    rodar(bases["mm"], Leitor([pedido()]), agora=T0 + timedelta(minutes=15))
    seqs = [s for _, s, *_ in fila(bases["mm"])]
    assert seqs == [1, 2, 3, 4, 5, 6]  # novo pedido_visto segue de 4, nunca volta a 1


def test_saida_de_aberto_relida_por_codigo_fecha_com_faturamento_antes_do_status(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    faturado = pedido(
        88213, item(1, fat="10", cod_nf=5512), item(2, fat="10", cod_nf=5512), status="FATURADO"
    )
    rodar(bases["mm"], Leitor([], {88213: faturado}), agora=T0 + timedelta(minutes=15))
    tipos = [t for t, *_ in fila(bases["mm"])][3:]
    assert tipos == ["faturamento_detectado", "faturamento_detectado", "status_alterado"]
    assert fila(bases["mm"])[3][4]["codNf"] == 5512
    with router.env_connect("mm") as conn:
        assert conn.execute("SELECT situacao FROM erp_pedido_estado").fetchone()[0] == "encerrado"
    # encerrado não é relido de novo
    assert rodar(bases["mm"], Leitor([]), agora=T0 + timedelta(minutes=30)).eventos == 0


def test_excluido_e_reaparecido_segue_o_seq(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    rodar(bases["mm"], Leitor([]), agora=T0 + timedelta(minutes=15))
    rodar(bases["mm"], Leitor([pedido()]), agora=T0 + timedelta(minutes=30))
    assert [(t, s) for t, s, *_ in fila(bases["mm"])][3:] == [
        ("pedido_excluido", 4),
        ("pedido_visto", 5),
        ("item_incluido", 6),
        ("item_incluido", 7),
    ]


def test_reabertura_cancelado_volta_a_pedido(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    rodar(
        bases["mm"],
        Leitor([], {88213: pedido(status="CANCELADO")}),
        agora=T0 + timedelta(minutes=15),
    )
    rodar(bases["mm"], Leitor([pedido()]), agora=T0 + timedelta(minutes=30))
    ultimo = fila(bases["mm"])[-1]
    assert (ultimo[0], ultimo[4]["antes"], ultimo[4]["depois"]) == (
        "status_alterado",
        "CANCELADO",
        "PEDIDO",
    )


def test_mesmo_codigo_nas_duas_bases_nao_colide(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    rodar(bases["nasmar"], Leitor([pedido()]))
    ids_mm = {p["id"] for *_, p in fila(bases["mm"])}
    ids_nasmar = {p["id"] for *_, p in fila(bases["nasmar"])}
    assert not ids_mm & ids_nasmar
    assert [s for _, s, *_ in fila(bases["nasmar"])] == [1, 2, 3]
    assert {p["base"] for *_, p in fila(bases["nasmar"])} == {"nasmar"}


def test_pedido_fora_do_contrato_fica_de_fora_sem_consumir_seq(bases):
    ruim = replace(pedido(), status="X" * 41)
    r = rodar(bases["mm"], Leitor([ruim, pedido(1)]))
    assert r.fora_do_contrato == 1
    assert {c for _, _, c, *_ in fila(bases["mm"])} == {1}
    with router.env_connect("mm") as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM erp_seq_marca WHERE erp_codigo = 88213").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM erp_pedido_estado WHERE erp_codigo = 88213"
            ).fetchone()[0]
            == 0
        )


def test_reserva_de_lote_segura_pedido_novo_para_intermediario(bases):
    with router.env_connect("mm") as conn:
        lote_fiscal.reservar_lote(conn, chave_lote="2026-W41", base_espelho="mm", agora=T0)
    r = rodar(bases["mm"], Leitor([pedido(90001, cnpj=NASMAR), pedido(1)]))
    assert r.adiados == 1
    assert {c for _, _, c, *_ in fila(bases["mm"])} == {1}


def test_eco_da_decisao_aplicada_vai_com_correlacao(bases):
    rodar(bases["mm"], Leitor([pedido()]))
    with router.env_connect("mm") as conn:
        eco.registrar_aplicacao(
            conn,
            "dec-77",
            base="mm",
            erp_codigos=[88213, 99],
            nova_data=date(2026, 12, 1),
            agora="2026-10-09T13:05:00.000+00:00",
        )
    agora = datetime.now(UTC)
    rodar(bases["mm"], Leitor([pedido(dt=date(2026, 12, 1))]), agora=agora)
    ultimo = fila(bases["mm"])[-1]
    assert (ultimo[0], ultimo[4]["correlacaoId"]) == ("data_entrega_alterada", "dec-77")
