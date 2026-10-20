"""Decisão do produto de um item só por chave exata (spec §2, §5.1).

Função pura: recebe os candidatos que o Fire devolveu para cada chave do item
(EAN e código, já normalizados) e o vínculo de-para, e decide. Nunca casa por
descrição, semelhança, prefixo ou IA, e nunca escolhe entre dois produtos
ativos (o menor SEQ não vence nada).

Ordem das chaves, que é também a fonte registrada: ean → depara → codprod_altern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Estado = Literal[
    "ok",
    "a_identificar",
    "ambiguo",
    "produto_bloqueado",
    "vinculo_invalido",
    "chaves_discordam",
]
Fonte = Literal["ean", "depara", "codprod_altern"]

# Prioridade quando nenhuma chave deu produto.
_PRIORIDADE_PROBLEMA: tuple[Estado, ...] = ("vinculo_invalido", "ambiguo", "produto_bloqueado")


@dataclass(frozen=True)
class Candidato:
    seq: int
    descricao: str
    codprod_altern: str | None
    bloqueado: bool
    unidade: str | None
    preco_venda: float | None


@dataclass(frozen=True)
class DecisaoProduto:
    estado: Estado
    fire_seq: int | None
    fonte: str | None
    candidatos: tuple[Candidato, ...]
    motivo: str


@dataclass(frozen=True)
class _Problema:
    estado: Estado
    candidatos: tuple[Candidato, ...]
    motivo: str


def _por_chave(nome: str, valor: str | None, candidatos: list[Candidato]):
    """Candidato único ativo, um problema, ou None (nenhum candidato)."""
    if not candidatos:
        return None
    ativos = [c for c in candidatos if not c.bloqueado]
    if len(ativos) == 1:
        return ativos[0]
    if ativos:
        return _Problema(
            "ambiguo",
            tuple(ativos),
            f"O {nome} {valor} está em {len(ativos)} produtos ativos.",
        )
    return _Problema(
        "produto_bloqueado",
        tuple(candidatos),
        f"O produto {candidatos[0].seq} está bloqueado no Fire.",
    )


def _por_vinculo(vinculo_seq: int | None, candidato: Candidato | None):
    if vinculo_seq is None:
        return None
    if candidato is None or candidato.bloqueado:
        return _Problema(
            "vinculo_invalido",
            (candidato,) if candidato is not None else (),
            f"O vínculo salvo aponta para o produto {vinculo_seq}, "
            "que não existe mais ou está bloqueado.",
        )
    return candidato


def decidir(
    *,
    codigo: str | None,
    ean: str | None,
    por_ean: list[Candidato],
    por_codigo: list[Candidato],
    vinculo_seq: int | None,
    vinculo_candidato: Candidato | None,
) -> DecisaoProduto:
    resultados: list[tuple[Fonte, Candidato | _Problema | None]] = [
        ("ean", _por_chave("EAN", ean, por_ean)),
        ("depara", _por_vinculo(vinculo_seq, vinculo_candidato)),
        ("codprod_altern", _por_chave("código", codigo, por_codigo)),
    ]

    produtos: dict[int, tuple[Fonte, Candidato]] = {}
    for fonte, r in resultados:
        if isinstance(r, Candidato):
            produtos.setdefault(r.seq, (fonte, r))

    if len(produtos) == 1:
        ((seq, (fonte, _)),) = produtos.items()
        return DecisaoProduto("ok", seq, fonte, (), "")
    if len(produtos) > 1:
        return DecisaoProduto(
            "chaves_discordam",
            None,
            None,
            tuple(c for _, c in produtos.values()),
            "O EAN e o código apontam para produtos diferentes.",
        )

    problemas = [r for _, r in resultados if isinstance(r, _Problema)]
    for estado in _PRIORIDADE_PROBLEMA:
        for p in problemas:
            if p.estado == estado:
                return DecisaoProduto(p.estado, None, None, p.candidatos, p.motivo)

    motivo = (
        f"O cliente mandou o código {codigo}, que não está cadastrado no Fire."
        if codigo
        else "O item veio sem código e sem EAN cadastrado no Fire."
    )
    return DecisaoProduto("a_identificar", None, None, (), motivo)
