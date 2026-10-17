"""O que o Flow diz que aceita (`capacidades` na resposta). O Zod do Flow
descarta chave desconhecida em silêncio (medido em 16/10/2026): sem a
capacidade, campo novo some sem 400. Ausente ou malformado = nenhuma."""

from __future__ import annotations

FISCAL = "fiscal.v1"
MATERIAIS_DRYRUN = "materiais.dryrun.v1"
MATERIAIS = "materiais.v1"
CADASTROS = "cadastros.v1"


def de(resposta) -> frozenset[str]:
    if isinstance(resposta, dict):
        bruto = resposta.get("capacidades")
    else:
        bruto = (getattr(resposta, "model_extra", None) or {}).get("capacidades")
        if bruto is None:
            bruto = getattr(resposta, "capacidades", None)
    if not isinstance(bruto, list):
        return frozenset()
    return frozenset(c for c in bruto if isinstance(c, str))
