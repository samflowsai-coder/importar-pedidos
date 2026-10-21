"""Prova ponta a ponta do lote intercompany na CÓPIA LOCAL do Fire. Nunca produção.

O que faz (docs/ai/modules/consolidators.md):

1. Lê N pedidos reais da Nasmar na cópia `.4` (padrão: 1157, 1158, 1161, as pernas
   do lote manual #4619) e os insere de novo, como pernas de origem, pelo
   `FirebirdExporter` do Portal (`PEDIDO_CLIENTE` = `VAL-<CODIGO>`, para não bater
   na idempotência). Produto pelo SEQ original, cliente pelo CODIGO original.
2. Anexa as pernas ao lote (`integracao.anexar_perna_de_origem`), fecha
   (`fechamento.fechar_lote`) e cadastra o espelho na cópia `.7` pelo mesmo
   caminho do botão: `preparar_envio_espelho` → `export(obs=)` →
   `concluir_envio_espelho`.
3. Imprime linhas e quantidade antes/depois, preço × (1 − fator) por linha,
   `DT_ENTREGA` = menor data, `OBS`, `PEDIDO_CLIENTE`, cliente, e a comparação
   coluna a coluna de `CAB_VENDAS`/`CORPO_VENDAS` contra o lote manual.

Fora do escopo do script: `check_order` + `is_blocking` (trava de preço) e o de-para
do `_send_one_to_fire` — precisam do `fdb` e têm teste próprio. O espelho casa o
produto no `.7` pelo EAN/`CODPROD_ALTERN` do cadastro da Nasmar.

Driver: o `fdb` do Portal não fala com o Firebird do Mac, então o script abre a
conexão com `firebirdsql` (Python puro; mesmo SQL de `app/erp/queries.py`, mesmo
estilo `?` de parâmetro) e a injeta no `FirebirdExporter` (só `_open` é trocado:
o insert é o código de produção). `firebirdsql` não é dependência do projeto: está
no `.venv` do checkout principal, ou instale à parte.

Segurança:
- só `localhost`/`127.0.0.1`, só a porta da cópia (`LOCAL_FB_PORT`, padrão 3055;
  3050 é recusada), só `/firebird/data/nasmar.fdb` e `/firebird/data/mm.fdb`;
- SQLite do Portal num `APP_DATA_DIR` temporário, nunca o `data/` do repo;
- tudo numa transação por base e **rollback** no fim. `--gravar` comita (na cópia).
  O gerador `GEN_CORPO_VENDAS_CODIGO` anda mesmo com rollback (não é transacional).

Uso:
    python -I tools/lote_validar_copia.py --dry-run
    LOCAL_FB_PASSWORD=localdev .venv/bin/python -I tools/lote_validar_copia.py \\
        --cnpj-revenda <CNPJ da Nasmar>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOSTS_LOCAIS = ("localhost", "127.0.0.1")
PORTA_PRODUCAO = 3050
BANCOS = {"nasmar": "/firebird/data/nasmar.fdb", "mm": "/firebird/data/mm.fdb"}
NOMES = {"nasmar": "Nasmar (cópia .4)", "mm": "MM Americanense (cópia .7)"}


# --- argumentos e cercas -------------------------------------------------------


def _args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Valida o lote intercompany Nasmar -> MM na cópia local do Fire.",
    )
    p.add_argument("--pernas", default="1157,1158,1161", help="CAB_VENDAS.CODIGO na Nasmar (.4)")
    p.add_argument("--lote-manual", type=int, default=4619, help="CAB_VENDAS.CODIGO na MM (.7)")
    p.add_argument("--cnpj-revenda", help="CNPJ da Nasmar (cliente do espelho na MM)")
    p.add_argument("--nome-revenda", default="Nasmar")
    p.add_argument("--fator", default="0.07", help="fator do lote (0.07 = preço × 0,93)")
    p.add_argument("--modo", default="desconto", choices=("desconto", "divisor"))
    p.add_argument("--cliente-esperado", type=int, default=2, help="CADASTRO.CODIGO na MM")
    p.add_argument(
        "--importado-em",
        help="data de importação comum às pernas (AAAA-MM-DD); padrão: DATA_PEDIDO de cada uma",
    )
    p.add_argument("--codfigfiscal-origem", type=int, help="padrão: o da 1ª perna original")
    p.add_argument("--codfigfiscal-espelho", type=int, help="padrão: o do lote manual")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--gravar", action="store_true", help="comita na cópia (padrão: rollback)")
    p.add_argument("--dry-run", action="store_true", help="só imprime o plano, não conecta")
    return p.parse_args(argv)


def _porta() -> int:
    raw = os.environ.get("LOCAL_FB_PORT", "3055").strip()
    try:
        return int(raw)
    except ValueError:
        sys.exit(f"LOCAL_FB_PORT inválida: {raw!r}")


def _cercas(a: argparse.Namespace, porta: int) -> list[int]:
    if a.host not in HOSTS_LOCAIS:
        sys.exit(f"RECUSADO: host {a.host!r}. Só a cópia local ({', '.join(HOSTS_LOCAIS)}).")
    if porta == PORTA_PRODUCAO:
        sys.exit("RECUSADO: porta 3050 é a do servidor Firebird. A cópia local usa LOCAL_FB_PORT.")
    try:
        pernas = [int(x) for x in a.pernas.split(",") if x.strip()]
    except ValueError:
        sys.exit(f"--pernas inválido: {a.pernas!r}")
    if not pernas:
        sys.exit("--pernas vazio")
    if a.importado_em:
        try:
            date.fromisoformat(a.importado_em)
        except ValueError:
            sys.exit(f"--importado-em inválido: {a.importado_em!r}")
    try:
        fator = Decimal(a.fator)
    except Exception:  # noqa: BLE001
        sys.exit(f"--fator inválido: {a.fator!r}")
    if not fator.is_finite() or not (Decimal("0") <= fator < Decimal("1")):
        sys.exit("--fator fora de faixa (0 <= fator < 1)")
    return pernas


def _plano(a: argparse.Namespace, porta: int, pernas: list[int]) -> None:
    alvo = f"{a.host}:{porta}"
    print("PLANO (nada é conectado em --dry-run)")
    print(f"  cópia local           {alvo}  nasmar={BANCOS['nasmar']}  mm={BANCOS['mm']}")
    print("  SQLite do Portal      APP_DATA_DIR temporário (tempfile), nunca data/ do repo")
    print(f"  1. lê CAB/CORPO_VENDAS {pernas} na Nasmar e reinsere como VAL-<CODIGO>")
    print("     pelo FirebirdExporter (produto pelo SEQ, cliente pelo CODIGO originais)")
    print(f"  2. lote_config nasmar->mm ativo, fator {a.fator} ({a.modo}); anexa e fecha o lote")
    print("  3. espelho: preparar_envio_espelho -> export(obs=) na MM -> concluir_envio_espelho")
    print(f"  4. relatório + comparação com o lote manual #{a.lote_manual} (CAB e CORPO_VENDAS)")
    print(f"  5. {'COMMIT na cópia (--gravar)' if a.gravar else 'ROLLBACK nas duas bases'}")


# --- Firebird da cópia --------------------------------------------------------


def _conectar(host: str, porta: int, banco: str):
    try:
        import firebirdsql
    except ImportError:
        sys.exit(
            "firebirdsql não instalado neste Python. Use o .venv do checkout principal "
            "ou instale à parte (não é dependência do projeto)."
        )
    return firebirdsql.connect(
        host=host,
        port=porta,
        database=banco,
        user="SYSDBA",
        password=os.environ.get("LOCAL_FB_PASSWORD", "localdev"),
        charset="WIN1252",
    )


def _linhas(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.cursor()
    cur.execute(sql, params)
    nomes = [d[0].strip() for d in cur.description]
    out = [dict(zip(nomes, row, strict=True)) for row in cur.fetchall()]
    cur.close()
    return out


def _limpo(v):
    return v.strip() if isinstance(v, str) else v


def _dia(v) -> date | None:
    if v is None:
        return None
    return v.date() if isinstance(v, datetime) else v


# --- o fluxo --------------------------------------------------------------------


def _preparar_portal() -> Path:
    """APP_DATA_DIR temporário ANTES de importar `app` e o `app` desta árvore."""
    dados = Path(tempfile.mkdtemp(prefix="lote_validar_"))
    os.environ["APP_DATA_DIR"] = str(dados)
    os.environ["PORTAL_LOTES"] = "0"
    sys.path.insert(0, str(ROOT))
    import app

    if Path(app.__file__).resolve().parent != ROOT / "app":
        sys.exit(f"RECUSADO: `app` veio de {app.__file__}, não de {ROOT}")
    return dados


def _exporter(env: dict, conn):
    from app.exporters.firebird_exporter import FirebirdExporter

    class _ExporterCopia(FirebirdExporter):
        """O insert de produção numa conexão firebirdsql já aberta. Sem commit:
        o script decide no fim. Erro no meio desfaz só este pedido (savepoint)."""

        @contextmanager
        def _open(self):
            cur = conn.cursor()
            cur.execute("SAVEPOINT lote_validar")
            try:
                yield conn
            except BaseException:
                cur.execute("ROLLBACK TO SAVEPOINT lote_validar")
                raise
            finally:
                cur.close()

    return _ExporterCopia(env=env)


def _ler_perna(conn, codigo: int) -> dict:
    cab = _linhas(
        conn,
        "SELECT CODIGO, CLIENTE, PEDIDO_CLIENTE, DATA_PEDIDO, DT_ENTREGA, CODFIGFISCAL "
        "FROM CAB_VENDAS WHERE CODIGO = ?",
        (codigo,),
    )
    if not cab:
        sys.exit(f"pedido {codigo} não existe na cópia da Nasmar")
    cli = _linhas(
        conn, "SELECT CPF_CNPJ, RAZAO_SOCIAL FROM CADASTRO WHERE CODIGO = ?", (cab[0]["CLIENTE"],)
    )
    itens = _linhas(
        conn,
        """
        SELECT I.CODIGO, I.CODPRODUTO, I.DESCRICAO, I.QTD, I.PRECO_UNITARIO, I.DT_ENTREGA_ITEM,
               TRIM(P.CODPROD_ALTERN) AS ALTERN, TRIM(P.CODIGO_EAN13) AS EAN
        FROM CORPO_VENDAS I LEFT JOIN PRODUTOS P ON P.SEQ = I.CODPRODUTO
        WHERE I.CODVENDA = ? ORDER BY I.CODIGO
        """,
        (codigo,),
    )
    if not itens:
        sys.exit(f"pedido {codigo} da Nasmar sem itens")
    return {"cab": cab[0], "cli": cli[0] if cli else {}, "itens": itens}


def _order_da_perna(codigo: int, perna: dict):
    from app.models.order import Order, OrderHeader, OrderItem

    itens = []
    for it in perna["itens"]:
        entrega = _dia(it["DT_ENTREGA_ITEM"])
        itens.append(
            OrderItem(
                description=_limpo(it["DESCRICAO"]),
                product_code=_limpo(it["ALTERN"]) or None,
                ean=_limpo(it["EAN"]) or None,
                quantity=float(it["QTD"]),
                unit_price=float(it["PRECO_UNITARIO"]),
                delivery_date=entrega.strftime("%d/%m/%Y") if entrega else None,
            )
        )
    return Order(
        header=OrderHeader(
            order_number=f"VAL-{codigo}",
            customer_cnpj=_limpo(perna["cli"].get("CPF_CNPJ")),
            customer_name=_limpo(perna["cli"].get("RAZAO_SOCIAL")),
        ),
        items=itens,
        source_file=f"fire:{codigo}",
    )


def _ambientes(a: argparse.Namespace, host: str, porta: int, dados: Path, perfis: dict) -> dict:
    from app.persistence import environments_repo

    envs = {}
    for slug in ("nasmar", "mm"):
        env = environments_repo.create(
            slug=slug,
            name=NOMES[slug],
            watch_dir=str(dados / f"{slug}_in"),
            output_dir=str(dados / f"{slug}_out"),
            fb_path=BANCOS[slug],
            fb_host=host,
            fb_port=str(porta),
            cnpj=a.cnpj_revenda if slug == "nasmar" else None,
        )
        if perfis.get(slug) is not None:
            env = environments_repo.update(env["id"], fiscal_codfigfiscal=int(perfis[slug]))
        envs[slug] = env
    return envs


def _config_lote(a: argparse.Namespace) -> None:
    from app.persistence import lotes_repo, router

    agora = datetime.now()
    with router.shared_connect() as conn:
        lotes_repo.save_config(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            cnpj_revenda="".join(c for c in a.cnpj_revenda if c.isdigit()),
            nome_revenda=a.nome_revenda,
            janela="semanal",
            dia_inicio=0,
            hora_fechamento=8,
            ativo=True,
            por="validacao-copia",
            agora=agora,
        )
        # Vigente desde sempre: o script não testa a vigência (tem teste próprio).
        lotes_repo.registrar_fator(
            conn,
            env_origem_slug="nasmar",
            fator=Decimal(a.fator),
            modo=a.modo,
            vigente_desde=date(2000, 1, 1),
            por="validacao-copia",
            agora=agora,
        )


def _inserir_pernas(a, envs, fb_n, pernas: list[int], lidas: dict) -> tuple[int, dict]:
    from app.consolidators import integracao
    from app.persistence import context as env_context
    from app.persistence import repo

    env = envs["nasmar"]
    exp = _exporter(env, fb_n)
    lotes: set[int] = set()
    orders = {}
    for codigo in pernas:
        order = _order_da_perna(codigo, lidas[codigo])
        importado = (
            date.fromisoformat(a.importado_em)
            if a.importado_em
            else _dia(lidas[codigo]["cab"]["DATA_PEDIDO"])
        )
        ident = f"val-{codigo}"
        entry = {
            "id": ident,
            "environment_id": env["id"],
            "source_filename": f"validacao-{codigo}.fire",
            "imported_at": f"{importado.isoformat()}T09:00:00",
            "order_number": order.header.order_number,
            "customer_cnpj": order.header.customer_cnpj,
            "customer": order.header.customer_name,
            "status": "success",
            "snapshot": order.model_dump(),
            "portal_status": "parsed",
        }
        with env_context.active_env(env["id"], env["slug"]):
            repo.insert_import(entry)
        produtos = {i: int(it["CODPRODUTO"]) for i, it in enumerate(lidas[codigo]["itens"])}
        result = exp.export(
            order,
            override_client_id=int(lidas[codigo]["cab"]["CLIENTE"]),
            produtos_por_item=produtos,
        )
        if result.skipped or result.fire_codigo is None:
            raise RuntimeError(f"perna {codigo} recusada pelo exporter: {result.skip_reason}")
        lote_id = integracao.anexar_perna_de_origem(ident, entry, order, result, env)
        if lote_id is None:
            raise RuntimeError(f"perna {codigo} não anexada ao lote (ver log)")
        print(
            f"  perna {codigo} -> Nasmar CODIGO {result.fire_codigo} "
            f"({result.items_inserted} itens), importada {importado}, lote {lote_id}"
        )
        lotes.add(lote_id)
        orders[ident] = order
    if len(lotes) != 1:
        raise RuntimeError(
            f"pernas caíram em {len(lotes)} lotes {sorted(lotes)}: janelas diferentes. "
            "Use --importado-em para fixar a data."
        )
    return lotes.pop(), orders


def _enviar_espelho(envs, fb_m, lote_id: int):
    from app.consolidators import fechamento, integracao
    from app.models.order import Order
    from app.persistence import context as env_context
    from app.persistence import lotes_repo, repo, router

    r = fechamento.fechar_lote(lote_id, por="validacao-copia")
    if r["resultado"] != "fechado":
        raise RuntimeError(f"lote não fechou: {r}")
    ident = r["import_id_espelho"]
    env = envs["mm"]
    with env_context.active_env(env["id"], env["slug"]):
        entry = repo.get_import(ident)
    if entry is None:
        raise RuntimeError(f"espelho {ident} não materializado no ambiente mm")
    order = Order.model_validate(entry["snapshot"])
    envio = integracao.preparar_envio_espelho(ident, order, env)
    if not isinstance(envio, integracao.EnvioEspelho):
        raise RuntimeError(f"espelho bloqueado: {envio}")
    result = _exporter(env, fb_m).export(order, obs=envio.obs)
    if result.skipped or result.fire_codigo is None:
        integracao.abortar_envio_espelho(envio, env)
        raise RuntimeError(f"espelho recusado pelo exporter: {result.skip_reason}")
    with env_context.active_env(env["id"], env["slug"]):
        integracao.concluir_envio_espelho(envio, result, env)
    with router.shared_connect() as conn:
        lote = lotes_repo.get(conn, lote_id)
    return lote, order, result


# --- relatório ----------------------------------------------------------------


def _relatorio(a, fb_m, lidas, pernas, orders, lote, result) -> int:
    from app.consolidators.lote import preco_espelho

    problemas = 0
    fator = Decimal(lote["fator_preco_aplicado"])
    modo = lote["modo_preco_aplicado"]
    cab = _linhas(fb_m, "SELECT * FROM CAB_VENDAS WHERE CODIGO = ?", (result.fire_codigo,))[0]
    corpo = _linhas(
        fb_m, "SELECT * FROM CORPO_VENDAS WHERE CODVENDA = ? ORDER BY CODIGO", (result.fire_codigo,)
    )
    por_codigo = {int(c["CODIGO"]): c for c in corpo}

    print(f"\nLOTE {lote['chave_lote']}  status={lote['status']}  fator={fator} ({modo})")
    print("\nANTES (pernas da Nasmar)")
    linhas_antes, qtd_antes = 0, Decimal("0")
    for codigo in pernas:
        itens = lidas[codigo]["itens"]
        q = sum((Decimal(str(i["QTD"])) for i in itens), Decimal("0"))
        linhas_antes += len(itens)
        qtd_antes += q
        print(f"  {codigo}: {len(itens)} linhas, {q} unidades")
    qtd_depois = sum((Decimal(str(c["QTD"])) for c in corpo), Decimal("0"))
    print(f"  total: {linhas_antes} linhas, {qtd_antes} unidades")
    print(f"DEPOIS (espelho na MM, CODIGO {result.fire_codigo})")
    print(f"  {len(corpo)} linhas, {qtd_depois} unidades")
    if qtd_depois != qtd_antes:
        problemas += 1
        print(f"  !! quantidade não bate: {qtd_antes} -> {qtd_depois}")

    print(
        f"\nPREÇO por linha (origem × {'(1 − fator)' if modo == 'desconto' else '1/(1 + fator)'})"
    )
    linha_do_indice = {indice: codigo for codigo, indice in result.itens}
    for i, origens in enumerate(lote["composicao"]):
        brutos = sorted(
            {Decimal(str(orders[o["import_id"]].items[o["indice"]].unit_price)) for o in origens}
        )
        esperado = preco_espelho(brutos[0], fator=fator, modo=modo)
        linha = por_codigo.get(linha_do_indice.get(i, -1), {})
        gravado = linha.get("PRECO_UNITARIO")
        ok = len(brutos) == 1 and gravado is not None and Decimal(str(gravado)) == esperado
        problemas += 0 if ok else 1
        print(
            f"  {'ok' if ok else '!!'} linha {i:>3} prod {linha.get('CODPRODUTO')!s:>7} "
            f"qtd {linha.get('QTD')!s:>8}  origem {', '.join(map(str, brutos))} -> "
            f"esperado {esperado}  gravado {gravado}"
        )

    datas = [
        _dia(it["DT_ENTREGA_ITEM"])
        for c in pernas
        for it in lidas[c]["itens"]
        if it["DT_ENTREGA_ITEM"] is not None
    ]
    menor = min(datas) if datas else None
    dt = _dia(cab.get("DT_ENTREGA"))
    problemas += 0 if dt == menor else 1
    print(
        f"\nDT_ENTREGA      {dt}  (menor data das pernas: {menor}) {'ok' if dt == menor else '!!'}"
    )
    print(f"OBS             {_limpo(cab.get('OBS'))}")
    pc = _limpo(cab.get("PEDIDO_CLIENTE"))
    print(f"PEDIDO_CLIENTE  {pc} {'ok' if pc == lote['chave_lote'] else '!!'}")
    problemas += 0 if pc == lote["chave_lote"] else 1
    cliente_ok = int(cab["CLIENTE"]) == a.cliente_esperado
    problemas += 0 if cliente_ok else 1
    print(
        f"CLIENTE         {cab['CLIENTE']} (esperado {a.cliente_esperado}) {'ok' if cliente_ok else '!!'}"
    )
    print(
        f"lote_fiscal_ok  {lote['lote_fiscal_ok']} (SP1 não configurado no script: esperado False)"
    )

    _comparar(fb_m, cab, corpo, a.lote_manual)
    return problemas


def _comparar(fb_m, cab: dict, corpo: list[dict], manual: int) -> None:
    cab_m = _linhas(fb_m, "SELECT * FROM CAB_VENDAS WHERE CODIGO = ?", (manual,))
    if not cab_m:
        print(f"\nlote manual #{manual} não existe na cópia da MM — sem comparação")
        return
    print(f"\nCAB_VENDAS: espelho {cab['CODIGO']} × lote manual #{manual}")
    iguais = 0
    for col, v in cab.items():
        m = cab_m[0].get(col)
        if _limpo(v) == _limpo(m):
            iguais += 1
            continue
        print(f"  {col:<22} espelho={_limpo(v)!r:<28} manual={_limpo(m)!r}")
    print(f"  ({iguais} colunas iguais)")

    corpo_m = _linhas(
        fb_m, "SELECT * FROM CORPO_VENDAS WHERE CODVENDA = ? ORDER BY CODIGO", (manual,)
    )
    print(f"\nCORPO_VENDAS: {len(corpo)} linhas no espelho × {len(corpo_m)} no manual")

    def por_produto(rows):
        out: dict = {}
        for r in rows:
            out.setdefault(r["CODPRODUTO"], []).append(r)
        return out

    e, m = por_produto(corpo), por_produto(corpo_m)
    so_e, so_m = sorted(set(e) - set(m), key=str), sorted(set(m) - set(e), key=str)
    if so_e:
        print(f"  produtos só no espelho: {so_e}")
    if so_m:
        print(f"  produtos só no manual:  {so_m}")
    difs: dict[str, list] = {}
    for prod in sorted(set(e) & set(m), key=str):
        qe = sum((Decimal(str(r["QTD"])) for r in e[prod]), Decimal("0"))
        qm = sum((Decimal(str(r["QTD"])) for r in m[prod]), Decimal("0"))
        pe = sorted({Decimal(str(r["PRECO_UNITARIO"])) for r in e[prod]})
        pm = sorted({Decimal(str(r["PRECO_UNITARIO"])) for r in m[prod]})
        if qe != qm or pe != pm:
            print(f"  prod {prod}: qtd {qe} × {qm}  preço {pe} × {pm}")
        for col, v in e[prod][0].items():
            if col in ("CODIGO", "CODVENDA"):
                continue
            if _limpo(v) != _limpo(m[prod][0].get(col)):
                difs.setdefault(col, []).append((prod, _limpo(v), _limpo(m[prod][0].get(col))))
    print("  colunas que diferem (1ª linha de cada produto em comum):")
    for col, casos in sorted(difs.items()):
        prod, v, mv = casos[0]
        print(f"    {col:<22} {len(casos)} produtos; ex. prod {prod}: espelho={v!r} manual={mv!r}")
    if not difs:
        print("    nenhuma")


def main(argv: list[str] | None = None) -> int:
    a = _args(argv)
    porta = _porta()
    pernas = _cercas(a, porta)
    _plano(a, porta, pernas)
    if a.dry_run:
        return 0
    if not a.cnpj_revenda:
        sys.exit("--cnpj-revenda é obrigatório fora do --dry-run")

    dados = _preparar_portal()
    print(f"\nAPP_DATA_DIR temporário: {dados}")
    fb_n = _conectar(a.host, porta, BANCOS["nasmar"])
    fb_m = _conectar(a.host, porta, BANCOS["mm"])
    try:
        lidas = {c: _ler_perna(fb_n, c) for c in pernas}
        manual = _linhas(
            fb_m, "SELECT CODFIGFISCAL FROM CAB_VENDAS WHERE CODIGO = ?", (a.lote_manual,)
        )
        perfis = {
            "nasmar": a.codfigfiscal_origem
            if a.codfigfiscal_origem is not None
            else lidas[pernas[0]]["cab"]["CODFIGFISCAL"],
            "mm": a.codfigfiscal_espelho
            if a.codfigfiscal_espelho is not None
            else (manual[0]["CODFIGFISCAL"] if manual else None),
        }
        print(f"CODFIGFISCAL usado: nasmar={perfis['nasmar']} mm={perfis['mm']}")
        envs = _ambientes(a, a.host, porta, dados, perfis)
        _config_lote(a)
        print("\nPERNAS")
        lote_id, orders = _inserir_pernas(a, envs, fb_n, pernas, lidas)
        lote, _order, result = _enviar_espelho(envs, fb_m, lote_id)
        problemas = _relatorio(a, fb_m, lidas, pernas, orders, lote, result)
        if a.gravar:
            fb_n.commit()
            fb_m.commit()
            print("\nCOMMIT na cópia (--gravar).")
        else:
            fb_n.rollback()
            fb_m.rollback()
            print("\nROLLBACK: nada ficou gravado na cópia (o gerador de CORPO_VENDAS andou).")
        print(f"{problemas} problema(s) nas conferências marcadas com !!")
        return 1 if problemas else 0
    except BaseException:
        fb_n.rollback()
        fb_m.rollback()
        print("\nROLLBACK nas duas bases (erro).", file=sys.stderr)
        raise
    finally:
        fb_n.close()
        fb_m.close()


if __name__ == "__main__":
    sys.exit(main())
