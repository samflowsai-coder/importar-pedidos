from __future__ import annotations

import re

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

# Colunas da tabela de itens da OC Daju. O xlsx é um PDF convertido, então
# colunas vazias se intercalam — o mapeamento é por nome, nunca por posição.
_COLUMNS = {
    "ref": "ref. forn.",
    "ean": "ean",
    "description": "descrição",
    "qty": "qtd.",
    "unit_price": "vl. unitário",
    "total": "valor total",
}


class DajuParser(BaseParser):
    """Parser para Ordem de Compra da Daju LTDA (xlsx convertido de PDF)."""

    def can_parse(self, extracted: dict) -> bool:
        text = extracted.get("text", "").upper()
        return "DAJU" in text and "ORDEM DE COMPRA" in text

    def parse(self, extracted: dict) -> Order | None:
        if not self.can_parse(extracted):
            return None

        rows = extracted.get("rows", [])
        if not rows:
            return None

        text = extracted.get("text", "")
        header = self._parse_header(rows, text)
        items = self._parse_items(rows, self._delivery_date(text))

        if not items:
            return None

        return Order(header=header, items=items)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _parse_header(self, rows: list, text: str) -> OrderHeader:
        order_number = self._find(text, r"N[ºo°]\s*OC:\s*(\S+)")
        issue_date = self._find(text, r"Emissão:\s*(\d{1,2}/\d{1,2}/\d{4})")
        customer_name, customer_cnpj = self._parse_customer(rows)
        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _parse_customer(self, rows: list) -> tuple[str | None, str | None]:
        # O comprador (Daju) é o primeiro bloco "Nome \n CNPJ: ..." do arquivo;
        # tudo a partir da linha "FORNECEDOR" descreve a Nasmar, não o cliente.
        for row in rows:
            for cell in row:
                if cell is None:
                    continue
                cell_text = str(cell)
                if "FORNECEDOR" in cell_text.upper():
                    return None, None
                m = re.search(
                    r"^(.+?)\s*[\r\n]+\s*CNPJ:\s*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})",
                    cell_text,
                )
                if m:
                    return m.group(1).strip(), m.group(2)
        return None, None

    def _delivery_date(self, text: str) -> str | None:
        # A OC pode vir com o dia perdido na conversão ("Entrega prevista: /09/2026").
        # Data incompleta não entra — o operador define no preview.
        return self._find(text, r"Entrega prevista:\s*(\d{1,2}/\d{1,2}/\d{4})")

    # ------------------------------------------------------------------
    # Itens
    # ------------------------------------------------------------------

    def _parse_items(self, rows: list, delivery_date: str | None) -> list[OrderItem]:
        header_idx, col_map = self._find_headers(rows)
        if header_idx is None:
            return []

        items = []
        for row in rows[header_idx + 1 :]:
            cells = [str(c).strip() if c is not None else "" for c in row]
            if any("INSTRUÇÕES" in c.upper() for c in cells):
                break

            ref = self._cell(cells, col_map, "ref")
            qty = self._parse_number(self._cell(cells, col_map, "qty"))
            if not ref or not qty:
                continue

            items.append(
                OrderItem(
                    product_code=ref,
                    ean=self._clean_ean(self._cell(cells, col_map, "ean")),
                    description=self._cell(cells, col_map, "description") or None,
                    quantity=qty,
                    unit_price=self._parse_number(self._cell(cells, col_map, "unit_price")),
                    total_price=self._parse_number(self._cell(cells, col_map, "total")),
                    delivery_date=delivery_date,
                )
            )

        return items

    def _find_headers(self, rows: list) -> tuple[int | None, dict]:
        for i, row in enumerate(rows):
            cells = [str(c).strip().lower() if c is not None else "" for c in row]
            if _COLUMNS["ref"] in cells and _COLUMNS["qty"] in cells:
                col_map = {
                    key: cells.index(label) for key, label in _COLUMNS.items() if label in cells
                }
                return i, col_map
        return None, {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cell(self, cells: list[str], col_map: dict, key: str) -> str:
        idx = col_map.get(key)
        if idx is None or idx >= len(cells):
            return ""
        return cells[idx]

    def _clean_ean(self, value: str) -> str | None:
        digits = re.sub(r"\.0$", "", value)
        return digits if re.match(r"^\d{8,14}$", digits) else None

    def _parse_number(self, value: str) -> float | None:
        cleaned = re.sub(r"[R$\s]", "", value)
        if not cleaned or cleaned in ("—", "-"):
            return None
        try:
            if "," in cleaned:
                return float(cleaned.replace(".", "").replace(",", "."))
            return float(cleaned)
        except ValueError:
            return None

    def _find(self, text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
