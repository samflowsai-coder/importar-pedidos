from __future__ import annotations

import re
from dataclasses import dataclass

from app.erp.queries import (
    LIST_PRODUTOS_CATALOGO,
    LIST_PRODUTOS_CATALOGO_MEIAS,
    LIST_PRODUTOS_KIT_COMPOSICAO,
)


@dataclass(frozen=True)
class ProdutoFireDTO:
    fire_produto_id: str  # str(SEQ) — PK durável imutável
    codigo: str  # str(SEQ) — o código usado é o sequencial
    nome: str  # DESCRICAO
    unidade: str | None  # UNIDADE
    ean: str | None  # CODIGO_EAN13
    ativo: bool  # BLOQUEADO <> 'Sim'
    tipo: str  # 'pack' | 'kit' | 'simples' — ver `_tipo`
    codigo_alternativo: str | None = None  # CODPROD_ALTERN (código do par no Apontaê, se par)
    tem_composicao: bool = False  # IS_KIT: tem linha em PRODUTOS_KIT como pai
    # Campos fiscais/cadastrais (catalogo_fiscal valida; aqui só o valor lido, aparado)
    ncm_bruto: str | None = None  # CLASSIF_FISCAL
    sped_bruto: int | None = None  # CODITEMSPED
    grupo_codigo: int | None = None  # GRUPO
    grupo_nome: str | None = None  # GRUPO_PRODUTOS.GRUPO
    codigo_nfe: str | None = None  # CODPARANFE
    cor: str | None = None  # DESCRICAO2


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


_PACK = re.compile(r"\bPACKS?\b")
_KIT = re.compile(r"\bKITS?\b|\bKIT\d")


def _tipo(is_kit, unidade: str | None, nome: str) -> str:
    """Para o Flow, simples = par: vira OP. Kit/pack sem composição fica fora do
    pool ("sem composição cadastrada"), então na dúvida é kit, nunca simples.
    Composição no Fire (PRODUTOS_KIT) não basta: em 15/10/2026, 321 dos 590 kits
    e packs da MM não tinham, e 90 deles nem têm unidade KIT (`unid`, `KG`)."""
    uni = (unidade or "").upper()
    nome_up = nome.upper()
    if uni == "PACK" or _PACK.search(nome_up):
        return "pack"
    if is_kit or uni == "KIT" or _KIT.search(nome_up):
        return "kit"
    return "simples"


def _int_ou_none(v) -> int | None:
    """Inteiro do Fire ou None; texto não numérico vira None (a regra de
    `catalogo_fiscal` decide o que fazer com vazio)."""
    s = _clean(v)
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def extract_produtos(fire_conn, *, apenas_meias: bool = False) -> list[ProdutoFireDTO]:
    """Lê o subconjunto de identidade de PRODUTOS do Fire. Read-only.
    codigo = fire_produto_id = str(SEQ) (o cliente usa o sequencial).
    tipo: `_tipo` — pack/kit pela composição, pela unidade ou pelo nome.

    `apenas_meias=True` (flowpcp_catalogo_apenas_meias) restringe ao que a fábrica
    vende: subgrupo MEIAS + produto em pedido aberto ou dos últimos 12 meses
    + os componentes desses produtos (`LIST_PRODUTOS_CATALOGO_MEIAS`);
    False = PRODUTOS inteiro."""
    query = LIST_PRODUTOS_CATALOGO_MEIAS if apenas_meias else LIST_PRODUTOS_CATALOGO
    cur = fire_conn.cursor()
    try:
        cur.execute(query)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: list[ProdutoFireDTO] = []
    for (
        seq,
        desc,
        uni,
        ean,
        bloqueado,
        is_kit,
        alternativo,
        ncm,
        sped,
        grupo,
        grupo_nome,
        nfe,
        cor,
    ) in rows:
        seq_s = str(seq)
        nome = _clean(desc) or ""
        out.append(
            ProdutoFireDTO(
                fire_produto_id=seq_s,
                codigo=seq_s,
                nome=nome,
                unidade=_clean(uni),
                ean=_clean(ean),
                ativo=(str(bloqueado or "").strip().lower() != "sim"),
                tipo=_tipo(is_kit, _clean(uni), nome),
                codigo_alternativo=_clean(alternativo),
                tem_composicao=bool(is_kit),
                ncm_bruto=_clean(ncm),
                sped_bruto=_int_ou_none(sped),
                grupo_codigo=_int_ou_none(grupo),
                grupo_nome=_clean(grupo_nome),
                codigo_nfe=_clean(nfe),
                cor=_clean(cor),
            )
        )
    return out


def extract_composicao(fire_conn) -> dict[str, list[tuple[str | None, float | None]]]:
    """PRODUTOS_KIT inteiro, agrupado por pai: {SEQ do pai: [(SEQ do filho, QTD)]},
    na ordem de cadastro. Read-only. Erro do driver sobe: o sync trata como
    "composição ausente em todos" (nunca como "sem composição"). Filho ou QTD
    nulos ficam None para a regra omitir o kit; pai nulo não tem a quem pertencer."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_PRODUTOS_KIT_COMPOSICAO)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: dict[str, list[tuple[str | None, float | None]]] = {}
    for pai, filho, qtd in rows:
        if pai is None:
            continue
        out.setdefault(str(pai), []).append(
            (None if filho is None else str(filho), None if qtd is None else float(qtd))
        )
    return out
