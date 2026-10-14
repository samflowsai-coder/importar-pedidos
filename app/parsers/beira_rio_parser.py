from __future__ import annotations

import re

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

# Color/size header line. O layout traz 1 ou 2 faixas de tamanho antes do "Total":
#   "103927 CINZA/BRANCO/PRETO 33/38 39/44 Total"   (layout 2025: duas faixas)
#   "8414 BRANCO/BRANCO/BRANCO 23/27 Total"          (layout 10/2026: uma faixa)
_SIZES = r"(?:\d{2}/\d{2}\s+)+"
_COLOR_LINE_RE = re.compile(rf"{_SIZES}Total")
_COLOR_PARTS_RE = re.compile(rf"^(?:\d+\s+)?(.+?)\s+({_SIZES})Total")

# Quantity line: data + uma quantidade por faixa + total.
#   "17/02/2026 9.000,000 9.000,000 18.000,000"  (2 faixas + total)
#   "10/12/2026 3.300,000 3.300,000"             (1 faixa + total)
_QTY_LINE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})((?:\s+[\d.,]+)+)")


class BeiranRioParser(BaseParser):
    """Parser para PDFs de pedido da Calcados Beira Rio."""

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "").upper()

    def parse(self, extracted: dict) -> Order | None:
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

    def _extract_issue_date(self, text: str) -> str | None:
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
        # A unidade varia por item (KIT, PAR): o preço vem depois dela, qualquer que seja.
        m1 = re.match(r"(\d{10})(.+?)\s+\d{8}\s+[A-Z]{2,4}\s+([\d,]+)", line1)
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
                cm = _COLOR_PARTS_RE.match(line)
                color = cm.group(1).strip() if cm else None
                sizes = cm.group(2).split() if cm else []

                # Next meaningful line: date + quantities
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i < len(lines) and sizes:
                    mq = _QTY_LINE_RE.match(lines[i].strip())
                    if mq:
                        delivery_date = mq.group(1)
                        numbers = mq.group(2).split()
                        # Um número por faixa, mais o total. Qualquer outra
                        # contagem é layout que não conhecemos: não chuta.
                        if len(numbers) == len(sizes) + 1:
                            full_desc = f"{desc_part} {color}" if color else desc_part
                            for size, raw_qty in zip(sizes, numbers):
                                qty = self._parse_br_number(raw_qty)
                                if qty and qty > 0:
                                    result.append(OrderItem(
                                        product_code=item_code,
                                        description=f"{full_desc} {size}",
                                        quantity=qty,
                                        unit_price=price,
                                        delivery_date=delivery_date,
                                        obs=size,
                                    ))
            i += 1

        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_br_number(self, value: str) -> float | None:
        if not value or not value.strip():
            return None
        try:
            if "," in value:
                return float(value.replace(".", "").replace(",", "."))
            return float(value.replace(".", ""))
        except ValueError:
            return None

    def _find(self, text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
