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
  ambiguo            mais de um pedido do Fire serve (mesmo número, mesmo cliente,
                     inseridos logo após a importação); lista os CABs, não compara
  nao_enviado        a importação nunca foi para o Fire (portal_status parsed)
  sem_pareamento     pedido de várias lojas: não deu para dizer qual pedido do
                     Fire é de qual loja (tamanho/CNPJ ambíguos); não comparado
  sem_data           o arquivo não casa com nenhuma importação (sem data, não
                     liga: sem a janela de datas o pedido mais antigo ganharia)
  (por arquivo)      sem_parse = não leu o arquivo; erro_check = Fire fora ou
                     vínculos não lidos (conferência incompleta)

Os originais ficam em subpastas (recebidos/<slug>/AAAA/MM/) com o nome
`<AAAAMMDD-HHMMSS>_<sha12>_<nome original>`: a pasta é varrida recursivamente e
cada arquivo vira sua importação pelo nome guardado em `imports.original_path`
(ou, na falta, pelo sha256) — nunca por `source_filename`, que repete.
Exit 0 só sem ok_seq_diferente, sem suspeito e sem erro_check, E com cobertura:
ao menos um arquivo comparado e no máximo 50% dos arquivos em sem_parse / sem_data /
sem_pedido_fire / ambiguo / sem_pareamento ("nada comparado" nunca parece sucesso).

Ordem dos itens no Fire: o insert direto grava AGRUPADO por local de entrega
(não na ordem do pedido), e a planilha sai uma por loja (um pedido do Fire por
planilha). A comparação por posição usa a MESMA permutação do exportador
(`firebird_exporter._to_erp_rows_indexados` / `ERPExporter.linhas_por_arquivo`),
e num pedido de várias lojas compara cada pedido do Fire só com os itens da sua loja.

Não escreve em nada: o SQLite do ambiente abre `mode=ro`; o Fire entra com
transação READ ONLY e uma guarda que só deixa passar SELECT/WITH; o intercompany
fica desligado (consultaria o Fire da revenda); nenhuma chamada de LLM.

  FB_USER=SYSDBA FB_PASSWORD=... .venv/bin/python tools/regressao_chave_exata.py \\
      --recebidos <dir dos originais> --db <app_state_mm.db> [--db <outro.db> ...] \\
      [--cnpj-revenda 34513679000134] --slug mm \\
      --fb-host 192.168.15.4 --fb-path 'C:\\Fire\\MM_CONFECCAO.FDB' \\
      --saida regressao-mm.json

Ligação ao pedido do Fire: CAB_VENDAS por PEDIDO_CLIENTE (variantes do número)
+ cliente esperado (cabeçalho, lojas, ou a revenda de --cnpj-revenda) + MECANICO = 99
+ inserido (ULT_INS_DTHR) de -5 min a +72 h depois da importação. Mais de um pedido
plausível = `ambiguo` (lista os CABs; nunca escolhe). Importação `parsed` que nunca
foi para o Fire e não tem pedido = `nao_enviado` (esperado; fora da cobertura).
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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

# Rodando como script, a raiz do projeto não está no sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.erp import queries  # noqa: E402
from app.erp.cnpj import cnpj_digits  # noqa: E402
from app.erp.fire_reconcile import (  # noqa: E402
    _TAMANHO_PEDIDO_CLIENTE,
    Candidato,
    _dentro_da_janela,
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
    "sem_pareamento",
    "ambiguo",
    "nao_enviado",
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
    """(PEDIDO_CLIENTE, CODIGO, STATUS, DATA_PEDIDO, CLIENTE.CODIGO, CPF_CNPJ, MECANICO,
    ULT_INS_DTHR) — as seis primeiras na ordem da reconciliação; MECANICO e o
    instante de INSERÇÃO do pedido no fim."""
    if n < 1:
        raise ValueError("IN () é SQL inválida no Firebird: n >= 1")
    marcas = ", ".join("?" for _ in range(n))
    return (
        "SELECT TRIM(V.PEDIDO_CLIENTE), V.CODIGO, V.STATUS, V.DATA_PEDIDO, "
        "C.CODIGO, TRIM(C.CPF_CNPJ), V.MECANICO, V.ULT_INS_DTHR "
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


JANELA_ANTES = timedelta(minutes=5)  # relógio do portal x relógio do Fire
JANELA_DEPOIS = timedelta(hours=72)  # planilha: o Fire importa horas depois do envio


def _instante(valor: Any) -> datetime | None:
    """datetime do driver ou texto ISO ('T' ou espaço); None se ilegível."""
    if isinstance(valor, datetime):
        return valor.replace(tzinfo=None)
    if isinstance(valor, str) and valor.strip():
        try:
            return datetime.fromisoformat(valor.strip().replace("Z", "")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def candidatos_fire(
    candidato: Candidato, linhas: list[tuple], *, cnpjs_extra: Iterable[str] = ()
) -> list[dict]:
    """Pedidos do importador (MECANICO 99) com o mesmo número (variantes), dentro
    da janela de 90 dias, de um cliente esperado: o do cabeçalho, uma das lojas ou
    `cnpjs_extra` (a revenda intercompany: o pedido da MM pode estar no nome dela).
    CANCELADO sai quando há alternativa. Cada um: codigo, cnpj, status, inserido_em."""
    do_importador = [linha for linha in linhas if _eh_importador(linha[6])]
    inseridos = {linha[1]: (linha[7] if len(linha) > 7 else None) for linha in do_importador}
    mesmas = _linhas_do_candidato(
        candidato, _indexar_por_numero([linha[:6] for linha in do_importador])
    )
    na_janela = [linha for linha in mesmas if _dentro_da_janela(linha[3], candidato.data_pedido)]
    alvos = {
        cnpj_digits(c) for c in (candidato.cnpj_header, *candidato.cnpjs_entrega, *cnpjs_extra)
    } - {""}
    casadas = [linha for linha in na_janela if cnpj_digits(linha[5]) in alvos]
    vivas = [linha for linha in casadas if (linha[2] or "").strip().upper() != "CANCELADO"]
    unicos: dict[int, dict] = {}
    for linha in vivas or casadas:
        unicos.setdefault(
            linha[1],
            {
                "codigo": linha[1],
                "cnpj": cnpj_digits(linha[5]),
                "status": (linha[2] or "").strip(),
                "inserido_em": inseridos.get(linha[1]),
            },
        )
    return [unicos[k] for k in sorted(unicos)]


def filtrar_por_horario(cands: list[dict], importado_em: Any) -> list[dict]:
    """Fica com os pedidos INSERIDOS pouco depois da importação (de -5 min a
    +72 h). Pedidos sem horário no Fire só valem se NENHUM tem horário (aí não há
    como filtrar). Sem hora de importação, não filtra."""
    t0 = _instante(importado_em)
    if t0 is None:
        return cands
    com_horario = [c for c in cands if _instante(c["inserido_em"]) is not None]
    if not com_horario:
        return cands
    return [
        c
        for c in com_horario
        if -JANELA_ANTES <= _instante(c["inserido_em"]) - t0 <= JANELA_DEPOIS
    ]


def ligar_unico(
    candidato: Candidato,
    linhas: list[tuple],
    *,
    importado_em: Any = None,
    cnpjs_extra: Iterable[str] = (),
) -> tuple[str, list[int]]:
    """('unico' | 'ambiguo' | 'nenhum', CABs candidatos). Com mais de um pedido
    plausível NUNCA escolhe: devolve 'ambiguo' com todos."""
    cands = filtrar_por_horario(
        candidatos_fire(candidato, linhas, cnpjs_extra=cnpjs_extra), importado_em
    )
    codigos = [c["codigo"] for c in cands]
    if not codigos:
        return "nenhum", []
    return ("unico" if len(codigos) == 1 else "ambiguo"), codigos


def ligar_pedido(candidato: Candidato, linhas: list[tuple], **kw: Any) -> int | None:
    """CAB_VENDAS.CODIGO do pedido do importador, ou None (nenhum OU ambíguo)."""
    veredito, codigos = ligar_unico(candidato, linhas, **kw)
    return codigos[0] if veredito == "unico" else None


def ligar_todos(
    candidato: Candidato,
    linhas: list[tuple],
    *,
    importado_em: Any = None,
    cnpjs_extra: Iterable[str] = (),
) -> list[tuple[int, str]]:
    """Todos os pedidos plausíveis [(CODIGO, CNPJ do cliente)] — para pedido de
    várias lojas, que a planilha por loja vira N pedidos no Fire."""
    cands = filtrar_por_horario(
        candidatos_fire(candidato, linhas, cnpjs_extra=cnpjs_extra), importado_em
    )
    return [(c["codigo"], c["cnpj"]) for c in cands]


def ordem_de_insercao(order: Order) -> list[int]:
    """Posição em `order.items` de cada item, NA ORDEM em que o insert direto os
    grava no Fire (agrupado por local de entrega). Reaproveita o exportador."""
    from app.exporters.firebird_exporter import _to_erp_rows_indexados

    return [indice for indice, _ in _to_erp_rows_indexados(order)]


def posicoes_por_planilha(order: Order) -> list[list[int]]:
    """Por planilha (= um pedido do Fire por loja), as posições em `order.items`,
    na ordem das linhas. Reaproveita o exportador."""
    from app.exporters.erp_exporter import ERPExporter

    return [posicoes for _, posicoes in ERPExporter().linhas_por_arquivo(order)]


def comparar_itens(
    itens_check: list[dict], seqs_fire: list[int | None], *, posicional: bool = True
) -> list[dict]:
    """Um registro por item. `itens_check` JÁ vem na ordem esperada no Fire (ver
    `comparar_em_ordem`); `posicional=False` quando essa ordem não é conhecida.
    `status`:

    - `pendente`: o check não decidiu;
    - `ok_igual`: mesma posição, mesmo SEQ;
    - `suspeito`: contagem igual, o SEQ decidido existe no pedido mas em OUTRA
      posição, DEPOIS de aplicada a ordem de gravação do exportador (agrupada por
      local de entrega): ordem trocada além disso merece olho;
    - `ok_igual_conjunto`: contagens diferentes e o SEQ está entre os do pedido;
    - `ok_seq_diferente`: o SEQ decidido não está (mais) entre os do pedido.

    Cada SEQ do Fire é consumido uma vez. `pareamento` diz por onde valeu."""
    produtos = [it.get("produto") or {} for it in itens_check]
    restantes = Counter(s for s in seqs_fire if s is not None)
    mesma_contagem = posicional and len(produtos) == len(seqs_fire)
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


def comparar_em_ordem(
    itens_check: list[dict],
    posicoes: list[int],
    seqs_fire: list[int | None],
    *,
    posicional: bool = True,
) -> list[dict]:
    """Compara só os itens `posicoes` (índices em `itens_check`), tomados NESSA
    ordem, com os SEQs do Fire. `indice` do resultado volta a ser o do pedido."""
    resultado = comparar_itens(
        [itens_check[p] for p in posicoes], seqs_fire, posicional=posicional
    )
    for r, p in zip(resultado, posicoes, strict=True):
        r["indice"] = p
    return sorted(resultado, key=lambda r: r["indice"])


def _com_fire(itens: list[dict], codigo: int | None) -> list[dict]:
    for it in itens:
        it["fire_codigo"] = codigo
    return itens


def _sem_comparacao(
    itens_check: list[dict], posicoes: list[int], status: str, candidatos: list[int] | None = None
) -> list[dict]:
    return [
        {
            "indice": p,
            "status": status,
            "estado": (itens_check[p].get("produto") or {}).get("estado"),
            "seq_decidido": (itens_check[p].get("produto") or {}).get("fire_seq"),
            "seq_fire": None,
            "pareamento": None,
            "fire_codigo": None,
            **({"candidatos_fire": candidatos} if candidatos else {}),
        }
        for p in posicoes
    ]


def comparar_pedido(
    order: Order,
    itens_check: list[dict],
    cand: Candidato,
    linhas: list[tuple],
    fire: Any,
    *,
    importado_em: Any = None,
    cnpjs_extra: Iterable[str] = (),
) -> list[dict]:
    """Liga `order` ao(s) pedido(s) do Fire e compara item a item, na ordem de
    gravação do exportador. Devolve um registro por item (ordenado por índice).

    - uma planilha só: um pedido do Fire com todos os itens, na ordem de
      `ordem_de_insercao`;
    - várias lojas: ou UM pedido do Fire com todos os itens (insert direto,
      agrupado por loja), ou um pedido do Fire POR LOJA (planilhas); cada um é
      pareado a uma loja pelo tamanho e pelo CNPJ do cliente, e só os itens
      daquela loja entram na comparação. Pareamento duvidoso = `sem_pareamento`.

    Candidatos: mesmo número, cliente esperado (cabeçalho, lojas ou revenda em
    `cnpjs_extra`) e inserido de -5 min a +72 h depois de `importado_em`. Mais de um
    candidato para o mesmo pedido = `ambiguo` (lista os CABs), nunca escolhe."""
    kw = {"importado_em": importado_em, "cnpjs_extra": tuple(cnpjs_extra)}
    n = len(itens_check)
    todos = list(range(n))
    try:
        ordem = ordem_de_insercao(order)
        ordem_ok = sorted(ordem) == todos
    except Exception:  # noqa: BLE001 — sem a permutação, só dá para comparar por conjunto
        ordem, ordem_ok = todos, False
    try:
        planilhas = posicoes_por_planilha(order)
    except Exception:  # noqa: BLE001
        planilhas = [todos]

    if len(planilhas) <= 1:
        veredito, codigos = ligar_unico(cand, linhas, **kw)
        if veredito == "nenhum":
            return _sem_comparacao(itens_check, todos, "sem_pedido_fire")
        if veredito == "ambiguo":
            return _sem_comparacao(itens_check, todos, "ambiguo", codigos)
        codigo = codigos[0]
        itens = comparar_em_ordem(
            itens_check, ordem if ordem_ok else todos, fire.seqs_do_pedido(codigo),
            posicional=ordem_ok,
        )
        return _com_fire(itens, codigo)

    achados = ligar_todos(cand, linhas, **kw)
    if not achados:
        return _sem_comparacao(itens_check, todos, "sem_pedido_fire")
    seqs = {codigo: fire.seqs_do_pedido(codigo) for codigo, _ in achados}

    inteiros = [codigo for codigo, _ in achados if len(seqs[codigo]) == n]
    if inteiros:  # insert direto: o pedido inteiro, agrupado por loja
        if len(inteiros) > 1 or len(achados) > 1:  # outro pedido plausível: não escolhe
            return _sem_comparacao(
                itens_check, todos, "ambiguo", sorted(codigo for codigo, _ in achados)
            )
        itens = comparar_em_ordem(
            itens_check, ordem if ordem_ok else todos, seqs[inteiros[0]], posicional=ordem_ok
        )
        return _com_fire(itens, inteiros[0])

    header = cnpj_digits(order.header.customer_cnpj)
    lojas = []
    for posicoes in planilhas:
        cnpjs = {cnpj_digits(order.items[p].delivery_cnpj) for p in posicoes} - {""}
        lojas.append((posicoes, cnpjs or ({header} - {""})))

    def donas(cnpj: str) -> list[int]:
        """Lojas a que um pedido do Fire pode pertencer: as do CNPJ dele; sem CNPJ
        ou fora de todas as lojas (cabeçalho, revenda), qualquer uma."""
        certas = [i for i, (_, cnpjs) in enumerate(lojas) if cnpj and cnpj in cnpjs]
        return certas or list(range(len(lojas)))

    por_loja: dict[int, set[int]] = {i: set() for i in range(len(lojas))}
    for codigo, cnpj in achados:
        for i in donas(cnpj):
            por_loja[i].add(codigo)
    cnpj_de = dict(achados)
    saida: list[dict] = []
    for i, (posicoes, _) in enumerate(lojas):
        codigos = sorted(por_loja[i])
        if not codigos:
            saida += _sem_comparacao(itens_check, posicoes, "sem_pedido_fire")
        elif len(codigos) > 1 or len(donas(cnpj_de[codigos[0]])) > 1:
            # dois pedidos para a mesma loja (reimportação), ou um pedido que
            # serve a mais de uma loja: nada de escolher em silêncio
            saida += _sem_comparacao(itens_check, posicoes, "ambiguo", codigos)
        elif len(seqs[codigos[0]]) != len(posicoes):
            saida += _sem_comparacao(itens_check, posicoes, "sem_pareamento")
        else:
            saida += _com_fire(
                comparar_em_ordem(itens_check, posicoes, seqs[codigos[0]]), codigos[0]
            )
    return sorted(saida, key=lambda r: r["indice"])


def _motivo_erro_check(check: dict) -> str | None:
    if not check.get("available"):
        return check.get("reason") or "fire_indisponivel"
    if check.get("produto_conferencia_incompleta"):
        return "conferencia_incompleta"  # vínculos/intercompany não lidos: não vale como prova
    return None


LIMITE_NAO_LIGADOS = 0.5


def _cobertura(arquivos: list[dict]) -> dict:
    """"Nada comparado" nunca pode parecer sucesso: sem nenhum arquivo comparado,
    ou com mais da metade em sem_parse / sem_data / sem_pedido_fire / sem_pareamento /
    ambiguo,
    a regressão falha, seja qual for o resto. `nao_enviado` (importação que nunca
    foi para o Fire) não entra na conta."""
    nao_enviados = sum(1 for a in arquivos if a["resultado"] == "nao_enviado")
    total = len(arquivos) - nao_enviados
    comparados = sum(1 for a in arquivos if a["resultado"] == "comparado")
    nao_ligados = sum(
        1
        for a in arquivos
        if a["resultado"]
        in ("sem_parse", "sem_data", "sem_pedido_fire", "sem_pareamento", "ambiguo")
    )
    mensagem = None
    if comparados == 0:
        mensagem = (
            f"NADA FOI COMPARADO: 0 de {total} arquivos chegaram a ser ligados a um pedido do Fire."
        )
    elif nao_ligados / total > LIMITE_NAO_LIGADOS:
        mensagem = (
            f"COBERTURA INSUFICIENTE: {nao_ligados} de {total} arquivos sem data, sem pedido no "
            f"Fire, sem pareamento, ambíguos ou ilegíveis (limite {int(LIMITE_NAO_LIGADOS * 100)}%). "
            "O resultado não prova nada."
        )
    return {
        "arquivos": len(arquivos),
        "comparados": comparados,
        "nao_ligados": nao_ligados,
        "nao_enviados": nao_enviados,
        "ok": mensagem is None,
        "mensagem": mensagem,
    }


def executar(
    pedidos: dict[str, Order | None],
    *,
    checar: Callable[[Order], dict],
    fire: Any,
    importacoes: dict[str, dict],
    cnpjs_revenda: Iterable[str] = (),
) -> dict:
    """Roda a regressão sobre `pedidos` (arquivo -> Order, ou None se não leu).

    `importacoes`: arquivo -> {imported_at, order_number, customer_cnpj,
    portal_status, fire_codigo, mapeamento} da importação dele. Sem ela (ou sem
    `imported_at`) o arquivo não liga ao Fire: sem a data, um número que se repete
    ligaria ao pedido errado — vai para `sem_data`. `cnpjs_revenda`: clientes
    extras aceitos no Fire (a revenda intercompany: o pedido da MM pode estar no
    nome dela). Importação `parsed`, sem `fire_codigo` e SEM planilha gerada
    (`tem_saida`) e sem pedido no Fire é `nao_enviado`: esperado, e fora da conta de
    cobertura. Com planilha gerada o Fire deveria ter importado: fica sem_pedido_fire."""
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
        importado_em = imp.get("imported_at") or None
        data = importado_em[:10] if importado_em else None
        cand = candidato_do_pedido(
            order,
            data_pedido=data,
            numero=imp.get("order_number"),
            cnpj=imp.get("customer_cnpj"),
        )
        todos = list(range(len(itens_check)))
        if data is None:
            itens = _sem_comparacao(itens_check, todos, "sem_data")
        else:
            numeros = [v for v in variantes(cand.numero) if len(v) <= _TAMANHO_PEDIDO_CLIENTE]
            if numeros:
                itens = comparar_pedido(
                    order, itens_check, cand, fire.pedidos_por_numero(numeros), fire,
                    importado_em=importado_em, cnpjs_extra=tuple(cnpjs_revenda),
                )
            else:
                itens = _sem_comparacao(itens_check, todos, "sem_pedido_fire")
        statuses = {it["status"] for it in itens}
        if "sem_data" in statuses:
            resultado = "sem_data"
        elif statuses - {"sem_pedido_fire", "sem_pareamento", "ambiguo"}:
            resultado = "comparado"
        elif "ambiguo" in statuses:
            resultado = "ambiguo"
        elif "sem_pareamento" in statuses:
            resultado = "sem_pareamento"
        elif (
            imp.get("portal_status") == "parsed"
            and not imp.get("fire_codigo")
            and not imp.get("tem_saida")
        ):
            # nunca foi para o Fire e nem gerou planilha: se saiu .xlsx, quem
            # importou foi o Fire e o pedido DEVERIA estar lá (fica sem_pedido_fire)
            resultado = "nao_enviado"
            for it in itens:
                it["status"] = "nao_enviado"
        else:
            resultado = "sem_pedido_fire"
        for it in itens:
            totais[it["status"]] += 1
            reg = {"arquivo": nome, "pedido": cand.numero, **it}
            if it["status"] == "ok_seq_diferente":
                perigosos.append(reg)
            elif it["status"] == "suspeito":
                suspeitos.append(reg)
        arquivos.append(
            {
                "arquivo": nome,
                "resultado": resultado,
                "pedido": cand.numero,
                "fire_codigos": sorted({it["fire_codigo"] for it in itens if it.get("fire_codigo")}),
                "mapeamento": imp.get("mapeamento"),
                "importacao": {
                    k: imp.get(k) for k in ("imported_at", "portal_status", "fire_codigo")
                },
                "itens": itens,
            }
        )
    cobertura = _cobertura(arquivos)
    return {
        "ok": cobertura["ok"]
        and not (totais["ok_seq_diferente"] or totais["suspeito"] or totais["erro_check"]),
        "cobertura": cobertura,
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


def _tem_saida(output_files_json: Any) -> bool:
    """A importação gerou planilha(s) (output_files_json não vazio)?"""
    if not output_files_json:
        return False
    try:
        return bool(json.loads(output_files_json))
    except (TypeError, ValueError):
        return True  # ilegível: na dúvida, trate como enviado (não esconde um sem_pedido_fire)


def _basename(caminho: str | None) -> str:
    """Último componente de um caminho Windows OU POSIX (o portal roda no Windows)."""
    return re.split(r"[\\/]", caminho or "")[-1]


def importacoes_dos_arquivos(
    db_paths: Path | list[Path], arquivos: list[Relido]
) -> dict[str, dict]:
    """chave do arquivo -> importação dele, para TODO arquivo. Procura em cada
    `--db` (as importações de uma empresa podem estar no banco de outra, por
    roteamento) pelo nome GUARDADO contra o basename de `imports.original_path` e,
    na falta, pelo sha256 do conteúdo. NUNCA por `source_filename` (o nome original
    se repete entre pedidos). Várias importações do mesmo arquivo: vale a mais
    recente. Sem achar, devolve a entrada sem `imported_at` (vira `sem_data`). Toda
    entrada traz `mapeamento`: o que foi tentado, em quais bancos, e por qual via
    casou — para diagnosticar `sem_data` sem adivinhar."""
    caminhos = [db_paths] if isinstance(db_paths, Path) else list(db_paths)
    colunas = (
        "original_path, file_sha256, imported_at, order_number, customer_cnpj, "
        "portal_status, fire_codigo, output_files_json"
    )
    indices: list[tuple[str, dict[str, dict], dict[str, dict], int]] = []
    for caminho in caminhos:
        conn = sqlite3.connect(f"{caminho.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = conn.execute(f"SELECT {colunas} FROM imports ORDER BY imported_at").fetchall()
        finally:
            conn.close()
        por_nome: dict[str, dict] = {}
        por_sha: dict[str, dict] = {}
        for caminho_orig, sha, quando, numero, cnpj, status, fire_cod, saidas in rows:  # a última vence
            reg = {
                "imported_at": quando,
                "order_number": numero,
                "customer_cnpj": cnpj,
                "portal_status": status,
                "fire_codigo": fire_cod,
                "tem_saida": _tem_saida(saidas),
            }
            if caminho_orig:
                por_nome[_basename(caminho_orig)] = reg
            if sha:
                por_sha[sha.strip().lower()] = reg
        indices.append((caminho.name, por_nome, por_sha, len(rows)))

    out: dict[str, dict] = {}
    for a in arquivos:
        achada: dict | None = None
        via = achado_em = None
        for nome_db, por_nome, por_sha, _ in indices:
            if a.guardado in por_nome:
                achada, via, achado_em = por_nome[a.guardado], "original_path", nome_db
            elif a.sha256 in por_sha:
                achada, via, achado_em = por_sha[a.sha256], "sha256", nome_db
            if achada is not None:
                break
        mapeamento = {
            "tentou": ["original_path (nome guardado)", "sha256 do conteúdo"],
            "bancos": [{"db": n, "importacoes": total} for n, _, _, total in indices],
            "por": via,
            "achado_em": achado_em,
            "nome_guardado": a.guardado,
            "sha256": a.sha256,
        }
        out[a.chave] = {**(achada or {}), "mapeamento": mapeamento}
    return out


def _imprimir(out: dict) -> None:
    t = out["totais"]
    print("Totais por item:")
    for b in BALDES_ITEM:
        print(f"  {b}: {t.get(b, 0)}")
    print(f"Arquivos: sem_parse={t.get('sem_parse', 0)} erro_check={t.get('erro_check', 0)}")
    c = out["cobertura"]
    print(f"Cobertura: {c['comparados']} comparados, {c['nao_ligados']} não ligados, de {c['arquivos']}")
    if c["mensagem"]:
        print(f"\n*** {c['mensagem']} ***")
    if t.get("sem_data"):
        print("  (sem_data: nenhuma importação casou com o arquivo; não foi ligado)")
        for a in out["arquivos"]:
            if a["resultado"] == "sem_data":
                m = a.get("mapeamento") or {}
                dbs = ", ".join(f"{b['db']}({b['importacoes']})" for b in m.get("bancos", []))
                print(f"    {a['arquivo']}: tentou original_path e sha256 em {dbs}")
    if t.get("ambiguo"):
        print("  (ambiguo: mais de um pedido do Fire serve; não escolhi nenhum)")
        vistos = set()
        for a in out["arquivos"]:
            if a["resultado"] == "ambiguo" and a["arquivo"] not in vistos:
                vistos.add(a["arquivo"])
                cabs = (a["itens"][0].get("candidatos_fire") if a["itens"] else None) or []
                print(f"    {a['arquivo']} pedido {a['pedido']}: CABs {cabs}")
    if c_nao_env := out["cobertura"].get("nao_enviados"):
        print(f"  (nao_enviado: {c_nao_env} arquivos de importação que nunca foi para o Fire)")
    if out["suspeitos"]:
        print("\nsuspeito (mesma contagem de itens, SEQ certo em OUTRA posição):")
        for p in out["suspeitos"]:
            print(
                f"  {p['arquivo']} pedido {p['pedido']} item {p['indice']}: "
                f"decidido {p['seq_decidido']} (Fire CAB {p.get('fire_codigo')})"
            )
    if out["perigosos"]:
        print("\nok_seq_diferente (decidiu um SEQ diferente do gravado no Fire):")
        for p in out["perigosos"]:
            print(
                f"  {p['arquivo']} pedido {p['pedido']} item {p['indice']}: "
                f"decidido {p['seq_decidido']} x Fire {p['seq_fire']} "
                f"(Fire CAB {p.get('fire_codigo')}, pareamento {p['pareamento']})"
            )
    else:
        print("\nok_seq_diferente: nenhum.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--recebidos", required=True, help="pasta com os arquivos originais")
    ap.add_argument(
        "--db",
        required=True,
        action="append",
        help="app_state_<slug>.db (cópia; abre só leitura). Repita para procurar as "
        "importações em mais de um banco (roteamento); os vínculos v2 vêm do "
        "app_state_<slug>.db, ou do primeiro",
    )
    ap.add_argument(
        "--cnpj-revenda",
        action="append",
        default=[],
        help="CNPJ da revenda intercompany, aceito como cliente no Fire (ex.: o da "
        "Nasmar, 34513679000134, para arquivos da MM). Pode repetir",
    )
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
    db_paths = [Path(d) for d in args.db]
    for d in db_paths:
        if not d.is_file():
            print(f"--db não existe: {d}", file=sys.stderr)
            return 2
    db_path = next((d for d in db_paths if d.name == f"app_state_{args.slug}.db"), db_paths[0])

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
            importacoes=importacoes_dos_arquivos(db_paths, relidos),
            cnpjs_revenda=args.cnpj_revenda,
        )
    Path(args.saida).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    _imprimir(out)
    print(f"\nResultado completo: {args.saida}")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
