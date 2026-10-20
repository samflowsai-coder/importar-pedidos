#!/usr/bin/env python3
"""Regressão da chave exata contra pedidos reais já importados. SÓ LEITURA.

Relê cada arquivo original (PDF/XLS/XLSX) pelo `pipeline.process` com o LLM
DESLIGADO, roda o `check_order` novo (vínculos v2 do `--db`, Fire em leitura),
acha o pedido que o importador gravou no Fire e compara, item a item, o SEQ
decidido com o SEQ gravado:

  ok_igual           decidido == gravado no Fire, no MESMO item (posição)
  ok_igual_conjunto  decidido está entre os SEQs do pedido, mas a contagem de
                     itens difere (pareamento fraco; olhar por amostragem)
  suspeito           mesma contagem, o SEQ existe no pedido mas em OUTRA posição
  ok_seq_diferente   decidido != gravado  (o balde PERIGOSO: tem que dar zero)
  pendente           o check novo não decide (fica para a operação vincular)
  sem_pedido_fire    o pedido não foi achado no Fire
  sem_data           o arquivo não casa com nenhuma importação (sem data, não
                     liga: sem a janela de datas o pedido mais antigo ganharia)
  (por arquivo)      sem_parse = não leu o arquivo; erro_check = Fire fora ou
                     vínculos não lidos (conferência incompleta)

Os originais ficam em subpastas (recebidos/<slug>/AAAA/MM/) com o nome
`<AAAAMMDD-HHMMSS>_<sha12>_<nome original>`: a pasta é varrida recursivamente e
cada arquivo vira sua importação pelo nome guardado em `imports.original_path`
(ou, na falta, pelo sha256) — nunca por `source_filename`, que repete.
Exit 0 só sem ok_seq_diferente, sem suspeito e sem erro_check.

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
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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
BALDES_ITEM = (
    "ok_igual",
    "ok_igual_conjunto",
    "suspeito",
    "ok_seq_diferente",
    "pendente",
    "sem_pedido_fire",
    "sem_data",
)
BLOCO = 200

_LEITURA = re.compile(r"^\s*(SELECT|WITH)\b")
# Uma passada só, na ordem em que aparecem: literal, identificador entre aspas ou
# comentário. Assim `'--'` não vira comentário nem `/* ' */` vira literal.
_TOKEN_OPACO = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*.*?\*/""", re.DOTALL
)


def _opaco(m: re.Match) -> str:
    t = m.group(0)
    return " " if t.startswith(("--", "/*")) else ("0" if t[0] == "'" else "X")
# Qualquer um destes, em qualquer lugar: escreve, trava, consome gerador ou
# executa procedimento. GEN_ID/NEXT VALUE FOR mexem em gerador mesmo dentro de um
# SELECT; FOR UPDATE/WITH LOCK travam linhas; `FROM proc(...)` executa procedimento
# selecionável (que pode escrever).
_PROIBIDO = re.compile(
    r"\b(GEN_ID|NEXT\s+VALUE\s+FOR|FOR\s+UPDATE|WITH\s+LOCK|INSERT|UPDATE|DELETE|MERGE"
    r"|EXECUTE|SET|ALTER|CREATE|DROP|GRANT|RECREATE|REVOKE|COMMIT|ROLLBACK)\b"
    r"|\b(?:FROM|JOIN)\s+[A-Z_][A-Z0-9_$]*\s*\("
)


def so_leitura(sql: str) -> str:
    """Só SELECT/WITH, uma instrução só, sem nada da lista proibida. O texto é
    limpo de comentários e literais ANTES de olhar (um `/* SELECT */ DELETE` não
    passa, e um literal 'UPDATE' não derruba a consulta). `raise`, não `assert`:
    com `-O` o assert some e a guarda junto. É defesa em profundidade: a
    transação READ ONLY do Fire é a garantia de fato."""
    limpo = _TOKEN_OPACO.sub(_opaco, sql or "")
    if "'" in limpo or '"' in limpo or "/*" in limpo or "*/" in limpo:
        raise RuntimeError("ferramenta só leitura: literal ou comentário sem fechar")
    limpo = limpo.strip().rstrip(";").upper()
    if not _LEITURA.match(limpo) or ";" in limpo:
        raise RuntimeError("ferramenta só leitura: apenas SELECT/WITH, uma instrução")
    achado = _PROIBIDO.search(limpo)
    if achado:
        raise RuntimeError(f"ferramenta só leitura: recusado ({achado.group(0).strip()})")
    return sql


class CursorSoLeitura:
    """Cursor com lista de atributos permitidos (nada de `prep`, `transaction`,
    `callproc`...): quem não está na lista não existe aqui."""

    def __init__(self, cur: Any) -> None:
        self._cur = cur

    def execute(self, sql: str, params: Any = ()) -> Any:
        so_leitura(sql)
        return self._cur.execute(sql, params)

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> Any:
        return self._cur.fetchall()

    def fetchmany(self, *args: Any) -> Any:
        return self._cur.fetchmany(*args)

    def close(self) -> None:
        self._cur.close()

    @property
    def description(self) -> Any:
        return self._cur.description

    @property
    def rowcount(self) -> Any:
        return self._cur.rowcount

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
def candidato_do_pedido(
    order: Order,
    *,
    data_pedido: str | None,
    numero: str | None = None,
    cnpj: str | None = None,
) -> Candidato:
    """`numero`/`cnpj` da importação (o que de fato foi para o Fire) vencem os do
    re-parse quando existem."""
    lojas = tuple(dict.fromkeys(i.delivery_cnpj for i in order.items if i.delivery_cnpj))
    numero = (numero or order.header.order_number or "").strip()
    return Candidato(
        import_id=order.source_file or numero,
        numero=numero,
        cliente_codigo=None,
        cnpj_header=(cnpj or "").strip() or order.header.customer_cnpj,
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
    """Um registro por item. `status`:

    - `pendente`: o check não decidiu;
    - `ok_igual`: mesma posição, mesmo SEQ;
    - `suspeito`: contagem igual, o SEQ decidido existe no pedido mas em OUTRA
      posição (o importador grava na ordem do pedido: ordem trocada merece olho);
    - `ok_igual_conjunto`: contagens diferentes e o SEQ está entre os do pedido;
    - `ok_seq_diferente`: o SEQ decidido não está (mais) entre os do pedido.

    Cada SEQ do Fire é consumido uma vez. `pareamento` diz por onde valeu."""
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
    for r in out:  # 2) o que sobrou, pelo conjunto
        if r["status"] is not None:
            continue
        r["pareamento"] = "conjunto"
        if restantes[r["seq_decidido"]] > 0:
            restantes[r["seq_decidido"]] -= 1
            r["status"] = "suspeito" if mesma_contagem else "ok_igual_conjunto"
        else:
            r["status"] = "ok_seq_diferente"
    return out


def _itens_sem_comparacao(itens_check: list[dict], status: str) -> list[dict]:
    return [
        {
            "indice": i,
            "status": status,
            "estado": (it.get("produto") or {}).get("estado"),
            "seq_decidido": (it.get("produto") or {}).get("fire_seq"),
            "seq_fire": None,
            "pareamento": None,
        }
        for i, it in enumerate(itens_check)
    ]


def _motivo_erro_check(check: dict) -> str | None:
    if not check.get("available"):
        return check.get("reason") or "fire_indisponivel"
    if check.get("produto_conferencia_incompleta"):
        return "conferencia_incompleta"  # vínculos/intercompany não lidos: não vale como prova
    return None


def executar(
    pedidos: dict[str, Order | None],
    *,
    checar: Callable[[Order], dict],
    fire: Any,
    importacoes: dict[str, dict],
) -> dict:
    """Roda a regressão sobre `pedidos` (arquivo -> Order, ou None se não leu).

    `importacoes`: arquivo -> {imported_at, order_number, customer_cnpj} da
    importação dele. Sem ela (ou sem `imported_at`) o arquivo não liga ao Fire:
    sem a janela de datas, um número que se repete entre anos ligaria ao pedido
    errado — vai para `sem_data`."""
    contados = (*BALDES_ITEM, "sem_parse", "erro_check")
    totais: Counter = Counter({b: 0 for b in contados})
    arquivos: list[dict] = []
    perigosos: list[dict] = []
    suspeitos: list[dict] = []
    for nome, order in sorted(pedidos.items()):
        if order is None:
            totais["sem_parse"] += 1
            arquivos.append({"arquivo": nome, "resultado": "sem_parse", "itens": []})
            continue
        check = checar(order)
        motivo = _motivo_erro_check(check)
        if motivo is not None:
            totais["erro_check"] += 1
            arquivos.append(
                {"arquivo": nome, "resultado": "erro_check", "motivo": motivo, "itens": []}
            )
            continue
        itens_check = check.get("items") or []
        imp = importacoes.get(nome) or {}
        data = (imp.get("imported_at") or "")[:10] or None
        cand = candidato_do_pedido(
            order,
            data_pedido=data,
            numero=imp.get("order_number"),
            cnpj=imp.get("customer_cnpj"),
        )
        codigo = None
        if data is None:
            resultado, itens = "sem_data", _itens_sem_comparacao(itens_check, "sem_data")
        else:
            numeros = [v for v in variantes(cand.numero) if len(v) <= _TAMANHO_PEDIDO_CLIENTE]
            codigo = ligar_pedido(cand, fire.pedidos_por_numero(numeros)) if numeros else None
            if codigo is None:
                resultado = "sem_pedido_fire"
                itens = _itens_sem_comparacao(itens_check, "sem_pedido_fire")
            else:
                resultado = "comparado"
                itens = comparar_itens(itens_check, fire.seqs_do_pedido(codigo))
        for it in itens:
            totais[it["status"]] += 1
            reg = {"arquivo": nome, "pedido": cand.numero, "fire_codigo": codigo, **it}
            if it["status"] == "ok_seq_diferente":
                perigosos.append(reg)
            elif it["status"] == "suspeito":
                suspeitos.append(reg)
        arquivos.append(
            {
                "arquivo": nome,
                "resultado": resultado,
                "pedido": cand.numero,
                "fire_codigo": codigo,
                "itens": itens,
            }
        )
    return {
        "ok": not (totais["ok_seq_diferente"] or totais["suspeito"] or totais["erro_check"]),
        "totais": dict(totais),
        "perigosos": perigosos,
        "suspeitos": suspeitos,
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


SUPORTADOS = (".pdf", ".xls", ".xlsx")
_PREFIXO_GUARDADO = re.compile(r"^\d{8}-\d{6}_[0-9a-f]{12}_(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class Relido:
    chave: str  # caminho relativo, único por arquivo
    guardado: str  # nome guardado: <AAAAMMDD-HHMMSS>_<sha12>_<nome original>
    sha256: str
    order: Order | None


def nome_original(guardado: str) -> str:
    """Nome que o parser enxerga: o original, sem o prefixo de guarda."""
    m = _PREFIXO_GUARDADO.match(guardado)
    return m.group(1) if m else guardado


def reler_arquivos(diretorio: str) -> list[Relido]:
    """Varre `diretorio` RECURSIVAMENTE (os originais ficam em AAAA/MM/) e relê
    cada PDF/XLS/XLSX pelo `pipeline.process` (LoadedFile como o resto do app)."""
    from app import pipeline
    from app.ingestion.file_loader import LoadedFile

    base = Path(diretorio)
    out: list[Relido] = []
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        if path.suffix.lower() not in SUPORTADOS:
            continue
        raw = path.read_bytes()
        chave = path.relative_to(base).as_posix()
        order: Order | None = None
        try:
            arq = LoadedFile(
                path=path.with_name(nome_original(path.name)),
                extension=path.suffix.lower(),
                raw=raw,
            )
            order = pipeline.process(arq)
        except Exception as exc:  # noqa: BLE001 — um arquivo ruim não derruba a regressão
            print(f"  não leu {chave}: {type(exc).__name__}: {exc}", file=sys.stderr)
        out.append(Relido(chave, path.name, hashlib.sha256(raw).hexdigest(), order))
    return out


def _basename(caminho: str | None) -> str:
    """Último componente de um caminho Windows OU POSIX (o portal roda no Windows)."""
    return re.split(r"[\\/]", caminho or "")[-1]


def importacoes_dos_arquivos(db_path: Path, arquivos: list[Relido]) -> dict[str, dict]:
    """chave do arquivo -> importação dele (imported_at, order_number,
    customer_cnpj). Casa pelo nome GUARDADO contra o basename de
    `imports.original_path` e, na falta, pelo sha256 do conteúdo. NUNCA por
    `source_filename` (o nome original se repete entre pedidos). Várias
    importações do mesmo arquivo: vale a mais recente."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT original_path, file_sha256, imported_at, order_number, customer_cnpj "
            "FROM imports ORDER BY imported_at"
        ).fetchall()
    finally:
        conn.close()
    por_nome: dict[str, dict] = {}
    por_sha: dict[str, dict] = {}
    for caminho, sha, quando, numero, cnpj in rows:  # crescente: a última vence
        reg = {"imported_at": quando, "order_number": numero, "customer_cnpj": cnpj}
        if caminho:
            por_nome[_basename(caminho)] = reg
        if sha:
            por_sha[sha.strip().lower()] = reg
    out: dict[str, dict] = {}
    for a in arquivos:
        achada = por_nome.get(a.guardado) or por_sha.get(a.sha256)
        if achada is not None:
            out[a.chave] = achada
    return out


def _imprimir(out: dict) -> None:
    t = out["totais"]
    print("Totais por item:")
    for b in BALDES_ITEM:
        print(f"  {b}: {t.get(b, 0)}")
    print(f"Arquivos: sem_parse={t.get('sem_parse', 0)} erro_check={t.get('erro_check', 0)}")
    if t.get("sem_data"):
        print("  (sem_data: arquivo sem importação correspondente em imports; não foi ligado)")
    if out["suspeitos"]:
        print("\nsuspeito (mesma contagem de itens, SEQ certo em OUTRA posição):")
        for p in out["suspeitos"]:
            print(
                f"  {p['arquivo']} pedido {p['pedido']} item {p['indice']}: "
                f"decidido {p['seq_decidido']} (Fire CAB {p['fire_codigo']})"
            )
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
        relidos = reler_arquivos(args.recebidos)
        if not relidos:
            print(f"nenhum PDF/XLS/XLSX em {args.recebidos} (nem nas subpastas)", file=sys.stderr)
            return 2
        print(f"{len(relidos)} arquivos relidos; conferindo no Fire (só leitura)...")
        out = executar(
            {r.chave: r.order for r in relidos},
            checar=lambda o: product_check.check_order(o, env={"slug": args.slug}),
            fire=FireLeitura(conn),
            importacoes=importacoes_dos_arquivos(db_path, relidos),
        )
    Path(args.saida).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    _imprimir(out)
    print(f"\nResultado completo: {args.saida}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
