"""
Maps our Order/ERPRow models to Fire Sistemas Firebird table rows.

Schema + data patterns verified against MM_AMERICANENSE 2026-04-21 backup
(Firebird 2.5 → restored to Firebird 5, ODS 13.1 with WIN1252 charset).

Key design decisions (data-driven):
- STATUS='PEDIDO' for newly imported orders — matches production convention
  (other statuses: 'EM ANÁLISE', 'FATURADO', 'CANCELADO').
- DOCUMENTO left NULL — the retailer's reference goes to PEDIDO_CLIENTE only.
- CLINAOCAD path abandoned — production has zero rows using it; every CAB_VENDAS
  has a CLIENTE FK. If client CNPJ can't be resolved in CADASTRO we skip and
  surface an error rather than insert an orphan record.
- CODPRODUTO may be NULL when product not found; item still inserted with description.
- Dates parsed from DD/MM/YYYY (already normalized by OrderNormalizer).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from app.erp.fiscal import PerfilFiscal
from app.models.order import ERPRow, Order


def _digits_only(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _item_total(item: ERPRow) -> float:
    """Regra unica do total de um item: valor_total explicito quando presente,
    senao qtd * preco_unitario (4 casas). Usada em CORPO_VENDAS.TOTAL aqui e
    na soma de CAB_VENDAS.VALOR_TOTAL no exporter — cabecalho e itens tem que
    nascer do MESMO numero por item, nunca de duas contas independentes.
    """
    if item.valor_total is not None:
        return item.valor_total
    qty = item.quantidade or 0.0
    unit_price = item.preco_unitario or 0.0
    return round(qty * unit_price, 4)


class FireSistemasMapper:
    """Maps Order model to Fire Sistemas CAB_VENDAS + CORPO_VENDAS rows."""

    EMPRESA_CODIGO = 1  # default company code; override via FB_CODEMPRESA env var
    STATUS_INICIAL = "PEDIDO"
    USUARIO_SISTEMA = "IMPORTADOR"

    def order_to_cabvendas(
        self,
        order: Order,
        header_pk: int,
        client_id: int,
        *,
        perfil: PerfilFiscal,
        valor_total: Decimal,
        obs: str | None = None,
        dt_entrega: date | None = None,
    ) -> tuple:
        """Tupla posicional para INSERT_CAB_VENDAS (23 elementos).

        `valor_total` vem calculado de fora (soma dos itens) porque o mapper
        nao conhece o resultado do de-para de produto nem do fator de preco.

        `obs` e `dt_entrega` eram cravados em None ate 2026-08. O lote
        intercompany depende dos dois: OBS carrega a lista de pedidos de
        origem, DT_ENTREGA a menor data das pernas.
        """
        import os

        empresa = int(os.environ.get("FB_CODEMPRESA", self.EMPRESA_CODIGO))
        pedido_cliente = (order.header.order_number or "")[:20] or None
        data_pedido = _parse_date(order.header.issue_date) or date.today()

        return (
            header_pk,                    # CODIGO
            empresa,                      # CODEMPRESA
            data_pedido,                  # DATA_PEDIDO
            client_id,                    # CLIENTE
            self.STATUS_INICIAL,          # STATUS = 'PEDIDO'
            pedido_cliente,               # PEDIDO_CLIENTE
            obs,                          # OBS
            dt_entrega,                   # DT_ENTREGA
            dt_entrega,                   # DT_BASE_FAT (= DT_ENTREGA em 100% dos medidos)
            valor_total,                  # VALOR_TOTAL
            valor_total,                  # TOTAL_PRODUTO
            Decimal("0"),                 # DESCONTO
            perfil.tipo_cob,              # TIPO_COB
            perfil.cod_class_finan,       # COD_CLASS_FINAN
            perfil.desc_class_finan,      # DESC_CLASS_FINAN
            perfil.classif_fat,           # CLASSIF_FAT
            perfil.codfigfiscal,          # CODFIGFISCAL
            perfil.mecanico,              # MECANICO
            "Nao",                        # SEM_IMP
            "Nao",                        # PED_ZF
            "Nao",                        # EH_VENDACONSUMIDOR
            Decimal("0"),                 # VENDEDOR_COMI
            self.USUARIO_SISTEMA,         # ULT_INS_USER
        )

    def item_to_corpovendas(
        self,
        item: ERPRow,
        item_pk: int,
        header_pk: int,
        product_seq: int | None,
        *,
        perfil: PerfilFiscal,
        unid: str = "UN",
    ) -> tuple:
        """Tupla posicional para INSERT_CORPO_VENDAS (15 elementos).

        `unid` vem do cadastro do produto no Fire. Ate 2026-08 era cravado
        "UN" aqui, o que estava errado para kits ("KIT" na producao).
        CFOP_PRINCIPAL (16o valor do INSERT) e anexado pelo exporter junto
        com o perfil, fora desta tupla — e constante por ambiente, nao por
        item.
        """
        qty = item.quantidade or 0.0
        unit_price = item.preco_unitario or 0.0
        total = _item_total(item)
        desc = (item.descricao or "")[:100]
        delivery = _parse_date(item.data_entrega)

        return (
            item_pk,               # CODIGO
            header_pk,             # CODVENDA
            product_seq,           # CODPRODUTO (FK or NULL if not found)
            desc,                  # DESCRICAO
            qty,                   # QTD
            unit_price,            # PRECO_UNITARIO
            total,                 # TOTAL
            unid,                  # UNID
            delivery,              # DT_ENTREGA_ITEM
            perfil.icms_porc,      # ICMS_PORC
            Decimal("0"),          # ICMS_BASE
            perfil.reducao,        # REDUCAO
            Decimal("0"),          # DESC_SOBRE_TOTAL
            Decimal("0"),          # PESO_BRUTO
            Decimal("0"),          # PESO_LIQUIDO
        )
