from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "BEIRA RIO"

_MONTH_MAP = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "marco": "03",
    "abril": "04", "maio": "05", "junho": "06", "julho": "07",
    "agosto": "08", "setembro": "09", "outubro": "10", "novembro": "11",
    "dezembro": "12",
}

# Detects the start of an item block: 10-digit code followed immediately by an uppercase letter
_ITEM_BLOCK_RE = re.compile(r"(?=^\d{10}[A-Z])", re.MULTILINE)

# Color/size header line: "103927 CINZA/BRANCO/PRETO 33/38 39/44 Total"
_COLOR_LINE_RE = re.compile(r"\d{2}/\d{2}\s+\d{2}/\d{2}\s+Total")

# Quantity line: "17/02/2026 9.000,000 9.000,000 18.000,000"
_QTY_LINE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)", re.MULTILINE)


class BeiranRioParser(BaseParser):
    """Parser para PDFs de pedido da Calcados Beira Rio."""

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "").upper()

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
        order_number = (
            self._find(text, r"(\d{8})\s+Aten")
            or self._find(text, r"CALCADOS BEIRA RIO.+?(\d{8})")
        )
        customer_cnpj = self._find(text, r"CGC:\s*([\d./-]+)")
        customer_name = self._find(text, r"(CALCADOS BEIRA RIO S/A[^-\n]*)")
        issue_date = self._extract_issue_date(text)
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name.strip() if customer_name else None,
            customer_cnpj=customer_cnpj,
        )

    def _extract_issue_date(self, text: str) -> Optional[str]:
        m = re.search(r"SAPIRANGA,\s*(\d+)/(\w+)/(\d{4})", text, re.IGNORECASE)
        if not m:
            return None
        day = m.group(1).zfill(2)
        month = _MONTH_MAP.get(m.group(2).lower())
        return f"{day}/{month}/{m.group(3)}" if month else None

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _parse_items(self, text: str) -> list[OrderItem]:
        items = []
        blocks = _ITEM_BLOCK_RE.split(text)
        for block in blocks:
            if not re.match(r"^\d{10}[A-Z]", block.strip()):
                continue
            items.extend(self._parse_item_block(block))
        return items

    def _parse_item_block(self, block: str) -> list[OrderItem]:
        lines = block.splitlines()
        if not lines:
            return []

        # ── Line 1: item_code + description + price ──────────────────
        line1 = lines[0].strip()
        m1 = re.match(r"(\d{10})(.+?)\s+\d{8}\s+KIT\s+([\d,]+)", line1)
        if m1:
            item_code = m1.group(1)
            desc_part = m1.group(2).strip().rstrip(" -").strip()
            price = self._parse_br_number(m1.group(3))
        else:
            m1b = re.match(r"(\d{10})(.+)", line1)
            if not m1b:
                return []
            item_code = m1b.group(1)
            desc_part = m1b.group(2).strip()
            price = None

        # ── Continuation lines: description may wrap before color ────
        i = 1
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            # Stop when we reach a color/size line
            if _COLOR_LINE_RE.search(line):
                break
            # Stop when we reach a date/qty line
            if re.match(r"\d{2}/\d{2}/\d{4}\s+[\d.,]+", line):
                break
            # Stop at structural markers
            if line.startswith(("Sequência", "Item Descrição", "Sub-Total", "Total:")):
                break
            # Continuation of description (strip any trailing " -")
            desc_part = f"{desc_part} {line}".rstrip(" -").strip()
            i += 1

        # ── Parse all color-variant + qty pairs inside this block ────
        result: list[OrderItem] = []
        while i < len(lines):
            line = lines[i].strip()

            if _COLOR_LINE_RE.search(line):
                # Extract color text: everything before "NN/NN NN/NN Total"
                cm = re.match(r"^(?:\d+\s+)?(.+?)\s+\d{2}/\d{2}\s+\d{2}/\d{2}\s+Total", line)
                color = cm.group(1).strip() if cm else None

                # Next meaningful line: date + quantities
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines):
                    qty_line = lines[i].strip()
                    mq = re.match(r"(\d{2}/\d{2}/\d{4})\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)", qty_line)
                    if mq:
                        delivery_date = mq.group(1)
                        qty_small = self._parse_br_number(mq.group(2))
                        qty_large = self._parse_br_number(mq.group(3))

                        full_desc = f"{desc_part} {color}" if color else desc_part

                        if qty_small and qty_small > 0:
                            result.append(OrderItem(
                                product_code=item_code,
                                description=f"{full_desc} 33/38",
                                quantity=qty_small,
                                unit_price=price,
                                delivery_date=delivery_date,
                                obs="33/38",
                            ))
                        if qty_large and qty_large > 0:
                            result.append(OrderItem(
                                product_code=item_code,
                                description=f"{full_desc} 39/44",
                                quantity=qty_large,
                                unit_price=price,
                                delivery_date=delivery_date,
                                obs="39/44",
                            ))
            i += 1

        return result

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
