"""Medições SÓ LEITURA no banco do ERP para a SP1 (docs/ai/modules/erp_eventos.md).

Garantias, nesta ordem:
1. só aceita SQL que começa com SELECT (recusa antes de mandar ao banco);
2. a transação é READ ONLY (o próprio Firebird recusa escrita);
3. nunca grava nada em lugar nenhum — só imprime.

Usa `firebirdsql` (Python puro): funciona do Mac, onde o `fdb` do Portal não
fala com a fbclient do Firebird 5.

    .venv/bin/python tools/erp_medir.py --host 127.0.0.1 --porta 3056 \\
        --banco /firebird/data/mm.fdb contrato

Senha em FB_PASSWORD (nunca na linha de comando).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import firebirdsql

_SO_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

MEDICOES_CONTRATO = {
    "pedidos abertos (STATUS fora de FATURADO/CANCELADO)": """
        SELECT COUNT(*) FROM CAB_VENDAS WHERE TRIM(STATUS) NOT IN ('FATURADO', 'CANCELADO')""",
    "status distintos dos abertos": """
        SELECT TRIM(STATUS), COUNT(*) FROM CAB_VENDAS
        WHERE TRIM(STATUS) NOT IN ('FATURADO', 'CANCELADO') GROUP BY 1""",
    "itens abertos": """
        SELECT COUNT(*) FROM CORPO_VENDAS I JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO')""",
    "itens abertos sem CODPRODUTO (vão com codProduto null)": """
        SELECT COUNT(*) FROM CORPO_VENDAS I JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO') AND I.CODPRODUTO IS NULL""",
    "itens abertos com QTD nula (pedido fica fora do envio)": """
        SELECT COUNT(*) FROM CORPO_VENDAS I JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO') AND I.QTD IS NULL""",
    "maior DESCRICAO / UNID / PEDIDO_CLIENTE (limites 500 / 10 / 120)": """
        SELECT MAX(CHAR_LENGTH(TRIM(I.DESCRICAO))), MAX(CHAR_LENGTH(TRIM(I.UNID))),
               MAX(CHAR_LENGTH(TRIM(V.PEDIDO_CLIENTE)))
        FROM CORPO_VENDAS I JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO')""",
    "maior número de itens num pedido aberto (unidade do lote: máx. 499 itens)": """
        SELECT FIRST 1 COUNT(*) FROM CORPO_VENDAS I JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO') GROUP BY I.CODVENDA ORDER BY 1 DESC""",
    "abertos cujo cliente não tem CNPJ de 14 dígitos (clienteErpCnpj null)": """
        SELECT COUNT(*) FROM CAB_VENDAS V LEFT JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO')
          AND CHAR_LENGTH(REPLACE(REPLACE(REPLACE(REPLACE(COALESCE(C.CPF_CNPJ, ''), '.', ''), '/', ''), '-', ''), ' ', '')) <> 14""",
    "itens abertos já com nota parcial (CORPO_VENDAS_NFS)": """
        SELECT COUNT(DISTINCT N.COD_ITEMVENDA) FROM CORPO_VENDAS_NFS N
        JOIN CORPO_VENDAS I ON I.CODIGO = N.COD_ITEMVENDA JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
        WHERE TRIM(V.STATUS) NOT IN ('FATURADO', 'CANCELADO')""",
}


def consultar(conn, sql: str) -> list[tuple]:
    if not _SO_SELECT.match(sql):
        raise SystemExit(f"recusado: só SELECT — {sql.strip()[:60]!r}")
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return cur.fetchall()
    finally:
        cur.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", required=True)
    ap.add_argument("--porta", type=int, default=3050)
    ap.add_argument("--banco", required=True, help="caminho do .fdb NO SERVIDOR")
    ap.add_argument("--charset", default="ISO8859_1")
    ap.add_argument("medicao", choices=["contrato"])
    args = ap.parse_args()
    senha = os.environ.get("FB_PASSWORD")
    if not senha:
        print("exporte FB_PASSWORD", file=sys.stderr)
        return 2
    conn = firebirdsql.connect(
        host=args.host,
        port=args.porta,
        database=args.banco,
        user="SYSDBA",
        password=senha,
        charset=args.charset,
    )
    conn.set_isolation_level(firebirdsql.ISOLATION_LEVEL_READ_COMMITED_RO)
    try:
        for titulo, sql in MEDICOES_CONTRATO.items():
            print(f"{titulo}: {consultar(conn, sql)}")
    finally:
        conn.rollback()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
