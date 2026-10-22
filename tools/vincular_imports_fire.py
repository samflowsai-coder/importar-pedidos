#!/usr/bin/env python3
"""Pareia import `parsed` do Portal com o pedido aberto do Fire e (com --aplicar) liga pela rota.

Para o que a reconciliação por número não alcança: o número que o Portal leu não é o
`PEDIDO_CLIENTE` que o Fire guarda ("TENNIS STATION LJ 180 181" × "TSLJ180181"). Pareia por CNPJ
(header ou loja de entrega) + nº de itens + data (±8 dias); desempata por número normalizado.
Ambíguo, sem par e duplicata NÃO são ligados: saem na lista.

DRY-RUN POR PADRÃO (só lê: Portal por GET, Fire por SELECT). `--aplicar` faz
`POST /api/imported/{id}/vincular-fire` (admin). Senha do admin em PORTAL_SENHA e do Fire em
FB_PASSWORD; nada vai em argumento.

    PORTAL_SENHA=... FB_PASSWORD=... .venv/bin/python tools/vincular_imports_fire.py \\
        --portal http://192.168.15.4:3636 --email admin@... --ambiente mm
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from http.cookiejar import CookieJar

from app.erp.cnpj import cnpj_digits
from app.erp_eventos.modelo import PedidoErp

JANELA_DIAS = 8
_FIRE_MM = ("192.168.15.7", r"C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB")


def norm_numero(numero: str | None) -> str:
    """Grafia comparável: só A-Z0-9, `TENNISSTATION` vira `TS`, zeros após letra somem."""
    s = re.sub(r"[^A-Z0-9]", "", (numero or "").upper())
    s = re.sub(r"^TENNISSTATION", "TS", s)
    return re.sub(r"(?<=[A-Z])0+(?=\d)", "", s)


@dataclass
class Pareamento:
    pares: list[tuple[str, int]] = field(default_factory=list)
    ambiguos: list[tuple[str, list[int]]] = field(default_factory=list)
    sem_par: list[str] = field(default_factory=list)
    duplicatas: list[tuple[str, int]] = field(default_factory=list)


def _candidatos(imp: dict, pedidos: list[PedidoErp], ja_ligados: set[int]) -> list[PedidoErp]:
    ancoras = {cnpj_digits(imp["customer_cnpj"])} | set(imp["delivery_cnpjs"])
    ancoras.discard("")
    dia = date.fromisoformat(imp["imported_at"][:10])
    saida = []
    for p in pedidos:
        if p.erp_codigo in ja_ligados or p.data_pedido is None:
            continue
        cnpj = cnpj_digits(p.cliente_erp_cnpj)
        por_override = (
            imp["cliente_override_codigo"] is not None
            and imp["cliente_override_codigo"] == p.cliente_erp_codigo
        )
        if not (por_override or (cnpj and cnpj in ancoras)):
            continue
        if len(p.itens) != imp["n_itens"] or abs((p.data_pedido - dia).days) > JANELA_DIAS:
            continue
        saida.append(p)
    return saida


def parear(imports: list[dict], pedidos: list[PedidoErp], ja_ligados: set[int]) -> Pareamento:
    res = Pareamento()
    escolhas: dict[int, list[dict]] = {}
    for imp in imports:
        cands = _candidatos(imp, pedidos, ja_ligados)
        if len(cands) > 1:
            iguais = [
                p
                for p in cands
                if norm_numero(p.pedido_cliente) == norm_numero(imp["order_number"])
            ]
            if len(iguais) == 1:
                cands = iguais
        if not cands:
            res.sem_par.append(imp["id"])
        elif len(cands) > 1:
            res.ambiguos.append((imp["id"], sorted(p.erp_codigo for p in cands)))
        else:
            escolhas.setdefault(cands[0].erp_codigo, []).append(imp)
    por_codigo = {p.erp_codigo: p for p in pedidos}
    for codigo, imps in escolhas.items():
        alvo = norm_numero(por_codigo[codigo].pedido_cliente)
        imps.sort(key=lambda i: (norm_numero(i["order_number"]) != alvo, i["imported_at"], i["id"]))
        res.pares.append((imps[0]["id"], codigo))
        res.duplicatas.extend((i["id"], codigo) for i in imps[1:])
    res.pares.sort(key=lambda x: x[1])
    return res


# ── I/O ───────────────────────────────────────────────────────────────────────


class _Portal:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def chamar(self, metodo: str, caminho: str, corpo: dict | None = None):
        dados = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(
            self.base + caminho,
            data=dados,
            method=metodo,
            headers={"Content-Type": "application/json"} if dados else {},
        )
        try:
            with self._op.open(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:300]


def _imports_parsed(portal: _Portal) -> list[dict]:
    saida, vistos_fire = [], set()
    for offset in range(0, 5000, 500):
        st, j = portal.chamar("GET", f"/api/imported?limit=500&offset={offset}")
        if st != 200:
            raise SystemExit(f"GET /api/imported falhou: {st} {j}")
        for e in j["entries"]:
            if e.get("fire_codigo"):
                vistos_fire.add(int(e["fire_codigo"]))
            elif e["portal_status"] == "parsed":
                saida.append(e)
        if len(j["entries"]) < 500:
            break
    portal.ja_ligados = vistos_fire
    return saida


def _detalhe(portal: _Portal, e: dict) -> dict:
    st, j = portal.chamar("GET", f"/api/imported/{e['id']}")
    ent = j["entry"]
    itens = (ent.get("snapshot") or {}).get("items") or []
    return {
        "id": e["id"],
        "order_number": e["order_number"] or "",
        "customer_cnpj": ent.get("customer_cnpj"),
        "imported_at": e["imported_at"],
        "n_itens": len(itens),
        "delivery_cnpjs": {cnpj_digits(i.get("delivery_cnpj")) for i in itens} - {""},
        "cliente_override_codigo": ent.get("cliente_override_codigo"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--portal", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--ambiente", default="mm", help="slug do ambiente (cookie portal_env)")
    ap.add_argument(
        "--desde",
        type=date.fromisoformat,
        default=date(2026, 1, 1),
        help="só imports a partir desta data",
    )
    ap.add_argument("--aplicar", action="store_true", help="liga de verdade (padrão: dry-run)")
    args = ap.parse_args(argv)

    import firebirdsql

    from app.erp_eventos.leitura import LeitorFirebird

    portal = _Portal(args.portal)
    st, j = portal.chamar(
        "POST", "/api/auth/login", {"email": args.email, "password": os.environ["PORTAL_SENHA"]}
    )
    if st != 200:
        raise SystemExit(f"login falhou: {st}")
    st, envs = portal.chamar("GET", "/api/admin/environments")
    env = next((e for e in envs if e["slug"] == args.ambiente), None)
    if env is None:
        raise SystemExit(f"ambiente '{args.ambiente}' não existe")
    portal._op.addheaders = [("Cookie", f"portal_env={env['id']}")]

    parsed = [e for e in _imports_parsed(portal) if e["imported_at"][:10] >= args.desde.isoformat()]
    imports = [_detalhe(portal, e) for e in parsed]

    conn = firebirdsql.connect(
        host=_FIRE_MM[0],
        port=3050,
        database=_FIRE_MM[1],
        user="SYSDBA",
        password=os.environ["FB_PASSWORD"],
        charset="WIN1252",
        isolation_level=firebirdsql.ISOLATION_LEVEL_READ_COMMITED_RO,
    )
    try:
        abertos = LeitorFirebird(conn).abertos()
    finally:
        conn.close()

    r = parear(imports, abertos, portal.ja_ligados)
    print(
        f"imports parsed: {len(imports)} | pares: {len(r.pares)} | ambíguos: {len(r.ambiguos)} "
        f"| duplicatas: {len(r.duplicatas)} | sem par: {len(r.sem_par)}"
    )
    nomes = {i["id"]: i["order_number"] for i in imports}
    for imp_id, codigo in r.pares:
        print(f"  PAR   {nomes[imp_id]!r:34} -> Fire {codigo}")
    for imp_id, codigos in r.ambiguos:
        print(f"  AMBÍGUO {nomes[imp_id]!r:32} -> {codigos}")
    for imp_id, codigo in r.duplicatas:
        print(f"  DUPLICATA {nomes[imp_id]!r:30} (mesmo pedido Fire {codigo})")
    if not args.aplicar:
        print("DRY-RUN: nada foi ligado. Use --aplicar.")
        return 0
    falhas = 0
    for imp_id, codigo in r.pares:
        st, corpo = portal.chamar(
            "POST", f"/api/imported/{imp_id}/vincular-fire", {"fire_codigo": codigo}
        )
        print(f"  {st} {nomes[imp_id]!r} -> {codigo}" + ("" if st == 200 else f"  {corpo}"))
        falhas += st != 200
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
