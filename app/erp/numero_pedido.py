"""Variantes de comparação do número do pedido.

O `PEDIDO_CLIENTE` do Fire é digitado à mão e não bate byte a byte com o que o
parser extrai. Medido em dado real: Sam's Club guarda `06654993-0000` no portal
e `06654993` no Fire; Centauro guarda `29852483` nos dois lados. Comparar só o
exato produz falso negativo silencioso — o pedido está lá e o portal diz que
não.

As variantes NUNCA substituem a segunda perna da chave (identidade do cliente).
Elas só ampliam o que conta como "mesmo número".
"""

from __future__ import annotations

import re

# Sufixo de loja/entrega: hífen (com ou sem espaços em volta) seguido de 1 a 4
# dígitos no fim. Eram só 4 dígitos até 2026-08-24, quando a Fire viva da MM
# mostrou o formato curto: o portal manda `AF049-6`, `AW033-6`, `AF090 - 3`, e
# o Fire guarda `AF049`, `AW033`, `AF090`. Eram 43 dos 137 pedidos pendentes;
# 19 de 19 amostrados existiam no Fire sob o MESMO CNPJ.
_SUFIXO_LOJA = re.compile(r"\s*-\s*\d{1,4}$")


def _resto_parece_numero_de_pedido(texto: str) -> bool:
    """Trava do corte: o que sobra ainda tem que identificar UM pedido.

    `AF-198` cortado vira `AF` — que casaria com qualquer pedido do mesmo
    cliente cujo número fosse `AF`. A spec já apontava esse contraexemplo, e a
    regra antiga só escapava dele por acidente (198 tem 3 dígitos, não 4):
    `AF-1985` ela cortava, com o mesmo problema. Exigir 3+ caracteres E ao
    menos um dígito fecha os dois casos e deixa passar todo número real
    observado (`AF049`, `06654993`, `AW033`).
    """
    return len(texto) >= 3 and any(c.isdigit() for c in texto)


def variantes(numero: str | None) -> list[str]:
    """Formas de comparação do número, sem duplicatas, mais específica primeiro."""
    base = (numero or "").strip()
    if not base:
        return []

    saida = [base]

    sem_sufixo = _SUFIXO_LOJA.sub("", base).strip()
    if sem_sufixo and sem_sufixo not in saida and _resto_parece_numero_de_pedido(sem_sufixo):
        saida.append(sem_sufixo)

    for candidato in list(saida):
        sem_zeros = candidato.lstrip("0")
        if sem_zeros and sem_zeros not in saida:
            saida.append(sem_zeros)

    return saida
