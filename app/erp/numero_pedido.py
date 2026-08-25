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

# Sufixo de loja/filial: hífen seguido de exatamente 4 dígitos no fim.
# `AF-198` não casa (3 dígitos) — cortar ali viraria match errado.
_SUFIXO_LOJA = re.compile(r"-\d{4}$")


def variantes(numero: str | None) -> list[str]:
    """Formas de comparação do número, sem duplicatas, mais específica primeiro."""
    base = (numero or "").strip()
    if not base:
        return []

    saida = [base]

    sem_sufixo = _SUFIXO_LOJA.sub("", base)
    if sem_sufixo and sem_sufixo not in saida:
        saida.append(sem_sufixo)

    for candidato in list(saida):
        sem_zeros = candidato.lstrip("0")
        if sem_zeros and sem_zeros not in saida:
            saida.append(sem_zeros)

    return saida
