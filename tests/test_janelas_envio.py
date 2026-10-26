"""proxima_janela: início da próxima janela de envio ao Flow."""

from __future__ import annotations

from datetime import UTC, datetime

from app.utils.janelas_envio import FUSO, proxima_janela


def _sp(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=FUSO)


def test_segunda_18h_vai_para_terca_8h():  # 2026-10-26 é segunda
    r = proxima_janela(_sp(2026, 10, 26, 18))
    assert r == _sp(2026, 10, 27, 8)
    assert r.utcoffset().total_seconds() == 0  # aware, UTC


def test_segunda_9h30_vai_para_11h():
    assert proxima_janela(_sp(2026, 10, 26, 9, 30)) == _sp(2026, 10, 26, 11)


def test_sabado_18h_pula_domingo():
    assert proxima_janela(_sp(2026, 10, 31, 18)) == _sp(2026, 11, 2, 8)


def test_domingo_10h_vai_para_segunda_8h():
    assert proxima_janela(_sp(2026, 11, 1, 10)) == _sp(2026, 11, 2, 8)


def test_janela_comeca_depois_de_agora_nao_igual():
    assert proxima_janela(_sp(2026, 10, 26, 11)) == _sp(2026, 10, 26, 14)


def test_aceita_instante_em_utc():
    # 21:00 UTC de segunda = 18:00 em São Paulo
    assert proxima_janela(datetime(2026, 10, 26, 21, 0, tzinfo=UTC)) == _sp(2026, 10, 27, 8)


def test_horas_vem_da_variavel_de_ambiente(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOW_JANELAS", "9,15")
    assert proxima_janela(_sp(2026, 10, 26, 10)) == _sp(2026, 10, 26, 15)
