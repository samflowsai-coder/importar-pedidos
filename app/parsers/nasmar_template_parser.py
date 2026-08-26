from __future__ import annotations

import datetime as _dt
import re

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers.base_parser import BaseParser

_CNPJ_RE = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}")
_ESPACO = re.compile(r"\s+")
# Texto no formato de milhar brasileiro: grupos de 3 dígitos separados por ponto,
# sem decimal (`1.300`, `1.234.567`). `300.0` NÃO casa — 1 dígito no último grupo
# é decimal, não milhar. Mesma regra do DajuParser (helper duplicado por dívida
# conhecida — ver docs/BACKLOG.md; a casa manda copiar do vizinho, não inventar).
_MILHAR_BR = re.compile(r"\d{1,3}(?:\.\d{3})+")

# Mesmo template de "Pedido" single-customer é usado por Authentic Feet, Magic
# Feet, pedidos "Pulmão" do Grupo Afeet e Tennis Station (mesmo fornecedor). A
# assinatura é o CABEÇALHO do template — não o nome da marca: pedidos Pulmão vêm
# com os campos de cliente em branco, sem nenhum texto 'AUTHENTICFEET'/'MAGICFEET'
# no conteúdo (a marca só aparece no nome do arquivo). O conjunto de 4 colunas
# abaixo é único deste fornecedor. A quantidade real fica em TOTAL KITS; sem este
# parser o arquivo cai no GenericParser, que lê o REF COR (cor) como quantidade.
#
# O match é por texto NORMALIZADO (upper + espaço colapsado), nunca por igualdade
# literal: a Tennis Station digitou `TOTAL Kits` e o arquivo inteiro escapou do
# parser por causa do caixa de duas letras.
_HEADER_TOKENS = ("REF.", "DESCRIÇÃO PRODUTO", "TOTAL KITS", "TOTAL R$")

# Colunas opcionais: rótulo normalizado -> chave do col_map.
_COLUNAS_OPCIONAIS = {
    "DESCRIÇÃO COR": "cor",
    "TAMANHOS": "tamanhos",
    "OBS": "obs",
    "CUSTO": "custo",
}


def _norm(value: object) -> str:
    """Texto canônico de uma célula para efeito de match de cabeçalho."""
    if value is None:
        return ""
    return _ESPACO.sub(" ", str(value)).strip().upper()


class NasmarTemplateParser(BaseParser):
    """Parser do template de pedido de kits do fornecedor (Nasmar/MM), em XLSX.

    Um template, N clientes: Authentic Feet, Magic Feet e os pedidos "Pulmão" do
    Grupo Afeet. A assinatura é o CABEÇALHO da tabela, nunca a marca.
    """

    def can_parse(self, extracted: dict) -> bool:
        # O cabeçalho completo do template de kits é a assinatura confiável (não o
        # nome da marca, que pode estar ausente). Mesma função que o _find_header_row
        # usa — antes eram duas cópias da regra, e a que decidia o col_map era ainda
        # mais estrita que a do gate.
        rows = extracted.get("rows", [])
        return any(self._match_header(row) is not None for row in rows[:30])

    def parse(self, extracted: dict) -> Order | None:
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
        customer_cnpj: str | None = None
        customer_name: str | None = None
        fantasia: str | None = None
        issue_date: str | None = None
        ordem_compra: str | None = None

        for row in rows[:header_idx]:
            cells = list(row)
            for j, cell in enumerate(cells):
                if cell is None:
                    continue
                label = _norm(cell).rstrip(":").strip()

                if not ordem_compra and label in ("ORDEM DE COMPRA", "ORDEM DE COMPRA Nº"):
                    ordem_compra = self._coerce_text(self._next_raw(cells, j))
                elif not customer_name and label in ("RAZÃO SOCIAL", "RAZAO SOCIAL"):
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

        # `Ordem de compra` é o número de pedido de verdade — campo próprio, que o
        # comprador preenche pra referenciar a OC dele. FANTASIA é apelido digitado
        # livre e só serve de fallback porque o template antigo (AF/MF) não tem
        # campo de número: foi de lá que saiu o `AF76` vs `AF076` que ficou aberto
        # na reconciliação com o Fire. Onde os dois existirem, o campo próprio ganha.
        order_number = ordem_compra or fantasia or issue_date

        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _next_raw(self, cells: list, label_idx: int):
        """Devolve o primeiro valor não-vazio à direita do label, preservando o tipo
        (datetime, float, str). Stringificar é responsabilidade do chamador.

        Se o primeiro não-vazio for OUTRO rótulo (string terminando em ':', como
        'FANTASIA:' logo após um 'RAZÃO SOCIAL:' em branco), o campo está vazio →
        devolve None em vez de capturar o rótulo do campo seguinte."""
        for k in range(label_idx + 1, len(cells)):
            v = cells[k]
            if v is None:
                continue
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    continue
                if s.endswith(":"):
                    return None
            return v
        return None

    def _coerce_text(self, value) -> str | None:
        """Texto de um campo de identificação, sem o `.0` do float do openpyxl.

        Número de pedido digitado como número puro (`4417`) volta como float, e
        `str()` daria `'4417.0'` — que vira a chave PEDIDO_CLIENTE no Fire e não
        casa com nada.
        """
        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value).strip() or None

    def _coerce_date(self, value) -> str | None:
        if value is None:
            return None
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.strftime("%d/%m/%Y")
        s = str(value).strip()
        return s or None

    # ------------------------------------------------------------------
    # Itens
    # ------------------------------------------------------------------

    def _match_header(self, row: list) -> dict | None:
        """Se `row` é o cabeçalho da tabela de kits, devolve o `col_map`; senão None.

        Fonte única da regra: é o gate do `can_parse` E o mapeamento de colunas. As
        4 colunas de `_HEADER_TOKENS` são obrigatórias — o resto é opcional e cada
        uma fica no índice da PRIMEIRA ocorrência.
        """
        cells = [_norm(c) for c in row]
        if not all(tok in cells for tok in _HEADER_TOKENS):
            return None

        col_map = {
            "ref": cells.index("REF."),
            "produto": cells.index("DESCRIÇÃO PRODUTO"),
            "total_kits": cells.index("TOTAL KITS"),
            "total_rs": cells.index("TOTAL R$"),
        }
        for j, c in enumerate(cells):
            chave = _COLUNAS_OPCIONAIS.get(c)
            if chave and chave not in col_map:
                col_map[chave] = j
        return col_map

    def _find_header_row(self, rows: list) -> tuple[int | None, dict]:
        for i, row in enumerate(rows):
            col_map = self._match_header(row)
            if col_map is not None:
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

            # Valor CRU, não o stringificado: é o que deixa `_to_number` distinguir
            # o int nativo do openpyxl do texto '1.300'. Stringificar antes joga
            # fora justamente a informação que decide milhar vs. decimal.
            qty = self._to_number(self._raw(row, col_map.get("total_kits")))
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
                unit_price=self._to_number(self._raw(row, col_map.get("custo"))),
                total_price=self._to_number(self._raw(row, col_map.get("total_rs"))),
                obs=self._cell(row, col_map.get("obs")) or None,
            ))

        return items

    # ------------------------------------------------------------------
    # Helpers locais
    # ------------------------------------------------------------------

    def _raw(self, row: list, idx: int | None):
        """Valor da célula com o tipo preservado. Use nos campos numéricos."""
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    def _cell(self, row: list, idx: int | None) -> str:
        """Valor da célula como texto. Use só nos campos textuais."""
        v = self._raw(row, idx)
        if v is None:
            return ""
        return str(v).strip()

    def _to_number(self, value) -> float | None:
        """Número da planilha, sem adivinhação.

        Os dois caminhos — célula nativa do openpyxl e célula em texto — precisam
        dar o MESMO resultado: um erro aqui entra no ERP como pedido de quantidade
        errada, passa no validador (qty > 0) e ninguém percebe. O template chega
        nativo hoje, mas o cliente que exporta de outro sistema manda texto, e a
        Daju provou que a mesma coluna troca de tipo entre arquivos.

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
