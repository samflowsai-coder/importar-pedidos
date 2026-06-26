from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.integrations.flowpcp.schema import (
    AcaoReconciliacao,
    ConfirmarReconciliacaoRequest,
    DecisoesResponse,
)


def test_parse_decisoes_response_from_contract():
    raw = {
        "decisoes": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "pedido_erp": "AW097",
                "cliente_cnpj": "12.345.678/0001-90",
                "nome_cliente": "MM Americanense",
                "prazo_entrega_original": "2026-07-10T03:00:00.000Z",
                "prazo_pactuado": "2026-07-17T03:00:00.000Z",
                "status": "em_pool",
                "motivo_decisao": "Negociado +7 dias",
                "atualizado_em": "2026-06-22T14:32:15.123Z",
            }
        ],
        "proximo_cursor": "2026-06-22T14:32:15.123Z",
    }
    resp = DecisoesResponse.model_validate(raw)
    assert len(resp.decisoes) == 1
    assert resp.decisoes[0].pedido_erp == "AW097"
    assert resp.decisoes[0].prazo_pactuado == "2026-07-17T03:00:00.000Z"
    assert resp.proximo_cursor == "2026-06-22T14:32:15.123Z"


def test_confirmar_request_rejects_unknown_acao():
    with pytest.raises(ValidationError):
        ConfirmarReconciliacaoRequest(acao="acao_inexistente")


def test_confirmar_request_serializes_optional_fields():
    req = ConfirmarReconciliacaoRequest(
        acao=AcaoReconciliacao.DATA_ATUALIZADA,
        fire_id_externo="AW097",
        observacoes="UPDATE OK",
    )
    body = req.model_dump(exclude_none=True)
    assert body == {
        "acao": "data_atualizada",
        "fire_id_externo": "AW097",
        "observacoes": "UPDATE OK",
    }
