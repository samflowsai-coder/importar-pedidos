"""Degrau 1 do roteamento: o CNPJ do fornecedor impresso no pedido.

Todo pedido de compra identifica o fornecedor, e fornecedor é, por definição,
quem vai faturar. Medido em 29 samples reais (spec, fato 14): 21 trazem o CNPJ
de uma das duas empresas e NENHUM traz os dois.

Essa ausência de ambiguidade é o que torna o degrau seguro, e é uma propriedade
que este módulo **preserva por construção**: dois CNPJs conhecidos no mesmo
documento devolvem `None`. Sem resposta é um estado válido; palpite não é.
"""

from __future__ import annotations

import re

from app.erp.cnpj import cnpj_digits

# Aceita 12.345.678/0001-99, 12345678000199 e as variantes que o pdfplumber
# devolve quando o PDF quebra a máscara com espaços. Os separadores são
# opcionais e individualmente tolerantes a espaço em volta.
#
# `(?<!\d)(?<!\d[.,])` no início, e `(?!\d)` no fim, no lugar de `\b`: `\b`
# marca fronteira entre "é dígito" e "não é dígito", mas um ponto ou vírgula
# grudados num número (planilha com float tipo "14255.400000000007" colado
# num CNPJ real logo depois) TAMBÉM conta como fronteira pro `\b` — a casada
# engole os 2 primeiros dígitos do CNPJ seguinte junto com o float vizinho, e
# o CNPJ verdadeiro desaparece dos achados. Isso já aconteceu com um sample
# real (Desmembramento Magic Feet.xlsx) e é grave: com um dos dois CNPJs
# "engolido", um documento ambíguo (os dois ambientes presentes) deixa de
# parecer ambíguo e `detectar_fornecedor` devolve palpite, não `None`.
#
# O que se quer proibir é começar a casada DENTRO de um número — não colado
# em qualquer pontuação. Um `.`/`,` só faz parte de um número quando vem
# depois de um dígito; ponto final de frase ("Fim da frase.35.394...") ou
# abreviação ("n.35.394...") não é isso, e o `pdf_extractor` tem tratamento
# inteiro pra PDF com espaçamento colapsado (`_chars_are_stacked`) onde
# pontuação de texto gruda no número seguinte — bloquear esse caso criaria
# falso-negativo novo. Por isso dois lookbehinds de largura fixa (`re` do
# Python não aceita largura variável num só) em vez de um só amplo: o
# primeiro rejeita dígito imediatamente antes, o segundo rejeita dígito
# seguido de `.`/`,` — juntos cobrem "estou no meio de um número" sem tocar
# em pontuação de texto.
_CNPJ_RE = re.compile(
    r"(?<!\d)(?<!\d[.,])"
    r"\d{2}\s*[.\s]?\s*\d{3}\s*[.\s]?\s*\d{3}\s*[/\s]?\s*\d{4}\s*[-\s]?\s*\d{2}"
    r"(?!\d)"
)


def cnpjs_no_texto(texto: str) -> set[str]:
    """Todos os CNPJs do texto, em dígitos. Vazio se não houver nenhum."""
    if not texto:
        return set()
    achados = {cnpj_digits(m.group(0)) for m in _CNPJ_RE.finditer(texto)}
    return {c for c in achados if len(c) == 14}


def detectar_fornecedor(texto: str, conhecidos: dict[str, str]) -> str | None:
    """CNPJ do ambiente que aparece no documento, ou `None`.

    `conhecidos` é `{cnpj_digits: env_slug}`. Devolve `None` em dois casos que
    são o mesmo caso: nenhum CNPJ conhecido no texto, ou mais de um. Nos dois,
    o documento não respondeu — quem responde é o degrau seguinte.

    Detecta PRESENÇA, não POSIÇÃO: não distingue o CNPJ que aparece como
    fornecedor do que aparece como cliente, transportadora etc. Isso é
    seguro porque a regra de ambiguidade cobre o caso (dois CNPJs conhecidos
    no mesmo documento, em qualquer papel, devolvem `None`), mas quem
    consome `supplier_cnpj` rio abaixo não deve supor que o valor veio do
    campo "fornecedor" do documento — veio de "único CNPJ conhecido
    presente".
    """
    if not conhecidos:
        return None
    candidatos = cnpjs_no_texto(texto) & set(conhecidos)
    if len(candidatos) != 1:
        return None
    return candidatos.pop()


def cnpjs_de_ambientes() -> dict[str, str]:
    """Mapa `{cnpj_digits: env_slug}` dos ambientes ativos que têm CNPJ.

    Import local: `app.routing.documento` é chamado de dentro do pipeline, que
    roda também no CLI e nos testes de parser — nenhum dos dois deve pagar o
    custo de importar a camada de persistência quando não há ambiente algum.
    """
    from app.persistence import environments_repo

    return {
        cnpj_digits(e["cnpj"]): e["slug"]
        for e in environments_repo.list_active()
        if cnpj_digits(e.get("cnpj"))
    }
