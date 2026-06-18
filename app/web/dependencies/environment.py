"""FastAPI dependencies de ambiente.

`current_environment(request)` lê o env já hidratado pelo middleware.
Retorna 412 (Precondition Failed) se ambiente não foi selecionado —
o cliente HTTP/UI deve redirecionar pra `/selecionar-ambiente`.

`current_env_db()` é uma dependency que abre conexão pra DB do env atual.
Combinada com `current_environment`, simplifica handlers:

    @app.get("/api/files")
    def files(env=Depends(current_environment), conn=Depends(current_env_db)):
        rows = conn.execute("SELECT ... FROM imports").fetchall()
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request

from app.persistence import router


def current_environment(request: Request) -> dict:
    """Retorna o ambiente ativo da request (hidratado pelo middleware).

    Levanta 412 se ambiente não selecionado/inválido — cliente deve
    redirecionar para `/selecionar-ambiente`.
    """
    env = getattr(request.state, "environment", None)
    if env is None:
        raise HTTPException(
            status_code=412,
            detail="Selecione um ambiente para continuar.",
        )
    return env


def current_env_db(
    env: dict = Depends(current_environment),
) -> Iterator[sqlite3.Connection]:
    """Abre conexão para a DB do ambiente atual."""
    with router.env_connect(env["slug"]) as conn:
        yield conn
