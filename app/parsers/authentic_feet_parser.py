from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
_SIGNATURE_TEXT = "AUTHENTICFEET"
_HEADER_TOKENS = ("REF.", "DESCRIÇÃO PRODUTO", "TOTAL KITS", "TOTAL R$")


class AuthenticFeetParser(BaseParser):
    """Parser para pedidos single-customer da rede Authentic Feet (XLSX)."""

    def can_parse(self, extracted: dict) -> bool:
        text_upper = extracted.get("text", "").upper()
        if _SIGNATURE_TEXT not in text_upper:
            return False
        rows = extracted.get("rows", [])
        for row in rows[:30]:
            cells = [str(c).strip() if c is not None else "" for c in row]
            if "REF." in cells and "TOTAL KITS" in cells:
                return True
        return False

    def parse(self, extracted: dict) -> Optional[Order]:
        if not self.can_parse(extracted):
            return None

        rows = extracted.get("rows", [])
        if not rows:
            return None

        header_idx, col_map = self._find_header_row(rows)
        if header_idx is None:
            return None

        order_header = self._parse_header_block(rows, header_idx)
        items = self._parse_items(rows, header_idx, col_map)

        if not items:
            return None

        return Order(header=order_header, items=items)

    # ------------------------------------------------------------------
    # Header (cliente / pedido)
    # ------------------------------------------------------------------

    def _parse_header_block(self, rows: list, header_idx: int) -> OrderHeader:
        customer_cnpj: Optional[str] = None
        customer_name: Optional[str] = None
        fantasia: Optional[str] = None
        issue_date: Optional[str] = None

        for row in rows[:header_idx]:
            cells = list(row)
            for j, cell in enumerate(cells):
                if cell is None:
                    continue
                label = str(cell).strip().upper().rstrip(":").strip()

                if not customer_name and label in ("RAZÃO SOCIAL", "RAZAO SOCIAL"):
                    raw = self._next_raw(cells, j)
                    if raw is not None:
                        customer_name = str(raw).strip() or None
                elif not customer_cnpj and label == "CNPJ":
                    raw = self._next_raw(cells, j)
                    if raw is not None:
                        s = str(raw).strip()
                        m = _CNPJ_RE.search(s)
                        customer_cnpj = m.group(0) if m else s
                elif not fantasia and label == "FANTASIA":
                    raw = self._next_raw(cells, j)
                    if raw is not None:
                        fantasia = str(raw).strip().rstrip(".").strip() or None
                elif not issue_date and label in ("DATA DO PEDIDO", "DATA PEDIDO"):
                    issue_date = self._coerce_date(self._next_raw(cells, j))

        order_number = fantasia or issue_date

        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _next_raw(self, cells: list, label_idx: int):
        """Devolve o primeiro valor não-vazio à direita do label, preservando o tipo
        (datetime, float, str). Stringificar é responsabilidade do chamador."""
        for k in range(label_idx + 1, len(cells)):
            v = cells[k]
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return v
        return None

    def _coerce_date(self, value) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.strftime("%d/%m/%Y")
        s = str(value).strip()
        return s or None

    # ------------------------------------------------------------------
    # Itens
    # ------------------------------------------------------------------

    def _find_header_row(self, rows: list) -> tuple[Optional[int], dict]:
        for i, row in enumerate(rows):
            cells = [str(c).strip() if c is not None else "" for c in row]
            if not all(tok in cells for tok in _HEADER_TOKENS):
                continue

            col_map = {
                "ref": cells.index("REF."),
                "produto": cells.index("DESCRIÇÃO PRODUTO"),
                "total_kits": cells.index("TOTAL KITS"),
                "total_rs": cells.index("TOTAL R$"),
            }
            for j, c in enumerate(cells):
                if c == "DESCRIÇÃO COR" and "cor" not in col_map:
                    col_map["cor"] = j
                elif c == "TAMANHOS" and "tamanhos" not in col_map:
                    col_map["tamanhos"] = j
                elif c == "OBS" and "obs" not in col_map:
                    col_map["obs"] = j
                elif c == "CUSTO" and "custo" not in col_map:
                    col_map["custo"] = j
            return i, col_map
        return None, {}

    def _parse_items(self, rows: list, header_idx: int, col_map: dict) -> list[OrderItem]:
        items: list[OrderItem] = []

        for row in rows[header_idx + 1:]:
            if not row or all(c is None or str(c).strip() == "" for c in row):
                continue

            ref = self._cell(row, col_map.get("ref"))
            if not ref:
                # totalizador da última linha (REF. vazio) ou linha de rodapé
                continue

            qty = self._to_number(self._cell(row, col_map.get("total_kits")))
            if qty is None or qty <= 0:
                continue

            produto = self._cell(row, col_map.get("produto")) or ""
            cor = self._cell(row, col_map.get("cor")) or ""
            tamanhos = self._cell(row, col_map.get("tamanhos")) or ""
            description = " - ".join(p for p in (produto, cor, tamanhos) if p)

            items.append(OrderItem(
                product_code=ref,
                description=description or None,
                quantity=qty,
                unit_price=self._to_number(self._cell(row, col_map.get("custo"))),
                total_price=self._to_number(self._cell(row, col_map.get("total_rs"))),
                obs=self._cell(row, col_map.get("obs")) or None,
            ))

        return items

    # ------------------------------------------------------------------
    # Helpers locais
    # ------------------------------------------------------------------

    def _cell(self, row: list, idx: Optional[int]) -> str:
        if idx is None or idx >= len(row):
            return ""
        v = row[idx]
        if v is None:
            return ""
        return str(v).strip()

    def _to_number(self, value) -> Optional[float]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = re.sub(r"[R$\s]", "", str(value))
        if not s:
            return None
        try:
            if "," in s:
                return float(s.replace(".", "").replace(",", "."))
            return float(s)
        except ValueError:
            return None
