"""I4 (revisão final): produto `ligado` + trava de preço fora de `ligado`.

A regra de preço de hoje (`is_blocking`) lia `price_status`/`fire_preco_venda` do
match LEGADO, que pode ser outro produto que não o decidido. Em `ligado` o item
`ok` é reclassificado pelo `preco_venda` do candidato decidido (lido do Fire no
mesmo check) antes da regra de hoje.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.erp import preco_trava, product_check
from tests.test_produto_saida import LIGADO, _order, isolated_sqlite  # noqa: F401


def _item(*, legado_seq, legado_preco, legado_status, decidido_seq, decidido_preco):
    return {
        "product_code": "C1",
        "ean": None,
        "match": True,
        "fire_product_id": legado_seq,
        "fire_preco_venda": legado_preco,
        "unit_price_order": 50.0,
        "price_status": legado_status,
        "price_diff": None,
        "produto": {
            "estado": "ok",
            "fire_seq": decidido_seq,
            "fonte": "depara",
            "motivo": "",
            "candidatos": [
                {
                    "seq": decidido_seq,
                    "descricao": "D",
                    "codprod_altern": "A",
                    "bloqueado": False,
                    "unidade": "PAR",
                    "ean": None,
                    "preco_venda": decidido_preco,
                }
            ],
            "troca_no_fire": None,
        },
    }


def _decidir(monkeypatch, item, env=LIGADO):
    check = {"available": True, "produto_conferencia_incompleta": False, "items": [item]}
    monkeypatch.setattr(product_check, "check_order", lambda order, **kw: check)
    monkeypatch.setattr(preco_trava, "_export_mode", lambda: "db")
    order = _order({"product_code": "C1"})
    order.items[0].unit_price = 50.0
    d = preco_trava.decidir_envio("id", order, {}, env=env, agora=datetime.now(UTC))
    return d, check


def test_decidido_com_preco_igual_passa(monkeypatch):
    item = _item(
        legado_seq=10,
        legado_preco=50.0,
        legado_status="match",
        decidido_seq=10,
        decidido_preco=50.0,
    )
    d, _ = _decidir(monkeypatch, item)
    assert d.bloqueado is False


def test_legado_de_outro_produto_divergente_nao_bloqueia_mais(monkeypatch):
    """O match legado (SEQ 99, R$ 80) bloquearia; o decidido (SEQ 10, R$ 50) bate."""
    item = _item(
        legado_seq=99,
        legado_preco=80.0,
        legado_status="mismatch",
        decidido_seq=10,
        decidido_preco=50.0,
    )
    d, check = _decidir(monkeypatch, item)
    assert d.bloqueado is False
    # O check aprovado (saída) não é mexido.
    assert check["items"][0]["price_status"] == "mismatch"
    assert d.check is check


def test_decidido_com_preco_divergente_bloqueia(monkeypatch):
    """O legado (outro produto) bateria; o decidido diverge: bloqueia."""
    item = _item(
        legado_seq=99,
        legado_preco=50.0,
        legado_status="match",
        decidido_seq=10,
        decidido_preco=70.0,
    )
    d, _ = _decidir(monkeypatch, item)
    assert (d.bloqueado, d.reason) == (True, "price_check_failed")
    assert d.block_detail["items_mismatch"][0]["fire_price"] == 70.0


def test_decidido_sem_preco_no_fire_pede_confirmacao(monkeypatch):
    item = _item(
        legado_seq=99,
        legado_preco=50.0,
        legado_status="match",
        decidido_seq=10,
        decidido_preco=None,
    )
    d, _ = _decidir(monkeypatch, item)
    assert d.bloqueado is True
    assert d.block_detail["items_no_price_unacked"]


@pytest.mark.parametrize("m", ["desligado", "observando"])
def test_fora_de_ligado_regra_de_hoje_igual(monkeypatch, m):
    """Fora de `ligado` nada muda: o legado divergente segue bloqueando."""
    item = _item(
        legado_seq=99,
        legado_preco=80.0,
        legado_status="mismatch",
        decidido_seq=10,
        decidido_preco=50.0,
    )
    d, _ = _decidir(monkeypatch, item, env={"slug": "mm", "produto_chave_modo": m})
    assert (d.bloqueado, d.reason) == (True, "price_check_failed")


def test_candidato_serializa_preco_venda():
    from app.erp.produto_chave import Candidato, DecisaoProduto

    d = DecisaoProduto(
        estado="ok",
        fire_seq=10,
        fonte="ean",
        motivo="",
        candidatos=(Candidato(10, "D", "A", False, "PAR", 12.5, None),),
    )
    assert product_check._produto_dict(d)["candidatos"][0]["preco_venda"] == 12.5
