"""tools/vincular_imports_fire.py — pareia import `parsed` com pedido aberto do Fire."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.erp_eventos.modelo import ItemErp, PedidoErp

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "vincular_imports_fire.py"
_spec = importlib.util.spec_from_file_location("vincular_imports_fire", _TOOL)
tool = importlib.util.module_from_spec(_spec)
sys.modules["vincular_imports_fire"] = tool
_spec.loader.exec_module(tool)


def _pedido(codigo, numero, cnpj, dia=date(2026, 8, 27), n=12) -> PedidoErp:
    itens = tuple(
        ItemErp(i, 1, None, "KIT", Decimal(12), Decimal(0), None, None, None, None)
        for i in range(n)
    )
    return PedidoErp(codigo, numero, 1, cnpj, None, "PEDIDO", dia, None, itens)


def _imp(id_, numero, cnpj, dia="2026-08-27", n=12, entrega=(), override=None) -> dict:
    return {
        "id": id_,
        "order_number": numero,
        "customer_cnpj": cnpj,
        "imported_at": dia,
        "n_itens": n,
        "delivery_cnpjs": set(entrega),
        "cliente_override_codigo": override,
    }


def test_norm_iguala_as_grafias_que_o_fire_e_o_portal_usam():
    n = tool.norm_numero
    assert n("AF009") == n("AF-09") == n("AF09") == "AF9"
    assert n("TENNIS STATION LJ 13 14M") == n("TSLJ1314M") == "TSLJ1314M"
    assert n("06839395-0000") == "068393950000"


def test_par_unico_por_cnpj_data_e_itens():
    r = tool.parear(
        [_imp("i1", "TENNIS STATION LJ 180 181", "09530553000120")],
        [_pedido(4692, "TSLJ180181", "09530553000120")],
        ja_ligados=set(),
    )
    assert r.pares == [("i1", 4692)]


def test_cnpj_de_entrega_tambem_ancora():
    r = tool.parear(
        [_imp("i1", "X", "00063960022331", entrega=["00063960058794"], n=2)],
        [_pedido(4821, "06839395-0000", "00063960058794", n=2)],
        ja_ligados=set(),
    )
    assert r.pares == [("i1", 4821)]


def test_itens_ou_data_diferentes_nao_pareiam():
    imp = [_imp("i1", "A", "09530553000120")]
    assert tool.parear(imp, [_pedido(1, "A", "09530553000120", n=11)], set()).sem_par == ["i1"]
    longe = _pedido(1, "A", "09530553000120", dia=date(2026, 9, 20))
    assert tool.parear(imp, [longe], set()).sem_par == ["i1"]


def test_varios_pedidos_candidatos_desempata_pelo_numero():
    r = tool.parear(
        [_imp("i1", "TENNIS STATION LJ 13 14M", "52671393000169")],
        [
            _pedido(4683, "TS", "52671393000169", dia=date(2026, 8, 25)),
            _pedido(4695, "TSLJ1314M", "52671393000169"),
        ],
        ja_ligados=set(),
    )
    assert r.pares == [("i1", 4695)]


def test_varios_candidatos_sem_desempate_e_ambiguo():
    r = tool.parear(
        [_imp("i1", "ZZZ", "52671393000169")],
        [_pedido(1, "A", "52671393000169"), _pedido(2, "B", "52671393000169")],
        ja_ligados=set(),
    )
    assert r.pares == []
    assert r.ambiguos == [("i1", [1, 2])]


def test_tres_uploads_do_mesmo_pedido_so_um_liga():
    """Artwalk: 'AF09 NOVEMBRO', 'AF-09' e 'AF09' são o mesmo pedido enviado 3 vezes."""
    imps = [
        _imp("a", "AF09 NOVEMBRO", "10319758000140", dia="2026-09-09", n=10),
        _imp("b", "AF-09", "10319758000140", dia="2026-09-10", n=10),
        _imp("c", "AF09", "10319758000140", dia="2026-09-10", n=10),
    ]
    r = tool.parear(imps, [_pedido(4792, "AF009", "10319758000140", date(2026, 9, 10), 10)], set())
    assert r.pares == [("b", 4792)]  # número igual vence; entre iguais, o mais antigo
    assert sorted(i for i, _ in r.duplicatas) == ["a", "c"]


def test_pedido_ja_ligado_a_outro_import_fica_fora():
    r = tool.parear(
        [_imp("i1", "A", "09530553000120")],
        [_pedido(4692, "A", "09530553000120")],
        ja_ligados={4692},
    )
    assert r.pares == [] and r.sem_par == ["i1"]
