from __future__ import annotations

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
    tipo: str  # 'kit' | 'simples' (derivado de PRODUTOS_KIT)


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def extract_produtos(fire_conn, *, apenas_meias: bool = False) -> list[ProdutoFireDTO]:
    """Lê o subconjunto de identidade de PRODUTOS do Fire. Read-only.
    codigo = fire_produto_id = str(SEQ) (o cliente usa o sequencial).
    tipo: 'kit' se o SEQ é pai em PRODUTOS_KIT (IS_KIT=1), senão 'simples'.

    `apenas_meias=True` restringe ao subgrupo MEIAS (flowpcp_catalogo_apenas_meias);
    default False preserva o comportamento atual (PRODUTOS inteiro)."""
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
        out.append(
            ProdutoFireDTO(
                fire_produto_id=seq_s,
                codigo=seq_s,
                nome=_clean(desc) or "",
                unidade=_clean(uni),
                ean=_clean(ean),
                ativo=(str(bloqueado or "").strip().lower() != "sim"),
                tipo=("kit" if is_kit else "simples"),
            )
        )
    return out
