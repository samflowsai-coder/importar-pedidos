from __future__ import annotations

import re

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_SIGNATURE = "DAKOTA NORDESTE"

_ITEM_CODE_RE = re.compile(r"^(\d{5}\.\d{3}/\d)", re.MULTILINE)

# Linha de rodapé da tabela ("4 itens TOTAL: 8,000.000 TOTAL R$.: 81,240.00").
# Delimita o fim dos itens: sem isso a cauda da descrição do ÚLTIMO item
# engoliria TRANSPORTE / INFORMACOES ADICIONAIS.
_ITEMS_END_RE = re.compile(r"^\s*\d+\s+itens\b", re.MULTILINE)

# Primeira linha de um item: COD.CLI. + início da descrição + números.
# A cauda da descrição vem nas linhas seguintes (quebra visual do pdfplumber).
_ITEM_HEAD_RE = re.compile(
    r"^(\d{5}\.\d{3}/\d)\s+(.+?)\s+([\d,]+\.?\d*)\s+UN\s+([\d.]+)\s+[\d.]+\s+([\d,.]+)\s*$"
)

# Referência da Nasmar dentro da descrição ("... KOLOSH KL403G-0003 (1 PTA/1...").
# É ela que o Fire guarda em PRODUTOS.CODPROD_ALTERN — o COD.CLI. da coluna
# esquerda é o código interno da Dakota e não existe no catálogo da Nasmar.
_NASMAR_REF_RE = re.compile(r"\bKOLOSH\s+([A-Z0-9]+(?:-[A-Z0-9]+)+)\b")


class KoloshParser(BaseParser):
    """Parser para PDFs de pedido Kolosh / Dakota Nordeste."""

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "")

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
        order_number = self._find(text, r"Numero\s*:\s*(\w+)")
        customer_cnpj = self._find(text, r"CNPJ:\s*([\d./-]+)")
        customer_name = self._find(text, r"Razao Social:\s*(.+?)\s+Numero")
        return OrderHeader(
            order_number=order_number,
            issue_date=self._extract_issue_date(text),
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _extract_issue_date(self, text: str) -> str | None:
        """`Emissao`, não `Entrega`.

        Vira `CAB_VENDAS.DATA_PEDIDO` no Fire (`app/erp/mapper.py`). Antes daqui
        saía a data de entrega e o pedido entrava no ERP com emissão meses à frente.
        """
        return self._extract_date(text, "Emissao")

    def _extract_delivery_date(self, text: str) -> str | None:
        return self._extract_date(text, "Entrega")

    def _extract_date(self, text: str, label: str) -> str | None:
        m = re.search(rf"{label}\s*:\s*(\d{{2}}/\d{{2}}/(\d{{2,4}}))", text)
        if not m:
            return None
        date_str, year_part = m.group(1), m.group(2)
        if len(year_part) == 2:
            date_str = date_str[:-2] + "20" + year_part
        return date_str

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _parse_items(self, text: str) -> list[OrderItem]:
        delivery_date = self._extract_delivery_date(text)

        end = _ITEMS_END_RE.search(text)
        section = text[: end.start()] if end else text

        items = []
        matches = list(_ITEM_CODE_RE.finditer(section))
        for i, match in enumerate(matches):
            start = match.start()
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(section)
            item = self._parse_block(section[start:stop], delivery_date)
            if item:
                items.append(item)
        return items

    def _parse_block(self, block: str, delivery_date: str | None = None) -> OrderItem | None:
        """Um item ocupa 1 linha de dados + N linhas de cauda da descrição.

        04145.007/9 KIT 3 PRS ... KL403G-0003 (1 PTA/1 2,000.000 UN 11.23 0.00 22,460.00
        BCA/1 CZA) NR 39/44
        """
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            return None

        m = _ITEM_HEAD_RE.match(lines[0])
        if not m:
            return None

        cod_cliente = m.group(1)
        description = " ".join([m.group(2).strip(), *lines[1:]]).strip()
        qty = self._parse_us_number(m.group(3))
        unit_price = self._parse_us_number(m.group(4))
        total_price = self._parse_us_number(m.group(5))

        if qty is None:
            return None

        ref = _NASMAR_REF_RE.search(description)

        return OrderItem(
            product_code=ref.group(1) if ref else cod_cliente,
            description=description,
            quantity=qty,
            unit_price=unit_price,
            total_price=total_price,
            # A OC exige o código da Dakota na nota fiscal, então ele não pode
            # sumir quando o product_code passa a ser a referência da Nasmar.
            obs=f"COD.CLI. {cod_cliente}",
            delivery_date=delivery_date,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_us_number(self, value: str) -> float | None:
        """Parse US-format numbers: 500.000 = 500, 1,000.000 = 1000, 4,985.00 = 4985."""
        if not value or not value.strip():
            return None
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None

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
