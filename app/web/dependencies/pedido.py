"""De qual empresa é este pedido?

O Portal roda N empresas em paralelo, cada uma com seu `app_state_<slug>.db`.
`imports.environment_id` é bind imutável — a empresa já é propriedade do
pedido. Este módulo é o que finalmente a lê de volta, para que agir num
pedido não dependa de ter uma empresa selecionada na sessão.

Só vale com `roteamento_modo = 'ligado'`. Nos outros modos a dependency sai
na hora e o cookie `portal_env` segue sendo a única fonte, exatamente como
antes — é o que permite deployar isto sem mudar nada para a operação.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.persistence import context as env_context
from app.persistence import environments_repo, roteamento_repo, router


def env_do_import_id(import_id: str) -> dict[str, Any] | None:
    """Empresa ATIVA que contém este pedido, ou `None`.

    `imports.id` é PRIMARY KEY em cada banco de empresa, então é acerto de
    índice por empresa.

    Usa `list_active()` de propósito: pedido de empresa desativada fica
    inalcançável. É coerente com `repo.list_imports_all_envs`, que também só
    soma ativas — o pedido nem aparece na caixa de entrada — e com o
    middleware, que já recusa cookie apontando para empresa inativa.
    """
    if not import_id:
        return None
    for env in environments_repo.list_active():
        with router.env_connect(env["slug"]) as conn:
            achou = conn.execute(
                "SELECT 1 FROM imports WHERE id = ? LIMIT 1", (import_id,)
            ).fetchone()
        if achou:
            return env
    return None


async def env_do_pedido(import_id: str):
    """Ativa a empresa dona deste pedido, quando o roteamento está ligado.

    **Tem que ser `async def`.** Medido em 2026-09-11: a versão síncrona com
    `yield` que entra num contextvar levanta
    `ValueError: Token was created in a different Context`, porque o FastAPI
    roda dependency síncrona via `contextmanager_in_threadpool` e o
    `__enter__`/`__exit__` caem em contextos diferentes. A variante `async`
    roda no mesmo contexto do handler, sync ou async.

    Em `ligado` o PEDIDO decide e o cookie não opina — o cookie passa a
    filtrar a listagem e nada mais. Isso resolve o link direto: abrir um
    pedido da Nasmar com "MM" no filtro funciona em vez de dar 404.
    """
    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        yield None
        return
    env = env_do_import_id(import_id)
    if env is None:
        raise HTTPException(
            status_code=404, detail="Pedido não encontrado em nenhuma empresa ativa"
        )
    with env_context.active_env(env["id"], env["slug"]):
        yield env
