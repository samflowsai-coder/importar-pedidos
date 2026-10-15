from __future__ import annotations

import re
from dataclasses import dataclass

from app.erp.queries import LIST_PRODUTOS_CATALOGO, LIST_PRODUTOS_CATALOGO_MEIAS


@dataclass(frozen=True)
class ProdutoFireDTO:
    fire_produto_id: str  # str(SEQ) — PK durável imutável
    codigo: str  # str(SEQ) — o código usado é o sequencial
    nome: str  # DESCRICAO
    unidade: str | None  # UNIDADE
    ean: str | None  # CODIGO_EAN13
    ativo: bool  # BLOQUEADO <> 'Sim'
    tipo: str  # 'pack' | 'kit' | 'simples' — ver `_tipo`


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
    for seq, desc, uni, ean, bloqueado, is_kit in rows:
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
            )
        )
    return out
