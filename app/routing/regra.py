"""Regra comercial de faturamento MM x Nasmar: tipos da política.

Só os dataclasses por ora; a avaliação da regra entra na Task 2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RedeFixa:
    """Rede que fatura sempre em `env_slug`, por raiz (8 díg.) ou CNPJ (14)."""

    nome: str
    env_slug: str
    raizes: frozenset[str]
    cnpjs: frozenset[str]


@dataclass(frozen=True)
class Politica:
    """Política ativa: empresa p/ destino em SP, fora de SP e redes fixas."""

    env_sp: str
    env_fora_sp: str
    redes: tuple[RedeFixa, ...]
