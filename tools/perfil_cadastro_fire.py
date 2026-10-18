"""Perfil das colunas de PRODUTOS nos últimos cadastros de par e de kit (P0).

SÓ LEITURA: conexão `connect_somente_leitura` (transação READ ONLY, o próprio
Firebird recusa escrita) e todo SQL passa por `so_select`. Nomes de coluna vêm
de `RDB$RELATION_FIELDS`, nunca de argumento, e ainda assim são validados
antes de entrar no texto do SQL. Os SEQs vão como parâmetro.

Para os últimos N SEQs de cada tipo no grupo 8 (par = unidade PAR; kit = SEQ
que é CODPRODUTO_PAI em PRODUTOS_KIT), mostra por coluna quantos têm valor
não nulo e não vazio e o valor mais comum. É a base para comparar com o que a
tela do Fire grava (PADROES_P0 em app/erp/cadastro_fire.py).

    .venv/bin/python tools/perfil_cadastro_fire.py mm --ultimos 30
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

GRUPO = 8
_SO_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_IDENT = re.compile(r"^[A-Z][A-Z0-9_$]*$")


def so_select(sql: str) -> str:
    assert _SO_SELECT.match(sql), "ferramenta só leitura: apenas SELECT"
    return sql


def _ident(nome: str) -> str:
    if not _IDENT.fullmatch(nome):
        raise ValueError(f"identificador inválido: {nome!r}")
    return f'"{nome}"'


def _preenchido(valor) -> bool:
    if valor is None:
        return False
    if isinstance(valor, str) and not valor.strip():
        return False
    return True


def perfil(colunas: list[str], linhas: list[dict]) -> list[dict]:
    """Por coluna: quantas linhas têm valor não nulo e não vazio, e o mais comum."""
    saida = []
    for coluna in colunas:
        valores = [ln.get(coluna) for ln in linhas if _preenchido(ln.get(coluna))]
        mais_comum = Counter(valores).most_common(1)[0][0] if valores else None
        saida.append({"coluna": coluna, "preenchidos": len(valores), "mais_comum": mais_comum})
    return saida


def _colunas_produtos(cur) -> list[str]:
    cur.execute(
        so_select(
            "SELECT TRIM(RDB$FIELD_NAME) FROM RDB$RELATION_FIELDS "
            "WHERE TRIM(RDB$RELATION_NAME) = ? ORDER BY RDB$FIELD_POSITION"
        ),
        ("PRODUTOS",),
    )
    return [r[0] for r in cur.fetchall()]


def _ultimos_seqs(cur, filtro: str, n: int) -> list[int]:
    # `n` é int validado pelo argparse; inline com int() (o FB 2.x aceitaria `?`).
    cur.execute(
        so_select(
            f"SELECT FIRST {int(n)} SEQ FROM PRODUTOS WHERE GRUPO = ? AND {filtro} ORDER BY SEQ DESC"
        ),
        (GRUPO,),
    )
    return [r[0] for r in cur.fetchall()]


def _linhas(cur, colunas: list[str], seqs: list[int]) -> list[dict]:
    if not seqs:
        return []
    lista = ", ".join(_ident(c) for c in colunas)
    marcas = ", ".join("?" for _ in seqs)
    cur.execute(so_select(f"SELECT {lista} FROM PRODUTOS WHERE SEQ IN ({marcas})"), tuple(seqs))
    return [dict(zip(colunas, r)) for r in cur.fetchall()]


def _imprimir(titulo: str, seqs: list[int], resultado: list[dict]) -> None:
    print(
        f"\n==== {titulo}: {len(seqs)} produtos (SEQ {min(seqs, default='-')}..{max(seqs, default='-')}) ===="
    )
    for p in resultado:
        mc = repr(p["mais_comum"]) if p["mais_comum"] is not None else "-"
        print(f"{p['coluna']:<28} {p['preenchidos']:>4}/{len(seqs):<4} {mc[:80]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="slug do ambiente (environments)")
    ap.add_argument("--ultimos", type=int, default=30)
    args = ap.parse_args(argv)
    if args.ultimos < 1:
        ap.error("--ultimos deve ser >= 1")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(args.slug)
    if env is None:
        print(f"ambiente '{args.slug}' não encontrado.", file=sys.stderr)
        return 1

    with FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env)) as conn:
        cur = conn.cursor()
        colunas = _colunas_produtos(cur)
        tipos = {
            "PAR (unidade PAR)": "TRIM(UNIDADE) = 'PAR'",
            "KIT (pai em PRODUTOS_KIT)": "SEQ IN (SELECT CODPRODUTO_PAI FROM PRODUTOS_KIT)",
        }
        for titulo, filtro in tipos.items():
            seqs = _ultimos_seqs(cur, filtro, args.ultimos)
            _imprimir(titulo, seqs, perfil(colunas, _linhas(cur, colunas, seqs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
