"""Backup lógico (gbak) do Firebird pelo serviço do próprio servidor.

O arquivo `.fbk` é gravado no SERVIDOR, no caminho dado (o Portal não toca o
disco do servidor). Usado antes de escritas pontuais no cadastro do Fire.
"""
from __future__ import annotations

from typing import Any


def gbak(cfg: dict, destino_no_servidor: str, *, services: Any = None) -> None:
    """Faz o backup do banco de `cfg` e só retorna quando o serviço termina.

    `cfg`: o dict de `environments_repo.to_fb_config` (path, host, user,
    password). Levanta ValueError se não houver host (embedded não é o caso da
    MM) e propaga qualquer falha do serviço: quem chama NÃO pode seguir se o
    backup não terminou.

    `services`: módulo de serviços injetável (testes); por padrão `fdb.services`,
    importado aqui dentro porque o projeto só carrega o `fdb` quando usa.
    """
    host = (cfg.get("host") or "").strip()
    if not host:
        raise ValueError("Backup via serviço exige Firebird em TCP (host vazio).")
    path = (cfg.get("path") or "").strip()
    if not path or not (destino_no_servidor or "").strip():
        raise ValueError("Backup exige o caminho do banco e o destino no servidor.")

    if services is None:
        from fdb import services  # type: ignore[import]

    svc = services.connect(
        host=host,
        user=(cfg.get("user") or "SYSDBA").strip(),
        password=cfg.get("password") or "",
    )
    try:
        # `backup` é assíncrono; `wait()` consome o log até o serviço acabar.
        svc.backup(path, destino_no_servidor)
        svc.wait()
    finally:
        svc.close()
