from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from app.erp_eventos.contrato import ContratoInvalidoError, validar_evento, validar_pagina
from app.erp_eventos.eventos import Vinculo, item_json, montar_eventos, pedido_json
from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.erp_eventos.mudancas import calcular_mudancas

T = "11111111-1111-4111-8111-111111111111"
OBS = "2026-10-09T13:00:00.000+00:00"


def item(codigo: int, qtd="10", cod_produto=4410, descricao="MEIA X", fat="0") -> ItemErp:
    return ItemErp(
        erp_item_codigo=codigo,
        cod_produto=cod_produto,
        produto_produtor=None,
        descricao=descricao,
        qtd=Decimal(qtd),
        qtd_faturada=Decimal(fat),
        cod_nf=None,
        preco_unitario=Decimal("3.15"),
        unid="PR",
        dt_entrega_item=None,
    )


def pedido(*itens, cnpj="11222333000144", status="PEDIDO") -> PedidoErp:
    return PedidoErp(
        erp_codigo=88213,
        pedido_cliente="PC-4471",
        cliente_erp_codigo=512,
        cliente_erp_cnpj=cnpj,
        cliente_real_cnpj=cnpj,
        status=status,
        data_pedido=date(2026, 10, 1),
        dt_entrega=date(2026, 11, 5),
        itens=tuple(itens),
    )


def eventos_novo(p, **kw):
    ms = calcular_mudancas(None, anterior_excluido=False, atual=p)
    args = dict(
        tenant=T,
        base="mm",
        observado_em=OBS,
        pedido=p,
        mudancas=ms,
        primeiro_seq=1,
        vinculos={},
        import_do_pedido=None,
    )
    args.update(kw)
    return montar_eventos(**args)


def test_visto_e_itens_mesma_unidade_seq_contiguo():
    evs = eventos_novo(pedido(item(1), item(2)))
    assert [e.seq for e in evs] == [1, 2, 3]
    assert len({e.unidade for e in evs}) == 1
    assert [e.tipo for e in evs] == ["pedido_visto", "item_incluido", "item_incluido"]


def test_payload_tem_todos_os_campos_com_null_e_numero_como_numero():
    ev = eventos_novo(pedido(item(1, qtd="1200.0000")))[1]
    p = ev.payload
    assert p["importId"] is None and p["indiceOriginal"] is None and p["correlacaoId"] is None
    assert p["item"]["qtd"] == 1200 and isinstance(p["item"]["qtd"], int)
    assert p["item"]["precoUnitario"] == 3.15
    assert p["pedido"]["dtEntrega"] == "2026-11-05"
    assert json.loads(json.dumps(p)) == p


def test_vinculo_leva_import_e_indice_do_item():
    evs = eventos_novo(
        pedido(item(1), item(2)), vinculos={2: Vinculo("imp-1", 3)}, import_do_pedido="imp-1"
    )
    assert (evs[2].payload["importId"], evs[2].payload["indiceOriginal"]) == ("imp-1", 3)
    assert (evs[0].payload["importId"], evs[0].payload["indiceOriginal"]) == ("imp-1", None)


def test_item_sem_produto_no_erp_vai_com_cod_produto_nulo():
    # Acordo com o Flow (2026-10-09): o Flow abre pendência "produto desconhecido";
    # deixar o pedido fora do envio seria perda silenciosa de cobertura.
    evs = eventos_novo(pedido(item(1), item(2, cod_produto=None)))
    assert evs[2].payload["item"]["codProduto"] is None


def test_cnpj_que_nao_tem_14_digitos_viola_o_contrato():
    with pytest.raises(ContratoInvalidoError, match="clienteErpCnpj"):
        eventos_novo(pedido(item(1), cnpj="12345678901"))


def test_correlacao_so_no_evento_de_data():
    antes = pedido(item(1))
    depois = PedidoErp(**{**antes.__dict__, "dt_entrega": date(2026, 12, 1)})
    ms = calcular_mudancas(antes, anterior_excluido=False, atual=depois)
    evs = montar_eventos(
        tenant=T,
        base="mm",
        observado_em=OBS,
        pedido=depois,
        mudancas=ms,
        primeiro_seq=7,
        vinculos={},
        import_do_pedido=None,
        correlacao_data_entrega="dec-9",
    )
    assert evs[0].payload["correlacaoId"] == "dec-9" and evs[0].seq == 7
    assert evs[0].payload["antes"] == "2026-11-05" and evs[0].payload["depois"] == "2026-12-01"


def test_contrato_recusa_numero_como_string_e_campo_omitido():
    ev = eventos_novo(pedido(item(1)))[1].payload
    with pytest.raises(ContratoInvalidoError):
        validar_evento({**ev, "item": {**ev["item"], "qtd": "10"}})
    sem_correlacao = {k: v for k, v in ev.items() if k != "correlacaoId"}
    with pytest.raises(ContratoInvalidoError):
        validar_evento(sem_correlacao)
    with pytest.raises(ContratoInvalidoError):
        validar_evento({**ev, "item": {**ev["item"], "descricao": "x" * 501}})


def test_contrato_aceita_lote_fiscal():
    validar_evento(
        {
            "id": "f" * 64,
            "base": "mm",
            "tipo": "lote_fiscal",
            "observadoEm": OBS,
            "seq": None,
            "importId": None,
            "indiceOriginal": None,
            "correlacaoId": None,
            "chaveLote": "2026-W41",
            "erpCodigoEspelho": 90001,
            "pernas": [
                {
                    "baseOrigem": "nasmar",
                    "erpCodigoOrigem": 7001,
                    "erpItemCodigoOrigem": 70011,
                    "produtoProdutor": 2613,
                    "quantidade": 300,
                    "linhaLote": 900011,
                }
            ],
        }
    )


def test_pagina_da_foto():
    p = pedido(item(1))
    pagina = {
        "schema": "pedido.snapshot_erp.v1",
        "base": "nasmar",
        "fotoId": "0b1f8a8e-1111-4111-8111-111111111111",
        "corteEm": OBS,
        "pagina": 1,
        "totalPaginas": 3,
        "fim": False,
        "pedidos": [{"pedido": pedido_json(p), "itens": [item_json(i) for i in p.itens]}],
    }
    validar_pagina(pagina)
    with pytest.raises(ContratoInvalidoError):
        validar_pagina({**pagina, "pagina": 4})
