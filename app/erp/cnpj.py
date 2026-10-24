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


def cnpj_dv_ok(value: str | None) -> bool:
    """CNPJ numérico de 14 dígitos com os dois dígitos verificadores certos (aceita máscara).

    Repetição de um dígito só (`11111111111111`) fecha a conta mas não é CNPJ: recusada.
    """
    d = cnpj_digits(value)
    if len(d) != 14 or len(set(d)) == 1:
        return False

    def dv(base: str) -> str:
        pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2][-len(base) :]
        resto = sum(int(c) * p for c, p in zip(base, pesos, strict=True)) % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = dv(d[:12])
    return d[12] == d1 and d[13] == dv(d[:12] + d1)
