"""O botão individual e o lote concordam sobre o ack de "sem preço no Fire".

O servidor dispensa o ack na planilha (`preco_trava.sem_preco_dispensado`) e
avisa a tela com `sem_preco_dispensado` no payload do preview. Sem isto o lote
gerava tudo, mas o preview individual seguia desabilitando a exportação e
pedindo "Confirmar e prosseguir": dois processos para a mesma regra.

Sem infra de JS no repo, mas há `node`: as funções reais saem do `index.html` e
são EXECUTADAS (mesmo padrão de `test_index_lote_motivo.py`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_HTML = (
    Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "index.html"
).read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node não instalado")


def _funcao(nome: str) -> str:
    m = re.search(rf"^function {nome}\(.*?^\}}\n", _HTML, flags=re.S | re.M)
    assert m, f"function {nome} ausente do index.html"
    return m.group(0)


def _avaliar(data: dict) -> dict:
    script = (
        "".join(_funcao(n) for n in ("precoLigado", "_isBlockedByPriceCheck", "_blockReasonText"))
        + f"const d = {json.dumps(data)};"
        + "process.stdout.write(JSON.stringify({blocked: _isBlockedByPriceCheck(d),"
        + " reason: _blockReasonText(d)}));"
    )
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _data(*status: str, dispensado: bool = False, ack: dict | None = None) -> dict:
    return {
        "sem_preco_dispensado": dispensado,
        "sem_preco_ack": ack,
        "check": {
            "available": True,
            "summary": {
                "price_summary": {
                    "items_mismatch": status.count("mismatch"),
                    "items_no_order_price": status.count("no_order_price"),
                }
            },
            "items": [
                {"ean": f"789{i}", "product_code": f"C{i}", "price_status": s}
                for i, s in enumerate(status)
            ],
        },
    }


def test_sem_dispensa_o_item_sem_preco_bloqueia_e_pede_confirmacao() -> None:
    r = _avaliar(_data("no_price_in_fire"))
    assert r["blocked"] is True
    assert "Confirme 1 item(ns) sem preço cadastrado" in r["reason"]


def test_com_dispensa_o_item_sem_preco_nao_bloqueia_nem_pede_confirmacao() -> None:
    assert _avaliar(_data("no_price_in_fire", dispensado=True)) == {"blocked": False, "reason": ""}


def test_dispensa_nao_afrouxa_divergencia_de_preco() -> None:
    r = _avaliar(_data("mismatch", "no_price_in_fire", dispensado=True))
    assert r["blocked"] is True
    assert "divergência" in r["reason"]


def test_dispensa_nao_afrouxa_pedido_sem_preco() -> None:
    r = _avaliar(_data("no_order_price", dispensado=True))
    assert r["blocked"] is True
    assert "sem preço no pedido" in r["reason"]


def test_o_banner_trata_a_dispensa_antes_de_oferecer_o_botao_de_confirmar() -> None:
    """O ramo "dispensado" tem que vir ANTES do que desenha o botão "Confirmar e
    prosseguir"; se a ordem inverter, o botão volta a aparecer com a flag ligada."""
    dispensado = _HTML.index("regraFire && data.sem_preco_dispensado && noPriceUnacked.length > 0")
    botao = _HTML.index("pvAckSemPrecoBtn")
    assert dispensado < botao
