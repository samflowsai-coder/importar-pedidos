"""Catálogo do Fire ao Flow uma vez por dia, de madrugada.

O sync de catálogo (`catalogo_sync.run_catalogo_sync`) só rodava à mão (rota
`POST /api/admin/environments/{id}/flowpcp/sync-catalogo`); produto novo no Fire
não chegava ao Flow e o pedido com ele ficava "produto desconhecido" lá
(medido em 15/10/2026: 192 dos 424 produtos dos pedidos abertos da MM).

Regra (combinada com o Flow e o Samuel, 15/10/2026): por ambiente com o FlowPCP
habilitado E "Enviar catálogo ao Flow" (`flowpcp_catalogo_push`) ligado, o
catálogo inteiro vai gravando (`dryRun=false`, `fullSync=true`) uma vez por dia,
dentro da hora `PORTAL_FLOWPCP_CATALOGO_HORA` (padrão 5, horário de Brasília,
junto da foto do SP1). Fora das janelas de envio de propósito: é um POST só,
fora do horário de trabalho do Flow.

"Já foi hoje" vem da cópia local (`catalogo_fire.extraido_em`, gravada a cada
sync, antes do envio), então sobrevive a reinício. Uma sincronização manual
feita dentro dessa mesma hora também conta como a do dia. Falha no envio ao
Flow depois da cópia local não repete no mesmo dia (fica no log de erro); falha
antes dela (Fire fora) repete a cada `PAUSA_FALHA` dentro da hora.

O promote do Flow não desativa produto que só existe no Flow (vai para o
relatório como `flowOnly`); produto inativo no Fire fica inativo lá.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta

from app.integrations.flowpcp.catalogo_sync import run_catalogo_sync
from app.integrations.flowpcp.config import enabled_flowpcp_envs
from app.persistence import router
from app.utils.janelas_envio import FUSO
from app.utils.logger import logger

HORA_PADRAO = 5
PAUSA_FALHA = timedelta(minutes=10)
# slug -> não tenta antes de. Falha antes da cópia local (Fire fora) repete
# dentro da hora, mas sem bater no Fire e no Flow (10/min por tenant) a cada tick.
_pausa: dict[str, datetime] = {}


def hora_do_catalogo() -> int:
    bruto = os.environ.get("PORTAL_FLOWPCP_CATALOGO_HORA", "").strip()
    if not bruto:
        return HORA_PADRAO
    try:
        hora = int(bruto)
    except ValueError:
        hora = -1
    if not 0 <= hora <= 23:
        logger.error(f"PORTAL_FLOWPCP_CATALOGO_HORA inválida ({bruto!r}); usando {HORA_PADRAO}")
        return HORA_PADRAO
    return hora


def _ultima_extracao(slug: str) -> datetime | None:
    with router.env_connect(slug) as conn:
        row = conn.execute("SELECT MAX(extraido_em) FROM catalogo_fire").fetchone()
    if not row or not row[0]:
        return None
    return datetime.fromisoformat(row[0].replace("Z", "+00:00"))


def devido(agora: datetime, ultima: datetime | None, hora: int) -> bool:
    """Dentro da hora do catálogo e sem extração desde o início dela, hoje."""
    local = agora.astimezone(FUSO)
    if local.hour != hora:
        return False
    inicio = local.replace(minute=0, second=0, microsecond=0)
    return ultima is None or ultima < inicio


def rodar(
    agora: datetime | None = None,
    *,
    sync: Callable[..., object] = run_catalogo_sync,
    ultima_extracao: Callable[[str], datetime | None] = _ultima_extracao,
) -> list[str]:
    """Uma volta: sincroniza os ambientes devidos. Devolve os slugs enviados."""
    agora = agora or datetime.now(FUSO)
    hora = hora_do_catalogo()
    if agora.astimezone(FUSO).hour != hora:
        return []
    enviados: list[str] = []
    for slug, cfg in enabled_flowpcp_envs().items():
        if not getattr(cfg, "catalogo_push", False):
            continue
        if _pausa.get(slug, datetime.min.replace(tzinfo=FUSO)) > agora:
            continue
        try:
            if not devido(agora, ultima_extracao(slug), hora):
                continue
            rep = sync(slug, dry_run=False, full_sync=True)
        except Exception as exc:  # noqa: BLE001 — a thread de envio não pode morrer
            _pausa[slug] = agora + PAUSA_FALHA
            logger.error(f"flowpcp.catalogo env={slug}: sync diário falhou: {exc!r}")
            continue
        enviados.append(slug)
        logger.info(f"flowpcp.catalogo env={slug}: sync diário enviado: {_resumo(rep)}")
    return enviados


def _resumo(rep: object) -> str:
    contagens = getattr(rep, "contagens", None)
    if contagens is None:
        return repr(rep)
    extras = {
        k: v
        for k, v in (getattr(rep, "model_extra", None) or {}).items()
        if isinstance(v, int | str)
    }
    return f"contagens={contagens.model_dump(by_alias=True)} {extras}".strip()
