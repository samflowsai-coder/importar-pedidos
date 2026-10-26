"""Perfil fiscal por ambiente: as constantes que o Fire exige num pedido
faturavel e que o mapper nao escrevia.

Todos os defaults vem de medicao na Fire viva (2026-08-24): 373 pedidos do
.7 (MM Americanense) e 90 do .4 (Nasmar), de 2026-06-01 em diante. Sao os
valores presentes em 82% a 100% dos pedidos digitados pela operacao.

CODFIGFISCAL, ICMS_PORC, REDUCAO e CFOP_PRINCIPAL deste perfil sao so o
ponto de partida: no insert direto `figura_fiscal.resolver` troca os quatro
pelos da figura escolhida para o cliente (a nota do Fire sai dela). A figura
do ambiente (`environments.fiscal_codfigfiscal`, 1 na MM, 5 na Nasmar) vale
so como ultimo degrau daquela escolha.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PerfilFiscal:
    codfigfiscal: int
    tipo_cob: int
    cod_class_finan: int
    desc_class_finan: str
    classif_fat: int
    mecanico: int
    icms_porc: Decimal
    reducao: Decimal
    cfop_principal: str


_DEFAULTS = PerfilFiscal(
    codfigfiscal=1,
    tipo_cob=4,
    cod_class_finan=335,
    desc_class_finan="Venda de Produtos",
    classif_fat=1,
    mecanico=99,
    icms_porc=Decimal("18"),
    reducao=Decimal("61.11"),
    cfop_principal="5.101",
)


def perfil_para(env: dict | None) -> PerfilFiscal:
    """Perfil do ambiente. Campo ausente ou NULL cai no default medido."""
    if not env:
        return _DEFAULTS
    codfig = env.get("fiscal_codfigfiscal")
    if codfig in (None, ""):
        return _DEFAULTS
    return PerfilFiscal(
        codfigfiscal=int(codfig),
        tipo_cob=_DEFAULTS.tipo_cob,
        cod_class_finan=_DEFAULTS.cod_class_finan,
        desc_class_finan=_DEFAULTS.desc_class_finan,
        classif_fat=_DEFAULTS.classif_fat,
        mecanico=_DEFAULTS.mecanico,
        icms_porc=_DEFAULTS.icms_porc,
        reducao=_DEFAULTS.reducao,
        cfop_principal=_DEFAULTS.cfop_principal,
    )
