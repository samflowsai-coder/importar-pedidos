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

from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException, Request

from app.persistence import context as env_context
from app.persistence import environments_repo, roteamento_repo, router

_AUSENTE = object()


def env_do_import_id(
    import_id: str, envs: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Empresa ATIVA que contém este pedido, ou `None`.

    `imports.id` é PRIMARY KEY em cada banco de empresa, então é acerto de
    índice por empresa.

    Usa `list_active()` de propósito (por padrão): pedido de empresa
    desativada fica inalcançável. É coerente com `repo.list_imports_all_envs`,
    que também só soma ativas — o pedido nem aparece na caixa de entrada — e
    com o middleware, que já recusa cookie apontando para empresa inativa.

    `envs`: lista já resolvida, pro chamador que resolve N ids em lote
    hoistar a query pra fora do loop — ver `app/web/server.py` (batch de
    envio ao Fire / export XLSX). `None` mantém o comportamento de sempre.
    """
    if not import_id:
        return None
    for env in envs if envs is not None else environments_repo.list_active():
        with router.env_connect(env["slug"]) as conn:
            achou = conn.execute(
                "SELECT 1 FROM imports WHERE id = ? LIMIT 1", (import_id,)
            ).fetchone()
        if achou:
            return env
    return None


async def env_do_pedido(
    import_id: str, request: Request
) -> AsyncIterator[dict[str, Any] | None]:
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

    **Ativa as DUAS metades, como o `EnvironmentMiddleware` faz.** O contextvar
    resolve o SQLite (`db.connect()`, `repo.*`); `request.state.environment`
    resolve todo o resto — a pasta de saída (`_get_cfg_for_request`), a conexão
    Firebird (`_firebird_open_for_request`), o `env` do check de preço, o slug
    do FlowPCP e o perfil fiscal. Ativar só o contextvar amarrava o pedido a
    três empresas ao mesmo tempo: o SQLite na empresa do pedido, o ERP na
    empresa do cookie, e a pasta na config legada quando não havia cookie —
    que em `ligado` é o estado normal, não a exceção. Era HTTP 200, sem aviso,
    com o pedido de compra entrando no ERP da empresa errada.

    Medido neste FastAPI (0.136.0 / Starlette 1.0.0) antes de escolher este
    caminho: escrever em `request.state` de dentro de uma dependency `async`
    com `yield` chega no handler — sync ou async — e sobrepõe o valor que o
    middleware pôs a partir do cookie, porque `request.state` é uma view sobre
    `scope["state"]`, o mesmo dict dos dois lados. Uma regra aqui em vez de
    seis nos handlers.
    """
    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        yield None
        return
    env = env_do_import_id(import_id)
    if env is None:
        raise HTTPException(
            status_code=404, detail="Pedido não encontrado em nenhuma empresa ativa"
        )
    # Simétrico na saída: `request.state` é por-request e não vaza entre
    # requests, mas restaurar é barato e mantém a dependency sem efeito
    # residual se algo mais rodar depois do `yield`. Sentinela em vez de
    # `None` porque "não havia cookie" é o atributo AUSENTE, não `None`.
    #
    # A restauração roda no caminho de exceção também: a exit stack das
    # dependencies vive na MESMA task do endpoint, então o `finally` desenrola
    # antes de qualquer exception handler ver a exceção.
    #
    # ARMADILHA, se você for adicionar um middleware: `BaseHTTPMiddleware`
    # devolve de `call_next()` assim que recebe o `http.response.start` — ANTES
    # do corpo ser drenado, e portanto antes deste `finally` rodar. Um
    # middleware montado FORA do `EnvironmentMiddleware` que leia
    # `request.state.environment` no bloco depois do `call_next` pode ver a
    # empresa do PEDIDO em vez da restaurada. Hoje ninguém faz isso
    # (`_no_cache_html` só mexe em header), mas um middleware de auditoria ou
    # log "na saída" cairia direto nessa janela e atribuiria a ação à empresa
    # errada. Se precisar do ambiente na saída, leia-o DENTRO do handler.
    anterior = getattr(request.state, "environment", _AUSENTE)
    request.state.environment = env
    try:
        with env_context.active_env(env["id"], env["slug"]):
            yield env
    finally:
        if anterior is _AUSENTE:
            del request.state.environment
        else:
            request.state.environment = anterior
