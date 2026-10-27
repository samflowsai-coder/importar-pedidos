"""Janelas de envio ao Flow (decisão do Samuel, 14/10/2026).

Nada sai para o Flow em tempo real: a fila do `/recebimento`
(`app/integrations/flowpcp/envio_portal.py`) e os lotes do SP1
(`app/erp_eventos/runner.py`) só saem em janelas de 1 h às 8h, 11h, 14h e 17h,
horário de Brasília, de segunda a sábado. Fora delas tudo fica enfileirado. A
foto noturna do SP1 não segue as janelas (sai no `foto_hora` da base).

`PORTAL_FLOW_JANELAS="8,11,14,17"` troca as horas; valor inválido cai no padrão
(com UM log de erro por valor). O fuso vem do UTC, não do relógio do Windows.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils.logger import logger

FUSO = ZoneInfo("America/Sao_Paulo")
JANELAS = (8, 11, 14, 17)  # hora cheia de início; cada janela dura 1 h
DIAS_UTEIS = frozenset(range(6))  # segunda (0) a sábado (5)

_invalida_logada: str | None = None


def horas_das_janelas() -> tuple[int, ...]:
    """Horas cheias das janelas em vigor (já considera PORTAL_FLOW_JANELAS)."""
    return _horas_das_janelas()


def _horas_das_janelas() -> tuple[int, ...]:
    global _invalida_logada
    bruto = os.environ.get("PORTAL_FLOW_JANELAS", "").strip()
    if not bruto:
        return JANELAS
    try:
        horas = tuple(sorted({int(h) for h in bruto.split(",") if h.strip()}))
    except ValueError:
        horas = ()
    if not horas or any(h < 0 or h > 23 for h in horas):
        if _invalida_logada != bruto:  # uma vez por valor, não a cada tick
            _invalida_logada = bruto
            logger.error(f"PORTAL_FLOW_JANELAS inválido ({bruto!r}), usando {JANELAS}")
        return JANELAS
    return horas


def janela_aberta(agora: datetime | None = None) -> str | None:
    """Rótulo da janela de envio aberta agora (ex.: "2026-10-15 08h") ou None."""
    local = (agora or datetime.now(FUSO)).astimezone(FUSO)
    if local.weekday() not in DIAS_UTEIS or local.hour not in _horas_das_janelas():
        return None
    return f"{local:%Y-%m-%d} {local.hour:02d}h"


def inicio_da_janela(agora: datetime) -> datetime:
    """Início (hora cheia, com fuso) da janela que contém `agora`."""
    return agora.astimezone(FUSO).replace(minute=0, second=0, microsecond=0)


def proxima_janela(agora: datetime) -> datetime:
    """Início (aware, UTC) da próxima janela que começa DEPOIS de `agora`, pulando domingo."""
    local = agora.astimezone(FUSO)
    horas = _horas_das_janelas()
    for dia in range(9):  # sobra para qualquer fim de semana
        data = (local + timedelta(days=dia)).date()
        if data.weekday() not in DIAS_UTEIS:
            continue
        for h in horas:
            inicio = datetime(data.year, data.month, data.day, h, tzinfo=FUSO)
            if inicio > local:
                return inicio.astimezone(UTC)
    raise RuntimeError("sem janela de envio configurada")  # inalcançável: horas nunca vazio


def janelas_recentes(
    agora: datetime | None = None, n: int = 8
) -> list[tuple[str, datetime, datetime]]:
    """As `n` janelas mais recentes já iniciadas (a aberta incluída), da mais nova
    para a mais velha: (rótulo, início, fim) em hora local ingênua, o formato de
    `outbox.sent_at` e `audit_log.created_at`."""
    local = (
        (agora or datetime.now(FUSO)).astimezone(FUSO).replace(minute=0, second=0, microsecond=0)
    )
    horas = _horas_das_janelas()
    saida: list[tuple[str, datetime, datetime]] = []
    for k in range(24 * 14):
        inicio = local - timedelta(hours=k)
        if inicio.weekday() in DIAS_UTEIS and inicio.hour in horas:
            ingenuo = inicio.replace(tzinfo=None)
            saida.append(
                (f"{inicio:%Y-%m-%d} {inicio.hour:02d}h", ingenuo, ingenuo + timedelta(hours=1))
            )
            if len(saida) == n:
                break
    return saida
