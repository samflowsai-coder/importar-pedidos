"""Drenagem da fila de uma base: lotes em série, resposta casada por id.

Regras (spec SP1 §4.1, contrato itens 8 e 9):
- sem pendente e com sinal de vida devido → manda lote VAZIO (o sinal de vida
  da base no Flow só conta com 202);
- um lote por vez; o próximo só depois do 202 do anterior;
- 202: aplica `aceito`/`duplicado`/`rejeitado` por id; id ausente segue pendente;
- 413: divide sem partir unidade; 429/5xx/rede: espera crescente persistida;
- 400/401/outros 4xx: BLOQUEIA a base (não reenvia o mesmo corpo em laço) até
  ação humana em /admin/ambientes.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.erp_eventos import lotes
from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.envio import ClienteErpEventos, Ritmo
from app.persistence import erp_envio_lote_repo, erp_estado_repo, router
from app.persistence.erp_bases_repo import BaseErpConfig
from app.utils.logger import logger

PRAZO_DRENAGEM_S = 120.0


@dataclass(frozen=True)
class ResultadoDrenagem:
    lotes: int
    aceitos: int
    duplicados: int
    rejeitados: int
    parou_por: str | None  # None | "esperando" | "bloqueada" | "prazo"


def drenar(
    cfg: BaseErpConfig,
    *,
    cliente: ClienteErpEventos,
    ritmo: Ritmo,
    agora_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    relogio: Callable[[], float] = time.monotonic,
    prazo_s: float = PRAZO_DRENAGEM_S,
    ate: str | None = None,
    origem: str = "auto",
    quem: str | None = None,
    envio_id: str | None = None,
) -> ResultadoDrenagem:
    """`ate`: só eventos observados até esse instante, sem lote vazio de sinal
    de vida (usado antes da foto). `origem`/`quem`/`envio_id`
    ficam no registro de cada lote (`erp_envio_lote`); lote sem evento com
    origem `auto` é registrado como `sinal` (sinal de vida)."""
    inicio = relogio()
    n_lotes = aceitos = duplicados = rejeitados = 0
    max_eventos = lotes.MAX_EVENTOS

    while True:
        agora = agora_fn()
        with router.env_connect(cfg.env_slug) as conn:
            envio = erp_estado_repo.obter_envio(conn, cfg.base)
            pend = erp_estado_repo.pendentes(conn, cfg.base, limite=lotes.MAX_EVENTOS + 1, ate=ate)
        if envio.bloqueio:
            return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "bloqueada")
        if envio.proxima_tentativa_em and envio.proxima_tentativa_em > formatar_instante(agora):
            return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "esperando")
        if not pend and (not envio.sinal_pendente or ate is not None):
            return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, None)
        if relogio() - inicio > prazo_s:
            return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "prazo")

        try:
            lote = lotes.proximo_lote(pend, max_eventos=max_eventos)
        except lotes.UnidadeGrandeDemaisError as exc:
            with router.env_connect(cfg.env_slug) as conn:
                erp_estado_repo.bloquear(
                    conn,
                    cfg.base,
                    formatar_instante(agora),
                    motivo="unidade_grande",
                    http=None,
                    erro=str(exc),
                )
            logger.critical(f"erp_eventos: base '{cfg.base}' bloqueada — {exc}")
            return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "bloqueada")

        lote_id = str(uuid.uuid4())
        ids = [p.id for p in lote]
        ritmo.aguardar(cfg.tenant_id, "eventos")
        with router.env_connect(cfg.env_slug) as conn:
            enviado_em = formatar_instante(agora_fn())
            erp_estado_repo.marcar_enviados(conn, ids, lote_id, enviado_em)
            erp_envio_lote_repo.abrir(
                conn,
                lote_id=lote_id,
                base=cfg.base,
                origem="sinal" if not ids and origem == "auto" else origem,
                quem=quem,
                envio_id=envio_id,
                enviado_em=enviado_em,
                eventos=len(ids),
            )
        resp = cliente.enviar_lote(lote_id, [json.loads(p.payload_json) for p in lote])
        n_lotes += 1
        agora = agora_fn()
        decisao = lotes.decidir(
            resp.status,
            retry_after=resp.retry_after,
            falhas_seguidas=envio.falhas_seguidas,
            agora=agora,
        )
        carimbo = formatar_instante(agora)

        def fechar_lote(conn, *, ac=0, du=0, re=0):
            erp_envio_lote_repo.fechar(
                conn,
                lote_id,
                http=resp.status,
                aceitos=ac,
                duplicados=du,
                rejeitados=re,
                respondido_em=carimbo,
            )

        if decisao.acao == "aplicar":
            resultados = lotes.ler_resultados(ids, resp.corpo or {})
            situacoes = [s for s, _ in resultados.values()]
            rej = situacoes.count("rejeitado")
            if resultados or not lote:
                with router.env_connect(cfg.env_slug) as conn:
                    erp_estado_repo.aplicar_resultados(conn, resultados, carimbo)
                    erp_estado_repo.registrar_202(conn, cfg.base, carimbo)
                    fechar_lote(
                        conn,
                        ac=situacoes.count("aceito"),
                        du=situacoes.count("duplicado"),
                        re=rej,
                    )
            aceitos += situacoes.count("aceito")
            duplicados += situacoes.count("duplicado")
            rejeitados += rej
            if rej:
                logger.warning(
                    f"erp_eventos: lote {lote_id} da base '{cfg.base}' teve {rej} rejeitado(s); "
                    f"ids={[i for i, (s, _) in resultados.items() if s == 'rejeitado'][:20]}"
                )
            faltando = len(ids) - len(resultados)
            if faltando:
                logger.warning(
                    f"erp_eventos: lote {lote_id}: {faltando} id(s) sem resultado — seguem pendentes"
                )
            if lote and not resultados:
                # 202 sem nenhum id casado: falha de protocolo, não "tudo pendente,
                # manda de novo já" — reenviar em laço é o que o contrato proíbe (I2).
                espera = lotes.espera_crescente(envio.falhas_seguidas)
                with router.env_connect(cfg.env_slug) as conn:
                    erp_estado_repo.registrar_espera(
                        conn,
                        cfg.base,
                        carimbo,
                        http=202,
                        erro=f"202 sem resultados casados: {str(resp.corpo)[:300]}",
                        ate=formatar_instante(agora + timedelta(seconds=espera)),
                    )
                    fechar_lote(conn)
                logger.error(f"erp_eventos: lote {lote_id} voltou 202 sem nenhum resultado casado")
                return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "esperando")
            if not lote:
                return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, None)
            max_eventos = lotes.MAX_EVENTOS
            continue

        if decisao.acao == "dividir":
            metades = lotes.dividir_lote(lote)
            if metades is None:
                decisao = lotes.Decisao("bloquear", bloqueio="lote_grande_demais")
            else:
                # O próximo lote é exatamente a 1ª metade: termina numa fronteira
                # de unidade, então nenhuma unidade passa do teto (revisão I1).
                max_eventos = len(metades[0])
                with router.env_connect(cfg.env_slug) as conn:
                    fechar_lote(conn)
                continue

        if decisao.acao == "esperar":
            ate = formatar_instante(agora + timedelta(seconds=decisao.espera_s))
            with router.env_connect(cfg.env_slug) as conn:
                erp_estado_repo.registrar_espera(
                    conn, cfg.base, carimbo, http=resp.status, erro=resp.erro or "", ate=ate
                )
                fechar_lote(conn)
            logger.warning(
                f"erp_eventos: base '{cfg.base}' HTTP {resp.status} — nova tentativa em {decisao.espera_s:.0f}s"
            )
            return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "esperando")

        with router.env_connect(cfg.env_slug) as conn:
            erp_estado_repo.bloquear(
                conn,
                cfg.base,
                carimbo,
                motivo=decisao.bloqueio or "desconhecido",
                http=resp.status,
                erro=resp.erro or "",
            )
            fechar_lote(conn)
        logger.critical(
            f"erp_eventos: base '{cfg.base}' BLOQUEADA ({decisao.bloqueio}, HTTP {resp.status}) — "
            "envio parado até ação em /admin/ambientes"
        )
        return ResultadoDrenagem(n_lotes, aceitos, duplicados, rejeitados, "bloqueada")
