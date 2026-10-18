"""Backup lógico (gbak) do Firebird pelo serviço do próprio servidor.

O arquivo `.fbk` é gravado no SERVIDOR, no caminho dado (o Portal não toca o
disco do servidor). Usado antes de escritas pontuais no cadastro do Fire.
"""
from __future__ import annotations

from typing import Any

# Última linha do log verbose do gbak quando o backup fecha direito.
_LINHA_FINAL = "closing file, committing, and finishing"


def _host_do_servico(cfg: dict) -> str:
    """`host` ou `host/porta` (forma que o Firebird aceita para o service
    manager). Porta validada como em `_fb_connect_kwargs`."""
    host = (cfg.get("host") or "").strip()
    if not host:
        raise ValueError("Backup via serviço exige Firebird em TCP (host vazio).")
    porta = (cfg.get("port") or "").strip()
    if not porta:
        return host
    try:
        int(porta)
    except ValueError as exc:
        raise ValueError(f"Porta do Firebird inválida: {porta!r}") from exc
    return f"{host}/{porta}"


def gbak(cfg: dict, destino_no_servidor: str, *, services: Any = None) -> str:
    """Faz o backup do banco de `cfg`, espera o serviço terminar e CONFERE o log.

    Devolve a última linha do log (prova). Levanta ValueError para config
    inválida (sem host, porta ruim) e RuntimeError, com as últimas 5 linhas do
    log, se alguma linha tiver "ERROR" ou faltar a linha final de sucesso. Falha
    do serviço propaga: quem chama NÃO pode seguir sem backup confirmado.

    `services`: módulo de serviços injetável (testes); por padrão `fdb.services`,
    importado aqui dentro porque o projeto só carrega o `fdb` quando usa.
    """
    host = _host_do_servico(cfg)
    path = (cfg.get("path") or "").strip()
    if not path or not (destino_no_servidor or "").strip():
        raise ValueError("Backup exige o caminho do banco e o destino no servidor.")

    if services is None:
        from fdb import services  # type: ignore[import]

    linhas: list[str] = []
    svc = services.connect(
        host=host,
        user=(cfg.get("user") or "SYSDBA").strip(),
        password=cfg.get("password") or "",
    )
    try:
        # `backup` é assíncrono; com `callback` ele consome o log até o fim e
        # `wait()` garante que o serviço terminou.
        svc.backup(path, destino_no_servidor, callback=linhas.append)
        svc.wait()
    finally:
        try:
            svc.close()
        except Exception:  # noqa: BLE001 — não mascara a falha original
            pass

    ultimas = "\n".join(linhas[-5:])
    if any("ERROR" in ln for ln in linhas):
        raise RuntimeError(f"Backup com erro no log do gbak. Últimas linhas:\n{ultimas}")
    if not any(_LINHA_FINAL in ln for ln in linhas):
        raise RuntimeError(f"Log do gbak sem a linha final de sucesso. Últimas linhas:\n{ultimas}")
    return linhas[-1].strip()
