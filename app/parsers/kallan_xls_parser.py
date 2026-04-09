from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "KALLAN"


class KallanXlsParser(BaseParser):
    """Parser para planilhas XLS/XLSX de pedido Kallan."""

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "").upper()

    def parse(self, extracted: dict) -> Optional[Order]:
        if not self.can_parse(extracted):
            return None

        rows = extracted.get("rows", [])
        if not rows:
            return None

        header = self._parse_header(rows)
        items = self._parse_items(rows, header.order_number)

        if not items:
            return None

        return Order(header=header, items=items)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _parse_header(self, rows: list) -> OrderHeader:
        customer_cnpj = None
        customer_name = None
        order_number = None

        for row in rows[:15]:
            cells = [str(c).strip() if c is not None else "" for c in row]
            row_text = " ".join(cells)

            # CNPJ — formatted or raw 14-digit integer
            if not customer_cnpj:
                m = re.search(r"(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})", row_text)
                if m:
                    customer_cnpj = m.group(1)
                else:
                    # Raw number from spreadsheet cell (e.g. 51540219004535)
                    for c in cells:
                        if re.match(r"^\d{14}$", c):
                            raw = c
                            customer_cnpj = f"{raw[:2]}.{raw[2:5]}.{raw[5:8]}/{raw[8:12]}-{raw[12:14]}"
                            break

            # Customer name
            if not customer_name:
                if any("RAZÃO SOCIAL" in c.upper() or "RAZAO SOCIAL" in c.upper() for c in cells):
                    for i, c in enumerate(cells):
                        if "RAZÃO SOCIAL" in c.upper() or "RAZAO SOCIAL" in c.upper():
                            if i + 1 < len(cells) and cells[i + 1]:
                                customer_name = cells[i + 1]
                            break

            # Order number — look for store code pattern like K01 in header row
            if not order_number:
                for c in cells:
                    if re.match(r"^K\d{2}$", c.strip()):
                        order_number = c.strip()
                        break

        return OrderHeader(
            order_number=order_number,
            customer_cnpj=customer_cnpj,
            customer_name=customer_name,
        )

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _parse_items(self, rows: list, order_number: Optional[str]) -> list[OrderItem]:
        header_idx, col_map = self._find_headers(rows)
        if header_idx is None:
            return []

        items = []
        for row in rows[header_idx + 1:]:
            if not row:
                continue
            cells = [str(c).strip() if c is not None else "" for c in row]

            # Skip total rows or empty rows
            produto = cells[col_map["produto"]] if col_map.get("produto") is not None and col_map["produto"] < len(cells) else ""
            if not produto or "TOTAL" in produto.upper():
                continue

            apresentacao = cells[col_map["apresentacao"]] if col_map.get("apresentacao") is not None and col_map["apresentacao"] < len(cells) else ""
            tipo = cells[col_map["tipo"]] if col_map.get("tipo") is not None and col_map["tipo"] < len(cells) else ""
            numeracao = cells[col_map["numeracao"]] if col_map.get("numeracao") is not None and col_map["numeracao"] < len(cells) else ""
            cor = cells[col_map["cor"]] if col_map.get("cor") is not None and col_map["cor"] < len(cells) else ""
            referencia = cells[col_map["referencia"]] if col_map.get("referencia") is not None and col_map["referencia"] < len(cells) else ""
            pdv_str = cells[col_map["pdv"]] if col_map.get("pdv") is not None and col_map["pdv"] < len(cells) else ""
            qty_str = cells[col_map["qty_col"]] if col_map.get("qty_col") is not None and col_map["qty_col"] < len(cells) else ""
            total_str = cells[col_map["custo_ttl"]] if col_map.get("custo_ttl") is not None and col_map["custo_ttl"] < len(cells) else ""

            qty = self._parse_number(qty_str)
            if qty is None or qty == 0:
                continue

            description_parts = [p for p in [produto, apresentacao, tipo, numeracao, cor] if p]
            description = " ".join(description_parts)

            unit_price = self._parse_number(pdv_str)
            total_price = self._parse_number(total_str)

            items.append(OrderItem(
                product_code=referencia if referencia else None,
                description=description,
                quantity=qty,
                unit_price=unit_price,
                total_price=total_price,
            ))

        return items

    def _find_headers(self, rows: list) -> tuple[Optional[int], dict]:
        for i, row in enumerate(rows):
            cells = [str(c).strip() if c is not None else "" for c in row]
            cells_lower = [c.lower() for c in cells]
            if "produto" not in cells_lower:
                continue

            col_map = {}
            col_map["produto"] = cells_lower.index("produto")

            # referencia / Referência (case-insensitive)
            ref_idx = next((j for j, c in enumerate(cells_lower) if c in ("referência", "referencia")), None)
            col_map["referencia"] = ref_idx

            col_map["apresentacao"] = next((j for j, c in enumerate(cells_lower) if c == "apresentação"), None)
            col_map["tipo"] = next((j for j, c in enumerate(cells_lower) if c == "tipo"), None)
            col_map["numeracao"] = next((j for j, c in enumerate(cells_lower) if c == "numeração"), None)
            col_map["cor"] = next((j for j, c in enumerate(cells_lower) if c == "cor"), None)
            col_map["pdv"] = next((j for j, c in enumerate(cells) if c == "PDV"), None)
            col_map["custo_ttl"] = next((j for j, c in enumerate(cells) if c == "CUSTO TTL"), None)

            # Find quantity column: store code pattern like K01, not PDV/NCM
            qty_col = None
            for j, c in enumerate(cells):
                if re.match(r"^[A-Z]\d{2}$", c) and c not in ("PDV", "NCM"):
                    qty_col = j
                    break

            # Fallback: column before CUSTO TTL
            if qty_col is None and col_map.get("custo_ttl") is not None:
                qty_col = col_map["custo_ttl"] - 1

            col_map["qty_col"] = qty_col
            return i, col_map

        return None, {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_number(self, value: str) -> Optional[float]:
        if not value or not value.strip():
            return None
        # Remove currency symbols
        value = re.sub(r"[R$\s]", "", value)
        try:
            if "," in value:
                return float(value.replace(".", "").replace(",", "."))
            return float(value)
        except ValueError:
            return None

    def _find(self, text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
