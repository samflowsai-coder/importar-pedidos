from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE_CNPJ = "00.063.960"
_SIGNATURE_TEXT = "Itens do Pedido"

_ITEM_RE = re.compile(
    r"^(\d+)\s+(\d{13})\s+Unidade\s+([\d,]+)\s+([\d,]+)\s+([\d,.]+)\s+([\d,.]+)"
    r".*?([\d,.]+)$",
    re.MULTILINE,
)


class SamsClubParser(BaseParser):
    """Parser para PDFs de pedido Sam's Club / Walmart."""

    def can_parse(self, extracted: dict) -> bool:
        text = extracted.get("text", "")
        return _SIGNATURE_CNPJ in text and _SIGNATURE_TEXT in text

    def parse(self, extracted: dict) -> Optional[Order]:
        text = extracted.get("text", "")
        if not self.can_parse(extracted):
            return None

        header = self._parse_header(text)
        items = self._parse_items(text)

        if not items:
            return None

        return Order(header=header, items=items)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _parse_header(self, text: str) -> OrderHeader:
        order_number = self._find(text, r"N[uú]mero Pedido:\s*([\d-]+)")
        issue_date = self._extract_date(text, r"Data de Emiss[aã]o:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})")
        customer_cnpj = self._find(text, r"CNPJ:\s*([\d./ -]+)")
        if customer_cnpj:
            customer_cnpj = re.sub(r"\s+", "", customer_cnpj)
        delivery_cnpj = self._find(text, r"CNPJ do Local de Entrega:\s*([\d./ -]+)")
        if delivery_cnpj:
            delivery_cnpj = re.sub(r"\s+", "", delivery_cnpj)
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
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

    def _parse_items(self, text: str) -> list[OrderItem]:
        # Restrict to section after "Itens do Pedido"
        idx = text.find(_SIGNATURE_TEXT)
        if idx == -1:
            section = text
        else:
            section = text[idx:]

        delivery_date = self._extract_date(text, r"Data Inicial:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})")
        delivery_cnpj_m = re.search(r"CNPJ do Local de Entrega:\s*([\d./ -]+)", text)
        delivery_cnpj = re.sub(r"\s+", "", delivery_cnpj_m.group(1)).strip() if delivery_cnpj_m else None

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

            items.append(OrderItem(
                ean=ean,
                product_code=ean,
                description=ean,
                quantity=qty,
                unit_price=preco_bruto,
                total_price=total_price,
                delivery_date=delivery_date,
                delivery_cnpj=delivery_cnpj,
            ))
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
