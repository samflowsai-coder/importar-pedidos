"""Janela do lote intercompany e a chave que a identifica.

Puro: so datas e strings. Sem I/O.

A chave entra em CAB_VENDAS.PEDIDO_CLIENTE, que o mapper trunca em 20
caracteres (mapper.py:64). `NAS-2026-S34` tem 12 -- nunca trunca. Isso
importa porque a idempotencia do exporter e PEDIDO_CLIENTE + CLIENTE
(queries.py:91): chave truncada colide dois lotes e o segundo e recusado
como duplicata.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta

MODOS = ("semanal", "quinzenal")


def _modo_invalido(modo: str) -> ValueError:
    return ValueError(f"modo de janela invalido: {modo!r} (use {MODOS})")


def janela_de(d: date, *, modo: str, dia_inicio: int = 0) -> tuple[date, date]:
    """Inicio e fim (inclusivos) da janela que contem `d`.

    `dia_inicio` vale so para o modo semanal (0 = segunda).
    """
    if modo == "semanal":
        inicio = d - timedelta(days=(d.weekday() - dia_inicio) % 7)
        return inicio, inicio + timedelta(days=6)
    if modo == "quinzenal":
        if d.day <= 15:
            return date(d.year, d.month, 1), date(d.year, d.month, 15)
        ultimo = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, 16), date(d.year, d.month, ultimo)
    raise _modo_invalido(modo)


def proxima_janela(fim: date, *, modo: str, dia_inicio: int = 0) -> tuple[date, date]:
    """A janela que comeca em `fim + 1 dia`."""
    return janela_de(fim + timedelta(days=1), modo=modo, dia_inicio=dia_inicio)


def chave_para(env_origem_slug: str, inicio: date, modo: str) -> str:
    """Chave do lote: <PREFIXO>-<ano>-S<semana ISO> ou -Q<quinzena>.

    O prefixo sao as 3 primeiras letras do slug em maiusculo, para que um
    segundo par origem->espelho no futuro nao colida. Semanal usa o ano ISO
    (a semana 53 de dezembro e a semana 01 de janeiro nao colidem).
    """
    prefixo = (env_origem_slug or "x")[:3].upper()
    if modo == "semanal":
        ano, semana, _ = inicio.isocalendar()
        return f"{prefixo}-{ano}-S{semana:02d}"
    if modo == "quinzenal":
        n = (inicio.month - 1) * 2 + (1 if inicio.day <= 15 else 2)
        return f"{prefixo}-{inicio.year}-Q{n:02d}"
    raise _modo_invalido(modo)


def instante_fechamento(fim: date, *, hora: int) -> datetime:
    """Momento em que a janela que termina em `fim` fecha.

    Dia seguinte ao fim, as `hora` h. Naive: hora local do servidor.
    """
    return datetime.combine(fim + timedelta(days=1), time(hora))
