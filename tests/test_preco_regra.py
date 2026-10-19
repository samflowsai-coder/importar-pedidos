"""Regra pura do §5 do contrato precos v1 (app/erp/preco_regra.py)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.erp import preco_regra
from app.erp.preco_regra import PrecoInvalido, PrecoLinha

VETORES = json.loads(
    (Path(__file__).parent / "fixtures" / "flow_precos_v1" / "vetores.json").read_text(
        encoding="utf-8"
    )
)


def _instante(s: str) -> datetime:
    return datetime.fromisoformat(s)


@pytest.mark.parametrize("caso", VETORES["dataReferencia"], ids=lambda c: c["caso"])
def test_vetores_data_referencia(caso):
    emitido = _instante(caso["emitidoEm"]) if caso["emitidoEm"] else None
    got = preco_regra.data_referencia(emitido, _instante(caso["recebidoEm"]))
    assert got == date.fromisoformat(caso["esperado"])


def test_data_referencia_recusa_instante_sem_fuso():
    with pytest.raises(ValueError):
        preco_regra.data_referencia(None, datetime(2026, 10, 20, 12, 0))


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("11.222.333/0012-88", "11222333001288"),
        ("12abc34501de35", "12ABC34501DE35"),
        ("12ABC34501DEA5", None),
        ("1122233300128", None),
        ("112223330012889", None),
        ("", None),
        (None, None),
        ("１１２２２３３３００１２８８", None),  # dígitos não ASCII
    ],
)
def test_normalizar_cnpj(bruto, esperado):
    assert preco_regra.normalizar_cnpj(bruto) == esperado


def test_cnpj_valido_nao_normaliza():
    assert preco_regra.cnpj_valido("11222333001288") is True
    assert preco_regra.cnpj_valido("12ABC34501DE35") is True
    assert preco_regra.cnpj_valido("12abc34501de35") is False
    assert preco_regra.cnpj_valido("11.222.333/0012-88") is False


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (45.895, 4590),
        (45.905, 4591),
        (45.9, 4590),
        (0.005, 1),
        (54, 5400),
        (0, None),
        (-1.0, None),
        (None, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
    ],
)
def test_centavos_meia_para_cima(valor, esperado):
    assert preco_regra.centavos(valor) == esperado


def _linha(preco_id, *, registrado="2026-10-19T10:00:00.000Z", **kw) -> PrecoLinha:
    base = {
        "preco_id": preco_id,
        "rede_id": "r",
        "empresa_cnpj": "11111111000191",
        "erp_produto_id": "1",
        "unidade_erp": "KIT",
        "preco_centavos": 100,
        "vigencia_inicio": date(2026, 10, 19),
        "registrado_em": registrado,
        "cancelado": False,
    }
    base.update(kw)
    return PrecoLinha(**base)


def _resolver(candidatos, *, rede_id="r", data=date(2026, 10, 20)):
    return preco_regra.resolver_preco(
        candidatos,
        empresa_cnpj="11111111000191",
        rede_id=rede_id,
        erp_produto_id="1",
        data=data,
    )


@pytest.mark.parametrize(
    "registrado", ["2026-10-19T10:00:00", "2026-10-19", "lixo", "2026-10-19T10:00:00+0000"]
)
def test_registrado_em_sem_fuso_explicito_e_erro(registrado):
    with pytest.raises(PrecoInvalido):
        _resolver([_linha("a"), _linha("b", registrado=registrado)])


def test_registrado_em_mais_00_00_vale():
    got = _resolver(
        [
            _linha("a", registrado="2026-10-19T10:00:00.000Z"),
            _linha("b", registrado="2026-10-19T10:00:01+00:00"),
        ]
    )
    assert got.preco_id == "b"


def test_registrado_invalido_fora_dos_candidatos_nao_conta():
    # Cancelado e de outro produto não são candidatos do passo 1.
    got = _resolver(
        [
            _linha("a"),
            _linha("b", registrado="lixo", cancelado=True),
            _linha("c", registrado="lixo", erp_produto_id="2"),
        ]
    )
    assert got.preco_id == "a"


def test_empate_por_preco_id_nao_depende_da_ordem():
    linhas = [_linha("b"), _linha("c"), _linha("a")]
    assert _resolver(linhas).preco_id == "c"
    assert _resolver(list(reversed(linhas))).preco_id == "c"


def test_sem_rede_usa_so_o_base():
    got = _resolver([_linha("a", rede_id="r"), _linha("b", rede_id=None)], rede_id=None)
    assert got.preco_id == "b"


def test_registrado_em_compara_ao_milissegundo():
    # .0002Z e .0001Z são o mesmo milissegundo: desempata pelo precoId.
    got = _resolver(
        [
            _linha("b", registrado="2026-10-19T10:00:00.0001Z"),
            _linha("a", registrado="2026-10-19T10:00:00.0002Z"),
        ]
    )
    assert got.preco_id == "b"
