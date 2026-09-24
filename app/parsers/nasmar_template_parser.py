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
# Letra do tamanho no início da célula TAMANHOS, colada à numeração:
# `M - 33-38`, `GG - 45-48`, `M/33-38`, `M 33-38`. O template do AF/MF/TS traz
# só a numeração (`33 - 38`) e não casa; `P/M - 33-38` também não (letra ambígua).
_LETRA_TAMANHO = re.compile(r"([A-Z]{1,3})\s*[-/]?\s*\d")
# Código de loja da H2S4 no FANTASIA: é a convenção da MM no PEDIDO_CLIENTE do
# Fire, e a reconciliação depende dela. Forma medida na Fire viva (21/09/2026):
# 675 de 700 códigos são 2 letras + 2 a 4 dígitos (`AF198`, `AW064`, `MF048`,
# `AF76`), às vezes com sufixo de loja (`AF090 - 3`). Nome de loja não casa, nem
# com dígito: `NBA Store Mogi Shopping`, `LOJA 10`, `NBA 3`, `CD 1`.
_CODIGO_DE_LOJA = re.compile(r"[A-Z]{2}\s*-?\s*\d{2,4}(\s*-\s*\d{1,4})?")

# Modelo da linha Kings: `KG 07`, `KG07`. Exatamente 2 dígitos — é a forma de
# todos os 28 kits KG no Fire da MM (`.7`, conferido em 24/09/2026).
_MODELO_KINGS = re.compile(r"KG\s*(\d{2})")
# Sufixo de cor do kit Kings no Fire (`KG07BR`), pelas duas fontes do template:
# o número em REF COR e o nome em DESCRIÇÃO COR. Só as 3 cores que existem lá.
_COR_KINGS_POR_NUMERO = {1: "BR", 2: "PR", 3: "ST"}
_COR_KINGS_POR_NOME = {"BRANC": "BR", "PRET": "PR", "SORTID": "ST"}

# Quantas células vazias seguidas o `_next_raw` atravessa antes de desistir do
# campo. Medido nos 4 samples do template: o valor nunca está a mais de 2 células
# do rótulo. As listas de validação dos dropdowns ficam a 19+ colunas.
_MAX_CELULAS_VAZIAS = 4

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
    "REF COR": "ref_cor",
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
        #
        # Só vale o que tem forma de número. O FANTASIA da NBA é o nome da loja e
        # entrou no Fire (e na nota) como `NBA STORE MOGI SHOPP` — pedido 4932,
        # 21/09/2026. A DATA saiu da cadeia: dois pedidos no mesmo dia colidem.
        # Sem número, devolve None e o pipeline gera um (`SN-<hash>`).
        if ordem_compra and not any(c in "123456789" for c in ordem_compra):
            ordem_compra = None
        if fantasia and not _CODIGO_DE_LOJA.fullmatch(fantasia.upper()):
            fantasia = None
        order_number = ordem_compra or fantasia

        return OrderHeader(
            order_number=order_number,
            issue_date=issue_date,
            customer_name=customer_name,
            customer_cnpj=customer_cnpj,
        )

    def _next_raw(self, cells: list, label_idx: int):
        """Devolve o valor à direita do label, preservando o tipo (datetime, float,
        str). Stringificar é responsabilidade do chamador.

        Para em três situações, todas devolvendo None (campo vazio):

        - o primeiro não-vazio é OUTRO rótulo (string terminando em ':', como
          'FANTASIA:' logo após um 'RAZÃO SOCIAL:' em branco);
        - a linha acabou;
        - passou de `_MAX_CELULAS_VAZIAS` células vazias seguidas. O template
          guarda as listas de validação dos dropdowns nas colunas remotas — o
          arquivo da Tennis Station tem 39 CNPJs de filiais a partir da coluna X —
          e nada disso termina em ':', então só o guard de rótulo não segura. Um
          CNPJ dali capturado como número de pedido iria pro Fire como
          PEDIDO_CLIENTE, a chave de idempotência.
        """
        vazias = 0
        for k in range(label_idx + 1, len(cells)):
            v = cells[k]
            if v is None or (isinstance(v, str) and not v.strip()):
                vazias += 1
                if vazias > _MAX_CELULAS_VAZIAS:
                    return None
                continue
            if isinstance(v, str) and v.strip().endswith(":"):
                return None
            return v
        return None

    def _coerce_text(self, value) -> str | None:
        """Texto de um campo de identificação, sem o `.0` do float do openpyxl.

        Número de pedido digitado como número puro (`4417`) volta como float, e
        `str()` daria `'4417.0'` — que vira a chave PEDIDO_CLIENTE no Fire e não
        casa com nada. O campo é texto livre, então o comprador também digita
        coisa date-like (`12/08`) que o Excel coage pra data: `str()` cru daria
        `'2026-08-12 00:00:00'`. Formato igual ao do `_coerce_date`.
        """
        if value is None:
            return None
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.strftime("%d/%m/%Y")
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

            ref_cor = self._cell(row, col_map.get("ref_cor"))
            codigo = self._codigo_kings(ref, ref_cor, cor) or self._codigo_variante(
                ref, ref_cor, tamanhos
            )

            items.append(OrderItem(
                product_code=codigo,
                description=description or None,
                quantity=qty,
                unit_price=self._to_number(self._raw(row, col_map.get("custo"))),
                total_price=self._to_number(self._raw(row, col_map.get("total_rs"))),
                obs=self._cell(row, col_map.get("obs")) or None,
            ))

        return items

    def _codigo_variante(self, ref: str, ref_cor: str, tamanhos: str) -> str:
        """Código do produto no Fire (`CODPROD_ALTERN`), que é sempre a VARIANTE.

        O template chega preenchido de dois jeitos:

        - AF / MF / Pulmão / TS: `REF.` já é a variante (`AFK3S-A-100-3338`) e
          `REF COR` é só a cor (`100`). Devolve `REF.`.
        - NBA: `REF.` é o modelo (`NB01`), `REF COR` é modelo + cor (`NB01 - 1`)
          e o tamanho vem com letra (`M - 33-38`). A variante no Fire é
          `NB01-1M`. Gravar o modelo fez as 6 linhas de cada modelo virarem um
          produto só (pedido 4932, 21/09/2026).

        O sinal de "REF. é modelo" é `REF COR` ser exatamente `REF.` + `-` + cor,
        não só começar com ele: `10` / `100` é coincidência, não composição.

        Sem letra de tamanho legível, o código leva o tamanho como veio
        (`NB01-1 33-38`): não existe no Fire e cai na vinculação manual, mas
        cada tamanho continua sendo um código distinto. Devolver só `REF COR`
        faria as linhas de tamanho da mesma cor dividirem um código — e um
        vínculo de-para por código as colapsaria de novo, como no 4932.
        """
        cor = _ESPACO.sub("", ref_cor).upper()
        modelo = _ESPACO.sub("", ref).upper()
        if not cor.startswith(modelo + "-") or cor == modelo + "-":
            return ref
        tam = tamanhos.strip().upper()
        m = _LETRA_TAMANHO.match(tam)
        if m:
            return cor + m.group(1)
        tam = _ESPACO.sub("", tam)
        return f"{cor} {tam}" if tam else cor

    def _codigo_kings(self, ref: str, ref_cor: str, cor: str) -> str | None:
        """Código do kit Kings no Fire, ou None se a linha não é da família KG.

        A Kings recebe o template com `REF.` = modelo (`KG 07`), `REF COR` = número
        da cor (`001`) e `DESCRIÇÃO COR` = nome (`Branco`). No Fire o kit é modelo
        sem espaço + sufixo de cor: `KG07BR`, `KG07PR`, `KG10ST`. O tamanho já
        está no modelo (`KG07` = 34-38, `KG08` = 39-44).

        A cor sai do número E do nome. Se um só estiver legível, ele decide; se os
        dois discordarem, nenhum decide. Sem cor confiável, devolve o modelo com a
        cor como veio, separados por espaço (`KG 07 004`): o importador de Excel do
        Fire casa por PREFIXO, e `KG07` sozinho entraria como `KG07BR` sem aviso.
        Com o espaço, não é prefixo de nenhum KG e cai na vinculação manual.
        """
        m = _MODELO_KINGS.fullmatch(ref.strip().upper())
        if not m:
            return None
        modelo = f"KG{m.group(1)}"

        por_numero = None
        if ref_cor.strip().isdigit():
            por_numero = _COR_KINGS_POR_NUMERO.get(int(ref_cor.strip()))
        nome = cor.strip().upper()
        por_nome = next(
            (suf for raiz, suf in _COR_KINGS_POR_NOME.items() if nome.startswith(raiz)), None
        )

        if por_numero and por_nome and por_numero != por_nome:
            sufixo = None
        else:
            sufixo = por_numero or por_nome
        if sufixo:
            return modelo + sufixo
        return f"{modelo} {ref_cor.strip() or cor.strip() or 'SEM COR'}"

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
