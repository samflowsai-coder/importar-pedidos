"""Ferramenta tools/flow_enviar_pedidos_fire.py — lote, livro, parada por rejeição."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.erp_eventos.modelo import ItemErp, PedidoErp
from app.integrations.flowpcp.client import FlowPCPClientError

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "flow_enviar_pedidos_fire.py"
_spec = importlib.util.spec_from_file_location("flow_enviar_pedidos_fire", _TOOL)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

HOJE = date(2026, 10, 22)


def _pedido(codigo: int, **kw) -> PedidoErp:
    item = ItemErp(
        erp_item_codigo=codigo * 10,
        cod_produto=3914,
        produto_produtor=None,
        descricao="KIT X",
        qtd=Decimal("12"),
        qtd_faturada=Decimal(0),
        cod_nf=None,
        preco_unitario=Decimal("10"),
        unid="KIT",
        dt_entrega_item=None,
    )
    base = dict(
        erp_codigo=codigo,
        pedido_cliente=f"PC{codigo}",
        cliente_erp_codigo=1,
        cliente_erp_cnpj="12345678000195",
        cliente_real_cnpj=None,
        status="PEDIDO",
        data_pedido=date(2026, 9, 1),
        dt_entrega=date(2026, 11, 30),
        itens=(item,),
    )
    base.update(kw)
    return PedidoErp(**base)


def _rodar(pedidos, livro, enviar, **kw):
    sleeps: list[float] = []
    contagem = tool.processar(
        pedidos,
        hoje=HOJE,
        desde=kw.pop("desde", None),
        slug="mm",
        razoes={1: "Cliente SA"},
        codigos_flow={},
        livro=livro,
        enviar=enviar,
        intervalo_s=7.0,
        max_falhas=kw.pop("max_falhas", 1),
        dormir=sleeps.append,
    )
    return contagem, sleeps


def _linhas(livro: Path) -> list[dict]:
    return [json.loads(x) for x in livro.read_text().splitlines()]


def test_dry_run_nao_chama_o_flow(tmp_path):
    chamadas = []
    contagem, _ = _rodar([_pedido(1), _pedido(2)], tmp_path / "l.jsonl", None)
    assert chamadas == []
    assert contagem["enviado"] == 0
    assert [r["resultado"] for r in _linhas(tmp_path / "l.jsonl")] == ["dry_run", "dry_run"]


def test_envia_registra_no_livro_e_respeita_o_intervalo(tmp_path):
    mandados = []

    def enviar(req):
        mandados.append(req.pedidoNumero)
        return {"pedidoItemIds": ["a"], "status": "pendente", "itens": []}

    contagem, sleeps = _rodar([_pedido(1), _pedido(2), _pedido(3)], tmp_path / "l.jsonl", enviar)
    assert mandados == ["PC1", "PC2", "PC3"]
    assert contagem["enviado"] == 3
    assert sleeps == [7.0, 7.0]  # entre POSTs, nunca antes do primeiro


def test_pedido_ja_enviado_no_livro_nao_e_reenviado(tmp_path):
    livro = tmp_path / "l.jsonl"
    enviar = lambda req: {"pedidoItemIds": []}  # noqa: E731
    _rodar([_pedido(1)], livro, enviar)
    mandados = []
    contagem, _ = _rodar(
        [_pedido(1), _pedido(2)], livro, lambda r: mandados.append(r.pedidoNumero) or {}
    )
    assert mandados == ["PC2"]
    assert contagem["pulado"] == 1


def test_recusa_local_nao_chega_ao_flow_e_vai_pro_livro(tmp_path):
    mandados = []
    ruim = replace(_pedido(1), status="FATURADO")
    contagem, _ = _rodar(
        [ruim, _pedido(2)], tmp_path / "l.jsonl", lambda r: mandados.append(1) or {}
    )
    assert mandados == [1]
    assert contagem["recusa_local"] == 1
    assert _linhas(tmp_path / "l.jsonl")[0]["resultado"] == "recusa_local"


def test_para_quando_o_flow_rejeita_mais_que_o_limite(tmp_path):
    def enviar(req):
        raise FlowPCPClientError("POST status 400", status_code=400)

    contagem, _ = _rodar([_pedido(i) for i in range(1, 6)], tmp_path / "l.jsonl", enviar)
    assert contagem["falha_flow"] == 2  # a 2ª rejeição passa do limite de 1 e para
    assert len(_linhas(tmp_path / "l.jsonl")) == 2


def test_already_processed_conta_a_parte(tmp_path):
    contagem, _ = _rodar(
        [_pedido(1)],
        tmp_path / "l.jsonl",
        lambda r: {"alreadyProcessed": True, "pedidoItemIds": []},
    )
    assert contagem["ja_processado"] == 1
    assert contagem["enviado"] == 0


def test_codigos_do_flow_vem_do_relatorio(tmp_path):
    csv_ = tmp_path / "r.csv"
    csv_.write_text(
        "Base;Pedido ERP;Item ERP;Produto ERP (Flow)\n"
        "mm;4709;40259;3914\nmm;4709;40261;KIT-2972\nmm;4710;1;\n",
        encoding="utf-8-sig",
    )
    assert tool.codigos_flow_do_relatorio(csv_) == {4709: {40259: "3914", 40261: "KIT-2972"}}
