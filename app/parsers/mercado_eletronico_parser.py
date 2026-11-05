from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser, PedidoRecusadoError

_SIGNATURE = "Mercado Eletrônico"
# Item header line: "NNN. D,DD % DD,DD % QQ,QQQQST BRL ..."
# (aceita "CANCELADO" logo após o número, para o bloco do item cancelado não
# ser engolido pelo item anterior)
_ITEM_LINE = re.compile(r"^\d+\.\s+(?:CANCELADO\s+)?[\d,]", re.MULTILINE)
# Item cancelado pela varejista: o ME imprime "CANCELADO" sob o número do item,
# mantém quantidade e preço unitário e só zera o "Preço Total Item". Dois sinais,
# porque a posição da palavra no texto depende da ordem que o pdfplumber dá à
# célula do número: a palavra (procurada só antes da descrição, para nome de
# produto não derrubar item) ou o Preço Total zerado com preço unitário > 0.
_CANCELADO_RE = re.compile(r"\bCANCELADO\b")
_DESCRICAO_RE = re.compile(r"Descri[çc][aã]o do Material")
_PRECO_TOTAL_ITEM_RE = re.compile(r"Pre[çc]o Total Item:\s*([\d.,]+)")
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


@dataclass(frozen=True)
class _Cancelado:
    numero: str
    codigo: str | None
    quantidade: float | None
    cnpj: str | None


def _fmt_qtd(qtd: float | None) -> str:
    if qtd is None:
        return "?"
    if qtd == int(qtd):
        return str(int(qtd))
    return str(qtd).replace(".", ",")


class MercadoEletronicoParser(BaseParser):
    """Parser para PDFs exportados do portal Mercado Eletrônico (Riachuelo/Guararapes).

    Lê do texto completo — cobre itens que cruzam quebra de página e que não
    foram capturados nas tabelas pelo pdfplumber.
    """

    def can_parse(self, extracted: dict) -> bool:
        return _SIGNATURE in extracted.get("text", "")

    def parse(self, extracted: dict) -> Order | None:
        text = extracted.get("text", "")
        if not self.can_parse(extracted):
            return None

        header = self._parse_header(text)
        items, cancelados = self._parse_items(text)

        if not items:
            if cancelados:
                pedido = f"do pedido {header.order_number} " if header.order_number else ""
                numeros = ", ".join(c.numero for c in cancelados)
                raise PedidoRecusadoError(
                    f"Todos os itens {pedido}estão CANCELADOS no Mercado Eletrônico "
                    f"(itens {numeros}): não há o que importar."
                )
            return None

        self._anotar_cancelados(items, cancelados, header.order_number)
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

    def _parse_items(self, full_text: str) -> tuple[list[OrderItem], list[_Cancelado]]:
        """Itens válidos e os itens CANCELADOS (que ficam fora do pedido)."""
        # Strip page-footer artifacts (portal URL + pagination stamps) before parsing
        full_text = _PAGE_ARTIFACT_RE.sub("", full_text)
        positions = [m.start() for m in _ITEM_LINE.finditer(full_text)]
        if not positions:
            return [], []

        items = []
        cancelados: list[_Cancelado] = []
        for i, start in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(full_text)
            block = full_text[start:end]
            if self._is_cancelado(block):
                cancelado = _Cancelado(
                    numero=block.split(".", 1)[0].strip(),
                    codigo=self._extract_desc_and_code(block)[1],
                    quantidade=self._extract_qty(block.splitlines()[0]),
                    cnpj=self._extract_cnpj(block),
                )
                cancelados.append(cancelado)
                logger.warning(f"Mercado Eletrônico: item CANCELADO fora do pedido: {cancelado}")
                continue
            item = self._parse_block(block)
            if item:
                items.append(item)
        return items, cancelados

    def _is_cancelado(self, block: str) -> bool:
        cabeca = _DESCRICAO_RE.split(block, maxsplit=1)[0]
        if _CANCELADO_RE.search(cabeca):
            return True
        m = _PRECO_TOTAL_ITEM_RE.search(block)
        if not m or self._parse_br_number(m.group(1)) != 0:
            return False
        unit_price, _ = self._extract_prices(block.splitlines()[0])
        return bool(unit_price)

    def _anotar_cancelados(
        self, items: list[OrderItem], cancelados: list[_Cancelado], order_number: str | None
    ) -> None:
        """Avisa na OBS da loja que item cancelado ficou fora do pedido.

        Pedido alterado costuma já estar no Fire (a operação exclui e sobe de
        novo); a OBS é o que chega ao preview e ao Fire. Mesmas regras da OBS
        do Sam's (`_anotar_packs`): começa pelo número do pedido, é a mesma em
        todas as linhas da loja e só vale para a loja do item cancelado.

        Loja com TODOS os itens cancelados não tem linha para levar a OBS: ela
        só não aparece no pedido (e no log, como WARNING). Não vai para a OBS de
        outra loja, que sai impressa na separação do pedido errado. No fluxo da
        operação (exclui o pedido alterado no Fire e sobe de novo) isso basta.
        """
        por_loja: dict[str, list[_Cancelado]] = {}
        for c in cancelados:
            por_loja.setdefault(c.cnpj or "", []).append(c)
        for cnpj, grupo in por_loja.items():
            fora = ", ".join(
                f"ITEM {c.numero} {c.codigo or '?'} QTD {_fmt_qtd(c.quantidade)}" for c in grupo
            )
            partes = [f"CANCELADO PELO CLIENTE, FORA DO PEDIDO: {fora}"]
            if order_number:
                partes.insert(0, order_number)
            obs = " | ".join(partes)
            for it in items:
                if (it.delivery_cnpj or "") == cnpj:
                    it.obs = f"{it.obs} | {obs}" if it.obs else obs

    def _parse_block(self, block: str) -> OrderItem | None:
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

    def _extract_qty(self, line: str) -> float | None:
        # Número BR com milhar opcional ("2.795,0000ST"): o ponto faz parte da quantidade.
        m = re.search(r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?)\s*ST", line)
        return self._parse_br_number(m.group(1)) if m else None

    def _extract_prices(self, line: str) -> tuple[float | None, float | None]:
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

    def _extract_cnpj(self, block: str) -> str | None:
        # Look inside "Local Entrega Item:" section
        m = re.search(r"Local Entrega Item:.+?(" + _CNPJ_RE.pattern + r")", block, re.DOTALL)
        if m:
            return m.group(1)
        # Fallback: any formatted CNPJ in block
        m = _CNPJ_RE.search(block)
        return m.group(0) if m else None

    def _extract_delivery_name(self, block: str) -> str | None:
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

    def _parse_br_number(self, value: str) -> float | None:
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

    def _find(self, text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None
