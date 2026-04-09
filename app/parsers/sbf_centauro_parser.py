from __future__ import annotations

import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "GrupoSaf@centauro.com.br"
_ITEM_HEADER_COLS = ["Item", "Código", "Descrição", "Ref. Forn", "Hierarquia", "Qtd", "UM", "R$ Unit.", "R$ Total"]
_VARIANT_HEADER_COLS = ["Item", "Código", "EAN", "Data Entrega", "Tamanho", "Qtd"]


class SbfCentauroParser(BaseParser):
    """Parser para PDFs de pedido do Grupo SBF/Centauro."""

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "")

    def parse(self, extracted: dict) -> Optional[Order]:
        text = extracted.get("text", "")
        tables = extracted.get("tables", [])
        if not self.can_parse(extracted):
            return None

        header = self._parse_header(text, tables)
        delivery_date = self._extract_delivery_date(tables)
        delivery_cnpj = self._extract_delivery_cnpj(text)
        obs = self._extract_obs(tables)
        ean_map = self._extract_ean_map(tables)
        items = self._parse_items(tables, delivery_date, delivery_cnpj, obs, ean_map)

        if not items:
            return None

        return Order(header=header, items=items)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _parse_header(self, text: str, tables: list) -> OrderHeader:
        order_number = self._find(text, r"Pedido:\s*(\d+)")
        issue_date = self._find(text, r"Data Emiss[aã]o:\s*(\d{2}\.\d{2}\.\d{4})")
        customer_name, customer_cnpj = self._extract_customer(tables)
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _extract_customer(self, tables: list):
        """Extract from 'Informações de Cobrança' table row."""
        for table in tables:
            for row in table:
                if not row or not row[0]:
                    continue
                cell = str(row[0])
                if "Informa" in cell and "Cobran" in cell:
                    name = re.search(r"Cobran[çc]a\s*\n(.+?)\s+Insc", cell, re.DOTALL)
                    cnpj = re.search(r"CNPJ:\s*([\d./-]+)", cell)
                    return (
                        name.group(1).strip() if name else None,
                        cnpj.group(1).strip() if cnpj else None,
                    )
        return None, None

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _parse_items(self, tables: list, delivery_date, delivery_cnpj, obs, ean_map: dict) -> list[OrderItem]:
        items = []
        for table in tables:
            if not self._is_item_table(table):
                continue
            for row in table[1:]:
                if not row or len(row) < len(_ITEM_HEADER_COLS):
                    continue
                if str(row[0] or "").strip() in ("Item", ""):
                    continue
                desc = str(row[2] or "").strip()
                product_code = str(row[1] or "").strip()
                qty = self._parse_br_number(str(row[5] or ""))
                unit_price = self._parse_br_number(str(row[7] or ""))
                total_price = self._parse_br_number(str(row[8] or ""))
                ean = ean_map.get(product_code) or next(
                    (v for k, v in ean_map.items() if k.startswith(product_code)), None
                )
                if desc and qty is not None:
                    items.append(OrderItem(
                        description=desc,
                        product_code=product_code,
                        ean=ean,
                        quantity=qty,
                        unit_price=unit_price,
                        total_price=total_price,
                        obs=obs,
                        delivery_date=delivery_date,
                        delivery_cnpj=delivery_cnpj,
                    ))
        return items

    def _is_item_table(self, table: list) -> bool:
        if not table or not table[0] or len(table[0]) < len(_ITEM_HEADER_COLS):
            return False
        h = table[0]
        return (
            str(h[0] or "").strip() == "Item"
            and str(h[2] or "").strip() == "Descrição"
            and str(h[5] or "").strip() == "Qtd"
            and str(h[7] or "").strip() == "R$ Unit."
        )

    # ------------------------------------------------------------------
    # Additional field extractors
    # ------------------------------------------------------------------

    def _extract_delivery_date(self, tables: list) -> Optional[str]:
        """From Dados Variante table: col[3] = Data Entrega."""
        for table in tables:
            if not table or not table[0]:
                continue
            header = table[0]
            if len(header) >= 4 and str(header[3] or "").strip() == "Data Entrega":
                for row in table[1:]:
                    if row and row[3]:
                        return str(row[3]).strip()
        return None

    def _extract_delivery_cnpj(self, text: str) -> Optional[str]:
        """From 'Dados para Entrega' section."""
        m = re.search(r"Dados para Entrega.+?CNPJ:\s*([\d./-]+)", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def _extract_ean_map(self, tables: list) -> dict:
        """Build {product_code: ean} from Dados Variante table."""
        ean_map = {}
        for table in tables:
            if not table or not table[0]:
                continue
            header = table[0]
            if len(header) >= 3 and str(header[2] or "").strip() == "EAN":
                for row in table[1:]:
                    if row and len(row) >= 3 and row[1] and row[2]:
                        # col[1]=Código (full with color suffix), col[2]=EAN
                        # The base product code is first 6 chars of col[1]
                        full_code = str(row[1]).strip()
                        ean = str(row[2]).strip()
                        ean_map[full_code] = ean
        return ean_map

    def _extract_obs(self, tables: list) -> Optional[str]:
        """First meaningful obs line from the observations table."""
        for table in tables:
            for row in table:
                if not row or not row[0]:
                    continue
                cell = str(row[0])
                m = re.search(r"Obs\.:\s*(.+?)(?:\n|$)", cell)
                if m:
                    return m.group(1).strip()
        return None

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
