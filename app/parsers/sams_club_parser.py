from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser
from app.utils.logger import logger

_SIGNATURE_CNPJ = "00.063.960"
_SIGNATURE_TEXT = "Itens do Pedido"
_GRADE_MARKER = "Cross Docking"

_ITEM_RE = re.compile(
    r"^(\d+)\s+(\d{13})\s+Unidade\s+([\d,]+)\s+([\d,]+)\s+([\d,.]+)\s+([\d,.]+)"
    r".*?([\d,.]+)$",
    re.MULTILINE,
)

# Linha "de dados" da tabela Cross Docking: <EAN local> <EAN produto> <qty> <data inicial>
_CD_DATA_RE = re.compile(r"^(\d{13})\s+(\d{13})\s+([\d,.]+)\s+(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})\s*$")
# CNPJ é quebrado pelo pdfplumber em 2 linhas (cabeçalho/rodapé do registro):
_CD_CNPJ_HEAD_RE = re.compile(r"^(\d{2}\.\d{3}\.\d{3})\s*/")
_CD_CNPJ_TAIL_RE = re.compile(r"^(\d{4}-\d{2})\b")


class SamsClubParser(BaseParser):
    """Parser para PDFs de pedido Sam's Club / Walmart.

    Suporta dois layouts do WebEDI/Neogrid:
    - Consolidado: 1 destino único (CD) → todos os itens recebem o mesmo delivery_cnpj.
    - GRADE (com seção "Cross Docking"): cada SKU é decomposto por loja final;
      cada linha vira 1 OrderItem com (delivery_cnpj, delivery_ean) próprios.
    """

    def can_parse(self, extracted: dict) -> bool:
        text = extracted.get("text", "")
        return _SIGNATURE_CNPJ in text and _SIGNATURE_TEXT.lower() in text.lower()

    def parse(self, extracted: dict) -> Optional[Order]:
        text = extracted.get("text", "")
        if not self.can_parse(extracted):
            return None

        header = self._parse_header(text)

        if _GRADE_MARKER in text:
            item_lookup = self._build_item_lookup(text)
            items = self._parse_cross_docking(text, item_lookup, header)
            self._warn_if_grade_diverges(text, items)
        else:
            items = self._parse_items(text)

        if not items:
            return None

        return Order(header=header, items=items)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _parse_header(self, text: str) -> OrderHeader:
        order_number = self._find(text, r"N[uú]mero (?:do )?Pedido:\s*([\d-]+)")
        issue_date = self._extract_date(
            text, r"Data de Emiss[aã]o:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})"
        )
        customer_cnpj = self._find(text, r"CNPJ:\s*([\d./ -]+)")
        if customer_cnpj:
            customer_cnpj = re.sub(r"\s+", "", customer_cnpj)
        customer_name = self._find(text, r"Destinat[aá]rio:\s*([^\n\r]+?)\s*(?:\n|$)")
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _extract_date(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        raw = m.group(1)
        # Remove spaces around slashes
        return re.sub(r"\s*/\s*", "/", raw)

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _items_section(self, text: str) -> str:
        """Recorta o texto a partir do cabeçalho 'Itens do Pedido' (case-insensitive)."""
        m = re.search(re.escape(_SIGNATURE_TEXT), text, re.IGNORECASE)
        return text[m.start() :] if m else text

    def _build_item_lookup(self, text: str) -> dict[str, dict[str, float]]:
        """Mapa {ean_produto: {'unit_price', 'pack_size'}} da tabela 'Itens do Pedido'.

        `pack_size` (Qtde. na Emb.) é crítico: na seção Cross Docking a quantidade
        é expressa em EMBALAGENS, não em unidades. Para obter o total de unidades
        por loja, multiplica-se qty_cross_docking × pack_size.
        """
        section = self._items_section(text)
        lookup: dict[str, dict[str, float]] = {}
        for m in _ITEM_RE.finditer(section):
            ean = m.group(2)
            pack_size = self._parse_br_number(m.group(3)) or 1.0
            unit_price = self._parse_br_number(m.group(5))
            lookup[ean] = {
                "pack_size": pack_size,
                "unit_price": unit_price if unit_price is not None else 0.0,
            }
        return lookup

    def _parse_cross_docking(
        self,
        text: str,
        item_lookup: dict[str, dict[str, float]],
        header: OrderHeader,
    ) -> list[OrderItem]:
        """Parsea a seção 'Cross Docking' do PDF GRADE.

        Layout (pdfplumber preserva quebra visual do CNPJ em 3 linhas):
            00.063.960 /                                          00 / 00 /
            7891737001698 7898686876711 16,00 00 / 00 / 0000
            0094-08                                                0000

        - Linha N-1: início do CNPJ (`00.063.960 /`)
        - Linha N:   `<EAN_local> <EAN_produto> <qty> <data_inicial>`
        - Linha N+1: final do CNPJ (`0094-08`)
        """
        idx = text.find(_GRADE_MARKER)
        if idx == -1:
            return []

        fallback_date = self._extract_date(text, r"Data Inicial:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})")

        lines = text[idx:].split("\n")
        items: list[OrderItem] = []
        for i, raw in enumerate(lines):
            m = _CD_DATA_RE.match(raw.strip())
            if not m:
                continue

            ean_local = m.group(1)
            ean_produto = m.group(2)
            packs = self._parse_br_number(m.group(3))
            data_inicial = re.sub(r"\s*/\s*", "/", m.group(4))

            if packs is None or packs <= 0:
                continue

            cnpj = self._stitch_cnpj(lines, i)

            if data_inicial == "00/00/0000":
                data_inicial = fallback_date

            info = item_lookup.get(ean_produto, {})
            pack_size = info.get("pack_size", 1.0)
            unit_price = info.get("unit_price")
            qty = packs * pack_size
            total_price = qty * unit_price if unit_price else None

            items.append(
                OrderItem(
                    ean=ean_produto,
                    product_code=ean_produto,
                    description=ean_produto,
                    quantity=qty,
                    unit_price=unit_price,
                    total_price=total_price,
                    delivery_date=data_inicial,
                    delivery_cnpj=cnpj,
                    delivery_ean=ean_local,
                )
            )
        return items

    def _stitch_cnpj(self, lines: list[str], data_idx: int) -> Optional[str]:
        """Junta as 2 metades do CNPJ que ficam acima/abaixo da linha de dados."""
        head = None
        tail = None
        if data_idx >= 1:
            mh = _CD_CNPJ_HEAD_RE.match(lines[data_idx - 1].strip())
            if mh:
                head = mh.group(1)
        if data_idx + 1 < len(lines):
            mt = _CD_CNPJ_TAIL_RE.match(lines[data_idx + 1].strip())
            if mt:
                tail = mt.group(1)
        if head and tail:
            return f"{head}/{tail}"
        return None

    def _warn_if_grade_diverges(self, text: str, items: list[OrderItem]) -> None:
        """Soma qty da grade por SKU e compara com a tabela superior. Warning se divergir."""
        section = self._items_section(text)

        agg: dict[str, float] = {}
        for m in _ITEM_RE.finditer(section):
            ean = m.group(2)
            emb = self._parse_br_number(m.group(3)) or 1.0
            ped = self._parse_br_number(m.group(4)) or 0.0
            agg[ean] = agg.get(ean, 0.0) + emb * ped

        grade_sum: dict[str, float] = {}
        for it in items:
            if it.ean and it.quantity:
                grade_sum[it.ean] = grade_sum.get(it.ean, 0.0) + it.quantity

        for ean, expected in agg.items():
            got = grade_sum.get(ean, 0.0)
            if abs(got - expected) > 0.01:
                logger.warning(
                    f"Sams GRADE: divergência qty SKU {ean}: agregado={expected} grade={got}"
                )

    def _parse_items(self, text: str) -> list[OrderItem]:
        # Restrict to section after "Itens do Pedido"
        section = self._items_section(text)

        delivery_date = self._extract_date(text, r"Data Inicial:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})")
        delivery_cnpj_m = re.search(r"CNPJ do Local de Entrega:\s*([\d./ -]+)", text)
        delivery_cnpj = (
            re.sub(r"\s+", "", delivery_cnpj_m.group(1)).strip() if delivery_cnpj_m else None
        )

        items = []
        for m in _ITEM_RE.finditer(section):
            ean = m.group(2)
            emb_qty = self._parse_br_number(m.group(3))
            pedida_qty = self._parse_br_number(m.group(4))
            preco_bruto = self._parse_br_number(m.group(5))
            total_str = m.group(7)
            total_price = self._parse_br_number(total_str)

            # Final quantity = emb_qty * pedida_qty (pack size * number of packs)
            qty = None
            if emb_qty is not None and pedida_qty is not None:
                qty = emb_qty * pedida_qty
            elif pedida_qty is not None:
                qty = pedida_qty

            if qty is None:
                continue

            items.append(
                OrderItem(
                    ean=ean,
                    product_code=ean,
                    description=ean,
                    quantity=qty,
                    unit_price=preco_bruto,
                    total_price=total_price,
                    delivery_date=delivery_date,
                    delivery_cnpj=delivery_cnpj,
                )
            )
        return items

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_br_number(self, value: str) -> Optional[float]:
        if not value or not value.strip():
            return None
        try:
            if "," in value:
                return float(value.replace(".", "").replace(",", "."))
            return float(value.replace(".", ""))
        except ValueError:
            return None

    def _find(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
