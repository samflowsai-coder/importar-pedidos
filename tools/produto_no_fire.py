"""Rastro de um SEQ de produto em toda tabela do Fire que tem coluna de produto (P0).

SÓ LEITURA: conexão `connect_somente_leitura` (transação READ ONLY) e todo SQL
passa por `so_select`. Tabelas e colunas vêm de `RDB$RELATION_FIELDS` (nunca de
argumento), são validadas contra `^[A-Z][A-Z0-9_$]*$` e entram entre aspas
duplas; o SEQ vai como parâmetro. Tabelas de sistema (RDB$, MON$, SEC$) ficam
de fora e cada tabela devolve no máximo 50 linhas.

Serve para o diff da P0: o que a tela do Fire gravou para um produto novo.

    .venv/bin/python tools/produto_no_fire.py <slug> --seq 12345
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# O catálogo, o guarda de SELECT e a validação de identificador moram em um lugar só.
from app.erp.produto_referencias import (  # noqa: E402
    PREFIXOS_SISTEMA as _PREFIXOS_SISTEMA,
)
from app.erp.produto_referencias import (  # noqa: E402
    SQL_CATALOGO,
    TIPOS_INTEIROS,
    _ident,
    so_select,
)

COLUNAS_PRODUTO = frozenset(
    {"SEQ", "CODPRODUTO", "CODPRODUTO_PAI", "PRODUTO", "CODPROD", "SEQPRODUTO", "COD_PRODUTO"}
)
LIMITE_LINHAS = 50
MAX_TEXTO = 200


def escolher_alvos(catalogo: list[tuple[str, str, int]]) -> list[tuple[str, str]]:
    """(tabela, coluna) do catálogo (tabela, coluna, tipo) cuja coluna é de produto
    e de tipo inteiro, sem tabelas de sistema."""
    alvos = {
        (t, c)
        for t, c, tipo in catalogo
        if c in COLUNAS_PRODUTO and tipo in TIPOS_INTEIROS and not t.startswith(_PREFIXOS_SISTEMA)
    }
    return sorted(alvos)


def montar_sql(tabela: str, coluna: str) -> str:
    return f"SELECT FIRST {LIMITE_LINHAS} * FROM {_ident(tabela)} WHERE {_ident(coluna)} = ?"


def _texto(valor) -> str:
    if isinstance(valor, bytes):
        return f"<{len(valor)} bytes>"
    texto = repr(valor)
    return texto if len(texto) <= MAX_TEXTO else texto[:MAX_TEXTO] + "…"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="slug do ambiente (environments)")
    ap.add_argument("--seq", type=int, required=True)
    args = ap.parse_args(argv)

    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(args.slug)
    if env is None:
        print(f"ambiente '{args.slug}' não encontrado.", file=sys.stderr)
        return 1

    with FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env)) as conn:
        cur = conn.cursor()
        cur.execute(so_select(SQL_CATALOGO))
        alvos = escolher_alvos([(r[0], r[1], r[2]) for r in cur.fetchall()])
        for tabela, coluna in alvos:
            # Uma tabela problemática (permissão, view quebrada, identificador
            # fora do padrão) vira aviso e não derruba o rastro inteiro.
            try:
                cur.execute(so_select(montar_sql(tabela, coluna)), (args.seq,))
                nomes = [d[0].strip() for d in cur.description]
                linhas = cur.fetchall()
            except Exception as e:  # noqa: BLE001
                print(f"AVISO {tabela}.{coluna}: {type(e).__name__}", file=sys.stderr)
                continue
            if not linhas:
                continue
            print(f"\n==== {tabela}.{coluna} = {args.seq}: {len(linhas)} linha(s) ====")
            for i, linha in enumerate(linhas, 1):
                print(f"-- linha {i}")
                for nome, valor in zip(nomes, linha):
                    print(f"   {nome}: {_texto(valor)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
