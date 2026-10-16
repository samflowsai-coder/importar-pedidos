"""Contrato `cadastros.v1` (Flow → Portal) e a validação de borda dele.

A fila do Flow chega como lista crua: cada item é validado à parte, para um item
ruim nunca afundar os outros. As regras aqui são mais estritas que as do Flow
(ids numéricos, WIN1252, limites das colunas do Fire): o que passa daqui ainda
precisa passar na conferência contra a cópia local e contra o Fire.

Nenhuma mensagem de erro carrega o valor recebido: só nome de campo, tipo de erro
e tamanho. O texto vai para o log, o livro e a tela.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_MENSAGEM = 500
MAX_COMPONENTES = 50
MAX_QUANTIDADE = 100000
# Colunas do Fire (PRODUTOS): DESCRICAO, DESCRICAO2, UNIDADE, CODIGO_EAN13, CODPARANFE, CODPROD_ALTERN.
LIMITES: dict[str, int] = {
    "nome": 200,
    "cor": 200,
    "unidadeErp": 10,
    "ean": 15,
    "codigoNfe": 20,
    "codigoMes": 30,
}
_SPED_VALIDOS = frozenset({*range(0, 11), 99})
_NOME_SEGURO = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class _Estrito(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GrupoRef(_Estrito):
    codigo: str = Field(pattern=r"^\d{1,10}$")


class ComponenteRef(_Estrito):
    erpProdutoId: str = Field(pattern=r"^\d{1,10}$")  # noqa: N815
    quantidade: float


class CadastroPendente(_Estrito):
    cadastroId: UUID  # noqa: N815
    versao: int = Field(ge=1)
    acao: Literal["criar", "atualizar", "inativar"]
    erpProdutoId: str | None = Field(default=None, pattern=r"^\d{1,10}$")  # noqa: N815
    tipo: Literal["simples", "kit", "pack", "material"]
    nome: str = Field(min_length=1)
    unidadeErp: str | None = None  # noqa: N815
    ean: str | None = None
    ncm: str | None = Field(default=None, pattern=r"^\d{8}$")
    tipoItemSped: int | None = None  # noqa: N815
    grupoErp: GrupoRef  # noqa: N815
    codigoNfe: str | None = None  # noqa: N815
    cor: str | None = None
    componentes: list[ComponenteRef] | None = None
    codigoMes: str | None = None  # noqa: N815
    aprovadoEm: datetime  # noqa: N815

    @field_validator("tipoItemSped")
    @classmethod
    def _sped(cls, v: int | None) -> int | None:
        if v is not None and v not in _SPED_VALIDOS:
            raise ValueError("tipoItemSped fora de 0..10 e 99")
        return v


class PendentesResposta(BaseModel):
    """Resposta da fila. Tolera chave nova; `cadastros` fica crua."""

    model_config = ConfigDict(extra="allow")

    capacidades: list[str] = Field(default_factory=list)
    cadastros: list[dict] = Field(default_factory=list)


def codifica_win1252(s: str) -> bool:
    try:
        s.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _corta(msg: str) -> str:
    return msg[:MAX_MENSAGEM]


def _parte(p: object) -> str:
    t = str(p)
    return t if _NOME_SEGURO.match(t) else "?"


def _mensagem_pydantic(exc: ValidationError) -> str:
    # Só `loc` e `type`: `input` e `msg` podem carregar o valor recebido.
    partes = []
    for e in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(_parte(p) for p in e.get("loc", ())) or "(raiz)"
        partes.append(f"{loc}: {_parte(e.get('type', '?'))}")
    return _corta("contrato inválido; " + "; ".join(partes[:10]))


def _textos(item: CadastroPendente) -> dict[str, str]:
    campos = {
        "nome": item.nome,
        "cor": item.cor,
        "unidadeErp": item.unidadeErp,
        "ean": item.ean,
        "codigoNfe": item.codigoNfe,
        "codigoMes": item.codigoMes,
    }
    return {k: v for k, v in campos.items() if v is not None}


def _tem_controle(s: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in s)


def _erro_componentes(item: CadastroPendente) -> str | None:
    comps = item.componentes or []
    if item.tipo in ("simples", "material"):
        return "tipo sem composição não aceita componentes" if comps else None
    if item.tipo == "kit" or item.tipo == "pack":
        if not 1 <= len(comps) <= MAX_COMPONENTES:
            return f"{item.tipo} exige de 1 a {MAX_COMPONENTES} componentes; veio {len(comps)}"
        for i, c in enumerate(comps):
            if not (math.isfinite(c.quantidade) and 0 < c.quantidade <= MAX_QUANTIDADE):
                return f"componentes[{i}].quantidade fora de (0, {MAX_QUANTIDADE}]"
            if item.erpProdutoId is not None and c.erpProdutoId == item.erpProdutoId:
                return f"componentes[{i}] é o próprio produto"
    return None


def validar_item(bruto: dict) -> tuple[CadastroPendente | None, str | None, str | None]:
    """`(item, None, None)` se válido; senão `(None, codigo_erro, mensagem)`."""
    try:
        item = CadastroPendente.model_validate(bruto)
    except ValidationError as exc:
        return None, "contrato_invalido", _mensagem_pydantic(exc)
    except Exception:  # noqa: BLE001 — entrada que nem vira dict de verdade
        return None, "contrato_invalido", "contrato inválido; item ilegível"

    textos = _textos(item)
    for campo, limite in LIMITES.items():
        valor = textos.get(campo)
        if valor is not None and len(valor) > limite:
            return (
                None,
                "campo_excede_fire",
                _corta(f"{campo} tem {len(valor)} caracteres; o Fire aceita {limite}"),
            )
    for campo, valor in textos.items():
        if not codifica_win1252(valor) or _tem_controle(valor):
            return None, "caractere_invalido", _corta(f"{campo} tem caractere que o Fire não grava")

    if item.acao == "criar" and item.erpProdutoId is not None:
        return None, "contrato_invalido", "criar não aceita erpProdutoId"
    if item.acao != "criar" and item.erpProdutoId is None:
        return None, "contrato_invalido", f"{item.acao} exige erpProdutoId"

    erro = _erro_componentes(item)
    if erro:
        return None, "componente_invalido", _corta(erro)
    return item, None, None
