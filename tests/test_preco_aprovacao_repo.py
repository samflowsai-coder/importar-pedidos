"""Aprovações de preço por item (app/persistence/preco_aprovacao_repo.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.erp.preco_avaliacao import ItemAvaliado
from app.models.order import OrderItem
from app.persistence import preco_aprovacao_repo as repo
from app.persistence import router

AGORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
DEPOIS = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as c:
        yield c


def _item(preco=54.0, qtd=10):
    return OrderItem(description="x", quantity=qtd, unit_price=preco)


def _aval(i=0, preco=5400, tabela=5000, motivo="preco_diferente"):
    return ItemAvaliado(i, "123", "fila", motivo, preco, tabela)


def _pendencia(conn, import_id="imp1", i=0, preco=54.0):
    ass = repo.assinatura(_item(preco), "123")
    ids = repo.registrar_pendencias(
        conn, import_id, [(_aval(i, round(preco * 100)), ass)], agora=AGORA
    )
    return ass, ids


def _decidir(conn, ass, **kw):
    base = {
        "import_id": "imp1",
        "item_index": 0,
        "assinatura": ass,
        "decisao": "acordo",
        "preco_final_centavos": 5400,
        "por": "rafael",
        "em": DEPOIS,
        "motivo": None,
        "flow_id": "f1",
    }
    base.update(kw)
    return repo.aplicar_decisao(conn, **base)


def test_assinatura_muda_com_preco_quantidade_e_produto():
    base = repo.assinatura(_item(), "123")
    assert base == repo.assinatura(_item(), "123")
    assert base != repo.assinatura(_item(preco=55.0), "123")
    assert base != repo.assinatura(_item(qtd=11), "123")
    assert base != repo.assinatura(_item(), "124")
    assert repo.assinatura(_item(preco=None), None)


def test_decisao_com_assinatura_antiga_e_descartada(conn):
    ass_antiga, _ = _pendencia(conn, preco=54.0)
    ass_nova = repo.assinatura(_item(55.0), "123")
    assert ass_nova != ass_antiga
    assert _decidir(conn, ass_antiga) is True  # linha da assinatura antiga ainda existe
    # item editado: a assinatura atual é outra -> decisão da antiga não vale
    assert repo.decisoes_validas(conn, "imp1", {0: ass_nova}) == {}


def test_decisao_sem_linha_com_a_assinatura_devolve_false(conn):
    _pendencia(conn, preco=54.0)
    ass_nova = repo.assinatura(_item(55.0), "123")
    assert _decidir(conn, ass_nova) is False
    assert repo.decisoes_validas(conn, "imp1", {0: ass_nova}) == {}


def test_decisao_valida_aparece(conn):
    ass, _ = _pendencia(conn)
    assert _decidir(conn, ass) is True
    v = repo.decisoes_validas(conn, "imp1", {0: ass})
    assert v[0]["decisao"] == "acordo"
    assert v[0]["preco_final_centavos"] == 5400
    assert v[0]["decidido_por"] == "rafael"


def test_pendente_nao_e_decisao_valida(conn):
    ass, _ = _pendencia(conn)
    assert repo.decisoes_validas(conn, "imp1", {0: ass}) == {}


def test_reavaliar_nao_duplica_pendencia(conn):
    ass, ids = _pendencia(conn)
    assert len(ids) == 1
    again = repo.registrar_pendencias(conn, "imp1", [(_aval(0), ass)], agora=DEPOIS)
    assert again == []
    assert conn.execute("SELECT COUNT(*) FROM preco_aprovacao").fetchone()[0] == 1
    _decidir(conn, ass)
    again = repo.registrar_pendencias(conn, "imp1", [(_aval(0), ass)], agora=DEPOIS)
    assert again == []
    assert conn.execute("SELECT COUNT(*) FROM preco_aprovacao").fetchone()[0] == 1


def test_recusado_fica_registrado_com_motivo(conn):
    ass, _ = _pendencia(conn)
    ok = _decidir(
        conn, ass, decisao="recusado", preco_final_centavos=None, motivo="preço fora do acordo"
    )
    assert ok is True
    v = repo.decisoes_validas(conn, "imp1", {0: ass})[0]
    assert v["decisao"] == "recusado"
    assert v["motivo_recusa"] == "preço fora do acordo"
    assert v["preco_final_centavos"] is None


def test_segunda_decisao_sobrescreve(conn):
    ass, _ = _pendencia(conn)
    _decidir(conn, ass, decisao="recusado", preco_final_centavos=None, motivo="não")
    assert _decidir(conn, ass, decisao="acordo", flow_id="f2") is True
    v = repo.decisoes_validas(conn, "imp1", {0: ass})[0]
    assert v["decisao"] == "acordo"
    assert v["flow_id"] == "f2"


def test_pendentes_nao_enviadas_e_marcar_enviada(conn):
    ass0, ids0 = _pendencia(conn, "a", 0)
    ass1 = repo.assinatura(_item(60.0), "123")
    ids1 = repo.registrar_pendencias(conn, "b", [(_aval(0, 6000), ass1)], agora=DEPOIS)
    fila = repo.pendentes_nao_enviadas(conn, 10)
    assert [r["id"] for r in fila] == [ids0[0], ids1[0]]
    assert fila[0]["motivo_fila"] == "preco_diferente"
    assert len(repo.pendentes_nao_enviadas(conn, 1)) == 1
    repo.marcar_enviada(conn, ids0, em=DEPOIS)
    assert [r["id"] for r in repo.pendentes_nao_enviadas(conn, 10)] == ids1


def test_decidida_nao_volta_para_a_fila_de_envio(conn):
    ass, _ = _pendencia(conn)
    _decidir(conn, ass)
    assert repo.pendentes_nao_enviadas(conn, 10) == []


def test_cancelar_do_pedido_tira_da_fila_e_marca_para_avisar_o_flow(conn):
    ass, ids = _pendencia(conn)
    ass2 = repo.assinatura(_item(60.0), "999")
    ids2 = repo.registrar_pendencias(conn, "imp1", [(_aval(1, 6000), ass2)], agora=AGORA)
    repo.marcar_enviada(conn, ids, em=AGORA)  # só a primeira foi ao Flow

    marcadas = repo.cancelar_do_pedido(conn, "imp1", motivo="pedido_cancelado", em=DEPOIS)
    assert sorted(marcadas) == sorted(ids + ids2)
    assert repo.pendentes_nao_enviadas(conn, 10) == []
    assert repo.decisoes_validas(conn, "imp1", {0: ass}) == {}
    # reaplicar é no-op; decisão tardia do Flow é descartada
    assert repo.cancelar_do_pedido(conn, "imp1", motivo="pedido_cancelado", em=DEPOIS) == []
    assert _decidir(conn, ass) is False

    avisar = repo.cancelamentos_nao_avisados(conn, 10)
    assert [r["id"] for r in avisar] == ids  # a nunca enviada não precisa de aviso
    assert avisar[0]["motivo_cancelamento"] == "pedido_cancelado"
    repo.marcar_cancelamento_avisado(conn, ids, em=DEPOIS)
    assert repo.cancelamentos_nao_avisados(conn, 10) == []


def test_cancelar_marca_tambem_linha_decidida(conn):
    ass, ids = _pendencia(conn)
    _decidir(conn, ass)
    assert repo.cancelar_do_pedido(conn, "imp1", motivo="substituido", em=DEPOIS) == ids
    assert repo.decisoes_validas(conn, "imp1", {0: ass}) == {}
