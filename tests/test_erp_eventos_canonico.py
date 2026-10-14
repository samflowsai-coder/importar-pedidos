"""Vetores fixos do `id` (spec SP1 §4.1). Se um destes hashes mudar, TODO
evento já enviado ao Flow passa a ter outro id no reenvio — é quebra de
contrato, não refactor. Não "atualize o valor esperado": descubra o que mudou."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.erp_eventos.canonico import (
    decimal_canonico,
    formatar_instante,
    id_evento,
    numero_json,
    texto_canonico,
)

T = "11111111-1111-4111-8111-111111111111"
OBS = "2026-10-09T13:00:00.000+00:00"


def _qtd(**kw):
    base = dict(
        tenant=T,
        base="mm",
        tipo="quantidade_alterada",
        erp_codigo=88213,
        erp_item_codigo=990133,
        campo="qtd",
        antes=Decimal("1000.0000"),
        depois=Decimal("1200"),
        observado_em=OBS,
    )
    base.update(kw)
    return id_evento(**base)


def test_vetor_quantidade_alterada():
    assert _qtd() == "2c9d11974b912da4886449d9b7ce4b9f703425c71a1b23dc4f58f0aa102817f1"


def test_vetor_pedido_visto_sem_item_sem_campo():
    assert (
        id_evento(
            tenant=T,
            base="mm",
            tipo="pedido_visto",
            erp_codigo=88213,
            erp_item_codigo=None,
            campo=None,
            antes=None,
            depois=None,
            observado_em=OBS,
        )
        == "dca5c8f849901819e56f58a405dfc790cc1322399d31a91ed587ed30458587d0"
    )


def test_vetor_lote_fiscal():
    assert (
        id_evento(
            tenant=T,
            base="mm",
            tipo="lote_fiscal",
            erp_codigo=90001,
            erp_item_codigo=0,
            campo="2026-W41",
            antes=None,
            depois=None,
            observado_em=OBS,
        )
        == "932584fb4e49b16e3f128bdfae80cd81d58b6faf7a42efc6458989a3c9b28e84"
    )


def test_vetor_preco_para_nulo():
    assert (
        id_evento(
            tenant=T,
            base="mm",
            tipo="preco_alterado",
            erp_codigo=88213,
            erp_item_codigo=990133,
            campo="precoUnitario",
            antes=Decimal("3.1500"),
            depois=None,
            observado_em=OBS,
        )
        == "f291d05613d84fc7c208984bfafeca36ed99be5c3bb2e3a034a8ef502e555b9d"
    )


def test_mesmo_numero_em_representacoes_diferentes_da_o_mesmo_id():
    assert _qtd(antes=1000, depois=1200.0) == _qtd()
    assert _qtd(antes=Decimal("1E+3"), depois=Decimal("1200.000")) == _qtd()


def test_base_entra_no_hash():
    assert _qtd(base="nasmar") == "c08e5a2aa96d185bb1dcec7e21039eb22db2eaabb6a8f30904a1791080a64ff8"
    assert _qtd(base="nasmar") != _qtd()


def test_mudanca_repetida_10_20_10_20_gera_ids_distintos():
    ids = {
        _qtd(antes=Decimal(a), depois=Decimal(d), observado_em=o)
        for a, d, o in [
            ("10", "20", "2026-10-09T13:00:00.000+00:00"),
            ("20", "10", "2026-10-09T13:15:00.000+00:00"),
            ("10", "20", "2026-10-09T13:30:00.000+00:00"),
            ("20", "10", "2026-10-09T13:45:00.000+00:00"),
        ]
    }
    assert len(ids) == 4


def test_dois_itens_com_a_mesma_mudanca_na_mesma_vigia_tem_ids_distintos():
    assert _qtd(erp_item_codigo=1) != _qtd(erp_item_codigo=2)


def test_decimal_canonico():
    assert decimal_canonico(Decimal("1200.0000")) == "1200"
    assert decimal_canonico(Decimal("3.1500")) == "3.15"
    assert decimal_canonico(Decimal("-0.00")) == "0"
    assert decimal_canonico(Decimal("0.0500")) == "0.05"
    assert decimal_canonico(1200.0) == "1200"
    with pytest.raises(ValueError):
        decimal_canonico(Decimal("NaN"))


def test_texto_canonico_recusa_separador_e_tipos_fora_do_contrato():
    with pytest.raises(ValueError):
        texto_canonico("A\x1fB")
    with pytest.raises(TypeError):
        texto_canonico(True)
    with pytest.raises(TypeError):
        texto_canonico(datetime(2026, 10, 9, tzinfo=UTC))
    assert texto_canonico(date(2026, 11, 5)) == "2026-11-05"
    assert texto_canonico(None) == ""


def test_formatar_instante_sempre_utc_com_milissegundos():
    sp = timezone(timedelta(hours=-3))
    assert formatar_instante(datetime(2026, 10, 9, 10, 0, tzinfo=sp)) == OBS
    with pytest.raises(ValueError):
        formatar_instante(datetime(2026, 10, 9, 10, 0))


def test_numero_json_inteiro_quando_inteiro():
    assert numero_json(Decimal("1200.000")) == 1200
    assert isinstance(numero_json(Decimal("1200.000")), int)
    assert numero_json(Decimal("3.1500")) == 3.15
    assert numero_json(None) is None
