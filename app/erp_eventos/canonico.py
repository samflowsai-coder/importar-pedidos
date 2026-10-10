"""Serialização canônica do contrato `pedido.evento_erp.v1` (spec SP1 §4.1).

O `id` de cada evento é a chave de idempotência do Flow, e o Flow NÃO o
recalcula: se o Portal gerar dois textos diferentes para a mesma mudança, o
Flow grava a mudança duas vezes; se gerar o mesmo texto para mudanças
diferentes, perde uma. Toda a estabilidade mora aqui.

Regras (documentadas em `docs/ai/modules/erp_eventos.md`, seção "id"):
- campos, nesta ordem: tenant, base, tipo, erpCodigo, erpItemCodigo (0 se não
  houver item), campo, antes, depois, observadoEm;
- separador fixo `\\x1f` (unit separator); valor que o contenha é recusado;
- número: Decimal normalizado, sem expoente e sem `.0` espúrio (`1200`,
  `3.15`, `0`); float entra por `str()`, nunca pelo binário;
- `None` vira string vazia; data civil `YYYY-MM-DD`;
- instante: UTC, milissegundos, `+00:00` (`formatar_instante`).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal

SEPARADOR = "\x1f"


def decimal_canonico(valor: Decimal | int | float) -> str:
    """`Decimal('1200.0000')` → `'1200'`; `Decimal('3.1500')` → `'3.15'`; zero (inclusive `-0`) → `'0'`."""
    if isinstance(valor, bool):
        raise TypeError("bool não é número do contrato")
    d = valor if isinstance(valor, Decimal) else Decimal(str(valor))
    if not d.is_finite():
        raise ValueError(f"número não finito: {valor!r}")
    if d == 0:
        return "0"
    return format(d.normalize(), "f")


def formatar_instante(dt: datetime) -> str:
    """Instante com fuso, sempre no mesmo formato: `2026-10-09T13:00:00.000+00:00`."""
    if dt.tzinfo is None:
        raise ValueError("instante sem fuso")
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def texto_canonico(valor: object) -> str:
    if valor is None:
        return ""
    if isinstance(valor, bool):
        raise TypeError("bool não é valor do contrato")
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, Decimal | float):
        return decimal_canonico(valor)
    if isinstance(valor, datetime):
        raise TypeError("instante entra já formatado por formatar_instante")
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, str):
        if SEPARADOR in valor:
            raise ValueError("valor contém o separador do id")
        return valor
    raise TypeError(f"tipo fora do contrato: {type(valor).__name__}")


def id_evento(
    *,
    tenant: str,
    base: str,
    tipo: str,
    erp_codigo: int,
    erp_item_codigo: int | None,
    campo: str | None,
    antes: object,
    depois: object,
    observado_em: str,
) -> str:
    """SHA-256 hex (64) do texto canônico. `observado_em` já formatado."""
    partes = [
        texto_canonico(tenant),
        texto_canonico(base),
        texto_canonico(tipo),
        texto_canonico(erp_codigo),
        texto_canonico(erp_item_codigo or 0),
        texto_canonico(campo),
        texto_canonico(antes),
        texto_canonico(depois),
        texto_canonico(observado_em),
    ]
    return hashlib.sha256(SEPARADOR.join(partes).encode("utf-8")).hexdigest()


def numero_json(valor: Decimal | None) -> int | float | None:
    """Número do payload JSON: inteiro quando é inteiro, senão float pelo texto canônico."""
    if valor is None:
        return None
    texto = decimal_canonico(valor)
    return int(texto) if "." not in texto else float(texto)
