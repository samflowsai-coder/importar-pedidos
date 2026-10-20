"""Decisão do produto de um item só por chave exata (spec §2, §5.1).

Função pura: recebe os candidatos que o Fire devolveu para cada chave do item
(EAN e código, já normalizados) e o vínculo de-para, e decide. Nunca casa por
descrição, semelhança, prefixo ou IA, e nunca escolhe entre dois produtos
ativos (o menor SEQ não vence nada).

Ordem das chaves, que é também a fonte registrada: ean → depara → codprod_altern.

R18: chave que se repete no MESMO pedido com descrições diferentes não
discrimina (`chave_repetida`, `desambiguar`); a pessoa escolhe o produto de cada
linha, só naquele pedido (`decidir_escolha`, fonte `escolha_pontual`).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Literal

Estado = Literal[
    "ok",
    "a_identificar",
    "ambiguo",
    "produto_bloqueado",
    "vinculo_invalido",
    "chaves_discordam",
    "chave_repetida",
]
Fonte = Literal["ean", "depara", "codprod_altern", "escolha_pontual"]
Chave = tuple[Literal["codigo", "ean"], str]

# Prioridade quando nenhuma chave deu produto (vinculo_invalido já é final antes).
_PRIORIDADE_PROBLEMA: tuple[Estado, ...] = ("ambiguo", "produto_bloqueado")
MOTIVO_CHAVES_DISCORDAM = (
    "As chaves do item (EAN, código ou vínculo salvo) apontam para produtos diferentes."
)


@dataclass(frozen=True)
class Candidato:
    seq: int
    descricao: str
    codprod_altern: str | None
    bloqueado: bool
    unidade: str | None
    preco_venda: float | None
    ean: str | None = None


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


def motivo_vinculo_invalido(seq: int) -> str:
    return f"O vínculo salvo aponta para o produto {seq}, que não existe mais ou está bloqueado."


def _todos_candidatos(resultados) -> tuple[Candidato, ...]:
    """Candidatos de todas as chaves, na ordem das chaves, sem repetir SEQ."""
    vistos: dict[int, Candidato] = {}
    for _, r in resultados:
        lista = (r,) if isinstance(r, Candidato) else r.candidatos if r is not None else ()
        for c in lista:
            vistos.setdefault(c.seq, c)
    return tuple(vistos.values())


def _por_vinculo(vinculo_seq: int | None, candidato: Candidato | None):
    if vinculo_seq is None:
        return None
    if candidato is None or candidato.bloqueado:
        return _Problema(
            "vinculo_invalido",
            (candidato,) if candidato is not None else (),
            motivo_vinculo_invalido(vinculo_seq),
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

    # R7(a): vínculo inválido é final. A pessoa escolheu um produto que não vale
    # mais; nenhuma outra chave passa por cima disso.
    for _, r in resultados:
        if isinstance(r, _Problema) and r.estado == "vinculo_invalido":
            return DecisaoProduto(r.estado, None, None, r.candidatos, r.motivo)

    produtos: dict[int, tuple[Fonte, Candidato]] = {}
    for fonte, r in resultados:
        if isinstance(r, Candidato):
            produtos.setdefault(r.seq, (fonte, r))
    problemas = [r for _, r in resultados if isinstance(r, _Problema)]

    # R7(b): uma chave com problema só deixa passar o SEQ que está entre os
    # candidatos dela (ex.: EAN ambíguo resolvido pelo vínculo).
    if len(produtos) == 1:
        ((seq, (fonte, escolhido)),) = produtos.items()
        if all(seq in {c.seq for c in p.candidatos} for p in problemas):
            return DecisaoProduto("ok", seq, fonte, (escolhido,), "")
    if produtos:
        return DecisaoProduto(
            "chaves_discordam", None, None, _todos_candidatos(resultados), MOTIVO_CHAVES_DISCORDAM
        )

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


# ── R18: chave repetida no pedido e escolha pontual ──────────────────────────


def descricao_normalizada(descricao: str | None) -> str:
    """Maiúsculas, sem acento, espaços colapsados. Só para comparar linhas do
    MESMO pedido entre si; nunca para casar produto."""
    sem_acento = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", descricao or "")
        if not unicodedata.combining(ch)
    )
    return re.sub(r"\s+", " ", sem_acento).strip().upper()


def chaves_repetidas(
    itens: list[tuple[str | None, str | None, str | None]],
) -> dict[Chave, int]:
    """{chave: nº de linhas} das chaves (já normalizadas) que aparecem em 2+
    itens do pedido com descrições normalizadas DIFERENTES. `itens`: (código,
    EAN, descrição) por item. Mesma chave e mesma descrição em linhas separadas
    (ex.: uma por loja) é a mesma coisa pedida: não entra."""
    linhas: dict[Chave, list[str]] = {}
    for codigo, ean, descricao in itens:
        desc = descricao_normalizada(descricao)
        for chave in (("codigo", codigo), ("ean", ean)):
            if chave[1]:
                linhas.setdefault(chave, []).append(desc)
    return {k: len(d) for k, d in linhas.items() if len(set(d)) > 1}


def motivo_chave_repetida(chave: Chave, n: int) -> str:
    nome = "código" if chave[0] == "codigo" else "EAN"
    return (
        f"O {nome} {chave[1]} aparece em {n} linhas com descrições diferentes; "
        "escolha o produto de cada linha."
    )


def desambiguar(
    decisao: DecisaoProduto,
    *,
    codigo: str | None,
    ean: str | None,
    por_ean: list[Candidato],
    por_codigo: list[Candidato],
    vinculo_codigo: int | None,
    vinculo_ean: int | None,
    repetidas: dict[Chave, int],
) -> DecisaoProduto:
    """Tira o `ok` que só vinha de chave repetida (R18).

    Cada fonte que deu o SEQ decidido carrega a(s) chave(s) do cliente de onde
    veio: o EAN, o código, ou a chave do vínculo salvo (o vínculo do código
    repetido também não separa as linhas). Fonte cuja chave se repete não vale;
    sobrando outra fonte concordante, o item fica `ok` por ela. Sem nenhuma,
    `chave_repetida` com o produto que a chave achava como candidato."""
    if decisao.estado != "ok" or not repetidas:
        return decisao
    seq = decisao.fire_seq
    fontes: list[tuple[Fonte, tuple[Chave, ...]]] = []
    u = _por_chave("EAN", ean, por_ean)
    if isinstance(u, Candidato) and u.seq == seq:
        fontes.append(("ean", (("ean", ean),)))
    chaves_vinculo = tuple(
        chave
        for chave, v in ((("codigo", codigo), vinculo_codigo), (("ean", ean), vinculo_ean))
        if v is not None and v == seq
    )
    if chaves_vinculo:
        fontes.append(("depara", chaves_vinculo))
    u = _por_chave("código", codigo, por_codigo)
    if isinstance(u, Candidato) and u.seq == seq:
        fontes.append(("codprod_altern", (("codigo", codigo),)))
    if not fontes:
        return decisao
    validas = [f for f, chaves in fontes if any(c not in repetidas for c in chaves)]
    if validas:
        return decisao if validas[0] == decisao.fonte else replace(decisao, fonte=validas[0])
    # O motivo cita a chave da fonte que decidia (a primeira na ordem das chaves).
    chave = (dict(fontes).get(decisao.fonte) or fontes[0][1])[0]
    return DecisaoProduto(
        "chave_repetida",
        None,
        None,
        decisao.candidatos,
        motivo_chave_repetida(chave, repetidas[chave]),
    )


def motivo_escolha_invalida(seq: int) -> str:
    return f"A escolha desta linha aponta para o produto {seq}, que não existe mais ou está bloqueado."


def decidir_escolha(seq: int, candidato: Candidato | None) -> DecisaoProduto:
    """Escolha pontual da linha (só neste pedido): `ok` se o SEQ existe e está
    ativo no Fire; sumido ou bloqueado é `vinculo_invalido`, como o vínculo salvo."""
    if candidato is None or candidato.bloqueado:
        return DecisaoProduto(
            "vinculo_invalido",
            None,
            None,
            (candidato,) if candidato is not None else (),
            motivo_escolha_invalida(seq),
        )
    return DecisaoProduto("ok", seq, "escolha_pontual", (candidato,), "")
