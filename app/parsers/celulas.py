"""Leitura de célula de planilha, sem adivinhação.

Funções de módulo (não método de parser) para o parser novo não virar mais uma
cópia de `_to_number` (Nasmar) / `_parse_number` (Daju), que já são idênticas.
A migração desses dois para cá está no BACKLOG — não foi feita junto porque
refactor não anda com feature.
"""

from __future__ import annotations

import datetime as _dt
import re

_MILHAR_BR = re.compile(r"\d{1,3}(?:\.\d{3})+")
_EXCEL_EPOCH = _dt.date(1899, 12, 30)
# Serial de data do Excel plausível para pedido: 2000-01-01 a 2099-12-31.
_SERIAL_MIN, _SERIAL_MAX = 36526, 73415


def numero(value) -> float | None:
    """Número da célula. Nativo e texto precisam dar o MESMO resultado.

    Um erro aqui entra no ERP como pedido de quantidade errada, passa no
    validador (qty > 0) e ninguém percebe.

    - `int`/`float` do openpyxl: usa direto, o tipo já resolveu.
    - texto com vírgula: vírgula é decimal, ponto é milhar (`1.234,56`).
    - texto só com ponto: **3 dígitos depois do último ponto = milhar**
      (`1.300` -> 1300), caso contrário é decimal (`300.0` -> 300.0).
    - texto com vírgula ANTES do ponto (`1,234.56`): None. Ninguém sabe se é US ou
      erro de digitação, e adivinhar erra por 1000.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    cleaned = re.sub(r"[R$\s]", "", str(value))
    if not cleaned or cleaned in ("—", "-"):
        return None
    try:
        if "," in cleaned:
            if cleaned.rfind(".") > cleaned.rfind(","):
                return None  # `1,234.56` (formato US): ambíguo, recusa em vez de errar por 1000
            return float(cleaned.replace(".", "").replace(",", "."))
        if _MILHAR_BR.fullmatch(cleaned):
            return float(cleaned.replace(".", ""))
        return float(cleaned)
    except ValueError:
        return None


def texto(value) -> str | None:
    """Texto de um campo de identificação, sem o `.0` do float do openpyxl.

    Código digitado como número puro volta como float e `str()` daria
    `'7909607842231.0'`. Data vira `dd/mm/aaaa` (o Excel coage texto date-like).
    """
    if value is None:
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip() or None


def data(value) -> str | None:
    """Data da célula como `dd/mm/aaaa`; aceita data nativa, serial Excel e texto."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, (int, float)) and _SERIAL_MIN <= value <= _SERIAL_MAX:
        return (_EXCEL_EPOCH + _dt.timedelta(days=int(value))).strftime("%d/%m/%Y")
    s = str(value).strip()
    return s or None
