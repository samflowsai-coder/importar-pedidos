#!/usr/bin/env python3
"""Migra o de-para v1 (`produto_depara`) para o v2 (`produto_vinculo_v2`).

Padrão é DRY-RUN: só mostra o que migraria. Com `--aplicar` grava, pelo
`produto_vinculo_repo.vincular` (uma transação por vínculo, origem
`migrado_v1`). Nunca apaga o v1, nunca sobrescreve vínculo v2 que aponte para
outro produto e pode rodar de novo sem duplicar nada.

  python tools/migrar_produto_vinculo_v2.py --slug mm
  python tools/migrar_produto_vinculo_v2.py --slug mm --aplicar

Regras (spec chave exata):
- linha v1 com CNPJ (client_key só dígitos) -> escopo de gravação do CNPJ
  (rede, senão raiz de 8 dígitos);
- linha v1 com nome -> procura em `imports` os CNPJs do mesmo cliente (nome
  normalizado como o `client_key`). Só migra se der UMA raiz e UM escopo;
  senão fica em `sem_cnpj_unico` para a operação vincular de novo na tela;
- linha v1 do CNPJ da REVENDA intercompany (config do ambiente) NÃO migra: o
  v1 sob a revenda valeria para todo cliente real dela (`revenda_intercompany`);
- `fire_codigo` do v2 é o CODPROD_ALTERN do catálogo local quando existe;
  o `fire_codigo` do v1 é o SEQ em texto e não serve.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

# Rodando como script, a raiz do projeto não está no sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.erp.cnpj import cnpj_digits  # noqa: E402
from app.erp.preco_regra import normalizar_cnpj  # noqa: E402
from app.erp.produto_escopo import escopo_de_gravacao  # noqa: E402
from app.persistence import produto_depara_repo, produto_vinculo_repo  # noqa: E402

BUCKETS = (
    "migrados",
    "ja_existiam",
    "conflito",
    "sem_cnpj_unico",
    "revenda_intercompany",
    "invalidos",
)


def _cnpjs_da_revenda(slug: str) -> set[str]:
    """CNPJ (dígitos) da revenda intercompany configurada no ambiente. Erro de
    leitura SOBE: sem saber quem é a revenda, migrar seria arriscar espalhar o
    v1 dela para todo cliente real."""
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(slug)
    alvo = cnpj_digits((env or {}).get("intercompany_cnpj"))
    return {alvo} if alvo else set()


def _cnpjs_por_nome(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """client_key (nome normalizado) -> CNPJs válidos vistos em `imports`."""
    out: dict[str, set[str]] = {}
    rows = conn.execute(
        "SELECT DISTINCT customer_name, customer_cnpj FROM imports "
        "WHERE customer_name IS NOT NULL AND customer_cnpj IS NOT NULL"
    ).fetchall()
    for nome, cnpj in rows:
        valido = normalizar_cnpj(cnpj)
        chave = produto_depara_repo.client_key(None, nome)
        if valido and chave:
            out.setdefault(chave, set()).add(valido)
    return out


def _codprod_altern(conn: sqlite3.Connection, seq: int) -> str | None:
    row = conn.execute(
        "SELECT codprod_altern FROM catalogo_fire WHERE fire_produto_id = ?", (str(seq),)
    ).fetchone()
    valor = ((row[0] if row else None) or "").strip().upper()
    return valor or None


def _escopo_do_cliente(
    conn: sqlite3.Connection,
    client_key: str,
    por_nome: dict[str, set[str]],
) -> tuple[str | None, set[str], str]:
    """(escopo, cnpjs usados, motivo se não resolveu)."""
    if client_key.isdigit() and normalizar_cnpj(client_key):
        cnpjs = {client_key}
    else:
        cnpjs = set(por_nome.get(client_key, set()))
        if not cnpjs:
            return None, set(), "nome sem CNPJ nos pedidos importados"
        if len({c[:8] for c in cnpjs}) != 1:
            return None, cnpjs, "nome com mais de uma raiz de CNPJ"
    escopos = {escopo_de_gravacao(conn, c) for c in cnpjs}
    escopos.discard(None)
    if len(escopos) != 1:
        return None, cnpjs, "CNPJs do nome caem em escopos diferentes"
    return escopos.pop(), cnpjs, ""


def migrar(
    conn: sqlite3.Connection,
    *,
    slug: str,
    aplicar: bool,
    agora: str,
    cnpjs_revenda: Iterable[str] | None = None,
) -> dict[str, list[dict]]:
    """Migra (ou, sem `aplicar`, só planeja) o v1 do ambiente para o v2.

    Devolve um dict com as listas de `BUCKETS`. Cada item traz a linha v1
    (`v1_id`, `client_key`, `chave_tipo`, `chave_valor`, `fire_seq`) e, quando
    resolvido, o `escopo`. `cnpjs_revenda` None = lê a config do ambiente."""
    revenda = (
        {cnpj_digits(c) for c in cnpjs_revenda} - {""}
        if cnpjs_revenda is not None
        else _cnpjs_da_revenda(slug)
    )
    por_nome = _cnpjs_por_nome(conn)
    rows = conn.execute(
        "SELECT id, client_key, chave_tipo, chave_valor, fire_produto_id, fire_nome, criado_por "
        "FROM produto_depara ORDER BY id"
    ).fetchall()

    out: dict[str, list[dict]] = {b: [] for b in BUCKETS}
    planejados: dict[tuple[str, str, str], int] = {}  # dry-run enxerga o que já planejou

    for v1_id, client_key, tipo, valor, fire_id, nome, criado_por in rows:
        item = {
            "v1_id": v1_id,
            "client_key": client_key,
            "chave_tipo": tipo,
            "chave_valor": valor,
            "fire_seq": fire_id,
        }
        seq_txt = str(fire_id or "").strip()
        chave = produto_vinculo_repo.norm_chave(tipo, valor)
        if not seq_txt.isdigit() or not chave or tipo not in ("codigo", "ean"):
            out["invalidos"].append(item)
            continue
        seq = int(seq_txt)

        if cnpj_digits(client_key) in revenda and client_key.isdigit():
            out["revenda_intercompany"].append(item)
            continue

        escopo, cnpjs, motivo = _escopo_do_cliente(conn, client_key, por_nome)
        if escopo is None:
            out["sem_cnpj_unico"].append({**item, "motivo": motivo})
            continue
        if any(cnpj_digits(c) in revenda for c in cnpjs):
            out["revenda_intercompany"].append({**item, "escopo": escopo})
            continue
        item["escopo"] = escopo

        atual = produto_vinculo_repo.lookup(
            conn,
            [escopo],
            codigos=[chave] if tipo == "codigo" else [],
            eans=[chave] if tipo == "ean" else [],
        ).get((tipo, chave))
        ja_seq = atual.fire_seq if atual is not None else planejados.get((escopo, tipo, chave))
        if ja_seq is not None:
            out["ja_existiam" if ja_seq == seq else "conflito"].append(
                {**item, "vigente_fire_seq": ja_seq}
            )
            continue

        if aplicar:
            try:
                produto_vinculo_repo.vincular(
                    conn,
                    escopo=escopo,
                    chave_tipo=tipo,
                    chave_valor=chave,
                    fire_seq=seq,
                    fire_codigo=_codprod_altern(conn, seq),
                    fire_descricao=nome,
                    por=criado_por or "migrado_v1",
                    pedido_id=None,
                    origem="migrado_v1",
                    agora=agora,
                )
            except produto_vinculo_repo.VinculoConflito as exc:
                out["conflito"].append({**item, "vigente_fire_seq": exc.atual.fire_seq})
                continue
        planejados[(escopo, tipo, chave)] = seq
        out["migrados"].append(item)
    return out


def _imprimir(resultado: dict[str, list[dict]], *, aplicar: bool) -> None:
    print("APLICADO" if aplicar else "DRY-RUN (nada foi gravado; use --aplicar)")
    for bucket in BUCKETS:
        print(f"{bucket}: {len(resultado[bucket])}")
    for bucket in ("sem_cnpj_unico", "conflito", "revenda_intercompany", "invalidos"):
        for it in resultado[bucket]:
            extra = it.get("motivo") or it.get("vigente_fire_seq") or ""
            print(
                f"  [{bucket}] v1#{it['v1_id']} {it['client_key']} "
                f"{it['chave_tipo']}={it['chave_valor']} -> SEQ {it['fire_seq']} {extra}"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug", required=True, help="ambiente (ex.: mm, nasmar)")
    ap.add_argument("--aplicar", action="store_true", help="grava (sem isto, só mostra)")
    args = ap.parse_args(argv)

    from app.persistence import router

    agora = datetime.now().isoformat(timespec="seconds")
    with router.env_connect(args.slug) as conn:
        resultado = migrar(conn, slug=args.slug, aplicar=args.aplicar, agora=agora)
    _imprimir(resultado, aplicar=args.aplicar)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
