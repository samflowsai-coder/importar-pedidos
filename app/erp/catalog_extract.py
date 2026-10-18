from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.erp.queries import (
    LIST_ENTRADAS_NCM,
    LIST_MATERIAIS,
    LIST_PRODUTOS_CATALOGO,
    LIST_PRODUTOS_CATALOGO_MEIAS,
    LIST_PRODUTOS_COM_NOTA,
    LIST_PRODUTOS_KIT_COMPOSICAO,
    LIST_PRODUTOS_MARCA,
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


@dataclass(frozen=True)
class MaterialFireDTO:
    """Material do Fire (tudo fora do grupo 8). Valores lidos, aparados; a
    validação fica em `materiais_sync`."""

    erp_produto_id: str  # str(SEQ)
    codigo: str  # str(SEQ)
    nome: str
    unidade: str | None
    ean: str | None
    ativo: bool  # BLOQUEADO <> 'Sim'
    ncm_bruto: str | None
    sped_bruto: int | None
    grupo_codigo: int | None
    grupo_nome: str | None


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


def extract_marcas(fire_conn) -> list[tuple[int, str | None]]:
    """PRODUTOS_MARCA inteiro como (CODIGO, MARCA), por código. Read-only. Código e
    nome vão brutos: `catalogo_marcas.filtrar` decide o que o Flow aceita. Erro do
    driver sobe (o sync trata como "marcas não enviadas")."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_PRODUTOS_MARCA)
        rows = cur.fetchall()
    finally:
        cur.close()
    return [(codigo, None if nome is None else str(nome)) for codigo, nome in rows]


def extract_com_nota(fire_conn) -> set[str]:
    """SEQs de produto que aparecem em NOTAPROD (já tiveram nota fiscal). Read-only.
    Erro do driver sobe: o sync trata como "temNotaFiscal ausente em todos"."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_PRODUTOS_COM_NOTA)
        rows = cur.fetchall()
    finally:
        cur.close()
    return {str(cod) for (cod,) in rows if cod is not None}


def extract_materiais(fire_conn) -> list[MaterialFireDTO]:
    """Materiais (GRUPO <> 8), bloqueados incluídos com `ativo=False`. Read-only."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_MATERIAIS)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: list[MaterialFireDTO] = []
    for seq, desc, uni, ean, bloqueado, ncm, sped, grupo, grupo_nome in rows:
        seq_s = str(seq)
        out.append(
            MaterialFireDTO(
                erp_produto_id=seq_s,
                codigo=seq_s,
                nome=_clean(desc) or "",
                unidade=_clean(uni),
                ean=_clean(ean),
                ativo=(str(bloqueado or "").strip().lower() != "sim"),
                ncm_bruto=_clean(ncm),
                sped_bruto=_int_ou_none(sped),
                grupo_codigo=_int_ou_none(grupo),
                grupo_nome=_clean(grupo_nome),
            )
        )
    return out


def extract_ultima_entrada(fire_conn) -> dict[str, tuple[str, date]]:
    """SEQ -> (NCM bruto, data de emissão) da nota de entrada mais recente com NCM
    válido (8 dígitos). Entrada com NCM inválido não esconde uma anterior válida.
    A query vem ordenada por (emissão, nota); em empate de data a última linha
    (maior nota) vence. Read-only."""
    from app.integrations.flowpcp.catalogo_fiscal import normalizar_ncm  # evita import circular

    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_ENTRADAS_NCM)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: dict[str, tuple[str, date]] = {}
    for cod, emissao, ncm in rows:
        ncm_s = _clean(ncm)
        if cod is None or emissao is None or ncm_s is None or normalizar_ncm(ncm_s)[0] is None:
            continue
        seq = str(cod)
        atual = out.get(seq)
        if atual is None or emissao >= atual[1]:
            out[seq] = (ncm_s, emissao)
    return out
