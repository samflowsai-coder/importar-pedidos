"""O alert do lote diz POR QUE cada pedido falhou, não só quantos.

Caso de origem (21/10/2026): 11 pedidos da Calcenter, "0 XLS gerado(s), 11 com
erro. Detalhes no console." A operadora não tinha console nem como saber que o
portão de preço tinha recusado (item sem preço no cadastro do Fire, sem
confirmação). O servidor já devolve `reason`/`detail` por pedido; faltava a tela
mostrar.

Sem infra de JS no repo, mas há `node`: o teste extrai `_motivosDasFalhas` do
`index.html` e a EXECUTA, em vez de só procurar texto.
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

_SEM_NODE = shutil.which("node") is None

_DETALHE_PRECO = "Pedido bloqueado: 1 item(ns) sem preço no cadastro do Fire (sem confirmação)."


def _funcao(nome: str) -> str:
    """Corpo de `function <nome>(...) { ... }` do index.html (fecha na `}` da coluna 0)."""
    m = re.search(rf"^function {nome}\(.*?^\}}\n", _HTML, flags=re.S | re.M)
    assert m, f"function {nome} ausente do index.html"
    return m.group(0)


def _rodar(results: list[dict]) -> str:
    script = (
        _funcao("_motivosDasFalhas")
        + f"\nprocess.stdout.write(_motivosDasFalhas({json.dumps(results)}));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=20
    )
    return out.stdout


def _falha(detail: str | None, reason: str | None = "price_check_failed") -> dict:
    return {"id": "x", "ok": False, "reason": reason, "detail": detail}


@pytest.mark.skipif(_SEM_NODE, reason="node não instalado")
def test_onze_falhas_iguais_viram_uma_linha_com_o_motivo_e_a_contagem() -> None:
    texto = _rodar([_falha(_DETALHE_PRECO) for _ in range(11)])
    assert texto.count(_DETALHE_PRECO) == 1
    assert "11" in texto


@pytest.mark.skipif(_SEM_NODE, reason="node não instalado")
def test_motivos_diferentes_aparecem_separados_do_mais_comum_pro_menos() -> None:
    resultados = [_falha("A")] + [_falha("B") for _ in range(3)] + [_falha("C") for _ in range(2)]
    linhas = _rodar(resultados).splitlines()
    ordem = [i for i, ln in enumerate(linhas) if ln.strip()[:1] == "•"]
    achados = [linhas[i] for i in ordem]
    assert [("B" in a, "C" in a, "A" in a) for a in achados] == [
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ]


@pytest.mark.skipif(_SEM_NODE, reason="node não instalado")
def test_sucesso_nao_entra_na_lista() -> None:
    texto = _rodar([{"id": "ok", "ok": True, "output_files": []}, _falha("só este")])
    assert "só este" in texto
    assert texto.count("•") == 1


@pytest.mark.skipif(_SEM_NODE, reason="node não instalado")
def test_sem_detail_cai_no_reason_e_sem_nada_diz_que_nao_ha_motivo() -> None:
    assert "wrong_status" in _rodar([_falha(None, reason="wrong_status")])
    assert "motivo não informado" in _rodar([_falha(None, reason=None)])


@pytest.mark.skipif(_SEM_NODE, reason="node não instalado")
def test_lista_longa_e_cortada_e_avisa_quantos_ficaram_de_fora() -> None:
    resultados = [_falha(f"motivo-{i}") for i in range(8)]
    texto = _rodar(resultados)
    assert texto.count("•") == 6  # 5 motivos + a linha "e mais N"
    assert "mais 3" in texto


@pytest.mark.skipif(_SEM_NODE, reason="node não instalado")
def test_texto_do_servidor_nao_vira_html_no_alert() -> None:
    """alert() é texto puro: o detalhe vai cru, sem escape que mostraria `&lt;`."""
    assert "<b>" in _rodar([_falha("<b>x</b>")])


def test_lote_primario_usa_os_motivos_no_alert_e_mantem_o_console() -> None:
    corpo = re.search(r"^async function doBatchPrimary\(\).*?^\}\n", _HTML, flags=re.S | re.M)
    assert corpo, "doBatchPrimary ausente do index.html"
    corpo_js = corpo.group(0)
    assert "_motivosDasFalhas(res.results)" in corpo_js
    # o console segue com o objeto completo, pra quem tiver acesso a ele
    assert "console.warn('Batch primary action failures:'" in corpo_js
    # a frase que mandava a operadora pro console (que ela não tem) saiu do alert
    assert "Detalhes no console" not in corpo_js
