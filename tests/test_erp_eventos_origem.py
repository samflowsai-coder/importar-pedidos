"""Lote fiscal (emissor), vínculo exato (insert e xlsx), leitura do ERP, foto e config."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.erp import queries
from app.erp_eventos import foto, lote_fiscal, vinculo
from app.erp_eventos.contrato import ContratoInvalidoError
from app.erp_eventos.leitura import LeitorFirebird
from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.exporters.erp_exporter import ERPExporter
from app.exporters.firebird_exporter import _to_erp_rows_indexados
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import db, environments_repo, erp_bases_repo, erp_vinculo_repo, router
from app.persistence.erp_vinculo_repo import LinhaXlsx
from app.security import secret_store

TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "fpe_" + "c" * 43
T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


@pytest.fixture
def mm(tmp_path: Path, monkeypatch):
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
        foto_hora=23,
        ativo=True,
        token=TOKEN,
    )
    yield erp_bases_repo.obter(env["id"])
    db.set_db_path(None)
    db.reset_init_cache()


def perna(item=70011, qtd="300"):
    return lote_fiscal.PernaLote("nasmar", 7001, item, 2613, Decimal(qtd), 900011)


# ── lote fiscal ──────────────────────────────────────────────────────────


def test_lote_fiscal_exige_reserva(mm):
    with router.env_connect("mm") as conn, pytest.raises(lote_fiscal.ReservaAusenteError):
        lote_fiscal.registrar_lote_fiscal(
            conn,
            tenant=TENANT,
            chave_lote="2026-W41",
            base_espelho="mm",
            erp_codigo_espelho=90001,
            pernas=[perna()],
            agora=T0,
        )


def test_lote_fiscal_entra_na_fila_sem_seq_e_libera_a_reserva(mm):
    with router.env_connect("mm") as conn:
        lote_fiscal.reservar_lote(conn, chave_lote="2026-W41", base_espelho="mm", agora=T0)
        ev = lote_fiscal.registrar_lote_fiscal(
            conn,
            tenant=TENANT,
            chave_lote="2026-W41",
            base_espelho="mm",
            erp_codigo_espelho=90001,
            pernas=[perna()],
            agora=T0,
        )
        assert not lote_fiscal.reserva_ativa(conn, "mm", T0)
        row = conn.execute("SELECT seq, tipo, erp_codigo FROM erp_eventos_saida").fetchone()
    assert tuple(row) == (None, "lote_fiscal", 90001)
    assert ev.payload["pernas"][0]["quantidade"] == 300 and ev.payload["seq"] is None


def test_item_de_origem_em_um_unico_lote(mm):
    with router.env_connect("mm") as conn:
        lote_fiscal.reservar_lote(conn, chave_lote="A", base_espelho="mm", agora=T0)
        lote_fiscal.registrar_lote_fiscal(
            conn,
            tenant=TENANT,
            chave_lote="A",
            base_espelho="mm",
            erp_codigo_espelho=1,
            pernas=[perna()],
            agora=T0,
        )
    with pytest.raises(lote_fiscal.PernaEmOutroLoteError), router.env_connect("mm") as conn:
        lote_fiscal.reservar_lote(conn, chave_lote="B", base_espelho="mm", agora=T0)
        lote_fiscal.registrar_lote_fiscal(
            conn,
            tenant=TENANT,
            chave_lote="B",
            base_espelho="mm",
            erp_codigo_espelho=2,
            pernas=[perna()],
            agora=T0,
        )


def test_reserva_abandonada_nao_segura_mais(mm):
    with router.env_connect("mm") as conn:
        lote_fiscal.reservar_lote(conn, chave_lote="A", base_espelho="mm", agora=T0)
        assert lote_fiscal.reserva_ativa(conn, "mm", T0)
        assert not lote_fiscal.reserva_ativa(conn, "mm", T0.replace(hour=15))


# ── vínculo ──────────────────────────────────────────────────────────────


def order_intercalado() -> Order:
    """Itens de duas lojas intercaladas: a ordem do INSERT ≠ a de order.items."""
    return Order(
        header=OrderHeader(order_number="PC-1", customer_name="X", customer_cnpj="11222333000144"),
        items=[
            OrderItem(
                description="A",
                product_code="A",
                quantity=1,
                unit_price=1,
                delivery_cnpj="22333444000155",
            ),
            OrderItem(
                description="B",
                product_code="B",
                quantity=2,
                unit_price=1,
                delivery_cnpj="33444555000166",
            ),
            OrderItem(
                description="C",
                product_code="C",
                quantity=3,
                unit_price=1,
                delivery_cnpj="22333444000155",
            ),
        ],
    )


def test_indice_do_insert_e_a_posicao_em_order_items_nao_o_contador():
    pares = [(i, r.codigo_produto) for i, r in _to_erp_rows_indexados(order_intercalado())]
    assert pares == [(0, "A"), (2, "C"), (1, "B")]


def test_linhas_por_arquivo_casam_com_os_arquivos_do_export(tmp_path):
    order = order_intercalado()
    paths = ERPExporter().export(order, str(tmp_path))
    conteudo = ERPExporter().linhas_por_arquivo(order)
    assert len(paths) == len(conteudo) == 2
    assert [idx for _, idx in conteudo] == [[0, 2], [1]]
    assert [[r.codigo_produto for r in rows] for rows, _ in conteudo] == [["A", "C"], ["B"]]


def ped_erp(qtds, numero="PC-1", cnpj="22333444000155") -> PedidoErp:
    return PedidoErp(
        erp_codigo=500,
        pedido_cliente=numero,
        cliente_erp_codigo=1,
        cliente_erp_cnpj=cnpj,
        cliente_real_cnpj=None,
        status="PEDIDO",
        data_pedido=None,
        dt_entrega=None,
        itens=tuple(
            ItemErp(100 + n, 1, None, "x", Decimal(q), Decimal(0), None, None, None, None)
            for n, q in enumerate(qtds)
        ),
    )


def arquivo(imp, indices_qtds, numero="PC-1", cnpj="22333444000155"):
    return vinculo.ArquivoXlsx(
        imp,
        f"{imp}.xlsx",
        tuple(
            LinhaXlsx(imp, f"{imp}.xlsx", n, idx, numero, cnpj, q, None)
            for n, (idx, q) in enumerate(indices_qtds, 1)
        ),
    )


def test_casamento_por_posicao_unico():
    r = vinculo.casar_por_posicao(
        ped_erp(["1", "3"]), [arquivo("imp", [(0, "1"), (2, "3")])], comparar_preco=False
    )
    assert r == ("imp", [(100, 0), (101, 2)])


def test_casamento_ambiguo_ou_divergente_nao_vincula():
    p = ped_erp(["1", "3"])
    dois = [arquivo("a", [(0, "1"), (2, "3")]), arquivo("b", [(0, "1"), (2, "3")])]
    assert vinculo.casar_por_posicao(p, dois, comparar_preco=False) is None
    assert (
        vinculo.casar_por_posicao(p, [arquivo("a", [(0, "1"), (2, "4")])], comparar_preco=False)
        is None
    )
    assert vinculo.casar_por_posicao(p, [arquivo("a", [(0, "1")])], comparar_preco=False) is None
    outro_cliente = [arquivo("a", [(0, "1"), (2, "3")], cnpj="99999999000199")]
    assert vinculo.casar_por_posicao(p, outro_cliente, comparar_preco=False) is None


def test_numero_truncado_e_com_zeros_ainda_casa():
    longo = "PEDIDO-MUITO-LONGO-QUE-PASSA-DE-20"
    p = ped_erp(["1"], numero=longo[:20])
    assert vinculo.casar_por_posicao(
        p, [arquivo("a", [(0, "1")], numero=longo)], comparar_preco=False
    )


def test_casar_novos_exige_que_o_recebimento_tenha_levado_a_mesma_quantidade(mm):
    with router.env_connect("mm") as conn:
        erp_vinculo_repo.gravar_linhas_xlsx(
            conn,
            "imp",
            list(arquivo("imp", [(0, "1"), (2, "3")]).linhas),
            "2026-10-09T12:00:00.000+00:00",
        )
        n = vinculo.casar_novos_por_xlsx(
            conn,
            base="mm",
            pedidos=[ped_erp(["1", "3"])],
            comparar_preco=False,
            quantidade_enviada=lambda imp, idx: None,
            desde="2026-01-01",
            agora="2026-10-09T13:00:00.000+00:00",
        )
        assert n == 0  # sem push ao Flow, sem vínculo
        n = vinculo.casar_novos_por_xlsx(
            conn,
            base="mm",
            pedidos=[ped_erp(["1", "3"])],
            comparar_preco=False,
            quantidade_enviada=lambda imp, idx: Decimal({0: "1", 2: "3"}[idx]),
            desde="2026-01-01",
            agora="2026-10-09T13:00:00.000+00:00",
        )
        assert n == 1
        v = erp_vinculo_repo.vinculos_por_item(conn, "mm", [100, 101])
    assert {k: (x.import_id, x.indice_original, x.origem) for k, x in v.items()} == {
        100: ("imp", 0, "xlsx"),
        101: ("imp", 2, "xlsx"),
    }


def test_registrar_insert_grava_vinculo_da_base(mm):
    from app.persistence import context as env_context

    with env_context.active_env(mm.environment_id, "mm"):
        vinculo.registrar_insert(
            "imp-9", environment_id=mm.environment_id, erp_codigo=88, itens=[(880, 0), (881, 2)]
        )
    with router.env_connect("mm") as conn:
        v = erp_vinculo_repo.vinculos_por_item(conn, "mm", [880, 881])
    assert {k: (x.indice_original, x.origem) for k, x in v.items()} == {
        880: (0, "insert"),
        881: (2, "insert"),
    }


# ── leitura do ERP ───────────────────────────────────────────────────────


def test_todo_sql_da_vigia_e_select():
    sqls = [
        queries.LIST_PEDIDOS_ABERTOS_ERP,
        queries.pedidos_erp_por_codigo_sql(3),
        queries.itens_erp_por_pedido_sql(3),
        queries.faturado_erp_por_item_sql(3),
        queries.clientes_reais_por_pedidos_cliente_sql(3),
        queries.CODIGOS_POR_PEDIDO_CLIENTE_E_CLIENTE,
    ]
    for sql in sqls:
        assert sql.strip().upper().startswith("SELECT"), sql
        for palavra in ("UPDATE ", "INSERT ", "DELETE ", "MERGE ", "EXECUTE "):
            assert palavra not in sql.upper()


class CursorFake:
    def __init__(self, respostas):
        self._respostas = respostas
        self._ultimo = None

    def execute(self, sql, params=None):
        chave = "CAB" if "CAB_VENDAS" in sql else "NFS" if "NFS" in sql else "ITENS"
        self._ultimo = self._respostas[chave]

    def fetchall(self):
        return self._ultimo

    def close(self):
        pass


class ConnFake:
    def __init__(self, respostas):
        self._r = respostas

    def cursor(self):
        return CursorFake(self._r)


def test_leitor_monta_pedido_com_faturado_acumulado_e_cnpj_so_de_14_digitos():
    conn = ConnFake(
        {
            "CAB": [
                (
                    1,
                    "PC-1",
                    512,
                    "11.222.333/0001-44",
                    "PEDIDO",
                    datetime(2026, 10, 1, 0, 0),
                    date(2026, 11, 5),
                ),
                (2, None, 7, "123.456.789-01", "PEDIDO", None, None),
            ],
            "ITENS": [
                (10, 1, 4410, "MEIA ", Decimal("12.0000"), Decimal("3.1500"), "PR ", None),
                (11, 1, None, None, Decimal("5"), None, None, None),
            ],
            "NFS": [(10, Decimal("8.0000"), 5512)],
        }
    )
    ps = {p.erp_codigo: p for p in LeitorFirebird(conn).abertos()}
    p = ps[1]
    assert p.cliente_erp_cnpj == "11222333000144" and p.data_pedido == date(2026, 10, 1)
    assert [(i.erp_item_codigo, i.qtd_faturada, i.cod_nf, i.cod_produto) for i in p.itens] == [
        (10, Decimal("8"), 5512, 4410),
        (11, Decimal("0"), None, None),
    ]
    assert ps[2].cliente_erp_cnpj is None and ps[2].pedido_cliente is None and ps[2].itens == ()


# ── foto ─────────────────────────────────────────────────────────────────


def pedidos_foto(n):
    return [
        PedidoErp(
            c,
            "P",
            1,
            None,
            None,
            "PEDIDO",
            None,
            None,
            (ItemErp(c * 10, 1, 1, "x", Decimal(1), Decimal(0), None, None, None, None),),
        )
        for c in range(1, n + 1)
    ]


def test_foto_paginada_ate_200_pedidos_com_total_constante():
    paginas = foto.montar_paginas(
        base="mm",
        foto_id="0b1f8a8e-1111-4111-8111-111111111111",
        corte_em="2026-10-09T02:00:00.000+00:00",
        pedidos=pedidos_foto(450),
    )
    assert [len(p["pedidos"]) for p in paginas] == [200, 200, 50]
    assert {p["totalPaginas"] for p in paginas} == {3}
    assert [p["fim"] for p in paginas] == [False, False, True]


def test_foto_sem_pedidos_e_uma_pagina_vazia():
    paginas = foto.montar_paginas(
        base="mm",
        foto_id="0b1f8a8e-1111-4111-8111-111111111111",
        corte_em="2026-10-09T02:00:00.000+00:00",
        pedidos=[],
    )
    assert len(paginas) == 1 and paginas[0]["pedidos"] == [] and paginas[0]["fim"]


def test_foto_com_pedido_fora_do_contrato_nao_e_criada():
    ruim = pedidos_foto(1)[0]
    ruim = PedidoErp(**{**ruim.__dict__, "status": "X" * 41})
    with pytest.raises(ContratoInvalidoError):
        foto.montar_paginas(
            base="mm",
            foto_id="0b1f8a8e-1111-4111-8111-111111111111",
            corte_em="2026-10-09T02:00:00.000+00:00",
            pedidos=[ruim],
        )


def test_foto_devida_so_dentro_da_hora_e_uma_por_dia(mm):
    noite = datetime(2026, 10, 10, 2, 30, tzinfo=UTC)  # 23:30 em São Paulo (foto_hora=23)
    tarde = datetime(2026, 10, 9, 18, 0, tzinfo=UTC)
    assert foto.devida(mm, noite, ja_existe=False)
    assert not foto.devida(mm, noite, ja_existe=True)
    assert not foto.devida(mm, tarde, ja_existe=False)


def test_foto_nao_sai_depois_da_hora_dela(mm):
    """Ligar a base às 22h com foto às 5h não tira a foto na hora (14/10/2026)."""
    cinco = replace(mm, foto_hora=5)
    sp = ZoneInfo("America/Sao_Paulo")
    assert foto.devida(cinco, datetime(2026, 10, 15, 5, 0, tzinfo=sp), ja_existe=False)
    assert foto.devida(cinco, datetime(2026, 10, 15, 5, 59, tzinfo=sp), ja_existe=False)
    assert not foto.devida(cinco, datetime(2026, 10, 15, 6, 0, tzinfo=sp), ja_existe=False)
    assert not foto.devida(cinco, datetime(2026, 10, 14, 22, 19, tzinfo=sp), ja_existe=False)
    assert not foto.devida(cinco, datetime(2026, 10, 15, 4, 59, tzinfo=sp), ja_existe=False)


# ── config da base ───────────────────────────────────────────────────────


def test_token_cifrado_e_nunca_exposto(mm):
    with router.shared_connect() as conn:
        enc = conn.execute("SELECT token_enc FROM erp_bases").fetchone()[0]
    assert enc and TOKEN not in enc
    assert mm.token == TOKEN
    view = erp_bases_repo.public_view(mm)
    assert TOKEN not in json.dumps(view) and "token" not in view and view["token_ilegivel"] is False
    assert mm.intermediarios == frozenset()


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("base", "MM"),
        ("papel", "dona"),
        ("tenant_id", "x"),
        ("destino_url", "http://flow.prod"),
        ("foto_hora", 24),
        ("token", "segredo"),
    ],
)
def test_config_invalida_recusada(mm, campo, valor):
    args = dict(
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=None,
        ativo=True,
        token=None,
    )
    args[campo] = valor
    with pytest.raises(erp_bases_repo.ConfigErpInvalidaError):
        erp_bases_repo.salvar(mm.environment_id, **args)


def test_none_mantem_o_token_e_vazio_limpa(mm):
    args = dict(
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=None,
        ativo=True,
    )
    erp_bases_repo.salvar(mm.environment_id, **args, token=None)
    assert erp_bases_repo.obter(mm.environment_id).token == TOKEN
    erp_bases_repo.salvar(mm.environment_id, **args, token="")
    assert erp_bases_repo.obter(mm.environment_id).token is None
    assert erp_bases_repo.listar_ativas() == []  # ligada sem credencial não roda


def test_schema_novo_nao_tem_fk_para_imports(mm):
    with router.env_connect("mm") as conn:
        for t in ("erp_pedido_estado", "erp_eventos_saida", "erp_item_vinculo", "erp_seq_marca"):
            assert conn.execute(f"PRAGMA foreign_key_list({t})").fetchall() == []
    assert isinstance(conn, sqlite3.Connection)


def test_credencial_que_nao_decifra_aparece_como_ilegivel(mm, tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / "outra.key")  # chave trocada
    cfg = erp_bases_repo.obter(mm.environment_id)
    assert cfg.token is None and cfg.token_ilegivel
    assert erp_bases_repo.public_view(cfg)["token_ilegivel"] is True
