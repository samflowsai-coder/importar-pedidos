"""Foto noturna de uma base (contrato `pedido.snapshot_erp.v1`, spec SP1 §4.2).

- uma por base por dia local, só dentro da hora `erp_bases.foto_hora` (o MESMO que o
  `fotoHoraSp` do Flow — o alerta de foto incompleta usa o de lá);
- `corteEm` = instante ANTES da leitura; a leitura é um snapshot read-only, e
  a vigia da base não roda junto (mesma trava), então todo evento depois tem
  `observadoEm > corteEm`;
- a fila sai a qualquer hora, mas a foto tem ordem própria: antes da primeira
  página, os eventos com `observadoEm <= corteEm` ainda pendentes saem
  (`runner._enviar_foto`, só eles), e a foto espera enquanto houver algum;
  a validade de 24 h é conferida antes dessa espera;
- páginas montadas e congeladas na criação (≤ 200 pedidos e ≤ 4,5 MB cada,
  `totalPaginas` constante); envio em série, uma página só depois do 202 da
  anterior; reenvio repete o mesmo corpo;
- foto com qualquer pedido fora do contrato NÃO é criada: uma foto sem um
  pedido diria ao Flow que ele saiu de aberto;
- não completou em 24 h → `expirada` (o Flow abre `foto_incompleta`).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.contrato import ContratoInvalidoError, validar_pagina
from app.erp_eventos.envio import ClienteErpEventos, Ritmo
from app.erp_eventos.eventos import item_json, pedido_json
from app.erp_eventos.lotes import decidir
from app.erp_eventos.modelo import PedidoErp
from app.persistence import erp_estado_repo, erp_foto_repo, router
from app.persistence.erp_bases_repo import BaseErpConfig
from app.utils.janelas_envio import FUSO
from app.utils.logger import logger

MAX_PEDIDOS = 200
MAX_BYTES = 4_500_000
VALIDADE = timedelta(hours=24)
SCHEMA = "pedido.snapshot_erp.v1"


def devida(cfg: BaseErpConfig, agora: datetime, ja_existe: bool) -> bool:
    """Só DENTRO da hora da foto (5h → das 5:00 às 5:59). Com ">=", ligar a base
    às 22h tirava a foto na hora, fora do horário combinado (14/10/2026). Portal fora do ar a hora toda = sem foto no dia: o Flow alerta
    `foto_incompleta`, que é o aviso certo."""
    if cfg.foto_hora is None or ja_existe:
        return False
    return agora.astimezone(FUSO).hour == cfg.foto_hora


def dia_local(agora: datetime) -> str:
    return agora.astimezone(FUSO).date().isoformat()


def montar_paginas(
    *, base: str, foto_id: str, corte_em: str, pedidos: list[PedidoErp]
) -> list[dict]:
    blocos: list[list[dict]] = [[]]
    tamanho = 0
    for p in sorted(pedidos, key=lambda p: p.erp_codigo):
        entrada = {"pedido": pedido_json(p), "itens": [item_json(i) for i in p.itens]}
        n = len(json.dumps(entrada, ensure_ascii=False).encode("utf-8")) + 1
        if blocos[-1] and (len(blocos[-1]) >= MAX_PEDIDOS or tamanho + n > MAX_BYTES):
            blocos.append([])
            tamanho = 0
        blocos[-1].append(entrada)
        tamanho += n
    total = len(blocos)
    paginas = [
        {
            "schema": SCHEMA,
            "base": base,
            "fotoId": foto_id,
            "corteEm": corte_em,
            "pagina": n,
            "totalPaginas": total,
            "fim": n == total,
            "pedidos": bloco,
        }
        for n, bloco in enumerate(blocos, 1)
    ]
    for pagina in paginas:
        validar_pagina(pagina)  # ContratoInvalidoError: foto não é criada
    return paginas


def criar(cfg: BaseErpConfig, *, corte: datetime, pedidos: list[PedidoErp]) -> str | None:
    """Cria a foto do dia. Pedido fora do contrato → a foto do dia fica registrada
    como `nao_criada` (com o motivo) e não é tentada de novo até amanhã: o Flow
    abre `foto_incompleta` e a tela mostra por quê."""
    foto_id = str(uuid.uuid4())
    corte_em = formatar_instante(corte)
    try:
        paginas = montar_paginas(base=cfg.base, foto_id=foto_id, corte_em=corte_em, pedidos=pedidos)
    except ContratoInvalidoError as exc:
        with router.env_connect(cfg.env_slug) as conn:
            erp_foto_repo.registrar_nao_criada(
                conn,
                foto_id=foto_id,
                base=cfg.base,
                dia=dia_local(corte),
                corte_em=corte_em,
                motivo=str(exc),
                agora=formatar_instante(datetime.now(UTC)),
            )
        logger.error(
            f"erp_eventos: foto da base '{cfg.base}' não criada hoje — pedido fora do contrato: {exc}"
        )
        return None
    with router.env_connect(cfg.env_slug) as conn:
        erp_foto_repo.criar(
            conn,
            foto_id=foto_id,
            base=cfg.base,
            dia=dia_local(corte),
            corte_em=corte_em,
            paginas=paginas,
            agora=formatar_instante(datetime.now(UTC)),
        )
    logger.info(
        f"erp_eventos: foto {foto_id} da base '{cfg.base}' criada com {len(paginas)} página(s)"
    )
    return foto_id


def expirar_se_vencida(cfg: BaseErpConfig, foto: erp_foto_repo.Foto, agora: datetime) -> bool:
    """Foto que não completou em `VALIDADE` vira `expirada` (o Flow abre
    `foto_incompleta`). Chamada antes de qualquer espera, para nenhuma espera
    segurar a foto em `enviando` para sempre."""
    if foto.criada_em >= formatar_instante(agora - VALIDADE):
        return False
    with router.env_connect(cfg.env_slug) as conn:
        erp_foto_repo.encerrar(conn, foto.foto_id, "expirada", formatar_instante(agora))
    logger.error(f"erp_eventos: foto {foto.foto_id} da base '{cfg.base}' não completou em 24 h")
    return True


def enviar(
    cfg: BaseErpConfig,
    *,
    cliente: ClienteErpEventos,
    ritmo: Ritmo,
    agora_fn=lambda: datetime.now(UTC),
) -> str | None:
    """Continua a foto em envio da base. Devolve a situação ao parar (None se não há foto)."""
    with router.env_connect(cfg.env_slug) as conn:
        foto = erp_foto_repo.em_envio(conn, cfg.base)
        envio = erp_estado_repo.obter_envio(conn, cfg.base)
    if foto is None:
        return None
    if envio.bloqueio:
        return "bloqueada"
    if envio.proxima_tentativa_em and envio.proxima_tentativa_em > formatar_instante(agora_fn()):
        return "esperando"
    if expirar_se_vencida(cfg, foto, agora_fn()):
        return "expirada"

    while True:
        with router.env_connect(cfg.env_slug) as conn:
            proxima = erp_foto_repo.proxima_pagina(conn, foto.foto_id)
        if proxima is None:
            with router.env_connect(cfg.env_slug) as conn:
                erp_foto_repo.encerrar(
                    conn, foto.foto_id, "completa", formatar_instante(agora_fn())
                )
            return "completa"
        pagina, corpo = proxima
        ritmo.aguardar(cfg.tenant_id, "snapshot-erp")
        resp = cliente.enviar_pagina(corpo)
        agora = agora_fn()
        if resp.status == 202:
            with router.env_connect(cfg.env_slug) as conn:
                erp_foto_repo.marcar_pagina_enviada(
                    conn, foto.foto_id, pagina, formatar_instante(agora)
                )
            continue
        if resp.status == 401:
            with router.env_connect(cfg.env_slug) as conn:
                erp_estado_repo.bloquear(
                    conn,
                    cfg.base,
                    formatar_instante(agora),
                    motivo="credencial",
                    http=401,
                    erro=resp.erro or "",
                )
            logger.critical(f"erp_eventos: base '{cfg.base}' BLOQUEADA (401 na foto)")
            return "bloqueada"
        decisao = decidir(
            resp.status,
            retry_after=resp.retry_after,
            falhas_seguidas=envio.falhas_seguidas,
            agora=agora,
        )
        if decisao.acao == "esperar":
            with router.env_connect(cfg.env_slug) as conn:
                erp_estado_repo.registrar_espera(
                    conn,
                    cfg.base,
                    formatar_instante(agora),
                    http=resp.status,
                    erro=resp.erro or "",
                    ate=formatar_instante(agora + timedelta(seconds=decisao.espera_s)),
                )
            logger.warning(
                f"erp_eventos: foto {foto.foto_id} página {pagina}: HTTP {resp.status}, tenta depois"
            )
            return "esperando"
        with router.env_connect(cfg.env_slug) as conn:
            erp_foto_repo.encerrar(conn, foto.foto_id, "recusada", formatar_instante(agora))
        logger.critical(
            f"erp_eventos: foto {foto.foto_id} página {pagina} RECUSADA (HTTP {resp.status}): {resp.erro}"
        )
        return "recusada"
