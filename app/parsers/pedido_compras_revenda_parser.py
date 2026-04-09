from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "PEDIDO DE COMPRAS REVENDA"


class PedidoComprasRevendaParser(BaseParser):
    """Parser para PDFs 'PEDIDO DE COMPRAS REVENDA' (formato Riachuelo/Guararapes revenda)."""

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
        order_number = self._find(text, r"PEDIDO DE COMPRAS REVENDA\s+(\d+)")
        issue_date = self._find(text, r"Data Emiss[aã]o:\s*(\d{2}\.\d{2}\.\d{4})")
        # Customer = delivery/billing company
        customer_name = self._find(text, r"Entrega:\s*\d+\s+(.+?)\s+CNPJ")
        customer_cnpj = self._find(text, r"Cobran[çc]a:.*?CNPJ:\s*(?:CNPJ:)?(\d{14})")
        if not customer_cnpj:
            customer_cnpj = self._find(text, r"Entrega:.*?CNPJ:(\d{14})")
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    # ------------------------------------------------------------------
    # Items — one PREPACK block per product
    # ------------------------------------------------------------------

    def _parse_items(self, text: str) -> list[OrderItem]:
        # Delivery date is in the header, not inside each PREPACK block
        delivery_date = self._extract_delivery_date(text)
        items = []
        blocks = re.split(r"(?=PREPACK:)", text)
        for block in blocks:
            if "PREPACK:" not in block:
                continue
            item = self._parse_block(block, delivery_date)
            if item:
                items.append(item)
        return items

    def _parse_block(self, block: str, delivery_date: Optional[str] = None) -> Optional[OrderItem]:
        desc = self._extract_description(block)
        qty = self._extract_quantity(block)
        product_code = self._find(block, r"PREPACK:\s*(\d+)")
        ean = self._extract_ean(block)
        unit_price = self._extract_unit_price(block)
        total_price = self._extract_total_price(block)
        obs = self._extract_obs(block)
        delivery_cnpj = self._find(block, r"Entrega:.*?CNPJ:(\d{14})")

        if desc and qty is not None:
            return OrderItem(
                description=desc,
                product_code=product_code,
                ean=ean,
                quantity=qty,
                unit_price=unit_price,
                total_price=total_price,
                obs=obs,
                delivery_date=delivery_date,
                delivery_cnpj=delivery_cnpj,
            )
        return None

    # ------------------------------------------------------------------
    # Field extractors
    # ------------------------------------------------------------------

    def _extract_description(self, block: str) -> Optional[str]:
        m = re.search(r"Montagem:\s*\n(.+?)(?:\n|$)", block)
        if m:
            return m.group(1).strip()
        m = re.search(r"DESCRI[CÇ][AÃ]O:\s*(.+?)(?:\s+COR:|$)", block, re.IGNORECASE)
        return m.group(1).strip() if m else None

    def _extract_quantity(self, block: str) -> Optional[float]:
        m = re.search(r"Qtd\.?\s*Total:\s*(\d[\d.]*)", block, re.IGNORECASE)
        if m:
            return self._parse_br_number(m.group(1))
        m = re.search(r"(?:PAR|KIT|PC|UN|UND|PCS|UNID|ST)\s+(\d[\d.]+)\s+\d", block, re.IGNORECASE)
        if m:
            return self._parse_br_number(m.group(1))
        return None

    def _extract_unit_price(self, block: str) -> Optional[float]:
        # After UNI qty, first price value: "PAR 1500 10,9500"
        m = re.search(r"(?:PAR|KIT|PC|UN|UND|PCS|UNID|ST)\s+[\d.]+\s+([\d,]+)", block, re.IGNORECASE)
        if m:
            return self._parse_br_number(m.group(1))
        return None

    def _extract_total_price(self, block: str) -> Optional[float]:
        # "Qtd. Total: 1500 16425.00 16.425,00 0,00" — second value is Total NF (no comma format)
        m = re.search(r"Qtd\.?\s*Total:\s*[\d.]+\s+([\d.]+)", block, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    def _extract_obs(self, block: str) -> Optional[str]:
        # First "Observações:" block inside the PREPACK section
        parts = block.split("Observações:")
        if len(parts) >= 2:
            line = parts[1].strip().splitlines()[0].strip()
            if line:
                return line
        return None

    def _extract_delivery_date(self, text: str) -> Optional[str]:
        """First business day of the delivery week.
        Format: 'Semana Ent.: 22(25/05 a 31/05/2026)' → first date = 25/05/2026 (Monday).
        """
        m = re.search(r"Semana Ent\.:\s*\d+\((\d{2}/\d{2})\s+a\s+\d{2}/\d{2}/(\d{4})\)", text)
        if not m:
            return None
        from datetime import datetime, timedelta
        raw_date = f"{m.group(1)}/{m.group(2)}"  # "25/05/2026"
        try:
            dt = datetime.strptime(raw_date, "%d/%m/%Y")
            # Advance to next weekday if it falls on weekend
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
            return dt.strftime("%d/%m/%Y")
        except ValueError:
            return raw_date

    def _extract_ean(self, block: str) -> Optional[str]:
        """EAN-13 appears on its own line or inline on the item data line."""
        m = re.search(r"\n(\d{13})\n", block)
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{13})\b", block)
        return m.group(1) if m else None

    def _parse_br_number(self, value: str) -> Optional[float]:
        try:
            if "," in value:
                return float(value.replace(".", "").replace(",", "."))
            return float(value.replace(".", ""))
        except ValueError:
            return None

    def _find(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else None
