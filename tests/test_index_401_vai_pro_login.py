"""O 401 na tela de pedidos manda pro login, não pinta erro cru.

Sem infra de JS no repo, então o teste lê o `index.html` como texto e trava
os trechos que não podem regredir em silêncio — mesmo padrão de
`tests/test_web_preview_intercompany.py`.

Por que isso virou teste: três rotas que a tela chama a todo momento
(`/api/pending`, `/api/imported/{id}` e `/preview`) passaram a exigir sessão
na entrega "o ambiente é propriedade do pedido". Antes respondiam sem, então
a sessão vencida seguia abrindo pedido. Agora devolvem 401, e sem este
tratamento o operador via a frase "autenticação requerida" numa caixa de erro,
sem caminho de saída.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HTML = (
    Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "index.html"
).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "trecho",
    [
        # o helper trata 401 ANTES do ramo genérico de !res.ok
        "if (res.status === 401) {",
        "window.location.href = '/login';",
    ],
)
def test_index_trata_401_indo_pro_login(trecho: str) -> None:
    assert trecho in _HTML, f"trecho ausente do index.html: {trecho}"


def test_o_401_e_tratado_antes_do_erro_generico() -> None:
    """Ordem importa: se o ramo `!res.ok` viesse primeiro, ele engoliria o 401
    e o redirect nunca rodaria."""
    pos_401 = _HTML.index("if (res.status === 401) {")
    pos_generico = _HTML.index("if (!res.ok) {", _HTML.index("async function api(url, opts)"))
    assert pos_401 < pos_generico


def test_login_volta_pra_raiz_onde_o_operador_estava() -> None:
    """O redirect não carrega `next`, e não precisa: `login.html` manda pra
    raiz depois de logar, que é a própria tela de pedidos. Se isso mudar, o
    comentário do helper passa a mentir."""
    login = (
        Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "login.html"
    ).read_text(encoding="utf-8")
    assert "window.location.href = '/';" in login
