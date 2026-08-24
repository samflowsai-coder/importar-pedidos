from __future__ import annotations

import re

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

# Colunas da tabela de itens da OC Daju. O xlsx é um PDF convertido, então
# colunas vazias se intercalam — o mapeamento é por nome, nunca por posição.
# Texto no formato de milhar brasileiro: grupos de 3 dígitos separados por
# ponto, sem decimal (`1.300`, `1.234.567`). `300.0` NÃO casa — 1 dígito no
# último grupo é decimal, não milhar.
_MILHAR_BR = re.compile(r"\d{1,3}(?:\.\d{3})+")

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
            # Número vai pelo valor CRU, não pela versão stringificada: quando o
            # openpyxl entrega int/float, o tipo já resolve a ambiguidade e não
            # há o que adivinhar.
            qty = self._parse_number(self._raw(row, col_map, "qty"))
            if not ref or not qty:
                continue

            items.append(
                OrderItem(
                    product_code=ref,
                    ean=self._clean_ean(self._cell(cells, col_map, "ean")),
                    description=self._cell(cells, col_map, "description") or None,
                    quantity=qty,
                    unit_price=self._parse_number(self._raw(row, col_map, "unit_price")),
                    total_price=self._parse_number(self._raw(row, col_map, "total")),
                    delivery_date=delivery_date,
                )
            )

        return items

    def _raw(self, row: list, col_map: dict, key: str):
        """Célula CRUA, com o tipo que o openpyxl devolveu (int/float/str/None)."""
        idx = col_map.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

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

    def _parse_number(self, value) -> float | None:
        """Número da planilha, sem adivinhação.

        A conversão PDF->xlsx que origina estes arquivos é instável (ela já come
        o dia da data de entrega), então a mesma coluna pode chegar como número
        nativo hoje e como texto amanhã. As duas formas precisam dar o MESMO
        resultado — um erro aqui entra no ERP como pedido de quantidade errada,
        passa no validador (qty > 0) e ninguém percebe.

        - `int`/`float` do openpyxl: usa direto, o tipo já resolveu.
        - texto com vírgula: vírgula é decimal, ponto é milhar (`1.234,56`).
        - texto só com ponto: **3 dígitos depois do último ponto = milhar**
          (`1.300` -> 1300), caso contrário é decimal (`300.0` -> 300.0).
          Sem essa regra, `"1.300"` virava `1.3` — pedido mil vezes menor.
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if value is None:
            return None

        cleaned = re.sub(r"[R$\s]", "", str(value))
        if not cleaned or cleaned in ("—", "-"):
            return None
        try:
            if "," in cleaned:
                return float(cleaned.replace(".", "").replace(",", "."))
            if _MILHAR_BR.fullmatch(cleaned):
                return float(cleaned.replace(".", ""))
            return float(cleaned)
        except ValueError:
            return None

    def _find(self, text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
