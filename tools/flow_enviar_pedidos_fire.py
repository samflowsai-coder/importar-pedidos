#!/usr/bin/env python3
"""Envia ao Flow (`/recebimento`) pedidos abertos que só existem no Fire.

Pedido que nunca passou pelo Portal não tem import: o payload sai do pedido lido do Fire
(`app/integrations/flowpcp/recebimento_fire.py`). O Fire é lido SÓ em SELECT, dentro de
transação READ ONLY. O Flow é escrito SÓ pela API.

PADRÃO É DRY-RUN: monta e valida tudo, imprime o resumo e não manda nada. Para enviar:
`--enviar`. Cada tentativa vai para o livro (JSONL) e um pedido já enviado com sucesso no
livro não é reenviado. O `externalId` é estável, então reenviar também seria no-op no Flow.

    FB_PASSWORD=... .venv/bin/python tools/flow_enviar_pedidos_fire.py \\
        --pedidos 4709,4710 --relatorio <relatorio-vinculo.csv>
    ... --enviar --token-arquivo ~/.flowpcp-prod-portal/token-mm

Token lido de arquivo (nunca impresso, nunca em argumento). Senha do Fire em FB_PASSWORD.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

from app.erp_eventos.leitura import LeitorFirebird
from app.erp_eventos.modelo import PedidoErp
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.recebimento_fire import montar_recebimento, recusa_local

_TENANT_MM_PROD = "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"
_BASE_URL_PROD = "https://gestor.samflowsai.com.br"
_FIRE_MM = ("192.168.15.7", r"C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB")
_SO_SELECT = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
# O Flow limita o /recebimento a 10 req/min por tenant.
INTERVALO_PADRAO_S = 7.0
DIR_LIVRO = Path("data/flow_envio_fire")


def codigos_flow_do_relatorio(caminho: Path) -> dict[int, dict[int, str]]:
    """pedido ERP → {item ERP → `produtos.codigo` no Flow}, do CSV do relatório de vínculo."""
    mapa: dict[int, dict[int, str]] = {}
    with caminho.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            codigo = (r.get("Produto ERP (Flow)") or "").strip()
            if codigo and r.get("Pedido ERP") and r.get("Item ERP"):
                mapa.setdefault(int(r["Pedido ERP"]), {})[int(r["Item ERP"])] = codigo
    return mapa


def razoes_sociais(conn, pedidos: list[PedidoErp]) -> dict[int, str]:
    clientes = sorted({p.cliente_erp_codigo for p in pedidos})
    if not clientes:
        return {}
    sql = (
        "SELECT C.CODIGO, TRIM(C.RAZAO_SOCIAL) FROM CADASTRO C "
        f"WHERE C.CODIGO IN ({', '.join('?' for _ in clientes)})"
    )
    if not _SO_SELECT.match(sql):
        raise SystemExit("recusado: só SELECT")
    cur = conn.cursor()
    try:
        cur.execute(sql, clientes)
        return {int(c): (n or "") for c, n in cur.fetchall()}
    finally:
        cur.close()


def ja_enviados(livro: Path) -> set[int]:
    if not livro.exists():
        return set()
    feitos = set()
    for linha in livro.read_text(encoding="utf-8").splitlines():
        r = json.loads(linha)
        if r.get("resultado") == "enviado":
            feitos.add(int(r["pedido"]))
    return feitos


def processar(
    pedidos: list[PedidoErp],
    *,
    hoje: date,
    slug: str,
    razoes: dict[int, str],
    codigos_flow: dict[int, dict[int, str]],
    livro: Path,
    enviar: Callable[[object], dict] | None,
    intervalo_s: float,
    max_falhas: int,
    dormir: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    """Monta, recusa localmente e (se `enviar`) manda um por um. Para ao passar de
    `max_falhas` rejeições do Flow. Devolve contagens por resultado."""
    contagem = {"enviado": 0, "ja_processado": 0, "recusa_local": 0, "falha_flow": 0, "pulado": 0}
    feitos = ja_enviados(livro)
    falhas = 0
    primeiro_post = True
    livro.parent.mkdir(parents=True, exist_ok=True)
    for p in pedidos:
        reg: dict = {"em": datetime.now(UTC).isoformat(), "pedido": p.erp_codigo}
        motivo = recusa_local(p, hoje=hoje)
        if p.erp_codigo in feitos:
            contagem["pulado"] += 1
            continue
        if motivo:
            reg.update(resultado="recusa_local", motivo=motivo)
            contagem["recusa_local"] += 1
        else:
            req = montar_recebimento(
                p,
                razao_social=razoes.get(p.cliente_erp_codigo) or "(sem razão social)",
                slug=slug,
                codigos_flow=codigos_flow.get(p.erp_codigo),
            )
            reg.update(
                externalId=req.externalId, pedidoNumero=req.pedidoNumero, itens=len(req.itens)
            )
            if enviar is None:
                reg.update(resultado="dry_run")
            else:
                if not primeiro_post:
                    dormir(intervalo_s)
                primeiro_post = False
                try:
                    resp = enviar(req)
                    ja = bool(resp.get("alreadyProcessed"))
                    reg.update(
                        resultado="enviado",
                        alreadyProcessed=ja,
                        pedidoItemIds=resp.get("pedidoItemIds"),
                        itens_resposta=resp.get("itens"),
                    )
                    contagem["ja_processado" if ja else "enviado"] += 1
                except FlowPCPClientError as exc:
                    reg.update(resultado="falha_flow", http=exc.status_code, erro=str(exc)[:300])
                    contagem["falha_flow"] += 1
                    falhas += 1
        with livro.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reg, ensure_ascii=False, default=str) + "\n")
        if falhas > max_falhas:
            print(f"PARADO: {falhas} rejeições do Flow (limite {max_falhas}).", file=sys.stderr)
            break
    return contagem


def _ler_pedidos(conn, codigos: list[int]) -> list[PedidoErp]:
    achados = LeitorFirebird(conn).por_codigo(codigos)
    faltando = sorted(set(codigos) - set(achados))
    if faltando:
        print(f"AVISO: não existem no Fire: {faltando}", file=sys.stderr)
    return [achados[c] for c in codigos if c in achados]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--pedidos", required=True, help="códigos do pedido no Fire, separados por vírgula"
    )
    ap.add_argument("--relatorio", type=Path, help="CSV do relatório de vínculo (códigos do Flow)")
    ap.add_argument("--enviar", action="store_true", help="manda de verdade (padrão: dry-run)")
    ap.add_argument("--slug", default="mm")
    ap.add_argument("--host", default=_FIRE_MM[0])
    ap.add_argument("--banco", default=_FIRE_MM[1])
    ap.add_argument("--base-url", default=_BASE_URL_PROD)
    ap.add_argument("--tenant", default=_TENANT_MM_PROD)
    ap.add_argument("--token-arquivo", type=Path)
    ap.add_argument("--intervalo", type=float, default=INTERVALO_PADRAO_S)
    ap.add_argument("--max-falhas", type=int, default=1)
    ap.add_argument("--livro", type=Path, default=DIR_LIVRO / "livro.jsonl")
    args = ap.parse_args(argv)

    import firebirdsql

    codigos = [int(x) for x in args.pedidos.split(",") if x.strip()]
    conn = firebirdsql.connect(
        host=args.host,
        port=3050,
        database=args.banco,
        user="SYSDBA",
        password=os.environ["FB_PASSWORD"],
        charset="WIN1252",
        isolation_level=firebirdsql.ISOLATION_LEVEL_READ_COMMITED_RO,
    )
    try:
        pedidos = _ler_pedidos(conn, codigos)
        razoes = razoes_sociais(conn, pedidos)
    finally:
        conn.close()
    codigos_flow = codigos_flow_do_relatorio(args.relatorio) if args.relatorio else {}

    enviar = None
    client = None
    if args.enviar:
        if not args.token_arquivo:
            raise SystemExit("--enviar exige --token-arquivo")
        token = args.token_arquivo.expanduser().read_text().strip()
        client = FlowPCPClient(base_url=args.base_url, service_token=token, tenant_id=args.tenant)
        enviar = lambda req: client.send_order(req, idempotency_key=f"send-{req.externalId}")  # noqa: E731

    try:
        contagem = processar(
            pedidos,
            hoje=date.today(),
            slug=args.slug,
            razoes=razoes,
            codigos_flow=codigos_flow,
            livro=args.livro,
            enviar=enviar,
            intervalo_s=args.intervalo,
            max_falhas=args.max_falhas,
        )
    finally:
        if client:
            client.close()
    modo = "ENVIO" if args.enviar else "DRY-RUN"
    print(f"{modo}: {json.dumps(contagem)} | livro: {args.livro}")
    return 1 if contagem["falha_flow"] else 0


if __name__ == "__main__":
    sys.exit(main())
