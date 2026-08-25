"""pytest fixtures and global config.

Auth bypass for legacy tests:
    Phase 4b introduced session auth on mutation routes. The pre-existing
    tests for those routes (preview/commit/send-to-fire/cancel/etc.) don't
    care about the auth flow — they exercise the *underlying* behavior.
    Setting `TEST_AUTH_BYPASS=1` makes `require_user` return a synthetic
    test admin without going through the cookie/login dance.

    New tests that DO want to exercise the real auth flow (login attempts,
    cookie validation, session expiry) explicitly unset the bypass via the
    `real_auth` fixture below.
"""
from __future__ import annotations

import os
import socket
import sqlite3
from pathlib import Path

import pytest


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    # Set BEFORE any app import resolves env. Tests that need real auth
    # use the `real_auth` fixture to override.
    os.environ.setdefault("TEST_AUTH_BYPASS", "1")
    # Cookies are sent over HTTP in the test client; mark cookie non-secure.
    os.environ.setdefault("PORTAL_COOKIE_SECURE", "0")
    # Desliga o loop periódico de reconciliação com o Fire (app.reconcile.
    # runner) globalmente na suíte. Sem isto, `tests/test_metrics.py` e
    # `tests/test_rate_limit.py` usam `with TestClient(app)`, que dispara o
    # startup de verdade — e a thread daemon do loop periódico abriria uma
    # conexão Firebird REAL de dentro do teste se a rodada atravessasse
    # 07h/12h/18h locais. Testes que querem exercitar o loop/gatilho
    # periódico de propósito usam `monkeypatch.setenv` para religar.
    os.environ.setdefault("PORTAL_RECONCILE_PERIODICO", "0")


# Toda variavel que decide PARA ONDE o app conecta ou como ele fala com o
# Firebird. Lidas em `app/erp/connection.py` E em `app/erp/mapper.py`
# (FB_CODEMPRESA); escritas por `firebird_config.apply_to_env()`. Se uma delas
# vazar de um teste para o proximo, o seguinte tenta abrir TCP de verdade contra
# um host que nao existe. `tests/test_env_leak_guard.py` falha se esta lista
# ficar para tras do codigo.
_FB_ENV_KEYS = (
    "FB_DATABASE",
    "FB_HOST",
    "FB_PORT",
    "FB_USER",
    "FB_CHARSET",
    "FB_PASSWORD",
    "FB_CLIENT_LIBRARY",
    "FB_CODEMPRESA",
    # Mesma classe de vazamento, dano menor: `db.set_db_path()` grava direto em
    # os.environ e varios testes escrevem a chave na mao. Aponta para disco
    # local, entao falha rapido e visivel -- nada de SYN retry. Uma linha.
    "APP_DATA_DIR",
)


@pytest.fixture(autouse=True)
def _isolate_firebird_env():
    """Impede que config de Firebird vaze de um teste para o proximo.

    `firebird_config.apply_to_env()` grava DIRETO em `os.environ` — e' o
    contrato dele em producao, para que `connection.py` (que le `os.environ` a
    cada conexao, sem cache) pegue a credencial nova sem reiniciar o app. Em
    teste isso escapa do `monkeypatch`: `monkeypatch.delenv(k, raising=False)`
    sobre uma chave que NAO existe nao registra nada para desfazer, entao o
    valor escrito durante o teste sobrevive ao teardown.

    O estrago e' invisivel na maquina do dev e caro no CI. Um teste gravou
    `FB_HOST=10.0.0.1`; dali em diante, todo teste que tocasse o Firebird
    tentava conectar naquele host. No macOS o connect falha na hora; no Linux
    do runner ele espera os SYN retries — ~127s por chamada. Custava 24 minutos
    de CI por push, contra 70 segundos locais, sem nenhum teste falhando.

    Snapshot antes, restaura depois. Barato (8 chaves) e vale para qualquer
    teste, presente ou futuro.
    """
    saved = {k: os.environ.get(k) for k in _FB_ENV_KEYS}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _no_real_sockets():
    """Nenhum teste abre socket para fora. Conferido: a suite faz ZERO conexoes.

    Segunda cerca para o mesmo pasto, de proposito. A guarda de env acima impede
    a causa conhecida; esta transforma QUALQUER recorrencia -- por env, por
    config, por caminho que ninguem previu -- de "minutos de espera em silencio"
    em erro instantaneo apontando a linha. Foi exatamente a falta desse sinal
    que deixou 24 minutos de CI passarem despercebidos, com a suite verde.

    Loopback e socket unix continuam livres: nao ha uso real hoje, mas bloquear
    o que e' legitimo seria trocar um falso negativo por um falso positivo.
    """
    original = socket.socket.connect

    def _bloqueia(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is None or str(host).startswith("127.") or str(host) in ("localhost", "::1"):
            return original(self, address)
        raise RuntimeError(
            f"teste tentou abrir socket para {address!r}. Testes nao falam com a "
            f"rede: mocke a chamada, ou confira se config vazou de outro teste "
            f"(ver tests/test_env_leak_guard.py)."
        )

    socket.socket.connect = _bloqueia
    try:
        yield
    finally:
        socket.socket.connect = original


@pytest.fixture
def real_auth(monkeypatch):
    """Disable auth bypass — caller will exercise the real login flow."""
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    yield


@pytest.fixture
def tmp_shared_db(tmp_path: Path):
    """Empty SQLite for shared-DB schema/repo tests (future app_shared.db)."""
    db_file = tmp_path / "app_shared.db"
    conn = sqlite3.connect(db_file, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


@pytest.fixture
def tmp_env_db(tmp_path: Path):
    """Empty SQLite for per-env schema/repo tests (future app_state_<slug>.db)."""
    db_file = tmp_path / "app_state_test.db"
    conn = sqlite3.connect(db_file, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
