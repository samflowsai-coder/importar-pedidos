# tests/test_consolidador_janela.py
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.consolidators.janela import (
    chave_para,
    instante_fechamento,
    janela_de,
    proxima_janela,
)


def test_janela_semanal_vai_de_segunda_a_domingo():
    ini, fim = janela_de(date(2026, 8, 20), modo="semanal", dia_inicio=0)
    assert ini == date(2026, 8, 17)  # segunda
    assert fim == date(2026, 8, 23)  # domingo


def test_janela_semanal_no_proprio_domingo_nao_vaza_pra_semana_seguinte():
    ini, fim = janela_de(date(2026, 8, 23), modo="semanal", dia_inicio=0)
    assert (ini, fim) == (date(2026, 8, 17), date(2026, 8, 23))


def test_quinzena_primeira_metade_do_mes():
    ini, fim = janela_de(date(2026, 8, 7), modo="quinzenal", dia_inicio=0)
    assert (ini, fim) == (date(2026, 8, 1), date(2026, 8, 15))


def test_quinzena_segunda_metade_termina_no_ultimo_dia():
    ini, fim = janela_de(date(2026, 8, 20), modo="quinzenal", dia_inicio=0)
    assert (ini, fim) == (date(2026, 8, 16), date(2026, 8, 31))


def test_quinzena_de_fevereiro_respeita_o_mes_curto():
    ini, fim = janela_de(date(2026, 2, 20), modo="quinzenal", dia_inicio=0)
    assert fim == date(2026, 2, 28)


def test_chave_semanal_usa_semana_iso():
    assert chave_para("nasmar", date(2026, 8, 17), "semanal") == "NAS-2026-S34"


def test_chave_quinzenal():
    assert chave_para("nasmar", date(2026, 8, 16), "quinzenal") == "NAS-2026-Q16"


def test_chave_cabe_no_pedido_cliente():
    """PEDIDO_CLIENTE trunca em 20 (mapper.py:64). Truncar chave de lote
    silenciosamente colidiria dois lotes."""
    c = chave_para("nasmar", date(2026, 8, 17), "semanal")
    assert len(c) <= 20


def test_modo_invalido_levanta():
    with pytest.raises(ValueError):
        janela_de(date(2026, 8, 20), modo="mensal", dia_inicio=0)


def test_proxima_janela_semanal():
    assert proxima_janela(date(2026, 8, 23), modo="semanal") == (
        date(2026, 8, 24),
        date(2026, 8, 30),
    )


def test_proxima_janela_quinzenal_vira_o_mes():
    assert proxima_janela(date(2026, 8, 31), modo="quinzenal") == (
        date(2026, 9, 1),
        date(2026, 9, 15),
    )


def test_fecha_na_segunda_8h_depois_do_domingo():
    assert instante_fechamento(date(2026, 8, 23), hora=8) == datetime(2026, 8, 24, 8, 0)


def test_chave_na_virada_de_ano_usa_ano_iso():
    # 2026-12-28 é segunda da semana ISO 53 de 2026; 2027-01-04 é S01 de 2027
    assert chave_para("nasmar", date(2026, 12, 28), "semanal") == "NAS-2026-S53"
    assert chave_para("nasmar", date(2027, 1, 4), "semanal") == "NAS-2027-S01"
