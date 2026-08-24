"""Regressão: config de Firebird não pode vazar de um teste para o próximo.

Por que este arquivo existe. `firebird_config.apply_to_env()` grava direto em
`os.environ` — é o contrato dele em produção. Em teste isso escapa do
`monkeypatch`, e um teste chegou a deixar `FB_HOST=10.0.0.1` para trás. Dali em
diante, todo teste que tocasse o Firebird tentava abrir TCP contra aquele host:
no macOS falha na hora, no Linux do CI espera os SYN retries (~127s por
chamada). Resultado: 24 minutos de CI contra 70 segundos locais, **sem nenhum
teste falhar**. Custo invisível é o mais caro.

Os testes dependem da ordem: um suja, o seguinte confere que a sujeira não
atravessou. pytest executa na ordem do arquivo (e todos os params de uma função
antes da próxima), então o par é determinístico. Se alguém remover a fixture
`_isolate_firebird_env` do `conftest.py`, os testes de conferência falham — que
é exatamente o ponto.

**Baseline, não ausência.** A fixture restaura o estado que existia ANTES do
teste, e esse estado pode legitimamente ter `FB_*`: basta um `source .env` antes
do pytest, cenário real neste repo (o `.env` define FB_DATABASE, FB_CHARSET,
FB_CLIENT_LIBRARY e FB_CODEMPRESA). Por isso comparamos contra um snapshot
tirado na importação do módulo, nunca contra "a chave não existe".
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.conftest import _FB_ENV_KEYS

_SENTINELA_HOST = "10.0.0.1"
_SENTINELA_VALOR = "vazamento-que-nao-pode-atravessar"

# Estado ANTES de qualquer teste deste módulo. É contra isto que conferimos —
# a guarda promete restaurar o baseline, não esvaziar o ambiente.
_BASELINE = {k: os.environ.get(k) for k in _FB_ENV_KEYS}


def _diff_do_baseline() -> dict[str, tuple[str | None, str | None]]:
    """Chaves cujo valor atual difere do baseline: `{chave: (baseline, agora)}`."""
    return {
        k: (_BASELINE[k], os.environ.get(k))
        for k in _FB_ENV_KEYS
        if os.environ.get(k) != _BASELINE[k]
    }


def test_um_teste_pode_sujar_o_ambiente():
    """Simula o que `apply_to_env()` faz: escreve direto, fora do monkeypatch."""
    os.environ["FB_HOST"] = _SENTINELA_HOST
    os.environ["FB_DATABASE"] = "/data/nao_existe.fdb"
    assert os.environ["FB_HOST"] == _SENTINELA_HOST


def test_o_teste_seguinte_recebe_o_baseline_de_volta():
    """Se este falhar, a suíte inteira volta a pendurar no CI."""
    assert _diff_do_baseline() == {}, (
        "config de Firebird vazou do teste anterior — a fixture "
        "_isolate_firebird_env do conftest.py sumiu ou parou de restaurar"
    )


@pytest.mark.parametrize("chave", _FB_ENV_KEYS)
def test_suja_cada_chave_uma_a_uma(chave: str):
    """Escreve DIRETO em os.environ, que é o caminho que escapa do monkeypatch.

    Sem `monkeypatch.setenv` de propósito: com ele, o undo do próprio monkeypatch
    limparia a sujeira e o teste seguinte passaria mesmo com a guarda quebrada —
    foi assim que a primeira versão deste arquivo virou tautologia.
    """
    os.environ[chave] = _SENTINELA_VALOR
    assert os.environ[chave] == _SENTINELA_VALOR


def test_todas_as_chaves_voltaram_ao_baseline():
    """Roda depois de TODOS os params acima — cobre a lista inteira, não só duas."""
    assert _diff_do_baseline() == {}, (
        "a guarda não restaurou todas as chaves FB_* depois da parametrização"
    )


# --- a guarda não pode envelhecer em silêncio ---------------------------------


def _raiz() -> Path:
    return Path(__file__).resolve().parent.parent


def test_a_lista_cobre_toda_fb_lida_em_app():
    """Varre `app/` inteiro, não um arquivo só.

    A primeira versão deste teste olhava apenas `connection.py` — e o próprio
    repo já a contradizia: `FB_CODEMPRESA` é lida em `app/erp/mapper.py:62`.
    Uma chave nova lida em qualquer outro módulo voltaria a vazar em silêncio.
    """
    lidas: set[str] = set()
    for py in (_raiz() / "app").rglob("*.py"):
        lidas |= set(re.findall(r'["\'](FB_[A-Z_]+)["\']', py.read_text(encoding="utf-8")))

    # Constante de motivo-de-skip em product_check.py, não variável de ambiente.
    lidas.discard("FB_DATABASE_NOT_SET")

    faltando = lidas - set(_FB_ENV_KEYS)
    assert not faltando, (
        f"app/ lê {sorted(faltando)}, que não está em _FB_ENV_KEYS do conftest.py — "
        f"essa chave pode vazar entre testes"
    )


def test_a_lista_cobre_tudo_que_apply_to_env_escreve():
    """A origem do vazamento é a ESCRITA, não a leitura.

    `firebird_config._ENV_MAP` é quem decide o que `apply_to_env()` grava em
    `os.environ`. Se alguém adicionar um campo lá, a guarda precisa saber.
    """
    from app import firebird_config

    escritas = set(firebird_config._ENV_MAP.values()) | {"FB_PASSWORD"}
    faltando = escritas - set(_FB_ENV_KEYS)
    assert not faltando, (
        f"apply_to_env() escreve {sorted(faltando)}, fora de _FB_ENV_KEYS — "
        f"vaza exatamente pelo caminho que causou o incidente"
    )
