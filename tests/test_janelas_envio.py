"""janelas_envio: só o FUSO e o aviso da variável obsoleta PORTAL_FLOW_JANELAS."""

from __future__ import annotations

from loguru import logger

from app.utils import janelas_envio


def _capturar():
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    return msgs, hid


def test_aviso_sai_uma_vez_por_processo(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOW_JANELAS", "8,11")
    monkeypatch.setattr(janelas_envio, "_avisou", False, raising=False)
    msgs, hid = _capturar()
    try:
        janelas_envio.avisar_variavel_obsoleta()
        janelas_envio.avisar_variavel_obsoleta()
    finally:
        logger.remove(hid)
    avisos = [m for m in msgs if "PORTAL_FLOW_JANELAS" in m]
    assert len(avisos) == 1


def test_sem_a_variavel_nao_avisa(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOW_JANELAS", raising=False)
    monkeypatch.setattr(janelas_envio, "_avisou", False, raising=False)
    msgs, hid = _capturar()
    try:
        janelas_envio.avisar_variavel_obsoleta()
    finally:
        logger.remove(hid)
    assert not msgs
