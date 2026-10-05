import json
from datetime import datetime
from pathlib import Path

import pytest

from app.ingestion.file_loader import LoadedFile
from app.models.order import Order, OrderHeader, OrderItem
from app.observability import leituras
from app.observability.leituras import (
    Diagnostico,
    limitar,
    montar_pedido,
    resumo_fire_check,
    serializar,
    sinais_fortes,
    sinais_suspeitos,
)

SAMPLES = Path(__file__).parent.parent / "samples"


def _item(desc="Sandalia Aurora", code="AF1", qty=10.0, price=5.0, total=None):
    return OrderItem(
        description=desc, product_code=code, quantity=qty, unit_price=price, total_price=total
    )


def _order(items=None, **hdr):
    base = dict(order_number="AF072", customer_name="Loja X", customer_cnpj="11222333000181")
    base.update(hdr)
    items = items if items is not None else [_item(code="A", qty=1), _item(code="B", qty=2)]
    return Order(header=OrderHeader(**base), items=items)


def _diag(**kw):
    d = Diagnostico(parser_usado="NasmarTemplateParser", validacao_ok=True, llm={"tentado": False})
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_pedido_limpo_sem_sinais():
    assert sinais_suspeitos(_order(), _diag()) == []


def test_order_none_sem_sinais():
    assert sinais_suspeitos(None, _diag()) == []


def test_parser_generico():
    assert "parser_generico" in sinais_suspeitos(_order(), _diag(parser_usado="GenericParser"))


def test_llm_usado():
    assert "llm_usado" in sinais_suspeitos(_order(), _diag(llm={"tentado": True, "motivo": "ok"}))


def test_sem_cnpj():
    assert "sem_cnpj" in sinais_suspeitos(_order(customer_cnpj=None), _diag())


def test_sem_cnpj_nao_dispara_se_algum_item_tem_cnpj_de_entrega():
    o = _order([_item(code="A", qty=1), _item(code="B", qty=2)], customer_cnpj=None)
    o.items[1].delivery_cnpj = "11.222.333/0001-81"
    assert "sem_cnpj" not in sinais_suspeitos(o, _diag())


def test_sem_cliente():
    assert "sem_cliente" in sinais_suspeitos(_order(customer_name=" "), _diag())


def test_numero_gerado():
    assert "numero_gerado" in sinais_suspeitos(_order(order_number_gerado=True), _diag())


@pytest.mark.parametrize("num", ["OBSERVACAO", "cnpj:", "Total", "DATA"])
def test_numero_parece_rotulo(num):
    assert "numero_parece_rotulo" in sinais_suspeitos(_order(order_number=num), _diag())


@pytest.mark.parametrize("desc", ["CNPJ:", "AB", None, ""])
def test_item_parece_rotulo(desc):
    o = _order([_item(desc=desc, code="A", qty=1), _item(code="B", qty=2)])
    assert "item_parece_rotulo" in sinais_suspeitos(o, _diag())


def test_qtd_fracionada():
    o = _order([_item(code="A", qty=1.5), _item(code="B", qty=2)])
    assert "qtd_fracionada" in sinais_suspeitos(o, _diag())


def test_sinais_removidos_nao_existem_mais():
    # kit legitimo: tudo igual, e sem preco (preco_ausente cobre o valor zero)
    o = _order([_item(code=c, qty=5, price=None) for c in "ABC"])
    sinais = sinais_suspeitos(o, _diag())
    assert "qtd_todas_iguais" not in sinais and "valor_total_zero_com_qtd" not in sinais
    assert "preco_ausente_em_todos" in sinais


def test_qtd_max_alta():
    o = _order([_item(code="A", qty=10001), _item(code="B", qty=2)])
    assert "qtd_max_alta" in sinais_suspeitos(o, _diag())


def test_preco_ausente_em_todos():
    o = _order([_item(code="A", qty=3, price=None), _item(code="B", qty=2, price=None)])
    assert "preco_ausente_em_todos" in sinais_suspeitos(o, _diag())


def test_quantidade_nan_nao_levanta():
    o = _order([_item(code="A", qty=float("nan")), _item(code="B", qty=float("inf"))])
    sinais_suspeitos(o, _diag())
    assert montar_pedido(o)["qtd_total"] == 0


def test_codigos_repetidos_so_com_mesma_loja_e_data():
    o = _order([_item(code="A", qty=1), _item(code="A", qty=2)])
    assert "codigos_repetidos" in sinais_suspeitos(o, _diag())
    for campo, valor in (("delivery_cnpj", "111"), ("delivery_name", "Loja 2"), ("delivery_date", "01/11/2026")):
        o = _order([_item(code="A", qty=1), _item(code="A", qty=2)])
        setattr(o.items[1], campo, valor)
        assert "codigos_repetidos" not in sinais_suspeitos(o, _diag()), campo


def test_validacao_reprovada():
    assert "validacao_reprovada" in sinais_suspeitos(_order(), _diag(validacao_ok=False))


def test_cliente_igual_ao_ambiente():
    o = _order(customer_cnpj="11.222.333/0001-81")
    assert "cliente_igual_ao_ambiente" in sinais_suspeitos(o, _diag(), "11222333000181")
    assert "cliente_igual_ao_ambiente" not in sinais_suspeitos(o, _diag(), "99")
    assert "cliente_igual_ao_ambiente" not in sinais_suspeitos(o, _diag())


def test_caso_quan_lido_pelo_generico_dispara_varios():
    o = Order(
        header=OrderHeader(),
        items=[
            OrderItem(description="CNPJ:", quantity=100),
            OrderItem(description="Sandalia Aurora", quantity=1),
        ],
    )
    sinais = sinais_suspeitos(o, _diag(parser_usado="GenericParser", validacao_ok=False))
    assert len(sinais) >= 5
    for s in ("parser_generico", "sem_cnpj", "sem_cliente", "item_parece_rotulo"):
        assert s in sinais


@pytest.mark.parametrize(
    "nome",
    ["Pedido Authentic Feet AF093 TOTAL KITS.xlsx", "Pedido Authentic Feet AF072 QUAN.xlsx"],
)
def test_samples_bons_nao_disparam_sinal(nome):
    from app import pipeline

    p = SAMPLES / nome
    if not p.exists():
        pytest.skip(f"Sample not found: {nome}")
    loaded = LoadedFile(path=p, extension=p.suffix.lower(), raw=p.read_bytes())
    diag = Diagnostico()
    order = pipeline.process_detailed(loaded, diag)
    assert order is not None
    assert sinais_suspeitos(order, diag) == []


def test_forte_so_para_sinais_de_leitura_errada():
    from app.observability.leituras import sinais_fortes

    assert sinais_fortes(["parser_generico"]) and sinais_fortes(["sem_cliente", "llm_usado"])
    for s in ("item_parece_rotulo", "numero_parece_rotulo", "validacao_reprovada", "cliente_igual_ao_ambiente"):
        assert sinais_fortes([s])
    for s in ("numero_gerado", "sem_cliente", "preco_ausente_em_todos", "sem_cnpj", "qtd_fracionada"):
        assert not sinais_fortes([s])
    assert not sinais_fortes([])


_ARQUIVOS_SAMPLES = sorted(
    p.name for p in SAMPLES.glob("*") if p.suffix.lower() in (".xlsx", ".xls", ".pdf")
)
_EM_BRANCO = "Planilha modelo cliente Kings.xlsx"  # modelo vazio: cai no GenericParser


@pytest.mark.parametrize("nome", _ARQUIVOS_SAMPLES)
def test_todos_os_samples_forte_so_no_modelo_em_branco(nome, monkeypatch):
    from app import pipeline

    monkeypatch.setattr(pipeline._llm, "parse", lambda *a, **k: None)  # nunca chama o LLM
    p = SAMPLES / nome
    loaded = LoadedFile(path=p, extension=p.suffix.lower(), raw=p.read_bytes())
    diag = Diagnostico()
    try:
        order = pipeline.process_detailed(loaded, diag)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"nao abre: {type(exc).__name__}")
    if order is None:
        pytest.skip("sem pedido")
    forte = sinais_fortes(sinais_suspeitos(order, diag))
    assert forte is (nome == _EM_BRANCO), (nome, sinais_suspeitos(order, diag))


# --- montar_pedido -----------------------------------------------------------


def test_montar_pedido_none():
    assert montar_pedido(None) is None


def test_montar_pedido_campos():
    o = _order(
        [
            _item(code="A", qty=2, price=10, total=20),
            _item(code=None, qty=3, price=4, total=None),
            _item(code="C", qty=1, price=None, total=None),
        ],
        issue_date="02/10/2026",
        supplier_cnpj="123",
    )
    p = montar_pedido(o)
    assert p == {
        "numero": "AF072",
        "numero_gerado": False,
        "emissao": "02/10/2026",
        "cliente": "Loja X",
        "cnpj": "11222333000181",
        "supplier_cnpj": "123",
        "itens": 3,
        "qtd_total": 6,
        "valor_total": 32.0,
        "itens_sem_preco": 1,
        "itens_sem_codigo": 1,
    }


# --- resumo_fire_check -------------------------------------------------------


def test_resumo_fire_check_none():
    assert resumo_fire_check(None) is None


def test_resumo_fire_check_indisponivel():
    check = {
        "available": False,
        "reason": "FB_DATABASE_NOT_SET",
        "client": {"match": False, "fire_id": None, "razao_social": None, "cnpj": "1"},
        "env": {"password": "segredo"},
        "items": [{"product_code": "A", "ean": None, "match": False}],
        "summary": {"items_total": 1, "items_matched": 0, "items_missing": 1},
    }
    r = resumo_fire_check(check)
    assert r["available"] is False
    assert r["reason"] == "FB_DATABASE_NOT_SET"
    assert r["nao_encontrados"] == []
    assert "segredo" not in json.dumps(r)


def test_resumo_fire_check_completo_sem_chaves_extras():
    itens = [{"product_code": f"P{i}", "ean": None, "match": False} for i in range(25)]
    itens.append({"product_code": "OK", "ean": None, "match": True})
    itens.append({"product_code": None, "ean": "789", "match": False})
    check = {
        "available": True,
        "reason": None,
        "env": {"fb_password": "x"},
        "client": {"match": True, "fire_id": 123, "razao_social": "ACME", "cnpj": "1"},
        "items": itens,
        "summary": {
            "items_total": 27,
            "items_matched": 1,
            "items_missing": 26,
            "items_troca_no_fire": 2,
            "price_summary": {
                "items_match": 1,
                "items_mismatch": 3,
                "items_no_price_in_fire": 4,
                "items_no_order_price": 5,
            },
        },
    }
    r = resumo_fire_check(check)
    assert set(r) == {
        "available",
        "reason",
        "cliente",
        "itens",
        "nao_encontrados",
        "preco",
        "troca_no_fire",
    }
    assert r["cliente"] == {"match": True, "fire_id": 123, "razao": "ACME"}
    assert r["itens"] == {"total": 27, "match": 1, "missing": 26}
    assert r["preco"] == {"match": 1, "mismatch": 3, "sem_preco_fire": 4, "sem_preco_pedido": 5}
    assert r["troca_no_fire"] == 2
    assert len(r["nao_encontrados"]) == 20
    assert "OK" not in r["nao_encontrados"]
    assert "env" not in json.dumps(r)


def test_resumo_fire_check_nao_encontrados_usa_ean_quando_sem_codigo():
    check = {
        "available": True,
        "client": {},
        "items": [{"product_code": None, "ean": "789", "match": False}],
        "summary": {},
    }
    assert resumo_fire_check(check)["nao_encontrados"] == ["789"]


# --- limitar / serializar ----------------------------------------------------


def test_limitar_listas_ate_20_e_marca_truncado():
    linha = {"v": 1, "avisos": [str(i) for i in range(30)], "outro": [1, 2]}
    r = limitar(linha)
    assert len(r["avisos"]) == 20 and r["outro"] == [1, 2]
    assert r["truncado"] is True
    assert len(linha["avisos"]) == 30  # nao muta a entrada


def test_limitar_linha_pequena_intacta():
    linha = {"v": 1, "avisos": ["a"]}
    assert limitar(linha) == linha
    assert "truncado" not in limitar(linha)


def test_limitar_16kb_derruba_campos_pesados():
    big = "x" * 1000
    linha = {
        "v": 1,
        "validacao": {"ok": False, "avisos": [big] * 20},
        "fire_check": {"available": True, "nao_encontrados": [big] * 20},
        "extracao": {"fmt": "xlsx", "cabecalho_candidato": {"linha": 1, "celulas": [big] * 20}},
    }
    r = limitar(linha)
    assert len(serializar(r).encode("utf-8")) <= 16 * 1024
    assert r["truncado"] is True
    assert r["validacao"]["avisos"] == []
    assert r["fire_check"]["nao_encontrados"] == []
    assert r["extracao"]["cabecalho_candidato"]["celulas"] == []
    assert r["validacao"]["ok"] is False


def test_serializar_datetime_float_e_acento():
    s = serializar({"ts": datetime(2026, 10, 5, 11, 0), "v": 1.5, "cliente": "Calçados Ação"})
    d = json.loads(s)
    assert d["v"] == 1.5 and d["cliente"] == "Calçados Ação"
    assert "2026-10-05" in d["ts"]
    assert "\n" not in s and "ç" in s


def test_modulo_nao_importa_pesados_no_topo():
    import sys

    assert leituras.__name__ in sys.modules
    src = Path(leituras.__file__).read_text(encoding="utf-8")
    topo = src.split("def ", 1)[0]
    for pesado in ("app.pipeline", "app.web", "app.persistence", "app.erp"):
        assert f"import {pesado}" not in topo and f"from {pesado}" not in topo
