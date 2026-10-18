"""Marcas do Fire (PRODUTOS_MARCA) e nota fiscal (NOTAPROD) na ida do catálogo
(contrato P4a §8). Uma marca fora das regras derruba o POST /catalogo inteiro
com 400: o Portal filtra antes e reporta."""

from __future__ import annotations

import os

MAX_MARCAS = 500
MAX_NOME = 30
MAX_CODIGO = 999_999_999


def _ligado(var: str, slug: str) -> bool:
    return slug.lower() in {
        s.strip().lower() for s in os.environ.get(var, "").split(",") if s.strip()
    }


def envio_ligado(slug: str) -> bool:
    return _ligado("PORTAL_FLOWPCP_CATALOGO_MARCAS", slug)


def nota_ligada(slug: str) -> bool:
    return _ligado("PORTAL_FLOWPCP_CATALOGO_NOTA", slug)


def filtrar(brutas) -> tuple[list[dict], list[dict]]:
    """(enviadas `{codigo, nome}`, filtradas `{codigo, motivo}`). Regras do Flow:
    código inteiro 1..999999999, nome aparado com 1..30 caracteres, até 500."""
    enviadas: list[dict] = []
    filtradas: list[dict] = []
    for codigo, nome in brutas:
        texto = str(nome or "").strip()
        if not isinstance(codigo, int) or isinstance(codigo, bool) or not 1 <= codigo <= MAX_CODIGO:
            filtradas.append({"codigo": codigo, "motivo": "codigo_invalido"})
        elif not texto:
            filtradas.append({"codigo": codigo, "motivo": "nome_vazio"})
        elif len(texto) > MAX_NOME:
            filtradas.append({"codigo": codigo, "motivo": "nome_longo"})
        elif len(enviadas) >= MAX_MARCAS:
            filtradas.append({"codigo": codigo, "motivo": "excede_500"})
        else:
            enviadas.append({"codigo": codigo, "nome": texto})
    return enviadas, filtradas
