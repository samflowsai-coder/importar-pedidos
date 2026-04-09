from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "DAKOTA NORDESTE"

_ITEM_CODE_RE = re.compile(r"^(\d{5}\.\d{3}/\d)", re.MULTILINE)


class KoloshParser(BaseParser):
    """Parser para PDFs de pedido Kolosh / Dakota Nordeste."""

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "")

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
        order_number = self._find(text, r"Numero\s*:\s*(\w+)")
        customer_cnpj = self._find(text, r"CNPJ:\s*([\d./-]+)")
        customer_name = self._find(text, r"Razao Social:\s*(.+?)\s+Numero")
        delivery_date = self._extract_delivery_date(text)
        return OrderHeader(
            order_number=order_number,
            issue_date=delivery_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _extract_delivery_date(self, text: str) -> Optional[str]:
        m = re.search(r"Entrega\s*:\s*(\d{2}/\d{2}/(\d{2,4}))", text)
        if not m:
            return None
        date_str = m.group(1)
        year_part = m.group(2)
        if len(year_part) == 2:
            date_str = date_str[:-2] + "20" + year_part
        return date_str

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _parse_items(self, text: str) -> list[OrderItem]:
        delivery_date = self._extract_delivery_date(text)
        items = []
        matches = list(_ITEM_CODE_RE.finditer(text))
        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[start:end]
            item = self._parse_block(block, delivery_date)
            if item:
                items.append(item)
        return items

    def _parse_block(self, block: str, delivery_date: Optional[str] = None) -> Optional[OrderItem]:
        # Join lines to handle multi-line descriptions
        joined = " ".join(block.splitlines())
        # Format: CODE DESC QTY UN IPI% UNIT_PRICE TOTAL
        # e.g.: 04032.003/6 KIT 3 PRS MEIA ... 500.000 UN 9.97 0.00 4,985.00
        m = re.search(
            r"(\d{5}\.\d{3}/\d)\s+(.+?)\s+([\d,]+\.?\d*)\s+UN\s+([\d.]+)\s+[\d.]+\s+([\d,.]+)",
            joined,
            re.DOTALL,
        )
        if not m:
            return None

        product_code = m.group(1)
        description = m.group(2).strip()
        qty = self._parse_us_number(m.group(3))
        unit_price = self._parse_us_number(m.group(4))
        total_price = self._parse_us_number(m.group(5))

        if qty is None:
            return None

        return OrderItem(
            product_code=product_code,
            description=description,
            quantity=qty,
            unit_price=unit_price,
            total_price=total_price,
            delivery_date=delivery_date,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_us_number(self, value: str) -> Optional[float]:
        """Parse US-format numbers: 500.000 = 500, 1,000.000 = 1000, 4,985.00 = 4985."""
        if not value or not value.strip():
            return None
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None

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
