"""Fuso do envio ao Flow e aviso da variável obsoleta `PORTAL_FLOW_JANELAS`.

Desde 27/10/2026 o envio ao Flow não tem janelas: sai quando há dado. Sobrou o
fuso (Brasília, vindo do UTC, não do relógio do Windows) e o aviso para quem
ainda tem a variável no `.env`.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from app.utils.logger import logger

FUSO = ZoneInfo("America/Sao_Paulo")

_avisou = False


def avisar_variavel_obsoleta() -> None:
    """Um warning por processo se `PORTAL_FLOW_JANELAS` ainda estiver no ambiente."""
    global _avisou
    if _avisou or not os.environ.get("PORTAL_FLOW_JANELAS", "").strip():
        return
    _avisou = True
    logger.warning("PORTAL_FLOW_JANELAS não é mais usada: o envio ao Flow sai quando há dado")
