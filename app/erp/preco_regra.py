"""Regra de resolução do contrato precos v1 (§5), idêntica no Portal e no Flow.

Pura: sem banco, sem relógio. Os dois lados rodam os mesmos vetores
(`tests/fixtures/flow_precos_v1/vetores.json`, cópia do pcp-app).

A regra NÃO normaliza CNPJ: quem chama passa por `normalizar_cnpj` antes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")

# Contrato §2.3: 12 de [0-9A-Z] + 2 dígitos ASCII. Sem IGNORECASE, sem \d.
_CNPJ = re.compile(r"[0-9A-Z]{12}[0-9]{2}")
_NAO_ALFANUM = re.compile(r"[^0-9A-Za-z]")
# Instante do §2.3: ISO 8601 com fuso explícito (`Z` ou `±hh:mm`).
_INSTANTE_COM_FUSO = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


class PrecoInvalido(ValueError):  # noqa: N818 — nome fixado na interface da Task 7
    """Dado de preço fora do contrato (ex.: `registradoEm` sem fuso explícito)."""


@dataclass(frozen=True)
class PrecoLinha:
    preco_id: str
    rede_id: str | None
    empresa_cnpj: str
    erp_produto_id: str
    unidade_erp: str | None
    preco_centavos: int
    vigencia_inicio: date
    registrado_em: str
    cancelado: bool


def cnpj_valido(cnpj: str | None) -> bool:
    """Formato do contrato, sem normalizar (máscara ou minúscula → False)."""
    return isinstance(cnpj, str) and _CNPJ.fullmatch(cnpj) is not None


def normalizar_cnpj(bruto: str | None) -> str | None:
    """Tira tudo que não é [0-9A-Za-z], passa para maiúsculas; só devolve se
    o resultado estiver no formato do contrato."""
    if not bruto:
        return None
    candidato = _NAO_ALFANUM.sub("", bruto).upper()
    return candidato if cnpj_valido(candidato) else None


def centavos(valor: float | None) -> int | None:
    """Reais → centavos inteiros, meia para cima. `None` ou `<= 0` → None
    (preço zero no pedido = pedido sem preço, contrato §2.3)."""
    if valor is None or not math.isfinite(valor):
        return None
    reais = Decimal(str(valor)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    if reais <= 0:
        return None
    return int(reais * 100)


def data_referencia(emitido_em: datetime | None, recebido_em: datetime) -> date:
    """Data civil em São Paulo da emissão; sem emissão, a do recebimento.
    Instante sem fuso é recusado (a data civil dependeria do servidor)."""
    instante = emitido_em if emitido_em is not None else recebido_em
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("data_referencia exige datetime com fuso")
    return instante.astimezone(TZ).date()


def _registrado(p: PrecoLinha) -> datetime:
    if not isinstance(p.registrado_em, str) or not _INSTANTE_COM_FUSO.fullmatch(p.registrado_em):
        raise PrecoInvalido(f"registradoEm inválido no preço {p.preco_id}")
    try:
        dt = datetime.fromisoformat(p.registrado_em)
    except ValueError as exc:
        raise PrecoInvalido(f"registradoEm inválido no preço {p.preco_id}") from exc
    # §5: compara ao milissegundo (o Flow emite 3 casas).
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def resolver_preco(
    candidatos: list[PrecoLinha],
    *,
    empresa_cnpj: str,
    rede_id: str | None,
    erp_produto_id: str,
    data: date,
) -> PrecoLinha | None:
    """Passos 1-5 do §5. Filtro de empresa antes de rede-vence-base; sem
    herança entre empresas. `registradoEm` inválido em qualquer candidato do
    passo 1 → `PrecoInvalido` (nunca escolhe por ordem de chegada)."""
    passo1 = [
        (p, _registrado(p))
        for p in candidatos
        if not p.cancelado
        and p.empresa_cnpj == empresa_cnpj
        and p.erp_produto_id == erp_produto_id
        and p.vigencia_inicio <= data
    ]
    da_rede = [c for c in passo1 if c[0].rede_id == rede_id]
    validos = da_rede if da_rede else [c for c in passo1 if c[0].rede_id is None]
    if not validos:
        return None
    return max(validos, key=lambda c: (c[0].vigencia_inicio, c[1], c[0].preco_id))[0]
