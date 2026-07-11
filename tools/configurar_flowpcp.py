#!/usr/bin/env python3
"""Configura a integração FlowPCP (IDA: Importador → Flow) no ambiente do cliente.

Grava a config no `app_shared.db` (mesma coisa que a tela /admin/ambientes faz),
pede os 2 segredos na hora (nunca ficam no pacote), testa a conexão Firebird e —
só se ela conectar E você confirmar — liga o push de pedido (EXPORT_MODE=both).

Escopo HOJE = só IDA (Importador → Flow):
  • Produto : Importador lê PRODUTOS do Fire → empurra pro Flow (catálogo, dry-run).
  • Pedido  : pedido cadastrado no Fire → empurrado pro Flow (push síncrono).
A VOLTA (Flow → UPDATE no Fire) e o worker NÃO são configurados aqui (backlog).

Rodar do root da instalação:
  .venv\\Scripts\\python.exe tools\\configurar_flowpcp.py            # MM
  .venv/bin/python tools/configurar_flowpcp.py --slug mm

Idempotente: pode rodar de novo. Enter num segredo = mantém o valor atual.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

# ── Config de PRODUÇÃO (não-secreta — pode viver no pacote) ───────────────────
BASE_URL_PROD = "https://gestor.samflowsai.com.br"  # = flowpcp.fly.dev (mesmo app)
TENANT_MM_PROD = "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"
DEFAULT_SLUG = "mm"


def _bootstrap_env() -> None:
    """Carrega .env e ancora APP_DATA_DIR na pasta data/ da instalação, igual aos
    outros tools. Precisa rodar ANTES de importar os módulos de persistência."""
    base = Path(__file__).resolve().parent.parent
    try:
        from dotenv import load_dotenv

        load_dotenv(base / ".env")
    except Exception:  # noqa: BLE001 — .env é opcional; segue com os defaults
        pass
    os.environ.setdefault("APP_DATA_DIR", str(base / "data"))


def testar_firebird(env: dict) -> tuple[bool, str | None]:
    """(ok, erro) — abre a conexão Firebird do ambiente e roda SELECT 1.
    Mesma checagem do botão 'Testar conexão' da UI (routes_environments._try_firebird)."""
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    cfg = environments_repo.to_fb_config(env)
    try:
        with FirebirdConnection().connect_with_config(cfg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
        return True, None
    except Exception as exc:  # noqa: BLE001 — captura tudo pra diagnóstico
        return False, str(exc)


def gravar_flowpcp(
    slug: str,
    *,
    service_token: str | None,
    base_url: str = BASE_URL_PROD,
    tenant_id: str = TENANT_MM_PROD,
    request_timeout_s: float = 300.0,
    catalogo_push: bool | None = None,
) -> dict:
    """Liga o FlowPCP no ambiente `slug` (enabled=1, url, tenant, token, timeout).
    `service_token=None` mantém o token atual; string vazia limpa; valor substitui.
    `catalogo_push=None` PRESERVA o gate atual (re-rodar o configurador não pode
    desligar o que o admin ligou); True/False seta explicitamente.
    `request_timeout_s=300` por padrão — o promote de milhares de itens é lento.
    dry_run=1 por segurança (só afeta a VOLTA, que não usamos hoje)."""
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(slug)
    if env is None:
        disponiveis = ", ".join(e["slug"] for e in environments_repo.list_all()) or "(nenhum)"
        raise SystemExit(
            f"Ambiente '{slug}' não encontrado. Ambientes existentes: {disponiveis}"
        )
    if catalogo_push is None:
        catalogo_push = bool(env.get("flowpcp_catalogo_push"))
    environments_repo.set_flowpcp_config(
        env["id"],
        enabled=True,
        base_url=base_url,
        tenant_id=tenant_id,
        dry_run=True,
        request_timeout_s=request_timeout_s,
        catalogo_push=catalogo_push,
        service_token=service_token,
    )
    return environments_repo.get_by_slug(slug)


def promover_catalogo(slug: str):
    """Simula (dry-run) e DEPOIS promove (apply) o catálogo do Fire do ambiente
    `slug` no FlowPCP. Retorna (relatorio_simulacao, relatorio_promote). Import
    tardio de `run_catalogo_sync` pra permitir monkeypatch nos testes."""
    from app.integrations.flowpcp.catalogo_sync import run_catalogo_sync

    sim = run_catalogo_sync(slug, dry_run=True, full_sync=True)
    prom = run_catalogo_sync(slug, dry_run=False, full_sync=True)
    return sim, prom


def gravar_senha_firebird(slug: str, fb_password: str | None) -> dict:
    """Grava a senha do Firebird (cifrada) se informada; senão mantém. Retorna o env."""
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(slug)
    if env is None:
        raise SystemExit(f"Ambiente '{slug}' não encontrado.")
    if fb_password:
        environments_repo.update(env["id"], fb_password=fb_password)
        env = environments_repo.get_by_slug(slug)
    return env


def ligar_push_pedido() -> dict:
    """EXPORT_MODE=both — pedido cadastrado no Fire passa a ser empurrado pro Flow.
    ⚠️ É GLOBAL: vale para TODOS os ambientes (MM e Nasmar)."""
    from app import config as app_config

    return app_config.save(export_mode="both")


# ── UX interativa ────────────────────────────────────────────────────────────

def _prompt_segredo(rotulo: str) -> str | None:
    """getpass (não ecoa). Enter vazio → None (mantém o atual)."""
    val = getpass.getpass(f"  {rotulo} (Enter = manter atual): ").strip()
    return val or None


def _confirmar(pergunta: str, *, exigir: str = "SIM") -> bool:
    resp = input(f"  {pergunta} Digite '{exigir}' para confirmar: ").strip()
    return resp == exigir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configura integração FlowPCP (IDA)")
    parser.add_argument("--slug", default=DEFAULT_SLUG, help="ambiente a configurar (default: mm)")
    parser.add_argument(
        "--base-url", default=BASE_URL_PROD, help=f"URL do Flow (default: {BASE_URL_PROD})"
    )
    parser.add_argument(
        "--tenant-id", default=TENANT_MM_PROD, help="tenant do ambiente no Flow"
    )
    parser.add_argument(
        "--nao-interativo",
        action="store_true",
        help="não pergunta nada (usa só flags/valores atuais; não liga push)",
    )
    parser.add_argument(
        "--token", default=None, help="service token (evita o prompt; p/ execução automática)"
    )
    parser.add_argument(
        "--promover",
        action="store_true",
        help="após configurar+testar, roda Simular + Promover o catálogo (sem prompt)",
    )
    args = parser.parse_args(argv)

    _bootstrap_env()

    print("=" * 62)
    print("  Integração FlowPCP — IDA (Importador → Flow)")
    print(f"  Ambiente: {args.slug}   Flow: {args.base_url}")
    print("=" * 62)

    token = args.token
    fb_pw = None
    if not (args.nao_interativo or args.promover):
        print("\n[1] Segredos (colados aqui, nunca ficam no pacote):")
        if token is None:
            token = _prompt_segredo("Service token do FlowPCP")
        fb_pw = _prompt_segredo("Senha do Firebird (SYSDBA) deste ambiente")

    # 2) Grava a config FlowPCP (enabled + url + tenant + token).
    # --promover liga o gate de envio de catálogo (é o "setar para enviar");
    # sem ele o gate atual é preservado.
    env = gravar_flowpcp(
        args.slug, service_token=token, base_url=args.base_url, tenant_id=args.tenant_id,
        catalogo_push=True if args.promover else None,
    )
    print("\n[2] FlowPCP habilitado neste ambiente:")
    print(f"      enabled        = {bool(env['flowpcp_enabled'])}")
    print(f"      base_url       = {env['flowpcp_base_url']}")
    print(f"      tenant_id      = {env['flowpcp_tenant_id']}")
    print(f"      envia catálogo = {bool(env['flowpcp_catalogo_push'])}")

    # 3) Firebird: grava senha (se dada) e testa a conexão
    env = gravar_senha_firebird(args.slug, fb_pw)
    print("\n[3] Testando conexão com o Firebird do Fire...")
    fb_ok, fb_err = testar_firebird(env)
    if fb_ok:
        print("      OK — conectou e rodou SELECT 1.")
    else:
        print(f"      FALHOU — {fb_err}")
        print("\n  ► FlowPCP ficou CONFIGURADO, mas dormente (modo de exportação intacto).")
        print("    Sem Firebird conectável, produto e pedido não têm como rodar.")
        print("    Corrija a senha/engine Firebird e rode este configurador de novo.")
        return 1

    # 4) Modo automático: Simular + Promover o catálogo em prod (1 clique).
    if args.promover:
        print("\n[4] Catálogo — Simulando e Promovendo...")
        sim, prom = promover_catalogo(args.slug)
        cg = sim.contagens
        print(
            f"      Simular:  fire={cg.fire_total} flow={cg.flow_total} "
            f"match={cg.match_limpo} criar={cg.fire_only} flow_only={cg.flow_only}"
        )
        pm = prom.model_dump().get("promote") or {}
        print(
            f"      Promover: criados={pm.get('criados')} atualizados={pm.get('atualizados')} "
            f"desativados={pm.get('desativados')} flow_only={pm.get('flowOnly')} "
            f"erros={pm.get('erros')}"
        )
        print("\n" + "=" * 62)
        print("  Promote concluído.")
        print("=" * 62)
        return 0

    # 4b) Produto (catálogo): sync puxa do Fire e guarda no importador; o envio
    # ao Flow depende do gate "Enviar catálogo ao Flow" (flowpcp_catalogo_push).
    print("\n[4] Produto (catálogo): pronto.")
    print("      Rode:  SINCRONIZAR-CATALOGO.bat   (ou tools/sync_catalogo_fire.py --slug "
          f"{args.slug})")
    print("      Sempre atualiza a cópia local (puxa do Fire). Envio ao Flow: "
          f"{'LIGADO' if env['flowpcp_catalogo_push'] else 'DESLIGADO — ligue na tela do ambiente quando quiser enviar'}.")

    # 5) Pedido: com FlowPCP habilitado, o push ao Flow JÁ dispara no Gerar XLS.
    # EXPORT_MODE=both é outra coisa (gravar direto no Fire) e segue opcional.
    print("\n[5] Pedido (push ao Flow): dispara automaticamente no 'Gerar XLS'.")
    print("    O Fire continua via planilha/manual — nada muda na operação.")
    if args.nao_interativo:
        print("      (modo não-interativo: EXPORT_MODE intacto.)")
    elif _confirmar("OPCIONAL: gravar pedidos DIRETO no Fire também (EXPORT_MODE=both, global MM+Nasmar)?"):
        cfg = ligar_push_pedido()
        print(f"      OK — export_mode = {cfg['export_mode']}.")
    else:
        print("      Mantido em xlsx (Fire manual). Push ao Flow segue ativo no Gerar XLS.")

    print("\n" + "=" * 62)
    print("  Concluído. VOLTA (Flow→Fire) e worker = backlog, não configurados.")
    print("  Lembre: o token acima tem que ser IDÊNTICO ao IMPORTADOR_SERVICE_TOKEN")
    print("  setado no Flow (fly secrets set ... -a flowpcp).")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
