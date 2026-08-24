"""Regressão: config de Firebird não pode vazar de um teste para o próximo.

Por que este arquivo existe. `firebird_config.apply_to_env()` grava direto em
`os.environ` — é o contrato dele em produção. Em teste isso escapa do
`monkeypatch`, e um teste chegou a deixar `FB_HOST=10.0.0.1` para trás. Dali em
diante, todo teste que tocasse o Firebird tentava abrir TCP contra aquele host:
no macOS falha na hora, no Linux do CI espera os SYN retries (~127s por
chamada). Resultado: 24 minutos de CI contra 70 segundos locais, **sem nenhum
teste falhar**. Custo invisível é o mais caro.

Os dois testes abaixo dependem da ordem: o primeiro suja, o segundo confere que
a sujeira não atravessou. pytest executa na ordem do arquivo, então o par é
determinístico. Se alguém remover a fixture `_isolate_firebird_env` do
`conftest.py`, o segundo teste falha — que é exatamente o ponto.
"""

from __future__ import annotations

import os

import pytest

from tests.conftest import _FB_ENV_KEYS

_SENTINELA = "10.0.0.1"


def test_um_teste_pode_sujar_o_ambiente():
    """Simula o que `apply_to_env()` faz: escreve direto, fora do monkeypatch."""
    os.environ["FB_HOST"] = _SENTINELA
    os.environ["FB_DATABASE"] = "/data/nao_existe.fdb"
    assert os.environ["FB_HOST"] == _SENTINELA


def test_o_teste_seguinte_recebe_o_ambiente_limpo():
    """Se este falhar, a suíte inteira volta a pendurar no CI."""
    assert "FB_HOST" not in os.environ, (
        "FB_HOST vazou do teste anterior — a fixture _isolate_firebird_env "
        "do conftest.py sumiu ou parou de restaurar"
    )
    assert "FB_DATABASE" not in os.environ


@pytest.mark.parametrize("chave", _FB_ENV_KEYS)
def test_toda_chave_fb_e_restaurada(chave: str, monkeypatch):
    """A guarda cobre a lista inteira, não só as duas do caso real.

    Usa monkeypatch para o valor PRÉ-existente e escreve por cima direto, que é
    o caminho que escapava. A fixture tem que devolver o valor original.
    """
    monkeypatch.setenv(chave, "valor-original")
    os.environ[chave] = "valor-vazado"
    # O assert real acontece no teardown da fixture autouse; aqui garantimos
    # que a chave está na lista que ela observa.
    assert chave in _FB_ENV_KEYS


def test_a_lista_cobre_o_que_connection_py_realmente_le():
    """A guarda precisa acompanhar o código, não uma lista escrita à mão uma vez.

    Se `connection.py` passar a ler uma `FB_*` nova e ninguém adicionar aqui,
    o vazamento volta silencioso — pelo caminho novo.
    """
    fonte = (
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "erp", "connection.py")
    )
    with open(fonte, encoding="utf-8") as f:
        texto = f.read()

    import re

    lidas = set(re.findall(r'["\'](FB_[A-Z_]+)["\']', texto))
    faltando = lidas - set(_FB_ENV_KEYS)
    assert not faltando, (
        f"connection.py lê {sorted(faltando)}, que não está em _FB_ENV_KEYS "
        f"do conftest.py — essa chave pode vazar entre testes"
    )
