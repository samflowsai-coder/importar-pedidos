"""Runner da reconciliação: amarra `fire_reconcile.buscar_no_fire` (leitura do
Fire) a `repo.list_parsed_for_reconcile` / `repo.mark_found_in_fire` (SQLite),
e é a função que os três gatilhos chamam (periódico no web, botão manual,
entrada do operador — ver `app/web/server.py`, `app/web/routes_env_select.py`
e `app/worker/scheduler.py`).

Política: NÃO filtra por `STATUS` do Fire. `found_in_fire` significa "existe
no Fire" — um pedido CANCELADO existe (foi cadastrado e depois cancelado).
Filtrar deixaria ele na fila de revisão para sempre sem a operadora entender
por quê. `mark_found_in_fire` já grava `fire_status_last_seen` com o status
real; a UI mostra junto do selo.

`reconciliar(env_slug)` resolve o ambiente pelo slug e ativa `active_env`
ELE MESMO — nunca herda do contexto ambiente. Isso importa para o gatilho de
entrada do operador: o `BackgroundTasks` que dispara em `routes_env_select.py`
roda depois que a resposta já foi enviada, quando o contexto do request (se
houvesse algum) já apontaria pro ambiente ANTERIOR, não pro recém-selecionado.
Resolver pelo slug passado, sempre, é o que evita reconciliar a empresa errada.

Trava (`_ULTIMA_EXECUCAO`) é em memória de PROCESSO — não coordena entre web e
worker (processos distintos). Isso é intencional: quem impede o evento
duplicado entre processos é o compare-and-set de `mark_found_in_fire` (Task 4);
a trava aqui só evita replay redundante DENTRO do mesmo processo (ex.: o loop
periódico do web disparando de novo antes de 10 minutos).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time

from app.erp import fire_reconcile
from app.erp.fire_reconcile import buscar_no_fire
from app.persistence import context as env_context
from app.persistence import environments_repo, repo, router
from app.utils.logger import logger

# Horários locais do gatilho periódico (07h/12h/18h) — mesma grade para o
# loop do processo web e o CronTrigger do worker (ver app/web/server.py e
# app/worker/scheduler.py).
HORARIOS_LOCAIS = (7, 12, 18)

# Janela da trava, em segundos. 10 minutos: curto o bastante para não atrasar
# uma reconciliação de verdade por muito tempo, longo o bastante para
# absorver o caso comum (o loop periódico do web e o cron do worker
# disparando perto um do outro no mesmo horário cheio).
_TRAVA_S = 600.0

# Clock injetável — mesma convenção de `fire_reconcile`/`depara_cliente`.
_clock = time.monotonic

# Timestamp (`_clock()`) da última execução bem-sucedida por env_slug.
_ULTIMA_EXECUCAO: dict[str, float] = {}


@dataclass(frozen=True)
class Resultado:
    """O que os três gatilhos recebem de volta de uma passada de reconciliação."""

    verificados: int
    casaram: int
    # `True` quando `buscar_no_fire` não conseguiu FALAR com o Fire (cool-down
    # de conexão armado) — distinto de "conversou e não achou nada". Sem essa
    # distinção a operadora vê "0 casaram" e conclui que a feature quebrou.
    erro_conexao: bool


def limpar_trava() -> None:
    """Zera a trava de execução (testes)."""
    _ULTIMA_EXECUCAO.clear()


def _trava_ativa(env_slug: str) -> bool:
    ultima = _ULTIMA_EXECUCAO.get(env_slug)
    return ultima is not None and (_clock() - ultima) < _TRAVA_S


def reconciliar(env_slug: str, *, respeitar_trava: bool = True) -> Resultado:
    """Reconcilia um ambiente. Nunca levanta — um Firebird fora não pode
    cancelar a varredura dos demais ambientes (cada um é uma chamada
    separada, dos três gatilhos) nem derrubar quem chamou (thread do
    startup, worker, handler HTTP)."""
    if respeitar_trava and _trava_ativa(env_slug):
        logger.info(f"reconcile.runner: trava ativa p/ '{env_slug}', pulando")
        return Resultado(verificados=0, casaram=0, erro_conexao=False)

    _ULTIMA_EXECUCAO[env_slug] = _clock()

    try:
        return _reconciliar_agora(env_slug)
    except Exception as exc:  # noqa: BLE001 — nunca levanta a partir daqui
        logger.warning(f"reconcile.runner: falha ao reconciliar '{env_slug}': {exc!r}")
        return Resultado(verificados=0, casaram=0, erro_conexao=True)


def _reconciliar_agora(env_slug: str) -> Resultado:
    env = environments_repo.get_by_slug(env_slug)
    if env is None:
        logger.warning(f"reconcile.runner: ambiente '{env_slug}' não existe")
        return Resultado(verificados=0, casaram=0, erro_conexao=False)

    with env_context.active_env(env["id"], env["slug"]):
        candidatos = repo.list_parsed_for_reconcile()
        if not candidatos:
            return Resultado(verificados=0, casaram=0, erro_conexao=False)

        try:
            achados = buscar_no_fire(candidatos, env_slug=env_slug)
        except Exception as exc:  # noqa: BLE001 — buscar_no_fire nunca deveria
            # levantar (contrato do módulo — ver docstring dele); blindagem
            # extra mesmo assim, porque quem chama precisa de um Resultado,
            # não de uma exceção subindo pro loop periódico/worker/handler.
            logger.warning(f"reconcile.runner: buscar_no_fire falhou p/ '{env_slug}': {exc!r}")
            return Resultado(verificados=len(candidatos), casaram=0, erro_conexao=True)

        erro_conexao = fire_reconcile.houve_falha_de_conexao(env_slug)

        agora = datetime.now(UTC).isoformat(timespec="seconds")
        casaram = 0
        for import_id, achado in achados.items():
            venceu = repo.mark_found_in_fire(
                import_id,
                fire_codigo=achado.fire_codigo,
                fire_status=achado.fire_status,
                caminho=achado.caminho,
                lojas_casadas=achado.lojas_casadas,
                at=agora,
            )
            if venceu:
                casaram += 1

        return Resultado(verificados=len(candidatos), casaram=casaram, erro_conexao=erro_conexao)


def reconciliar_todos_os_ambientes(*, respeitar_trava: bool = True) -> None:
    """Reconcilia cada ambiente ativo, um de cada vez. `reconciliar()` já
    nunca levanta — um ambiente quebrado não impede os demais aqui também."""
    for slug in router.list_env_slugs():
        reconciliar(slug, respeitar_trava=respeitar_trava)


def proxima_janela(agora: datetime) -> datetime:
    """Próximo horário (entre `HORARIOS_LOCAIS`), estritamente depois de
    `agora` — hoje se ainda não passou, senão o primeiro horário de amanhã."""
    hoje = agora.date()
    for hora in HORARIOS_LOCAIS:
        candidato = datetime.combine(hoje, dt_time(hour=hora))
        if candidato > agora:
            return candidato
    amanha = hoje + timedelta(days=1)
    return datetime.combine(amanha, dt_time(hour=HORARIOS_LOCAIS[0]))


def loop_periodico() -> None:
    """Dorme até a próxima janela (07h/12h/18h local) e reconcilia todos os
    ambientes ativos — para sempre. Chamado numa thread daemon do processo
    web (ver `app/web/server.py`): `scripts/setup-service.ps1` só registra
    `ui.py` como tarefa agendada no Windows do cliente, o worker nunca roda
    lá, então um job só no scheduler do worker nunca dispararia em produção.
    """
    while True:
        agora = datetime.now()
        alvo = proxima_janela(agora)
        time.sleep(max((alvo - agora).total_seconds(), 0.0))
        try:
            reconciliar_todos_os_ambientes()
        except Exception as exc:  # noqa: BLE001 — a thread daemon não pode morrer
            logger.error(f"reconcile.runner: loop_periodico falhou: {exc!r}")
