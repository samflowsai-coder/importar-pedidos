#!/usr/bin/env python3
"""Regressão da chave exata contra pedidos reais já importados. SÓ LEITURA.

Relê cada arquivo original (PDF/XLS/XLSX) pelo `pipeline.process` com o LLM
DESLIGADO, roda o `check_order` novo (vínculos v2 do `--db`, Fire em leitura),
acha o pedido que o importador gravou no Fire e compara, item a item, o SEQ
decidido com o SEQ gravado:

  ok_igual           decidido == gravado no Fire
  ok_seq_diferente   decidido != gravado  (o balde PERIGOSO: tem que dar zero)
  pendente           o check novo não decide (fica para a operação vincular)
  sem_pedido_fire    o pedido não foi achado no Fire
  (por arquivo)      sem_parse = não leu o arquivo; erro_check = Fire fora

Não escreve em nada: o SQLite do ambiente abre `mode=ro`; o Fire entra com
transação READ ONLY e uma guarda que só deixa passar SELECT/WITH; o intercompany
fica desligado (consultaria o Fire da revenda); nenhuma chamada de LLM.

  FB_USER=SYSDBA FB_PASSWORD=... .venv/bin/python tools/regressao_chave_exata.py \\
      --recebidos <dir dos originais> --db <app_state_mm.db> --slug mm \\
      --fb-host 192.168.15.4 --fb-path 'C:\\Fire\\MM_CONFECCAO.FDB' \\
      --saida regressao-mm.json

Ligação ao pedido do Fire: CAB_VENDAS por PEDIDO_CLIENTE (variantes do número)
+ CNPJ do cliente + janela de 90 dias antes da importação + MECANICO = 99
(criado pelo importador); a mesma chave dupla da reconciliação.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

# Rodando como script, a raiz do projeto não está no sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.erp import queries  # noqa: E402
from app.erp.fire_reconcile import (  # noqa: E402
    _TAMANHO_PEDIDO_CLIENTE,
    Candidato,
    _decidir_candidato,
    _indexar_por_numero,
    _linhas_do_candidato,
)
from app.erp.numero_pedido import variantes  # noqa: E402
from app.models.order import Order  # noqa: E402

MECANICO_IMPORTADOR = 99
BALDES_ITEM = ("ok_igual", "ok_seq_diferente", "pendente", "sem_pedido_fire")
BLOCO = 200

_LEITURA = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


# ── Guarda de leitura ────────────────────────────────────────────────────────
def so_leitura(sql: str) -> str:
    """Só SELECT/WITH, e uma instrução só. `raise`, não `assert`: com `-O` o
    assert some e a guarda junto."""
    corpo = (sql or "").strip().rstrip(";")
    if not _LEITURA.match(corpo) or ";" in corpo:
        raise RuntimeError("ferramenta só leitura: apenas SELECT/WITH")
    return sql


class CursorSoLeitura:
    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def execute(self, sql: str, params: Any = ()) -> Any:
        so_leitura(sql)
        return self._cur.execute(sql, params)

    def __getattr__(self, nome: str) -> Any:
        if nome in ("executemany", "callproc"):
            raise RuntimeError("ferramenta só leitura")
        return getattr(self._cur, nome)

    def __iter__(self) -> Iterator:
        return iter(self._cur)


class ConexaoSoLeitura:
    """Conexão do Fire (transação READ ONLY) que só entrega cursores guardados.
    Sai sempre por rollback."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def cursor(self) -> CursorSoLeitura:
        return CursorSoLeitura(self._conn.cursor())

    def __enter__(self) -> ConexaoSoLeitura:
        return self

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            self._conn.rollback()
        with contextlib.suppress(Exception):
            self._conn.close()

    def commit(self) -> None:
        raise RuntimeError("ferramenta só leitura: sem commit")


def abrir_fire(
    *, host: str, path: str, port: int, user: str, password: str, charset: str
) -> ConexaoSoLeitura:
    import firebirdsql  # driver Python puro: roda do Mac, sem fbclient

    conn = firebirdsql.connect(
        host=host, port=port, database=path, user=user, password=password, charset=charset
    )
    conn.set_isolation_level(firebirdsql.ISOLATION_LEVEL_READ_COMMITED_RO)
    return ConexaoSoLeitura(conn)


# ── Fire: pedido e itens gravados pelo importador ────────────────────────────
def sql_pedidos_por_numero(n: int) -> str:
    """(PEDIDO_CLIENTE, CODIGO, STATUS, DATA_PEDIDO, CLIENTE.CODIGO, CPF_CNPJ, MECANICO)
    — as seis primeiras na ordem da reconciliação; MECANICO no fim."""
    if n < 1:
        raise ValueError("IN () é SQL inválida no Firebird: n >= 1")
    marcas = ", ".join("?" for _ in range(n))
    return (
        "SELECT TRIM(V.PEDIDO_CLIENTE), V.CODIGO, V.STATUS, V.DATA_PEDIDO, "
        "C.CODIGO, TRIM(C.CPF_CNPJ), V.MECANICO "
        "FROM CAB_VENDAS V JOIN CADASTRO C ON C.CODIGO = V.CLIENTE "
        f"WHERE TRIM(V.PEDIDO_CLIENTE) IN ({marcas})"
    )


class FireLeitura:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def pedidos_por_numero(self, numeros: list[str]) -> list[tuple]:
        linhas: list[tuple] = []
        for i in range(0, len(numeros), BLOCO):
            bloco = numeros[i : i + BLOCO]
            cur = self._conn.cursor()
            try:
                cur.execute(sql_pedidos_por_numero(len(bloco)), bloco)
                linhas.extend(cur.fetchall())
            finally:
                cur.close()
        return linhas

    def seqs_do_pedido(self, codvenda: int) -> list[int | None]:
        """CODPRODUTO (= SEQ) dos itens, na ordem em que o importador gravou."""
        cur = self._conn.cursor()
        try:
            cur.execute(queries.itens_erp_por_pedido_sql(1), (codvenda,))
            itens = sorted(cur.fetchall(), key=lambda r: r[0])
        finally:
            cur.close()
        return [int(r[2]) if r[2] is not None else None for r in itens]


# ── Ligação e comparação (puras) ─────────────────────────────────────────────
def candidato_do_pedido(order: Order, *, data_pedido: str | None) -> Candidato:
    lojas = tuple(dict.fromkeys(i.delivery_cnpj for i in order.items if i.delivery_cnpj))
    return Candidato(
        import_id=order.source_file or order.header.order_number or "",
        numero=order.header.order_number or "",
        cliente_codigo=None,
        cnpj_header=order.header.customer_cnpj,
        cnpjs_entrega=lojas,
        data_pedido=data_pedido,
    )


def _eh_importador(mecanico: Any) -> bool:
    return str(mecanico).strip() == str(MECANICO_IMPORTADOR)


def ligar_pedido(candidato: Candidato, linhas: list[tuple]) -> int | None:
    """CAB_VENDAS.CODIGO do pedido que o importador gravou para o candidato, ou
    None. Só linhas com MECANICO = 99; resto da decisão é o da reconciliação."""
    do_importador = [linha[:6] for linha in linhas if _eh_importador(linha[6])]
    achado = _decidir_candidato(
        candidato, _linhas_do_candidato(candidato, _indexar_por_numero(do_importador))
    )
    return achado.fire_codigo if achado is not None else None


def comparar_itens(itens_check: list[dict], seqs_fire: list[int | None]) -> list[dict]:
    """Um registro por item: ok_igual / ok_seq_diferente / pendente.

    Pareia por posição (o importador grava na ordem do pedido) e, no que sobrar,
    pelo conjunto de SEQs do pedido no Fire; cada SEQ do Fire é consumido uma
    vez. `pareamento` diz qual dos dois valeu. Sem pareamento certo (contagens
    diferentes), um SEQ ausente do Fire é marcado `ok_seq_diferente`: o lado
    seguro é mostrar para um humano olhar."""
    produtos = [it.get("produto") or {} for it in itens_check]
    restantes = Counter(s for s in seqs_fire if s is not None)
    mesma_contagem = len(produtos) == len(seqs_fire)
    out: list[dict] = []
    for i, p in enumerate(produtos):
        decidido = p.get("fire_seq") if p.get("estado") == "ok" else None
        out.append(
            {
                "indice": i,
                "estado": p.get("estado"),
                "seq_decidido": decidido,
                "seq_fire": seqs_fire[i] if mesma_contagem else None,
                "status": "pendente" if decidido is None else None,
                "pareamento": None,
            }
        )
    for r in out:  # 1) posição
        if r["status"] is None and mesma_contagem and r["seq_decidido"] == r["seq_fire"]:
            r["status"], r["pareamento"] = "ok_igual", "posicao"
            restantes[r["seq_decidido"]] -= 1
    for r in out:  # 2) conjunto
        if r["status"] is not None:
            continue
        r["pareamento"] = "conjunto"
        if restantes[r["seq_decidido"]] > 0:
            restantes[r["seq_decidido"]] -= 1
            r["status"] = "ok_igual"
        else:
            r["status"] = "ok_seq_diferente"
    return out


def executar(
    pedidos: dict[str, Order | None],
    *,
    checar: Callable[[Order], dict],
    fire: Any,
    datas: dict[str, str],
) -> dict:
    """Roda a regressão sobre `pedidos` (arquivo -> Order ou None se não leu)."""
    totais: Counter = Counter({b: 0 for b in (*BALDES_ITEM, "sem_parse", "erro_check")})
    arquivos: list[dict] = []
    perigosos: list[dict] = []
    for nome, order in sorted(pedidos.items()):
        if order is None:
            totais["sem_parse"] += 1
            arquivos.append({"arquivo": nome, "resultado": "sem_parse", "itens": []})
            continue
        check = checar(order)
        if not check.get("available"):
            totais["erro_check"] += 1
            arquivos.append(
                {"arquivo": nome, "resultado": "erro_check", "motivo": check.get("reason"),
                 "itens": []}
            )
            continue
        cand = candidato_do_pedido(order, data_pedido=datas.get(nome))
        numeros = [
            v for v in variantes(cand.numero) if len(v) <= _TAMANHO_PEDIDO_CLIENTE
        ]
        codigo = ligar_pedido(cand, fire.pedidos_por_numero(numeros)) if numeros else None
        itens_check = check.get("items") or []
        if codigo is None:
            itens = [
                {"indice": i, "status": "sem_pedido_fire",
                 "estado": (it.get("produto") or {}).get("estado"),
                 "seq_decidido": (it.get("produto") or {}).get("fire_seq"),
                 "seq_fire": None, "pareamento": None}
                for i, it in enumerate(itens_check)
            ]
        else:
            itens = comparar_itens(itens_check, fire.seqs_do_pedido(codigo))
        for it in itens:
            totais[it["status"]] += 1
            if it["status"] == "ok_seq_diferente":
                perigosos.append({"arquivo": nome, "pedido": cand.numero,
                                  "fire_codigo": codigo, **it})
        arquivos.append(
            {"arquivo": nome, "resultado": "comparado" if codigo else "sem_pedido_fire",
             "pedido": cand.numero, "fire_codigo": codigo, "itens": itens}
        )
    return {
        "ok": totais["ok_seq_diferente"] == 0 and totais["erro_check"] == 0,
        "totais": dict(totais),
        "perigosos": perigosos,
        "arquivos": arquivos,
    }


# ── Costuras (só no main) ────────────────────────────────────────────────────
@contextlib.contextmanager
def _costuras(*, db_path: Path, fire_conn: ConexaoSoLeitura) -> Iterator[None]:
    """Aponta o check novo para as cópias de leitura: vínculos v2 do `--db`
    (SQLite `mode=ro`), a conexão ÚNICA do Fire (só leitura; o check não a fecha),
    sem intercompany e sem LLM."""

    @contextlib.contextmanager
    def env_connect_ro(_slug: str) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def connect_fire(_self: object, _cfg: dict) -> Iterator[ConexaoSoLeitura]:
        yield fire_conn  # sem `with`: quem fecha é o main, uma vez só

    from app import pipeline
    from app.erp.connection import FirebirdConnection
    from app.integrations.flowpcp import intercompany
    from app.persistence import environments_repo, router

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(router, "env_connect", env_connect_ro))
        stack.enter_context(
            mock.patch.object(
                environments_repo, "to_fb_config", lambda _env: {"path": "somente-leitura"}
            )
        )
        stack.enter_context(
            mock.patch.object(FirebirdConnection, "connect_with_config", connect_fire)
        )
        stack.enter_context(mock.patch.object(intercompany, "resolucao_para", lambda *a, **k: None))
        stack.enter_context(mock.patch.object(pipeline._llm, "parse", lambda *a, **k: None))
        yield


def reler_arquivos(diretorio: str) -> dict[str, Order | None]:
    from app import pipeline
    from app.ingestion.file_loader import FileLoader

    out: dict[str, Order | None] = {}
    for arq in FileLoader().load_files(diretorio):
        try:
            out[arq.path.name] = pipeline.process(arq)
        except Exception as exc:  # noqa: BLE001 — um arquivo ruim não derruba a regressão
            print(f"  não leu {arq.path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            out[arq.path.name] = None
    return out


def datas_de_importacao(db_path: Path) -> dict[str, str]:
    """source_filename -> data (YYYY-MM-DD) da importação mais recente."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT source_filename, MAX(imported_at) FROM imports GROUP BY source_filename"
        ).fetchall()
    finally:
        conn.close()
    return {nome: (quando or "")[:10] for nome, quando in rows if quando}


def _imprimir(out: dict) -> None:
    t = out["totais"]
    print("Totais por item:")
    for b in BALDES_ITEM:
        print(f"  {b}: {t.get(b, 0)}")
    print(f"Arquivos: sem_parse={t.get('sem_parse', 0)} erro_check={t.get('erro_check', 0)}")
    if out["perigosos"]:
        print("\nok_seq_diferente (decidiu um SEQ diferente do gravado no Fire):")
        for p in out["perigosos"]:
            print(
                f"  {p['arquivo']} pedido {p['pedido']} item {p['indice']}: "
                f"decidido {p['seq_decidido']} x Fire {p['seq_fire']} "
                f"(Fire CAB {p['fire_codigo']}, pareamento {p['pareamento']})"
            )
    else:
        print("\nok_seq_diferente: nenhum.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--recebidos", required=True, help="pasta com os arquivos originais")
    ap.add_argument("--db", required=True, help="app_state_<slug>.db (cópia; abre só leitura)")
    ap.add_argument("--slug", required=True, choices=["mm", "nasmar"])
    ap.add_argument("--fb-host", required=True)
    ap.add_argument("--fb-path", required=True, help="caminho do .fdb NO SERVIDOR")
    ap.add_argument("--fb-port", type=int, default=3050)
    ap.add_argument("--fb-charset", default="WIN1252")
    ap.add_argument("--saida", required=True, help="JSON com o resultado completo")
    args = ap.parse_args(argv)

    user = os.environ.get("FB_USER", "").strip()
    password = os.environ.get("FB_PASSWORD", "")
    if not user or not password:
        print("exporte FB_USER e FB_PASSWORD (nunca na linha de comando)", file=sys.stderr)
        return 2
    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"--db não existe: {db_path}", file=sys.stderr)
        return 2

    from app.erp import product_check

    with abrir_fire(
        host=args.fb_host, path=args.fb_path, port=args.fb_port,
        user=user, password=password, charset=args.fb_charset,
    ) as conn, _costuras(db_path=db_path, fire_conn=conn):
        pedidos = reler_arquivos(args.recebidos)
        print(f"{len(pedidos)} arquivos relidos; conferindo no Fire (só leitura)...")
        out = executar(
            pedidos,
            checar=lambda o: product_check.check_order(o, env={"slug": args.slug}),
            fire=FireLeitura(conn),
            datas=datas_de_importacao(db_path),
        )
    Path(args.saida).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    _imprimir(out)
    print(f"\nResultado completo: {args.saida}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
