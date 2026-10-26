"""Task 8: a tela estática /admin/envio-flow (servida, menu e contrato de segurança)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.web.auth import COOKIE_NAME

STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"


def _client() -> TestClient:
    from app.web.server import app

    return TestClient(app)


def test_sem_cookie_redireciona_para_login(monkeypatch):
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    r = _client().get("/admin/envio-flow", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


def test_com_sessao_serve_a_pagina(monkeypatch):
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    c = _client()
    c.cookies.set(COOKIE_NAME, "qualquer")
    r = c.get("/admin/envio-flow")
    assert r.status_code == 200
    assert 'id="btn-enviar-agora"' in r.text and 'id="q"' in r.text


def test_menu_admin_tem_envio_ao_flow():
    js = (STATIC / "js" / "shell.js").read_text(encoding="utf-8")
    assert "route: 'admin-envio-flow'" in js
    assert "'/admin/envio-flow':          'admin-envio-flow'" in js
    assert js.index("route: 'admin-ambientes'") < js.index("route: 'admin-envio-flow'")


def test_pagina_sem_innerhtml_nem_confirm_e_com_acessibilidade():
    html = (STATIC / "admin-envio-flow.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html
    assert not re.search(r"\bconfirm\(", html)
    for trecho in (
        'role="dialog"',
        'aria-modal="true"',
        'aria-live="polite"',
        "history.replaceState",
        "America/Sao_Paulo",
        "Lê o Fire agora e manda tudo o que estiver na fila",
        "Ainda rodando; veja Últimos envios daqui a pouco.",
        "Nada novo para enviar; o Flow confirmou que o portal está vivo.",
    ):
        assert trecho in html


def test_tela_de_ambiente_nao_duplica_os_textos_de_bloqueio():
    html = (STATIC / "admin-ambiente-edit.html").read_text(encoding="utf-8")
    assert "BLOQUEIOS" not in html and "bloqueio_texto" in html
