from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")

# Header keywords that identify the header row
_HEADER_KEYWORDS = ("Produto", "DESCRIÇÃO PRODUTO", "CÓD", "COD")


class DesmembramentoXlsParser(BaseParser):
    """Parser para planilhas de desmembramento (Authentic Feet, Magic Feet, NBA)."""

    def can_parse(self, extracted: dict) -> bool:
        text = extracted.get("text", "").upper()
        # Check known signature keywords in text
        if any(kw in text for kw in ("DESMEMBRAMENTO", "NBA", "ADULTO", "INFANTIL")):
            return True
        # Authentic Feet / similar: has shopping center names + store code columns
        if "SHOPPING CENTER" in text or "SHOPPING PÁTIO" in text:
            return True
        # Fallback: check if header row structure matches (Foto + Produto columns)
        rows = extracted.get("rows", [])
        for row in rows[:6]:
            cells = [str(c).strip() if c is not None else "" for c in (row or [])]
            cells_set = set(cells)
            if "Foto" in cells_set and "Produto" in cells_set:
                return True
        return False

    def parse(self, extracted: dict) -> Optional[Order]:
        if not self.can_parse(extracted):
            return None

        rows = extracted.get("rows", [])
        if not rows:
            return None

        order_number = self._derive_order_number(extracted)
        header_idx, col_map, store_cols = self._find_structure(rows)

        if header_idx is None:
            return None

        items = self._parse_items(rows, header_idx, col_map, store_cols)

        if not items:
            return None

        order_header = OrderHeader(order_number=order_number)
        return Order(header=order_header, items=items)

    # ------------------------------------------------------------------
    # Structure detection
    # ------------------------------------------------------------------

    def _find_structure(self, rows: list) -> tuple[Optional[int], dict, list]:
        """Return (header_row_idx, col_map, store_columns_list).

        store_columns_list: list of (col_idx, store_name, cnpj_or_None)
        """
        cnpj_row_idx = None
        header_idx = None

        # First pass: find header row
        for i, row in enumerate(rows):
            cells = [str(c).strip() if c is not None else "" for c in row]
            if self._is_header_row(cells):
                header_idx = i
                break

        if header_idx is None:
            return None, {}, []

        header_cells = [str(c).strip() if c is not None else "" for c in rows[header_idx]]

        # Check row above header for CNPJs
        cnpjs_by_col = {}
        if header_idx > 0:
            prev_row = [str(c).strip() if c is not None else "" for c in rows[header_idx - 1]]
            for j, cell in enumerate(prev_row):
                if _CNPJ_RE.search(cell):
                    cnpjs_by_col[j] = cell.strip()

        # Also check row 2 (index 2) for Magic Feet CNPJ pattern
        if header_idx > 1 and not cnpjs_by_col:
            row2 = [str(c).strip() if c is not None else "" for c in rows[2]] if len(rows) > 2 else []
            for j, cell in enumerate(row2):
                if _CNPJ_RE.search(cell):
                    cnpjs_by_col[j] = cell.strip()

        # Build col_map
        col_map = {}
        cor_candidates = []
        for j, cell in enumerate(header_cells):
            cu = cell.upper()
            if not col_map.get("produto") and ("PRODUTO" in cu or "DESCRIÇÃO" in cu or "DESCRIÇÃO PRODUTO" in cu):
                col_map["produto"] = j
            if not col_map.get("codigo") and ("CÓD" in cu or "COD" in cu or "CÓDIGO" in cu):
                col_map["codigo"] = j
            if cu == "COR":
                cor_candidates.append(j)
            if not col_map.get("custo") and ("CUSTO" in cu) and ("TTL" in cu or "TOTAL" in cu or cu == "CUSTO"):
                col_map["custo"] = j
        # Prefer "Cor" (sentence case, readable color name) over "COR" (numeric code)
        # In practice the last "COR"/"Cor" column tends to be the text name
        if cor_candidates:
            # Prefer any column whose original cell is "Cor" (not all-caps) — text color names
            text_cor = [j for j in cor_candidates if header_cells[j] == "Cor"]
            col_map["cor"] = text_cor[0] if text_cor else cor_candidates[-1]

        # Identify store columns — those that come after known data columns
        # "Total" and summary cols at the end should be excluded from store cols
        _SUMMARY_NAMES = {"total", "total kits", "total r$", "kits"}

        def _is_summary_col(name: str) -> bool:
            nl = name.lower()
            return nl in _SUMMARY_NAMES or "total r$" in nl or "total kits" in nl

        # Known non-store header substrings (must NOT match store column names)
        _NON_STORE_SUFFIXES = ("produto", "código", "cod", "cor", "apresentação",
                               "tamanho", "custo", "pdv", "mark", "sugestão",
                               "ncm", "composição", "foto", "imagem", "tipo", "numeração")

        def _is_data_col(name: str) -> bool:
            nl = name.lower()
            return any(ns in nl for ns in _NON_STORE_SUFFIXES)

        # Find last data column index (excluding summary/total columns)
        last_data_col = -1
        for j, cell in enumerate(header_cells):
            if cell and _is_data_col(cell) and not _is_summary_col(cell):
                last_data_col = j

        store_cols = []
        if last_data_col >= 0:
            for j in range(last_data_col + 1, len(header_cells)):
                col_name = header_cells[j]
                if not col_name:
                    continue
                # Skip summary columns and numeric-only "headers" (from merged cells)
                if _is_summary_col(col_name):
                    continue
                # Skip if it looks like a number (merged cell artifact)
                try:
                    float(col_name)
                    continue
                except (ValueError, TypeError):
                    pass
                cnpj = cnpjs_by_col.get(j)
                store_cols.append((j, col_name, cnpj))

        return header_idx, col_map, store_cols

    def _is_header_row(self, cells: list[str]) -> bool:
        cells_upper = [c.upper() for c in cells]
        for kw in _HEADER_KEYWORDS:
            if kw.upper() in cells_upper:
                return True
        return False

    # ------------------------------------------------------------------
    # Item parsing
    # ------------------------------------------------------------------

    def _parse_items(self, rows: list, header_idx: int, col_map: dict, store_cols: list) -> list[OrderItem]:
        items = []
        for row in rows[header_idx + 1:]:
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue

            cells = [str(c).strip() if c is not None else "" for c in row]

            produto = cells[col_map["produto"]] if col_map.get("produto") is not None and col_map["produto"] < len(cells) else ""
            if not produto or "TOTAL" in produto.upper():
                continue

            codigo = cells[col_map["codigo"]] if col_map.get("codigo") is not None and col_map["codigo"] < len(cells) else ""
            cor = cells[col_map["cor"]] if col_map.get("cor") is not None and col_map["cor"] < len(cells) else ""

            description = f"{produto} {cor}".strip() if cor else produto

            for (col_idx, store_name, store_cnpj) in store_cols:
                if col_idx >= len(cells):
                    continue
                qty_str = cells[col_idx]
                qty = self._parse_number(qty_str)
                if qty is None or qty <= 0:
                    continue

                items.append(OrderItem(
                    product_code=codigo if codigo else None,
                    description=description,
                    quantity=qty,
                    delivery_cnpj=store_cnpj,
                    delivery_name=store_name,
                ))

        return items

    # ------------------------------------------------------------------
    # Order number from source file
    # ------------------------------------------------------------------

    def _derive_order_number(self, extracted: dict) -> Optional[str]:
        rows = extracted.get("rows", [])
        # Look for a title cell that contains meaningful keywords
        for row in rows[:4]:
            for cell in row:
                if cell is None:
                    continue
                s = str(cell).strip()
                if not s or len(s) < 3:
                    continue
                s_upper = s.upper()
                if "DESMEMBRAMENTO" in s_upper:
                    after = re.sub(r"DESMEMBRAMENTO\s*", "", s_upper).strip()
                    return after if after else s_upper
                if "NBA" in s_upper:
                    m = re.search(r"NBA[\s-]*(\w+)", s_upper)
                    return f"NBA {m.group(1)}" if m else "NBA"
                # Skip shop/location names and numbers
                try:
                    float(s)
                    continue  # skip pure numbers
                except (ValueError, TypeError):
                    pass
                if "SHOPPING" in s_upper or "LOJA" in s_upper:
                    continue  # skip store names
        # Fallback: look in the first row for a short numeric/alphanumeric code
        for row in rows[:3]:
            for cell in row:
                if cell is None:
                    continue
                s = str(cell).strip()
                if re.match(r"^\d{3,}$", s):
                    return s
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_number(self, value: str) -> Optional[float]:
        if not value or not value.strip():
            return None
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
