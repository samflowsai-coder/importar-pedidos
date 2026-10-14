from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.erp_eventos.lotes import (
    Pendente,
    UnidadeGrandeDemaisError,
    decidir,
    dividir_lote,
    espera_crescente,
    ler_resultados,
    proximo_lote,
)

AGORA = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


def fila(*tamanhos_unidade: int) -> list[Pendente]:
    """Uma unidade por número; cada unidade com aquele tanto de eventos."""
    saida, ordem = [], 0
    for u, n in enumerate(tamanhos_unidade):
        for _ in range(n):
            ordem += 1
            saida.append(
                Pendente(ordem=ordem, id=f"{ordem:064x}", unidade=f"u{u}", payload_json='{"x":1}')
            )
    return saida


def test_lote_nunca_parte_um_pedido_visto_com_seus_itens():
    lote = proximo_lote(fila(300, 250), max_eventos=500)
    assert len(lote) == 300 and {p.unidade for p in lote} == {"u0"}


def test_lote_junta_unidades_que_cabem():
    assert len(proximo_lote(fila(200, 200, 200), max_eventos=500)) == 400


def test_lote_respeita_bytes():
    pend = [
        Pendente(ordem=i, id=f"{i:064x}", unidade=f"u{i}", payload_json="x" * 1000)
        for i in range(10)
    ]
    assert len(proximo_lote(pend, max_bytes=200 + 3 * 1001)) == 3


def test_unidade_maior_que_o_lote_e_erro_explicito():
    with pytest.raises(UnidadeGrandeDemaisError):
        proximo_lote(fila(501), max_eventos=500)
    assert len(proximo_lote(fila(10, 501), max_eventos=500)) == 10


def test_fila_vazia_da_lote_vazio_sinal_de_vida():
    assert proximo_lote([]) == []


def test_dividir_para_413_sem_partir_unidade():
    a, b = dividir_lote(fila(3, 3, 3, 3))
    assert len(a) == 6 and len(b) == 6
    assert dividir_lote(fila(5)) is None


def test_resposta_casada_por_id_nunca_por_posicao():
    enviados = ["a" * 64, "b" * 64, "c" * 64]
    corpo = {
        "loteId": "x",
        "resultados": [
            {"id": "c" * 64, "situacao": "rejeitado", "motivo": "item.qtd: inválido"},
            {"id": "a" * 64, "situacao": "aceito"},
            {"id": "", "situacao": "rejeitado"},
            {"id": "z" * 64, "situacao": "aceito"},
        ],
    }
    r = ler_resultados(enviados, corpo)
    assert r == {"a" * 64: ("aceito", None), "c" * 64: ("rejeitado", "item.qtd: inválido")}
    assert "b" * 64 not in r  # ausente da resposta: segue pendente


@pytest.mark.parametrize(
    ("status", "acao", "bloqueio"),
    [
        (202, "aplicar", None),
        (413, "dividir", None),
        (401, "bloquear", "credencial"),
        (400, "bloquear", "lote_invalido"),
        (404, "bloquear", "http_404"),
        (503, "esperar", None),
        (None, "esperar", None),
    ],
)
def test_decisao_por_status(status, acao, bloqueio):
    d = decidir(status, retry_after=None, falhas_seguidas=0, agora=AGORA)
    assert (d.acao, d.bloqueio) == (acao, bloqueio)


def test_429_respeita_retry_after_e_espera_crescente():
    assert decidir(429, retry_after="120", falhas_seguidas=0, agora=AGORA).espera_s == 120
    assert decidir(429, retry_after="5", falhas_seguidas=3, agora=AGORA).espera_s == 240
    assert (
        decidir(
            429, retry_after="Fri, 09 Oct 2026 13:01:00 GMT", falhas_seguidas=0, agora=AGORA
        ).espera_s
        == 60
    )


def test_espera_crescente_tem_teto():
    assert [espera_crescente(n) for n in range(7)] == [30, 60, 120, 240, 480, 900, 900]
