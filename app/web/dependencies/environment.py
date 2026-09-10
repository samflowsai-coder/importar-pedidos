"""FastAPI dependencies de ambiente.

`current_environment(request)` lê o env já hidratado pelo middleware.
Retorna 412 (Precondition Failed) se ambiente não foi selecionado. Nenhuma
rota declara esta dependency hoje (verificado — zero `Depends(current_environment)`
no repo); o 412 que o operador realmente vê quando falta ambiente é
`NoActiveEnvironmentError` → `_no_env_handler` em `app/web/server.py`. Com
roteamento `ligado` não existe mais tela de seleção — não redirecione pra
`/selecionar-ambiente` a partir daqui nem do handler de verdade.

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

    Levanta 412 se ambiente não selecionado/inválido. Com o roteamento
    `ligado` não existe mais tela de seleção — o ambiente é propriedade do
    pedido, não da sessão — então a mensagem não manda "selecionar": diz a
    verdade, que é a ação em si que exige uma empresa específica.
    """
    env = getattr(request.state, "environment", None)
    if env is None:
        raise HTTPException(
            status_code=412,
            detail="Esta ação é de uma empresa específica — abra o pedido para agir nele.",
        )
    return env


def current_env_db(
    env: dict = Depends(current_environment),
) -> Iterator[sqlite3.Connection]:
    """Abre conexão para a DB do ambiente atual."""
    with router.env_connect(env["slug"]) as conn:
        yield conn
