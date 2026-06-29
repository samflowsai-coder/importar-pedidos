#!/usr/bin/env python3
"""Fecha o E2E da ponte Importador<->FlowPCP (ambiente MM).

Pré-requisito do lado Fire: engine embedded Firebird 2.5 (arm64) restaurada num
caminho ESTÁVEL (nunca /tmp) e `FB_CLIENT_LIBRARY` apontando pra ela — via `.env`
ou env var. O backup do MM é ODS 11 (Firebird 2.5); engine 3.0/5.0 NÃO abre.

Modos:
  (sem flag)   PROBE read-only — valida a conexão Firebird (SELECT 1) + a
               conectividade com o Flow (list_decisoes). NÃO escreve em lugar
               nenhum. Use pra confirmar que o ambiente está pronto.
  --commit     E2E REAL — roda `poll_decisoes_once`: UPDATE DT_ENTREGA no Fire
               (conforme `dry_run` do ambiente) E confirmar-reconciliacao no
               Flow. MUTA o Fire e o feed do Flow. Só rode pra fechar de fato.

Rodar do ROOT do projeto:
  .venv/bin/python tools/close_flowpcp_e2e.py            # probe
  .venv/bin/python tools/close_flowpcp_e2e.py --commit   # E2E real
  FB_CLIENT_LIBRARY=/caminho/estavel/libfbclient.dylib .venv/bin/python tools/close_flowpcp_e2e.py
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

SLUG = "mm"


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _section(title: str) -> None:
    _p()
    _p(f"==== {title} ====")


def _check_client_library() -> tuple[bool, str]:
    """Valida FB_CLIENT_LIBRARY: existe? arquitetura bate com este python?"""
    lib = os.environ.get("FB_CLIENT_LIBRARY", "").strip()
    if not lib:
        return False, (
            "FB_CLIENT_LIBRARY não setado (.env ou env var).\n"
            "  Sem isso o Firebird embedded não conecta. Restaure a engine FB 2.5\n"
            "  arm64 num caminho estável e aponte FB_CLIENT_LIBRARY pra ela."
        )
    path = Path(lib)
    if not path.exists():
        return False, (
            f"FB_CLIENT_LIBRARY aponta pra caminho inexistente:\n  {lib}\n"
            "  (lembrete: /tmp é volátil — use um caminho que sobrevive a reboot)"
        )
    py_arch = os.uname().machine  # arm64 / x86_64
    try:
        out = subprocess.run(
            ["file", "-b", str(path)], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        out = ""
    detail = f"lib: {lib}\n  arch lib: {out or '?'} | python: {py_arch}"
    if out and py_arch not in out:
        return False, (
            detail + f"\n  ⚠ ARQUITETURA INCOMPATÍVEL: a lib precisa ser {py_arch} "
            "pra carregar neste python.\n  (rode o python sob a mesma arch da lib)"
        )
    return True, detail


def _probe_firebird(slug: str, lib_ok: bool) -> bool:
    from app.erp.connection import FirebirdConnection
    from app.erp.exceptions import FirebirdConnectionError
    from app.persistence import environments_repo

    if not lib_ok:
        _p("✗ pulando: FB_CLIENT_LIBRARY inválido (veja preflight).")
        return False
    env = environments_repo.get_by_slug(slug)
    if env is None:
        _p(f"✗ ambiente '{slug}' não encontrado.")
        return False
    fbcfg = environments_repo.to_fb_config(env)
    host_disp = fbcfg["host"] or "embedded"
    _p(f"path={fbcfg['path']!r}")
    _p(f"mode={host_disp} charset={fbcfg['charset']}")
    try:
        with FirebirdConnection().connect_with_config(fbcfg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            _p(f"✓ CONNECT OK — SELECT 1 → {cur.fetchone()}")
            try:
                cur.execute("SELECT COUNT(*) FROM CAB_VENDAS")
                _p(f"  CAB_VENDAS rows: {cur.fetchone()[0]}")
            except Exception as e:  # noqa: BLE001
                _p(f"  (count CAB_VENDAS pulado: {type(e).__name__})")
        return True
    except FirebirdConnectionError as e:
        _p(f"✗ FALHA Firebird: {str(e)[:300]}")
    except Exception as e:  # noqa: BLE001
        _p(f"✗ ERRO inesperado: {type(e).__name__}: {str(e)[:300]}")
    return False


def _probe_flow(cfg) -> tuple[bool, int]:
    import httpx

    from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError

    client = FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )
    try:
        resp = client.list_decisoes(limit=10)
        n = len(resp.decisoes)
        _p(f"✓ list_decisoes OK — total={n} proximo_cursor={resp.proximo_cursor!r}")
        for d in resp.decisoes:
            _p(
                f"  - id={d.id} status={d.status} pedido_erp={d.pedido_erp} "
                f"prazo_orig={d.prazo_entrega_original} pactuado={d.prazo_pactuado}"
            )
        return True, n
    except FlowPCPClientError as e:
        _p(f"✗ FALHA Flow (HTTP {e.status_code}): {e} body={(e.body or '')[:200]}")
    except httpx.TransportError as e:
        _p(f"✗ Flow INACESSÍVEL em {cfg.base_url}: {type(e).__name__}: {e}")
        _p("  → confirme que o pcp-app está no ar (node escutando a porta do base_url).")
    except Exception as e:  # noqa: BLE001
        _p(f"✗ ERRO inesperado: {type(e).__name__}: {str(e)[:200]}")
    finally:
        client.close()
    return False, 0


def _run_e2e(slug: str, cfg) -> bool:
    """Espelha o wiring do worker (`run_poll_flowpcp`) pra um único slug, com
    saída explícita do antes/depois do cursor."""
    from app.erp.connection import FirebirdConnection
    from app.integrations.flowpcp.client import FlowPCPClient
    from app.integrations.flowpcp.poll_decisoes import poll_decisoes_once
    from app.persistence import environments_repo, flowpcp_repo, router

    env = environments_repo.get_by_slug(slug)
    client = FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )
    try:
        with router.env_connect(slug) as conn, FirebirdConnection().connect_with_config(
            environments_repo.to_fb_config(env)
        ) as fire_conn:
            cur_before = flowpcp_repo.get_last_cursor(conn)
            n = poll_decisoes_once(
                client=client, fire_conn=fire_conn, conn=conn, config=cfg
            )
            cur_after = flowpcp_repo.get_last_cursor(conn)
        _p(f"✓ poll_decisoes_once concluído — decisões no lote: {n}")
        moved = (
            "avançou (lote 100% confirmado)"
            if cur_after != cur_before
            else "inalterado (alguma não confirmou, ou lote vazio)"
        )
        _p(f"  cursor: {cur_before!r} → {cur_after!r} ({moved})")
        return True
    except Exception as e:  # noqa: BLE001
        _p(f"✗ ERRO no E2E: {type(e).__name__}: {str(e)[:400]}")
        return False
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fechar E2E Importador<->FlowPCP (MM)")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Roda o E2E real (UPDATE no Fire + confirmar no Flow). Sem isso, só probe read-only.",
    )
    parser.add_argument("--slug", default=SLUG, help="Ambiente alvo (default: mm)")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    load_dotenv(base / ".env")
    os.environ.setdefault("APP_DATA_DIR", str(base / "data"))
    slug = args.slug

    _section("PREFLIGHT")
    lib_ok, lib_detail = _check_client_library()
    _p(lib_detail)

    from app.integrations.flowpcp.config import flowpcp_config_for_slug

    cfg = flowpcp_config_for_slug(slug)
    if cfg is None:
        _p(f"✗ ambiente '{slug}' não tem FlowPCP habilitado (ou não existe). Abortando.")
        return 2
    _p(f"config: enabled={cfg.enabled} base_url={cfg.base_url} tenant={cfg.tenant_id}")
    _p(
        f"        dry_run={cfg.dry_run} timeout={cfg.request_timeout_s}s "
        f"token_present={bool(cfg.service_token)}"
    )

    _section("FASE 1 — Firebird (read-only)")
    fire_ok = _probe_firebird(slug, lib_ok)

    _section("FASE 2 — Flow list_decisoes (read-only)")
    flow_ok, n_decisoes = _probe_flow(cfg)

    if not args.commit:
        _section("RESUMO (probe read-only)")
        _p(
            f"Firebird: {'OK' if fire_ok else 'FALHOU'} | "
            f"Flow: {'OK' if flow_ok else 'FALHOU'} | "
            f"decisões pendentes: {n_decisoes}"
        )
        if fire_ok and flow_ok:
            if n_decisoes == 0:
                _p(
                    "→ Tudo conectado. 0 decisões no Flow: semeie uma renegociação de data\n"
                    "  no pcp-app e rode de novo com --commit pra fechar o E2E."
                )
            else:
                _p(
                    "→ Tudo conectado e há decisões pendentes. Rode com --commit pra fechar\n"
                    "  o E2E (escreve no Fire + confirma no Flow)."
                )
        else:
            _p("→ Resolva as falhas acima antes do --commit.")
        return 0 if (fire_ok and flow_ok) else 1

    _section("FASE 3 — E2E REAL (--commit)  ⚠ ESCREVE NO FIRE + MUTA O FLOW")
    if not (fire_ok and flow_ok):
        _p("✗ abortando --commit: o probe falhou (Firebird ou Flow indisponível).")
        return 1
    escreve = (
        "NÃO escreve no Fire (dry_run), mas CONFIRMA no Flow"
        if cfg.dry_run
        else "UPDATE real no Fire + CONFIRMA no Flow"
    )
    _p(f"dry_run do ambiente = {cfg.dry_run}  →  {escreve}")
    ok = _run_e2e(slug, cfg)
    if ok:
        _p()
        _p(
            "→ E2E fechado. Confira no Fire o DT_ENTREGA do pedido reconciliado\n"
            "  e no Flow o status reconciliado (sai do feed de decisões pendentes)."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
