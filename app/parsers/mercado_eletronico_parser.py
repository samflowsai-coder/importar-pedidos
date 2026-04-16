from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "Mercado Eletrônico"
# Item header line: "NNN. D,DD % DD,DD % QQ,QQQQST BRL ..."
_ITEM_LINE = re.compile(r"^\d+\.\s+[\d,]", re.MULTILINE)
# Formatted CNPJ: XX.XXX.XXX/XXXX-XX
_CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
# Page-footer artifacts printed by the portal between item rows on page breaks:
#   "04/03/2026, 16:19 Mercado Eletrônico"  ← timestamp + brand header
#   "https://www.me.com.br/..."             ← print URL
#   "1 2/4"                                 ← pagination stamp
_PAGE_ARTIFACT_RE = re.compile(
    r"^\s*\d{2}/\d{2}/\d{4},\s*\d{2}:\d{2}\s+Mercado Eletr[oô]nico\s*$"
    r"|^\s*https?://\S+.*$"
    r"|^\s*\d+\s+\d+/\d+\s*$",
    re.MULTILINE,
)


class MercadoEletronicoParser(BaseParser):
    """Parser para PDFs exportados do portal Mercado Eletrônico (Riachuelo/Guararapes).

    Lê do texto completo — cobre itens que cruzam quebra de página e que não
    foram capturados nas tabelas pelo pdfplumber.
    """

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
        order_number = self._find(text, r"PEDIDO\s+(\d+)")
        issue_date = self._find(text, r"Data Envio:\s*(\d{2}/\d{2}/\d{4})")
        customer_name = self._find(text, r"Empresa:\s*\w+\s*-\s*(.+?)(?:\n|$)")
        # Customer CNPJ is not in the header — each store has its own CNPJ in items
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
        )

    # ------------------------------------------------------------------
    # Items — text-based to capture all items including cross-page ones
    # ------------------------------------------------------------------

    def _parse_items(self, full_text: str) -> list[OrderItem]:
        # Strip page-footer artifacts (portal URL + pagination stamps) before parsing
        full_text = _PAGE_ARTIFACT_RE.sub("", full_text)
        positions = [m.start() for m in _ITEM_LINE.finditer(full_text)]
        if not positions:
            return []

        items = []
        for i, start in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(full_text)
            block = full_text[start:end]
            item = self._parse_block(block)
            if item:
                items.append(item)
        return items

    def _parse_block(self, block: str) -> Optional[OrderItem]:
        first_line = block.splitlines()[0]

        qty = self._extract_qty(first_line)
        unit_price, total_price = self._extract_prices(first_line)
        desc, product_code = self._extract_desc_and_code(block)
        delivery_cnpj = self._extract_cnpj(block)
        delivery_name = self._extract_delivery_name(block)
        delivery_date = self._find(block, r"Data de entrega prevista:\s*(\d{2}/\d{2}/\d{4})")

        if desc is None or qty is None:
            return None

        return OrderItem(
            description=desc,
            product_code=product_code,
            quantity=qty,
            unit_price=unit_price,
            total_price=total_price,
            delivery_cnpj=delivery_cnpj,
            delivery_name=delivery_name,
            delivery_date=delivery_date,
        )

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------

    def _extract_qty(self, line: str) -> Optional[float]:
        m = re.search(r"([\d,]+)\s*ST", line)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None

    def _extract_prices(self, line: str) -> tuple[Optional[float], Optional[float]]:
        # "BRL 13,70 BRL 137,00"  or  "BRL 13,70BRL 2.041,30"
        matches = re.findall(r"BRL\s*([\d,.]+)", line)
        unit_price = self._parse_br_number(matches[0]) if len(matches) >= 1 else None
        total_price = self._parse_br_number(matches[1]) if len(matches) >= 2 else None
        return unit_price, total_price

    def _extract_desc_and_code(self, block: str):
        m = re.search(r"Descri[çc][aã]o do Material:\s*\n(.+?)(?:\n|$)", block)
        if not m:
            return None, None
        raw = m.group(1).strip()
        # Guard: URL artifacts that survived pre-processing or appeared mid-block
        if raw.startswith("http://") or raw.startswith("https://"):
            return None, None
        code_m = re.match(r"^(\d+)_", raw)
        product_code = code_m.group(1) if code_m else None
        desc = re.sub(r"^\d+_\w+\s*-\s*", "", raw).strip()
        desc = self._deduplicate(desc)
        return desc, product_code

    def _extract_cnpj(self, block: str) -> Optional[str]:
        # Look inside "Local Entrega Item:" section
        m = re.search(r"Local Entrega Item:.+?(" + _CNPJ_RE.pattern + r")", block, re.DOTALL)
        if m:
            return m.group(1)
        # Fallback: any formatted CNPJ in block
        m = _CNPJ_RE.search(block)
        return m.group(0) if m else None

    def _extract_delivery_name(self, block: str) -> Optional[str]:
        """Extract store identifier, e.g. 'Lojas Riachuelo S/A - LJ270'."""
        m = re.search(r"Local Entrega Item:\s*(.+?)(?:\n|CNPJ|$)", block, re.DOTALL)
        if not m:
            return None
        raw = m.group(1).strip()
        # Take first two dash-separated parts: "Company - StoreCode"
        parts = [p.strip() for p in raw.split(" - ")]
        if len(parts) >= 2:
            return f"{parts[0]} - {parts[1]}"
        return parts[0] if parts else None

    def _parse_br_number(self, value: str) -> Optional[float]:
        if not value:
            return None
        try:
            if "," in value:
                return float(value.replace(".", "").replace(",", "."))
            return float(value.replace(".", ""))
        except ValueError:
            return None

    def _deduplicate(self, text: str) -> str:
        n = len(text)
        for split in range(n // 2, n):
            left = text[:split].strip()
            right = text[split:].strip()
            if left == right:
                return left
        return text

    def _find(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
