from __future__ import annotations

import re

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser
from app.utils.logger import logger

_SIGNATURE_CNPJ = "00.063.960"
_SIGNATURE_TEXT = "Itens do Pedido"
_GRADE_MARKER = "Cross Docking"

_ITEM_RE = re.compile(
    r"^(\d+)\s+(\d{13})\s+Unidade\s+([\d,]+)\s+([\d,]+)\s+([\d,.]+)\s+([\d,.]+)"
    r".*?([\d,.]+)$",
    re.MULTILINE,
)

# Linha "de dados" da tabela Cross Docking: <EAN local> <EAN produto> <qty> <data inicial>
_CD_DATA_RE = re.compile(r"^(\d{13})\s+(\d{13})\s+([\d,.]+)\s+(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})\s*$")
# CNPJ é quebrado pelo pdfplumber em 2 linhas (cabeçalho/rodapé do registro):
_CD_CNPJ_HEAD_RE = re.compile(r"^(\d{2}\.\d{3}\.\d{3})\s*/")
_CD_CNPJ_TAIL_RE = re.compile(r"^(\d{4}-\d{2})\b")

# Consolidado: CNPJ e NOME do CD dividem a mesma linha.
#   CNPJ do Local de Entrega: 00.063.960 / 0587-94 CD SAM'S DF
_DELIVERY_LINE_RE = re.compile(
    r"CNPJ do Local de Entrega:\s*(\d{2}\.\d{3}\.\d{3}\s*/\s*\d{4}-\d{2})[ \t]*([^\n]*)"
)
# Shape antiga, mantida como rede: só o CNPJ, sem exigir a máscara completa.
_DELIVERY_CNPJ_FALLBACK_RE = re.compile(r"CNPJ do Local de Entrega:\s*([\d./ -]+)")
# O EAN do CD fica sozinho numa linha, entre o rótulo quebrado em duas partes:
#   Código EAN do Local de
#   7891737676667
#   Entrega:
# O bloco de Cobrança tem o mesmo rótulo, por isso o âncora "Entrega:" no fim.
_DELIVERY_EAN_RE = re.compile(r"C[oó]digo EAN do Local de\s*\n\s*(\d{13})\s*\n\s*Entrega:")


class SamsClubParser(BaseParser):
    """Parser para PDFs de pedido Sam's Club / Walmart.

    Suporta dois layouts do WebEDI/Neogrid:
    - Consolidado: 1 destino único (CD) → todos os itens recebem o mesmo delivery_cnpj.
    - GRADE (com seção "Cross Docking"): cada SKU é decomposto por loja final;
      cada linha vira 1 OrderItem com (delivery_cnpj, delivery_ean) próprios.
    """

    def can_parse(self, extracted: dict) -> bool:
        text = extracted.get("text", "")
        return _SIGNATURE_CNPJ in text and _SIGNATURE_TEXT.lower() in text.lower()

    def parse(self, extracted: dict) -> Order | None:
        text = extracted.get("text", "")
        if not self.can_parse(extracted):
            return None

        grade = _GRADE_MARKER in text
        header = self._parse_header(text, grade=grade)

        if grade:
            item_lookup = self._build_item_lookup(text)
            items = self._parse_cross_docking(text, item_lookup, header)
            self._warn_if_grade_diverges(text, items)
        else:
            items = self._parse_items(text)

        if not items:
            return None

        return Order(header=header, items=items)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _parse_header(self, text: str, *, grade: bool = False) -> OrderHeader:
        """Cabeçalho do pedido.

        **O cliente é o LOCAL DE ENTREGA, não o Comprador.** A MM cadastra no
        Fire o CD que recebe (`00.063.960/0587-94` = CD SAM'S DF), não cada
        clube que emite a ordem — o `CNPJ:` do bloco Comprador muda a cada
        pedido (`/0044-30`, `/0048-64`, `/0223-31`) e nenhum deles existe no
        `CADASTRO`. Mesma regra do SBF/Centauro, que casa pelo CNPJ de
        faturamento e não pela matriz. Confirmado pela MM em 10/09/2026.

        Efeito colateral bom: `customer_name` deixa de ser nulo (era
        `SEM_CLIENTE` no nome do arquivo) e passa a ser o nome do CD.

        ⚠️ **A GRADE fica de fora.** Lá o `Local de Entrega` do cabeçalho é um
        CD de trânsito e a mercadoria é cross-docked para N lojas — cada uma já
        vira um arquivo próprio no split. Quem é o cliente de cada perna é
        pergunta em aberto (ver `docs/BACKLOG.md`); até ter um caso real
        reportado, o comportamento antigo continua.
        """
        order_number = self._find(text, r"N[uú]mero (?:do )?Pedido:\s*([\d-]+)")
        issue_date = self._extract_date(
            text, r"Data de Emiss[aã]o:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})"
        )
        customer_cnpj = self._find(text, r"CNPJ:\s*([\d./ -]+)")
        if customer_cnpj:
            customer_cnpj = re.sub(r"\s+", "", customer_cnpj)
        customer_name = self._find(text, r"Destinat[aá]rio:\s*([^\n\r]+?)\s*(?:\n|$)")

        if not grade:
            entrega_cnpj, entrega_nome = self._parse_delivery_location(text)
            # Sem local de entrega legível, o Comprador segue como rede — nunca
            # devolver cabeçalho sem CNPJ (o exporter trata isso como Riachuelo).
            if entrega_cnpj:
                customer_cnpj = entrega_cnpj
                customer_name = entrega_nome or customer_name

        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _extract_date(self, text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        raw = m.group(1)
        # Remove spaces around slashes
        return re.sub(r"\s*/\s*", "/", raw)

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def _items_section(self, text: str) -> str:
        """Recorta o texto a partir do cabeçalho 'Itens do Pedido' (case-insensitive)."""
        m = re.search(re.escape(_SIGNATURE_TEXT), text, re.IGNORECASE)
        return text[m.start() :] if m else text

    def _build_item_lookup(self, text: str) -> dict[str, float]:
        """Mapa {ean_produto: preço} da tabela 'Itens do Pedido'.

        A seção Cross Docking não repete o preço — só a quantidade por loja.

        ⚠️ `Qtde na Emb.` (grupo 3) é lido e **descartado de propósito**: ver
        `_parse_items`. Não usar como multiplicador.
        """
        section = self._items_section(text)
        lookup: dict[str, float] = {}
        for m in _ITEM_RE.finditer(section):
            unit_price = self._parse_br_number(m.group(5))
            lookup[m.group(2)] = unit_price if unit_price is not None else 0.0
        return lookup

    def _parse_cross_docking(
        self,
        text: str,
        item_lookup: dict[str, float],
        header: OrderHeader,
    ) -> list[OrderItem]:
        """Parsea a seção 'Cross Docking' do PDF GRADE.

        Layout (pdfplumber preserva quebra visual do CNPJ em 3 linhas):
            00.063.960 /                                          00 / 00 /
            7891737001698 7898686876711 16,00 00 / 00 / 0000
            0094-08                                                0000

        - Linha N-1: início do CNPJ (`00.063.960 /`)
        - Linha N:   `<EAN_local> <EAN_produto> <qty> <data_inicial>`
        - Linha N+1: final do CNPJ (`0094-08`)

        A quantidade da grade está na MESMA unidade da `Qtde Pedida` da tabela
        superior — medido no sample: SKU 7898686879194 tem `Qtde Pedida = 2` lá
        em cima e duas linhas de `1,00` aqui. Não multiplicar por nada.
        """
        idx = text.find(_GRADE_MARKER)
        if idx == -1:
            return []

        fallback_date = self._extract_date(text, r"Data Inicial:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})")

        lines = text[idx:].split("\n")
        items: list[OrderItem] = []
        for i, raw in enumerate(lines):
            m = _CD_DATA_RE.match(raw.strip())
            if not m:
                continue

            ean_local = m.group(1)
            ean_produto = m.group(2)
            qty = self._parse_br_number(m.group(3))
            data_inicial = re.sub(r"\s*/\s*", "/", m.group(4))

            if qty is None or qty <= 0:
                continue

            cnpj = self._stitch_cnpj(lines, i)

            if data_inicial == "00/00/0000":
                data_inicial = fallback_date

            unit_price = item_lookup.get(ean_produto)
            # 2 casas: a grade não traz total impresso, então ele nasce aqui —
            # sem arredondar, o float vaza (730.4400000000001) para o XLSX e
            # para CORPO_VENDAS.TOTAL.
            total_price = round(qty * unit_price, 2) if unit_price else None

            items.append(
                OrderItem(
                    ean=ean_produto,
                    product_code=ean_produto,
                    description=ean_produto,
                    quantity=qty,
                    unit_price=unit_price,
                    total_price=total_price,
                    delivery_date=data_inicial,
                    delivery_cnpj=cnpj,
                    delivery_ean=ean_local,
                )
            )
        return items

    def _stitch_cnpj(self, lines: list[str], data_idx: int) -> str | None:
        """Junta as 2 metades do CNPJ que ficam acima/abaixo da linha de dados."""
        head = None
        tail = None
        if data_idx >= 1:
            mh = _CD_CNPJ_HEAD_RE.match(lines[data_idx - 1].strip())
            if mh:
                head = mh.group(1)
        if data_idx + 1 < len(lines):
            mt = _CD_CNPJ_TAIL_RE.match(lines[data_idx + 1].strip())
            if mt:
                tail = mt.group(1)
        if head and tail:
            return f"{head}/{tail}"
        return None

    def _warn_if_grade_diverges(self, text: str, items: list[OrderItem]) -> None:
        """Soma qty da grade por SKU e compara com a tabela superior. Warning se divergir."""
        section = self._items_section(text)

        agg: dict[str, float] = {}
        for m in _ITEM_RE.finditer(section):
            ean = m.group(2)
            ped = self._parse_br_number(m.group(4)) or 0.0
            agg[ean] = agg.get(ean, 0.0) + ped

        grade_sum: dict[str, float] = {}
        for it in items:
            if it.ean and it.quantity:
                grade_sum[it.ean] = grade_sum.get(it.ean, 0.0) + it.quantity

        for ean, expected in agg.items():
            got = grade_sum.get(ean, 0.0)
            if abs(got - expected) > 0.01:
                logger.warning(
                    f"Sams GRADE: divergência qty SKU {ean}: agregado={expected} grade={got}"
                )

    def _parse_items(self, text: str) -> list[OrderItem]:
        """Itens do layout consolidado.

        ⚠️ `Qtde na Emb.` (grupo 3 do `_ITEM_RE`) NÃO é multiplicador. O produto
        vendido é o KIT: `Qtde na Emb.` diz quantas peças vão dentro dele e
        `Qtde Pedida` quantos kits o Sam's quer. `Preço Bruto` é o preço do kit,
        e `Valor Total Item` fecha como `Qtde Pedida × Preço Bruto` nos três
        samples — o pedido 06839396-0000 tem emb=22, pedida=1, bruto=694,98 e
        total=694,98: um kit de 22, não 22 unidades.

        Até 2026-09 a quantidade saía como `emb × pedida`, então um kit de 22
        entrava no Fire como 22 kits (22x o pedido real) e `QUANTIDADE ×
        PRECO_UNITARIO` não fechava com `VALOR_TOTAL` no XLSX. Passou meses
        despercebido porque emb=1 na esmagadora maioria dos itens (16 dos 18 do
        sample de janeiro).
        """
        # Restrict to section after "Itens do Pedido"
        section = self._items_section(text)

        delivery_date = self._extract_date(text, r"Data Inicial:\s*(\d{2}\s*/\s*\d{2}\s*/\s*\d{4})")
        delivery_cnpj, delivery_name = self._parse_delivery_location(text)
        ean_m = _DELIVERY_EAN_RE.search(text)
        delivery_ean = ean_m.group(1) if ean_m else None

        items = []
        for m in _ITEM_RE.finditer(section):
            ean = m.group(2)
            qty = self._parse_br_number(m.group(4))
            preco_bruto = self._parse_br_number(m.group(5))
            total_str = m.group(7)
            total_price = self._parse_br_number(total_str)

            if qty is None:
                continue

            items.append(
                OrderItem(
                    ean=ean,
                    product_code=ean,
                    description=ean,
                    quantity=qty,
                    unit_price=preco_bruto,
                    total_price=total_price,
                    delivery_date=delivery_date,
                    delivery_cnpj=delivery_cnpj,
                    delivery_name=delivery_name,
                    delivery_ean=delivery_ean,
                )
            )
        return items

    def _parse_delivery_location(self, text: str) -> tuple[str | None, str | None]:
        """CNPJ e nome do CD de entrega no layout consolidado.

        O nome importa quando o Sam's abre um CD novo: sem ele o pedido chega
        no preview como um CNPJ solto e o operador não sabe para onde vai.
        """
        m = _DELIVERY_LINE_RE.search(text)
        if m:
            return re.sub(r"\s+", "", m.group(1)), (m.group(2).strip() or None)

        fallback = _DELIVERY_CNPJ_FALLBACK_RE.search(text)
        if fallback:
            return re.sub(r"\s+", "", fallback.group(1)).strip(), None
        return None, None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
