from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"\D")


def cnpj_digits(value: str | None) -> str:
    """Normalizador canônico de CNPJ/CPF: só os dígitos.

    Forma única e inequívoca usada por TODO alimentador do Flow (carga de
    clientes E envio de pedido em runtime) para o casamento por CNPJ bater.
    `None`/vazio → "".
    """
    if not value:
        return ""
    return _NON_DIGIT.sub("", value)
