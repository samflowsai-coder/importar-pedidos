from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser


class GenericParser(BaseParser):
    def parse(self, extracted: dict) -> Order | None:
        text = extracted.get("text", "")
        tables = extracted.get("tables", [])

        header = self._parse_header(text)
        items = self._parse_items_from_tables(tables)

        if not items and text:
            items = self._parse_items_from_text(text)

        if not items:
            return None

        return Order(header=header, items=items)

    def _parse_header(self, text: str) -> OrderHeader:
        order_number = self._find(text, r"(?:pedido|order|po|n[uú]mero)[:\s#]*([A-Z0-9][\w\-/]{1,20})")
        issue_date = self._find(text, r"(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})")
        customer = self._find(text, r"(?:cliente|customer|empresa|company)[:\s]+(.+?)(?:\n|$)")
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer,
        )

    def _parse_items_from_tables(self, tables: list) -> list[OrderItem]:
        items = []
        for table in tables:
            for row in table:
                if not row:
                    continue
                desc, qty = self._extract_desc_qty(row)
                if desc and qty is not None:
                    items.append(OrderItem(description=desc, quantity=qty))
        return items

    def _parse_items_from_text(self, text: str) -> list[OrderItem]:
        items = []
        pattern = re.compile(r"(.{5,80}?)\s{2,}(\d+(?:[.,]\d+)?)\s*(?:un|pç|pc|kg|l|m)?", re.IGNORECASE)
        for match in pattern.finditer(text):
            desc = match.group(1).strip()
            qty_str = match.group(2).replace(",", ".")
            try:
                qty = float(qty_str)
                items.append(OrderItem(description=desc, quantity=qty))
            except ValueError:
                continue
        return items

    def _extract_desc_qty(self, row: list) -> tuple[Optional[str], Optional[float]]:
        desc = None
        qty = None
        for cell in row:
            if cell is None:
                continue
            cell_str = str(cell).strip()
            if not cell_str:
                continue
            try:
                val = float(cell_str.replace(",", "."))
                if val > 0 and qty is None:
                    qty = val
            except ValueError:
                if desc is None and len(cell_str) > 3:
                    desc = cell_str
        return desc, qty

    def _find(self, text: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None
