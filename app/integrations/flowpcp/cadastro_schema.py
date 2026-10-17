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
from typing import Any, Literal
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


_ID = r"^[1-9][0-9]{0,8}$"  # cabe no INTEGER de 32 bits do Fire, sem zero à esquerda


class _Estrito(BaseModel):
    # strict: nada de coerção (True->1, "1"->1, bytes->str) em campo que vira escrita no ERP.
    model_config = ConfigDict(extra="forbid", strict=True)


class GrupoRef(_Estrito):
    codigo: str = Field(pattern=_ID)


class ComponenteRef(_Estrito):
    erpProdutoId: str = Field(pattern=_ID)  # noqa: N815
    quantidade: float


class MarcaRef(_Estrito):
    # PRODUTOS_MARCA.CODIGO é INTEGER no Fire: cabe em 32 bits.
    codigo: int = Field(ge=1, le=2**31 - 1)


class CadastroPendente(_Estrito):
    # Chave de topo nova não derruba o item: o Flow só acrescenta campo junto com
    # capacidade nova. `chaves_desconhecidas` lista o que foi ignorado, para o log.
    model_config = ConfigDict(extra="ignore", strict=True)

    cadastroId: UUID  # noqa: N815
    produtoId: UUID  # noqa: N815 — id estável do produto no Flow (igual em todas as versões)
    versao: int = Field(ge=1, le=2**31 - 1)
    acao: Literal["criar", "atualizar", "inativar"]
    erpProdutoId: str | None = Field(default=None, pattern=_ID)  # noqa: N815
    tipo: Literal["simples", "kit", "pack", "material"]
    nome: str = Field(min_length=1)
    # Texto opcional: null = "não mexer"; "" é erro de contrato (min_length=1).
    unidadeErp: str | None = Field(default=None, min_length=1)  # noqa: N815
    ean: str | None = Field(default=None, min_length=1)
    ncm: str | None = Field(default=None, pattern=r"^[0-9]{8}$")
    tipoItemSped: int | None = None  # noqa: N815
    grupoErp: GrupoRef  # noqa: N815
    codigoNfe: str | None = Field(default=None, min_length=1)  # noqa: N815
    cor: str | None = Field(default=None, min_length=1)
    componentes: list[ComponenteRef] | None = None
    marcaErp: MarcaRef | None = None  # noqa: N815
    codigoMes: str | None = Field(default=None, min_length=1)  # noqa: N815
    aprovadoEm: datetime  # noqa: N815

    @field_validator("cadastroId", "produtoId", mode="before")
    @classmethod
    def _uuid(cls, v: object) -> UUID:
        if not isinstance(v, str):
            raise ValueError("cadastroId deve ser texto")
        return UUID(v)

    @field_validator("aprovadoEm", mode="before")
    @classmethod
    def _instante(cls, v: object) -> datetime:
        if not isinstance(v, str):
            raise ValueError("aprovadoEm deve ser texto")
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("aprovadoEm sem fuso")
        return dt

    @field_validator("nome")
    @classmethod
    def _nome_sem_borda(cls, v: str) -> str:
        if not v.strip() or v != v.strip():
            raise ValueError("nome vazio ou com espaço nas pontas")
        return v

    @field_validator("tipoItemSped")
    @classmethod
    def _sped(cls, v: int | None) -> int | None:
        if v is not None and v not in _SPED_VALIDOS:
            raise ValueError("tipoItemSped fora de 0..10 e 99")
        return v


class PendentesResposta(BaseModel):
    """Resposta da fila. Tolera chave nova; `cadastros` fica crua: item que não é
    objeto vira erro só dele em `validar_item`. `capacidades` malformada = nenhuma
    (`capacidades.de` filtra). Só `cadastros` que não é lista derruba a leitura."""

    model_config = ConfigDict(extra="allow")

    capacidades: Any = None
    cadastros: list[Any] = Field(default_factory=list)


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
    """kit/pack: `criar` exige lista; `atualizar` com null = não mexer na composição,
    lista não-nula segue as regras do `criar`. `inativar` chega aqui com null."""
    if item.componentes is None and item.acao != "criar":
        return None
    comps = item.componentes or []
    if item.tipo == "kit" or item.tipo == "pack":
        if not 1 <= len(comps) <= MAX_COMPONENTES:
            return f"{item.tipo} exige de 1 a {MAX_COMPONENTES} componentes; veio {len(comps)}"
        for i, c in enumerate(comps):
            if not (math.isfinite(c.quantidade) and 0 < c.quantidade <= MAX_QUANTIDADE):
                return f"componentes[{i}].quantidade fora de (0, {MAX_QUANTIDADE}]"
            if item.erpProdutoId is not None and c.erpProdutoId == item.erpProdutoId:
                return f"componentes[{i}] é o próprio produto"
    return None


def chaves_desconhecidas(bruto: dict) -> list[str]:
    """Nomes de chave de topo que `CadastroPendente` não conhece, ordenados.
    Nome fora de `_NOME_SEGURO` vira "?": a chave veio do Flow e não vai cru ao log."""
    if not isinstance(bruto, dict):
        return []
    conhecidas = CadastroPendente.model_fields.keys()
    return sorted(_parte(k) for k in bruto if k not in conhecidas)


def validar_item(bruto: dict) -> tuple[CadastroPendente | None, str | None, str | None]:
    """`(item, None, None)` se válido; senão `(None, codigo_erro, mensagem)`."""
    if isinstance(bruto, dict) and bruto.get("acao") == "inativar" and "componentes" in bruto:
        # inativar ignora componentes: qualquer valor, sem validar.
        bruto = {**bruto, "componentes": None}
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
    if item.acao == "criar" and item.marcaErp is None:
        return None, "contrato_invalido", "criar exige marcaErp"
    if item.acao == "inativar" and item.marcaErp is not None:
        return None, "contrato_invalido", "inativar exige marcaErp null"
    if item.tipo in ("simples", "material") and item.componentes is not None:
        return None, "contrato_invalido", f"{item.tipo} exige componentes null"

    erro = _erro_componentes(item)
    if erro:
        return None, "componente_invalido", _corta(erro)
    return item, None, None
