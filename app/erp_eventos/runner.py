"""Thread do processo web que roda a SP1 para cada base configurada.

Por que no processo web: no Windows da MM só o `ui.py` sobe como serviço (o
worker nunca rodou lá) — mesma razão de `app/reconcile/runner.py`.

A cada `TICK_S`, para cada base ativa (`erp_bases`), sob a trava da base
(non-blocking: se a anterior ainda roda, pula):
1. vigia, se passaram 15 min da última (e sempre na primeira);
2. drenagem (manda pendentes; manda lote vazio se a vigia acabou de rodar);
3. foto noturna: cria no horário, continua o envio se ficou pela metade.

`PORTAL_ERP_VIGIA=0` desliga tudo sem redeploy (os testes desligam por padrão).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.erp import depara_cliente
from app.erp.connection import FirebirdConnection
from app.erp_eventos import consulta, drenagem, foto, produtor, vigia, vinculo
from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.envio import ClienteErpEventos, Ritmo
from app.erp_eventos.leitura import LeitorFirebird
from app.erp_eventos.modelo import PedidoErp
from app.persistence import context as env_context
from app.persistence import (
    environments_repo,
    erp_bases_repo,
    erp_envio_lote_repo,
    erp_estado_repo,
    erp_foto_repo,
    outbox_repo,
    router,
)
from app.persistence.erp_bases_repo import BaseErpConfig
from app.utils.janelas_envio import FUSO, avisar_variavel_obsoleta
from app.utils.logger import logger

TICK_S = 30.0
INTERVALO_VIGIA = timedelta(minutes=15)
JANELA_XLSX = timedelta(days=120)
_ENV_VAR = "PORTAL_ERP_VIGIA"

_ritmo = Ritmo()
PAUSA_APOS_FALHA = timedelta(minutes=5)
SINAL_DE_VIDA = timedelta(minutes=60)  # sem 202 por isto → lote vazio de sinal de vida
_FALHA_ERP: dict[tuple[str, str], datetime] = {}
_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


# Envio manual ("Enviar agora"): o request só registra o pedido; a thread atende.
INTERVALO_MANUAL = timedelta(seconds=60)
VALIDADE_RESULTADO = timedelta(hours=1)
PEDIDO_MANUAL_TTL = timedelta(minutes=5)  # = POLL_MAX_MS da tela: depois disso ninguém espera mais
_MANUAL_GUARD = threading.Lock()
_PEDIDO_MANUAL: dict[tuple[str, str], str] = {}  # base → envio_id aguardando a thread
_EM_ANDAMENTO: dict[tuple[str, str], str] = {}  # base → envio_id que a thread está atendendo
_ATENDIDO_EM: dict[tuple[str, str], datetime] = {}  # base → fim do último atendimento
_RESULTADOS: dict[str, dict] = {}


def habilitado() -> bool:
    return os.environ.get(_ENV_VAR, "1").strip() != "0"


def _lock(cfg: BaseErpConfig) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((cfg.environment_id, cfg.base), threading.Lock())


class EnvioManualRecusado(Exception):  # noqa: N818 - nome fixado pelo plano
    """`codigo`: vigia_desligada | desligada | sem_credencial | bloqueada | recuo |
    em_andamento | intervalo. `detalhe` é o texto da tela."""

    def __init__(self, codigo: str, detalhe: str):
        super().__init__(detalhe)
        self.codigo = codigo
        self.detalhe = detalhe


def _vencido(envio_id: str, agora: datetime) -> bool:
    r = _RESULTADOS.get(envio_id)
    return (
        bool(r)
        and r["situacao"] == "pendente"
        and r["pedido_em"] < formatar_instante(agora - PEDIDO_MANUAL_TTL)
    )


def _expirar_pedido(chave: tuple[str, str], agora: datetime) -> None:
    """Fecha como `expirado` um pedido que ninguém atendeu a tempo. Segura `_MANUAL_GUARD`."""
    envio_id = _PEDIDO_MANUAL.pop(chave, None)
    if envio_id is not None and envio_id in _RESULTADOS:
        _RESULTADOS[envio_id].update(
            situacao="terminado", parou_por="expirado", terminado_em=formatar_instante(agora)
        )


def _checar_manual(cfg: BaseErpConfig, agora: datetime) -> EnvioManualRecusado | None:
    """Corpo de `checar_envio_manual`; o chamador segura `_MANUAL_GUARD`."""
    if not habilitado():
        return EnvioManualRecusado(
            "vigia_desligada", "O envio de pedidos ao Flow está desligado neste portal."
        )
    motivo = erp_bases_repo.base_atendida(cfg)
    if motivo == "desligada":
        return EnvioManualRecusado("desligada", "Envio desligado nesta empresa.")
    if motivo == "sem_credencial":
        return EnvioManualRecusado(
            "sem_credencial",
            "Base ligada sem credencial: salve a credencial na edição do ambiente.",
        )
    with router.env_connect(cfg.env_slug) as conn:
        envio = erp_estado_repo.obter_envio(conn, cfg.base)
    if envio.bloqueio:
        return EnvioManualRecusado(
            "bloqueada",
            f"Envio parado: {consulta.texto_bloqueio(envio.bloqueio, envio.ultimo_http)}",
        )
    if envio.proxima_tentativa_em and envio.proxima_tentativa_em > formatar_instante(agora):
        hora = datetime.fromisoformat(envio.proxima_tentativa_em).astimezone(FUSO)
        return EnvioManualRecusado(
            "recuo", f"O Flow pediu para esperar: nova tentativa às {hora:%H:%M}."
        )
    chave = (cfg.environment_id, cfg.base)
    pendente = _PEDIDO_MANUAL.get(chave)
    if pendente is not None and _vencido(pendente, agora):
        _expirar_pedido(chave, agora)
    if chave in _PEDIDO_MANUAL or chave in _EM_ANDAMENTO:
        return EnvioManualRecusado(
            "em_andamento", "Já tem um envio manual em andamento para esta empresa."
        )
    atendido = _ATENDIDO_EM.get(chave)
    if atendido is not None and agora - atendido < INTERVALO_MANUAL:
        return EnvioManualRecusado("intervalo", "Aguarde um minuto entre envios manuais.")
    return None


def checar_envio_manual(cfg: BaseErpConfig, agora: datetime) -> EnvioManualRecusado | None:
    """Mesmas checagens de `pedir_envio_manual`, sem gravar nada (usada pela tela)."""
    with _MANUAL_GUARD:
        return _checar_manual(cfg, agora)


def pedir_envio_manual(cfg: BaseErpConfig, quem: str, *, agora: datetime | None = None) -> str:
    """Registra o pedido; a thread o atende no próximo tick. Não lê o Fire nem
    fala com o Flow. Levanta `EnvioManualRecusado`."""
    agora = agora or _relogio()
    chave = (cfg.environment_id, cfg.base)
    with _MANUAL_GUARD:
        recusa = _checar_manual(cfg, agora)
        if recusa is not None:
            raise recusa
        envio_id = str(uuid.uuid4())
        _PEDIDO_MANUAL[chave] = envio_id
        _RESULTADOS[envio_id] = {
            "envio_id": envio_id,
            "environment_id": cfg.environment_id,
            "base": cfg.base,
            "quem": quem,
            "situacao": "pendente",
            "pedido_em": formatar_instante(agora),
            "iniciado_em": None,
            "terminado_em": None,
            "vigia": None,
            "lotes": 0,
            "aceitos": 0,
            "duplicados": 0,
            "rejeitados": 0,
            "parou_por": None,
        }
    return envio_id


def resultado_manual(envio_id: str) -> dict | None:
    """Estado do pedido; `None` se desconhecido ou terminado há mais de 1 h."""
    agora = _relogio()
    limite = formatar_instante(agora - VALIDADE_RESULTADO)
    with _MANUAL_GUARD:
        for chave, pendente in list(_PEDIDO_MANUAL.items()):
            if _vencido(pendente, agora):
                _expirar_pedido(chave, agora)
        for k in [
            k for k, r in _RESULTADOS.items() if r["terminado_em"] and r["terminado_em"] < limite
        ]:
            del _RESULTADOS[k]
        r = _RESULTADOS.get(envio_id)
        return dict(r) if r else None


def _retirar_pedido_manual(cfg: BaseErpConfig, agora: datetime) -> tuple[str, str] | None:
    chave = (cfg.environment_id, cfg.base)
    with _MANUAL_GUARD:
        envio_id = _PEDIDO_MANUAL.get(chave)
        if envio_id is None:
            return None
        if _vencido(envio_id, agora):
            _expirar_pedido(chave, agora)
            return None
        del _PEDIDO_MANUAL[chave]
        _EM_ANDAMENTO[chave] = envio_id
        r = _RESULTADOS[envio_id]
        r["situacao"] = "em_andamento"
        r["iniciado_em"] = formatar_instante(agora)
        return envio_id, r["quem"]


def _drenar_manual(cfg: BaseErpConfig, envio_id: str, quem: str):
    cliente = ClienteErpEventos(
        destino_url=cfg.destino_url, tenant_id=cfg.tenant_id, token=cfg.token or ""
    )
    try:
        return drenagem.drenar(
            cfg, cliente=cliente, ritmo=_ritmo, origem="manual", quem=quem, envio_id=envio_id
        )
    finally:
        cliente.close()


def _atender_manual(
    cfg: BaseErpConfig, envio_id: str, quem: str, agora: datetime, vigiar_fn
) -> bool:
    """drenar (se há fila) → vigia forçada → drenar. Devolve se o ERP segue liberado."""
    chave = (cfg.environment_id, cfg.base)
    falhou_em = _FALHA_ERP.get(chave)
    erp_liberado = falhou_em is None or agora - falhou_em >= PAUSA_APOS_FALHA
    parcial = {"vigia": None, "lotes": 0, "aceitos": 0, "duplicados": 0, "rejeitados": 0}
    parou_por = None

    def somar(res) -> None:
        nonlocal parou_por
        parcial["lotes"] += res.lotes
        parcial["aceitos"] += res.aceitos
        parcial["duplicados"] += res.duplicados
        parcial["rejeitados"] += res.rejeitados
        parou_por = res.parou_por or parou_por

    try:
        with router.env_connect(cfg.env_slug) as conn:
            ha_fila = bool(erp_estado_repo.pendentes(conn, cfg.base, limite=1))
        if ha_fila:  # manda o que já existe antes de ler o ERP (fdb sem timeout)
            somar(_drenar_manual(cfg, envio_id, quem))
        if erp_liberado:
            if _etapa(cfg, "vigia manual", vigiar_fn):
                _FALHA_ERP.pop(chave, None)
                parcial["vigia"] = "ok"
                # Enviar agora com fila vazia ainda manda 1 lote (vazio, origem manual).
                with router.env_connect(cfg.env_slug) as conn:
                    erp_estado_repo.pedir_sinal_de_vida(conn, cfg.base, formatar_instante(agora))
            else:
                _FALHA_ERP[chave] = agora
                erp_liberado = False
                parcial["vigia"] = "falhou"
        else:
            parcial["vigia"] = "pulada"
        somar(_drenar_manual(cfg, envio_id, quem))
    except Exception as exc:  # noqa: BLE001 — o pedido sempre termina
        logger.error(f"erp_eventos: envio manual {envio_id} da base '{cfg.base}' falhou: {exc!r}")
        parou_por = "erro"
    finally:
        fim = _relogio()
        with _MANUAL_GUARD:
            _RESULTADOS[envio_id].update(
                parcial,
                parou_por=parou_por,
                situacao="terminado",
                terminado_em=formatar_instante(fim),
            )
            _EM_ANDAMENTO.pop(chave, None)
            _ATENDIDO_EM[chave] = fim
    logger.info(f"erp_eventos: envio manual {envio_id} ({quem}) base '{cfg.base}': {parcial}")
    return erp_liberado


def _resolver_clientes(cfg: BaseErpConfig):
    def resolver(pedidos: list[PedidoErp]) -> dict[int, str | None]:
        saida = {p.erp_codigo: p.cliente_erp_cnpj for p in pedidos}
        if cfg.papel != "produtora" or not cfg.intermediarios or not cfg.revenda_slug:
            return saida
        alvo = [p for p in pedidos if p.cliente_erp_cnpj in cfg.intermediarios]
        if not alvo:
            return saida
        res = depara_cliente.resolver_clientes_reais(
            [p.pedido_cliente or "" for p in alvo], revenda_slug=cfg.revenda_slug
        )
        for p in alvo:
            r = res.get((p.pedido_cliente or "").strip())
            # cliente final desconhecido → None (o Flow mostra "cliente final
            # desconhecido"; nunca o intermediário no lugar do cliente)
            saida[p.erp_codigo] = r.cnpj if r and r.resolvido and len(r.cnpj or "") == 14 else None
        return saida

    return resolver


def _quantidade_enviada(cfg: BaseErpConfig):
    """Quantidade que o /recebimento levou em `itens[indice]` (payload no outbox)."""
    cache: dict[str, list | None] = {}

    def consultar(import_id: str, indice: int) -> Decimal | None:
        if import_id not in cache:
            with env_context.active_env(cfg.environment_id, cfg.env_slug):
                row = outbox_repo.find_by_idempotency_key(f"send-{import_id}")
            cache[import_id] = (row.payload or {}).get("itens") if row else None
        itens = cache[import_id]
        if not itens or indice >= len(itens):
            return None
        qtd = itens[indice].get("quantidade")
        return Decimal(str(qtd)) if qtd is not None else None

    return consultar


def _casar_vinculos(cfg: BaseErpConfig, agora: datetime):
    def casar(novos: list[PedidoErp]) -> None:
        if not novos:
            return
        with router.env_connect(cfg.env_slug) as conn:
            vinculo.casar_novos_por_xlsx(
                conn,
                base=cfg.base,
                pedidos=novos,
                comparar_preco=vinculo.COMPARAR_PRECO,
                quantidade_enviada=_quantidade_enviada(cfg),
                desde=formatar_instante(agora - JANELA_XLSX),
                agora=formatar_instante(agora),
            )

    return casar


def processar_base(cfg: BaseErpConfig, *, agora: datetime | None = None) -> None:
    lock = _lock(cfg)
    if not lock.acquire(blocking=False):
        logger.info(f"erp_eventos: base '{cfg.base}' ainda em execução, pulando")
        return
    try:
        agora = agora or datetime.now(UTC)
        env = environments_repo.get(cfg.environment_id)
        if env is None:
            return
        with router.env_connect(cfg.env_slug) as conn:
            envio = erp_estado_repo.obter_envio(conn, cfg.base)
            foto_hoje = erp_foto_repo.foto_do_dia(conn, cfg.base, foto.dia_local(agora))
        vigia_devida = envio.ultima_vigia_em is None or envio.ultima_vigia_em <= formatar_instante(
            agora - INTERVALO_VIGIA
        )
        resolver_clientes = _resolver_clientes(cfg)

        def resolver_produtores(pedidos):
            return produtor.resolver(cfg, pedidos, agora=agora)

        fb_cfg = environments_repo.to_fb_config(env)

        # Cada etapa isolada: ERP fora do ar ou foto inviável nunca impedem a
        # drenagem do que já está na fila nem o sinal de vida (revisão C1).
        # ERP fora do ar: vigia e foto tentam de novo em 5 min, não a cada 30 s
        # (`fdb` não tem timeout; host inalcançável segura a conexão).
        # A fila sai em todo tick (sem janela) e ANTES de ler o ERP: uma vigia
        # travada (fdb sem timeout) não segura o envio nem o sinal de vida. O que
        # a vigia enfileirar agora sai no próximo tick.
        _etapa(cfg, "envio", lambda: _drenar_fila(cfg))

        chave = (cfg.environment_id, cfg.base)
        falhou_em = _FALHA_ERP.get(chave)
        erp_liberado = falhou_em is None or agora - falhou_em >= PAUSA_APOS_FALHA
        # Pedido manual: a vigia forçada dele toma o lugar da vigia normal deste tick.
        manual = _retirar_pedido_manual(cfg, agora)
        if manual is not None:
            erp_liberado = _atender_manual(
                cfg,
                *manual,
                agora,
                lambda: _vigiar(cfg, fb_cfg, agora, resolver_clientes, resolver_produtores),
            )
        elif vigia_devida and erp_liberado:
            if _etapa(
                cfg,
                "vigia",
                lambda: _vigiar(cfg, fb_cfg, agora, resolver_clientes, resolver_produtores),
            ):
                _FALHA_ERP.pop(chave, None)
            else:
                _FALHA_ERP[chave] = agora
                erp_liberado = False
        if erp_liberado and foto.devida(cfg, agora, foto_hoje is not None):
            # Pedido fora do contrato não é falha (vira `nao_criada` dentro de
            # foto.criar); exceção aqui é ERP/leitura, e pausa como a vigia.
            if not _etapa(
                cfg,
                "foto",
                lambda: _criar_foto(cfg, fb_cfg, resolver_clientes, resolver_produtores),
            ):
                _FALHA_ERP[chave] = agora
        _etapa(cfg, "envio da foto", lambda: _enviar_foto(cfg))
        if _retencao_devida(cfg, agora):
            _etapa(cfg, "retenção", lambda: _reter(cfg, agora))
    except Exception as exc:  # noqa: BLE001 — uma base ruim não derruba as outras nem a thread
        logger.error(f"erp_eventos: base '{cfg.base}' falhou neste ciclo: {exc!r}")
    finally:
        lock.release()


_FOTO_AGUARDANDO: dict[str, int] = {}


def _enviar_foto(cfg: BaseErpConfig) -> None:
    """A foto só sai depois de tudo o que foi observado ATÉ o corte dela.

    A foto sai no `foto_hora` (5h) e a fila sai em todo tick; mesmo assim, se a
    fila anterior ao corte ainda não entrou (Flow recuando, base bloqueada), um
    pedido faturado às 19h estaria na foto antes de o Flow receber o evento, e
    a conciliação da manhã divergiria. Por isso a foto espera essa fila."""
    with router.env_connect(cfg.env_slug) as conn:
        f = erp_foto_repo.em_envio(conn, cfg.base)
    if f is None or foto.expirar_se_vencida(cfg, f, _relogio()):
        return
    with router.env_connect(cfg.env_slug) as conn:
        devendo = erp_estado_repo.pendentes_ate(conn, cfg.base, f.corte_em)
    cliente = ClienteErpEventos(
        destino_url=cfg.destino_url, tenant_id=cfg.tenant_id, token=cfg.token or ""
    )
    try:
        if devendo:
            # Só o que é anterior ao corte: o resto segue o fluxo normal.
            drenagem.drenar(cfg, cliente=cliente, ritmo=_ritmo, ate=f.corte_em, origem="foto")
            with router.env_connect(cfg.env_slug) as conn:
                devendo = erp_estado_repo.pendentes_ate(conn, cfg.base, f.corte_em)
            if devendo:
                if _FOTO_AGUARDANDO.get(f.foto_id) != devendo:  # log só quando muda
                    _FOTO_AGUARDANDO[f.foto_id] = devendo
                    logger.warning(
                        f"erp_eventos: foto {f.foto_id} da base '{cfg.base}' aguarda {devendo} "
                        "evento(s) anteriores ao corte"
                    )
                return
        _FOTO_AGUARDANDO.pop(f.foto_id, None)
        foto.enviar(cfg, cliente=cliente, ritmo=_ritmo)
    finally:
        cliente.close()


def _relogio() -> datetime:
    return datetime.now(UTC)


def _drenar_fila(cfg: BaseErpConfig) -> None:
    agora = _relogio()  # hora do envio, não a do início do tick
    _garantir_sinal_de_vida(cfg, agora)
    cliente = ClienteErpEventos(
        destino_url=cfg.destino_url, tenant_id=cfg.tenant_id, token=cfg.token or ""
    )
    try:
        drenagem.drenar(cfg, cliente=cliente, ritmo=_ritmo, agora_fn=_relogio)
    finally:
        cliente.close()


def _garantir_sinal_de_vida(cfg: BaseErpConfig, agora: datetime) -> None:
    """O Flow usa o 202 como sinal de vida e alerta se a base calar. Sem 202 há
    `SINAL_DE_VIDA` → pede o lote vazio (a drenagem o manda se a fila estiver
    vazia; com fila cheia, o próprio lote com dado é o 202)."""
    limite = formatar_instante(agora - SINAL_DE_VIDA)
    with router.env_connect(cfg.env_slug) as conn:
        envio = erp_estado_repo.obter_envio(conn, cfg.base)
        if not envio.sinal_pendente and (envio.ultimo_202_em or "") < limite:
            erp_estado_repo.pedir_sinal_de_vida(conn, cfg.base, formatar_instante(agora))


def _etapa(cfg: BaseErpConfig, nome: str, fn) -> bool:
    try:
        fn()
        return True
    except Exception as exc:  # noqa: BLE001 — ver processar_base
        logger.error(f"erp_eventos: base '{cfg.base}' — {nome} falhou: {exc!r}")
        return False


def _vigiar(cfg, fb_cfg, agora, resolver_clientes, resolver_produtores) -> None:
    with FirebirdConnection().connect_somente_leitura(fb_cfg) as fb:
        vigia.vigiar(
            cfg,
            leitor=LeitorFirebird(fb),
            agora=agora,
            resolver_clientes=resolver_clientes,
            resolver_produtores=resolver_produtores,
            casar_vinculos=_casar_vinculos(cfg, agora),
        )


def _criar_foto(cfg, fb_cfg, resolver_clientes, resolver_produtores) -> None:
    corte = datetime.now(UTC)
    with FirebirdConnection().connect_somente_leitura(fb_cfg) as fb:
        abertos = LeitorFirebird(fb).abertos()
    pedidos = vigia.enriquecer(
        abertos, resolver_clientes=resolver_clientes, resolver_produtores=resolver_produtores
    )
    foto.criar(cfg, corte=corte, pedidos=pedidos)


# Retenção da fila (revisão M2): uma vez por dia por base, neste processo.
RETENCAO_FILA = timedelta(days=30)
_ULTIMA_RETENCAO: dict[tuple[str, str], str] = {}


def _retencao_devida(cfg: BaseErpConfig, agora: datetime) -> bool:
    return _ULTIMA_RETENCAO.get((cfg.environment_id, cfg.base)) != foto.dia_local(agora)


def _reter(cfg: BaseErpConfig, agora: datetime) -> None:
    with router.env_connect(cfg.env_slug) as conn:
        corte = formatar_instante(agora - RETENCAO_FILA)
        n = erp_estado_repo.apagar_entregues_antigos(conn, corte)
        erp_envio_lote_repo.apagar_antigos(conn, corte)
    _ULTIMA_RETENCAO[(cfg.environment_id, cfg.base)] = foto.dia_local(agora)
    if n:
        logger.info(f"erp_eventos: retenção apagou {n} evento(s) entregue(s) da base '{cfg.base}'")


def tick() -> None:
    if not habilitado():
        return
    for cfg in erp_bases_repo.listar_ativas():
        processar_base(cfg)


def loop() -> None:
    logger.info("erp_eventos: loop iniciado")
    avisar_variavel_obsoleta()
    while True:
        time.sleep(TICK_S)
        try:
            tick()
        except Exception as exc:  # noqa: BLE001 — a thread daemon não pode morrer
            logger.error(f"erp_eventos: tick falhou: {exc!r}")
