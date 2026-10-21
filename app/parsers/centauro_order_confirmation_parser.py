from __future__ import annotations

import re

from app.models.order import Order, OrderHeader, OrderItem
from app.parsers import celulas
from app.parsers.base_parser import BaseParser, PedidoRecusadoError

# A planilha não traz o cliente — o comprador é sempre a Centauro. Mesmo CNPJ de
# FATURAMENTO que o `SbfCentauroParser` (PDF) lê de "Dados para Entrega": é o
# único cadastro da raiz 06347409 no Fire (CADASTRO.CODIGO=498), a matriz
# /0001-65 não existe lá.
_CLIENTE_NOME = "SBF COMERCIO PRODUTOS ESPORTIVOS"
_CLIENTE_CNPJ = "06.347.409/0296-51"

# Rótulos (em maiúsculas) que identificam a tabela de itens. Os da assinatura
# bastam para o `can_parse`; todos são exigidos para achar o cabeçalho.
_ASSINATURA = ("OC CENTAURO", "SUGESTÃO DE AGENDA", "CUSTO (VALOR NF)")
_COLUNAS = {
    "cnpj": "CNPJ",
    "sku": "SKU",
    "descricao": "DESCRIÇÃO",
    "ean": "EAN",
    "preco": "CUSTO (VALOR NF)",
    "qtd": "TOTAL QTD",
    "total": "TOTAL $",
    "agenda": "SUGESTÃO DE AGENDA",
    "oc": "OC CENTAURO",
}
_OBRIGATORIAS = ("sku", "descricao", "preco", "qtd", "total", "oc")


class CentauroOrderConfirmationParser(BaseParser):
    """Parser da planilha ORDER CONFIRMATION da Centauro (1 linha por SKU)."""

    def can_parse(self, extracted: dict) -> bool:
        texto = extracted.get("text", "").upper()
        return all(rotulo in texto for rotulo in _ASSINATURA)

    def parse(self, extracted: dict) -> Order | None:
        if not self.can_parse(extracted):
            return None
        rows = extracted.get("rows") or []
        achado = self._achar_cabecalho(rows, extracted.get("sheets") or [])
        if achado is None:
            # A assinatura bateu (os rótulos aparecem no texto, inclusive na aba MANUAL)
            # mas a tabela de itens não: um rótulo mudou. Devolver None deixaria o
            # GenericParser gravar o lixo que este parser existe para evitar.
            raise PedidoRecusadoError(
                "Planilha da Centauro reconhecida, mas o cabeçalho da tabela de itens mudou "
                "(esperado: SKU, DESCRIÇÃO, CUSTO (VALOR NF), TOTAL QTD, TOTAL $, OC CENTAURO). "
                "Avise o suporte com o arquivo."
            )
        idx, fim, col = achado

        linhas = []
        for n, row in enumerate(rows[idx + 1 : fim], start=idx + 2):
            if self._linha_do_pedido(row, col, n):
                linhas.append(row)
        items = [self._item(r, col) for r in linhas]
        if not items:
            raise PedidoRecusadoError("Planilha da Centauro sem nenhum item com SKU e quantidade.")

        cnpjs = {re.sub(r"\D", "", celulas.texto(self._cel(r, col, "cnpj")) or "") for r in linhas}
        cnpjs.discard("")
        return Order(
            header=OrderHeader(
                order_number=self._numero_do_pedido(linhas, col),
                customer_name=_CLIENTE_NOME,
                customer_cnpj=_CLIENTE_CNPJ,
                supplier_cnpj=next(iter(cnpjs))
                if len(cnpjs) == 1 and len(next(iter(cnpjs))) == 14
                else None,
            ),
            items=items,
        )

    # ------------------------------------------------------------------
    # Cabeçalho da tabela
    # ------------------------------------------------------------------

    def _achar_cabecalho(self, rows: list, sheets: list) -> tuple[int, int, dict] | None:
        """(índice da linha de rótulos, fim da aba, {campo: coluna}).

        O extractor concatena TODAS as abas em `rows`; a aba MANUAL repete os
        rótulos e `BASE OPÇÕES` tem centenas de linhas. Os itens só valem até o
        fim da aba onde o cabeçalho foi achado.
        """
        limites, acc = [], 0
        for s in sheets:
            limites.append((acc, acc + s["rows"]))
            acc += s["rows"]
        for i, row in enumerate(rows):
            rotulos = [" ".join(str(c).split()).upper() if c is not None else "" for c in row]
            col = {}
            for campo, rotulo in _COLUNAS.items():
                if rotulo in rotulos:
                    col[campo] = rotulos.index(rotulo)
            if all(c in col for c in _OBRIGATORIAS):
                fim = next((b for a, b in limites if a <= i < b), len(rows))
                return i, fim, col
        return None

    # ------------------------------------------------------------------
    # Itens
    # ------------------------------------------------------------------

    def _cel(self, row: list, col: dict, campo: str):
        i = col.get(campo)
        return row[i] if i is not None and i < len(row) else None

    def _linha_do_pedido(self, row: list, col: dict, n: int) -> bool:
        """True se a linha é um item; False se é vazia ou de quantidade zero.

        Linha pela metade RECUSA em vez de sumir: item perdido sem aviso é pedido
        que vai ao Fire faltando mercadoria.
        """
        sku = celulas.texto(self._cel(row, col, "sku"))
        bruto = self._cel(row, col, "qtd")
        qtd = celulas.numero(bruto)
        if not sku and bruto is None:
            return False  # linha vazia (só OC, comprador etc.)
        if qtd is None:
            raise PedidoRecusadoError(
                f"Linha {n} da planilha Centauro (SKU {sku or 'vazio'}): quantidade "
                f"{'vazia' if bruto is None else repr(bruto) + ' ilegível'}."
            )
        if qtd == 0:
            return False  # zero explícito: o comprador zerou o item
        if qtd < 0 or not sku:
            raise PedidoRecusadoError(
                f"Linha {n} da planilha Centauro: quantidade {qtd:g} "
                f"{'negativa' if qtd < 0 else 'sem SKU'}."
            )
        for campo, rotulo in (("preco", "CUSTO (VALOR NF)"), ("total", "TOTAL $")):
            valor = self._cel(row, col, campo)
            if valor is not None and celulas.numero(valor) is None:
                raise PedidoRecusadoError(
                    f"Linha {n} da planilha Centauro (SKU {sku}): {rotulo} {valor!r} ilegível."
                )
        return True

    def _item(self, row: list, col: dict) -> OrderItem:
        ean = celulas.texto(self._cel(row, col, "ean"))
        return OrderItem(
            description=celulas.texto(self._cel(row, col, "descricao")),
            product_code=celulas.texto(self._cel(row, col, "sku")),
            ean=ean if ean and ean.isdigit() else None,
            quantity=celulas.numero(self._cel(row, col, "qtd")),
            unit_price=celulas.numero(self._cel(row, col, "preco")),
            total_price=celulas.numero(self._cel(row, col, "total")),
            delivery_date=celulas.data(self._cel(row, col, "agenda")),
            delivery_cnpj=_CLIENTE_CNPJ,
        )

    def _numero_do_pedido(self, linhas: list, col: dict) -> str | None:
        """A OC CENTAURO do arquivo; None se a Centauro ainda não preencheu.

        O `Order` tem um número só: duas OCs no mesmo arquivo seriam misturadas
        num pedido. Recusa (PedidoRecusadoError) em vez de devolver None — com None o GenericParser
        pegaria o arquivo e gravaria lixo (foi o que aconteceu antes deste parser).
        """
        todas = [celulas.texto(self._cel(r, col, "oc")) for r in linhas]
        ocs = set(todas) - {None}
        if len(ocs) > 1:
            raise PedidoRecusadoError(
                f"Planilha Centauro com mais de uma OC ({', '.join(sorted(ocs))}): "
                "importe uma OC por arquivo."
            )
        if ocs and None in todas:
            raise PedidoRecusadoError(
                f"Planilha Centauro com linha sem OC (as demais têm {next(iter(ocs))}): "
                "preencha a coluna OC CENTAURO."
            )
        return next(iter(ocs), None)
