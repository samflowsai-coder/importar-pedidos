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
periódico do web disparando de novo antes de 10 minutos, ou a operadora
clicando o botão duas vezes seguidas — ver `_lock_for` abaixo).

Fix round 1 (review): a trava por timestamp sozinha é rate-limit, não mutex —
um run que passe de 10 minutos deixa outro concorrente do mesmo ambiente
começar (ex.: entrada do operador dispara o background E ela clica o botão 5s
depois, que ignora a trava de propósito). Um `threading.Lock` por `env_slug`,
adquirido non-blocking (`acquire(blocking=False)`), resolve isso, o TOCTOU do
"checa a trava, depois grava o timestamp" (as duas etapas agora só acontecem
por quem já segura o lock), e a pré-condição de `erro_conexao` estar correto
(só uma reconciliação por ambiente em voo por vez neste processo).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Literal

from app.erp.fire_reconcile import _buscar_no_fire_detalhado
from app.observability.metrics import reconcile_fire_last_run_ok
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

# Um `Lock` por env_slug — protege contra dois `reconciliar()` concorrentes
# do MESMO ambiente rodando ao mesmo tempo (duplo clique no botão, thread
# periódica + entrada do operador se sobrepondo, etc). `_LOCKS_GUARD` só
# protege a criação da entrada no dict; a exclusão mútua de verdade é o
# `acquire(blocking=False)` em `reconciliar()`.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

# Guard de operação: PORTAL_RECONCILE_PERIODICO=0 desliga a reconciliação
# periódica (loop do web + CronTrigger do worker) sem redeploy. NÃO afeta o
# botão manual nem a entrada do operador — só os dois gatilhos que varrem
# TODOS os ambientes sozinhos. Também é o que evita a suíte de testes abrir
# Firebird de verdade se uma rodada atravessar 07h/12h/18h locais com a
# thread do startup de pé (`tests/test_metrics.py`, `tests/test_rate_limit.py`
# usam `with TestClient(app)`, que dispara o startup de verdade) —
# `tests/conftest.py` seta esse env var para "0" globalmente nos testes.
_ENV_VAR_PERIODICO = "PORTAL_RECONCILE_PERIODICO"


# Fix round 1 complemento (review): `status` substitui o antigo `erro_conexao:
# bool` — um campo único de motivo em vez de dois booleanos independentes.
# Os dois booleanos permitiam representar um estado impossível ("erro_conexao
# E já em execução" ao mesmo tempo, quando na real são mutuamente exclusivos
# — cada caminho de saída de `reconciliar()` só preenche UM motivo). Com
# `status`, o tipo já impede essa combinação inválida.
ResultadoStatus = Literal["ok", "erro_conexao", "em_execucao", "trava_ativa"]


@dataclass(frozen=True)
class Resultado:
    """O que os três gatilhos recebem de volta de uma passada de reconciliação.

    `status`:
      - `"ok"`: rodou e terminou normalmente. `casaram == 0` pode ser
        "não achei nada" — não é erro.
      - `"erro_conexao"`: rodou, mas não conseguiu FALAR com o Fire (ver
        `fire_reconcile._buscar_no_fire_detalhado`). Distinto de "conversou
        e não achou nada" — sem essa distinção a operadora vê "0 casaram" e
        conclui que a feature quebrou.
      - `"em_execucao"`: NÃO rodou — outra reconciliação do MESMO ambiente já
        está em andamento neste processo (`_lock_for`, `acquire(blocking=
        False)` negado). Mesma família de zero silencioso que
        `"erro_conexao"` existe para evitar, pelo caminho mais comum: a
        operadora troca de ambiente (dispara a entrada do operador em
        background), abre a tela e clica o botão — o lock nega, e sem este
        status ela veria "0 casaram" como se tivesse rodado e não achado
        nada.
      - `"trava_ativa"`: NÃO rodou — já rodou há menos de `_TRAVA_S`
        segundos e `respeitar_trava=True`. Só os gatilhos periódicos passam
        por aqui; o botão manual sempre usa `respeitar_trava=False` e nunca
        vê este status.
    """

    verificados: int
    casaram: int
    status: ResultadoStatus
    # Ponteiros de `fire_codigo` reapontados nesta corrida (pedidos que já
    # estavam marcados). Zero é o caso normal em regime permanente.
    corrigidos: int = 0


def limpar_trava() -> None:
    """Zera a trava de execução e os locks por-ambiente (testes)."""
    _ULTIMA_EXECUCAO.clear()
    with _LOCKS_GUARD:
        _LOCKS.clear()


def _trava_ativa(env_slug: str) -> bool:
    ultima = _ULTIMA_EXECUCAO.get(env_slug)
    return ultima is not None and (_clock() - ultima) < _TRAVA_S


def _lock_for(env_slug: str) -> threading.Lock:
    """Lock do ambiente, criado sob demanda. A criação é protegida por
    `_LOCKS_GUARD`; a exclusão mútua entre reconciliações concorrentes do
    MESMO ambiente é o `acquire(blocking=False)` de quem chama isto."""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(env_slug)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[env_slug] = lock
        return lock


def reconciliar(env_slug: str, *, respeitar_trava: bool = True) -> Resultado:
    """Reconcilia um ambiente. Nunca levanta — um Firebird fora não pode
    cancelar a varredura dos demais ambientes (cada um é uma chamada
    separada, dos três gatilhos) nem derrubar quem chamou (thread do
    startup, worker, handler HTTP).

    `respeitar_trava=False` (botão manual) ignora só o rate-limit de 10
    minutos — NÃO ignora o lock por ambiente: duplo clique no botão, ou o
    botão sobrepondo a entrada do operador, ainda serializa (a segunda
    chamada devolve na hora, sem esperar, em vez de disparar um segundo scan
    completo de até 500 candidatos)."""
    lock = _lock_for(env_slug)
    if not lock.acquire(blocking=False):
        logger.info(f"reconcile.runner: '{env_slug}' já em execução, pulando sem esperar")
        return Resultado(verificados=0, casaram=0, status="em_execucao")

    try:
        if respeitar_trava and _trava_ativa(env_slug):
            logger.info(f"reconcile.runner: trava ativa p/ '{env_slug}', pulando")
            return Resultado(verificados=0, casaram=0, status="trava_ativa")

        _ULTIMA_EXECUCAO[env_slug] = _clock()

        logger.info(f"reconcile.runner: iniciando '{env_slug}' (respeitar_trava={respeitar_trava})")
        try:
            resultado = _reconciliar_agora(env_slug)
        except Exception as exc:  # noqa: BLE001 — nunca levanta a partir daqui
            logger.warning(f"reconcile.runner: falha ao reconciliar '{env_slug}': {exc!r}")
            resultado = Resultado(verificados=0, casaram=0, status="erro_conexao")

        logger.info(
            f"reconcile.runner: concluído '{env_slug}' verificados={resultado.verificados} "
            f"casaram={resultado.casaram} status={resultado.status}"
        )
        reconcile_fire_last_run_ok.labels(environment=env_slug).set(
            0 if resultado.status == "erro_conexao" else 1
        )
        return resultado
    finally:
        lock.release()


def _reconciliar_agora(env_slug: str) -> Resultado:
    env = environments_repo.get_by_slug(env_slug)
    if env is None:
        logger.warning(f"reconcile.runner: ambiente '{env_slug}' não existe")
        return Resultado(verificados=0, casaram=0, status="ok")

    with env_context.active_env(env["id"], env["slug"]):
        candidatos = repo.list_parsed_for_reconcile()
        if not candidatos:
            # Nada em revisão ainda NÃO significa nada a fazer: os já marcados
            # continuam precisando de reconferência de representante e de
            # status. Retornar aqui direto desligaria a correção justamente
            # quando a fila de pendentes zera, que é o estado desejado.
            return Resultado(
                verificados=0,
                casaram=0,
                status="ok",
                corrigidos=_corrigir_representantes(env_slug),
            )

        try:
            achados, erro_conexao = _buscar_no_fire_detalhado(candidatos, env_slug=env_slug)
        except Exception as exc:  # noqa: BLE001 — _buscar_no_fire_detalhado
            # nunca deveria levantar (contrato do módulo — ver docstring
            # dele); blindagem extra mesmo assim, porque quem chama precisa
            # de um Resultado, não de uma exceção subindo pro loop
            # periódico/worker/handler.
            logger.warning(f"reconcile.runner: buscar_no_fire falhou p/ '{env_slug}': {exc!r}")
            return Resultado(verificados=len(candidatos), casaram=0, status="erro_conexao")

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

        # Segunda passada: reconferir quem JÁ está marcado. A primeira passada
        # só enxerga `parsed`, então um pedido marcado por uma regra pior de
        # representante ficaria com o ponteiro errado para sempre. Foi o que
        # aconteceu na MM: 58 dos 169 marcados dividiam `fire_codigo` com
        # outro pedido, porque a regra antiga elegia sempre a linha mais
        # antiga. Pular quando a primeira passada já não conseguiu falar com
        # o Fire — não há o que reconferir contra nada.
        corrigidos = 0 if erro_conexao else _corrigir_representantes(env_slug)

        status: ResultadoStatus = "erro_conexao" if erro_conexao else "ok"
        return Resultado(
            verificados=len(candidatos),
            casaram=casaram,
            status=status,
            corrigidos=corrigidos,
        )


def _corrigir_representantes(env_slug: str) -> int:
    """Reaponta pedidos já `found_in_fire` para a linha certa do Fire.

    Não muda estado e não grava evento de ciclo de vida: o pedido continua
    existindo no Fire: o que muda é QUAL linha o representa. Nunca levanta —
    corrigir ponteiro é melhoria, não pode derrubar a reconciliação que já
    deu certo.
    """
    try:
        marcados = repo.list_found_in_fire_for_recheck()
        if not marcados:
            return 0
        achados, erro = _buscar_no_fire_detalhado(marcados, env_slug=env_slug)
        if erro:
            return 0
        agora = datetime.now(UTC).isoformat(timespec="seconds")
        return sum(
            1
            for import_id, achado in achados.items()
            if repo.corrigir_representante(
                import_id,
                fire_codigo=achado.fire_codigo,
                fire_status=achado.fire_status,
                at=agora,
            )
        )
    except Exception as exc:  # noqa: BLE001 — ver docstring
        logger.warning(f"reconcile.runner: correção de representante falhou em '{env_slug}': {exc!r}")
        return 0


def _periodico_habilitado() -> bool:
    """`False` só quando `PORTAL_RECONCILE_PERIODICO=0` — qualquer outro
    valor (inclusive ausente) mantém ligado. Ver docstring de
    `_ENV_VAR_PERIODICO` acima para o porquê."""
    return os.environ.get(_ENV_VAR_PERIODICO, "1").strip() != "0"


def reconciliar_todos_os_ambientes(*, respeitar_trava: bool = True) -> None:
    """Reconcilia cada ambiente ativo, um de cada vez. `reconciliar()` já
    nunca levanta — um ambiente quebrado não impede os demais aqui também.

    Só os dois gatilhos VERDADEIRAMENTE periódicos passam por aqui (loop do
    web, `CronTrigger` do worker) — o botão manual e a entrada do operador
    chamam `reconciliar()` direto pra um único ambiente, e não são afetados
    pelo guard `PORTAL_RECONCILE_PERIODICO`."""
    if not _periodico_habilitado():
        logger.info(f"reconcile.runner: periódico desabilitado via {_ENV_VAR_PERIODICO}=0, pulando")
        return
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

    Loga ao subir, antes e depois de cada janela — num servidor Windows sem
    console, o log é a única forma de responder "o job das 12h rodou?" ou
    "a thread ainda está viva?"."""
    logger.info("reconcile.runner: loop_periodico iniciado")
    while True:
        agora = datetime.now()
        alvo = proxima_janela(agora)
        logger.info(f"reconcile.runner: loop_periodico dormindo até {alvo.isoformat()}")
        time.sleep(max((alvo - agora).total_seconds(), 0.0))

        logger.info("reconcile.runner: loop_periodico acordou, reconciliando ambientes ativos")
        try:
            reconciliar_todos_os_ambientes()
        except Exception as exc:  # noqa: BLE001 — a thread daemon não pode morrer
            logger.error(f"reconcile.runner: loop_periodico falhou: {exc!r}")
        else:
            logger.info("reconcile.runner: loop_periodico concluiu a janela")
