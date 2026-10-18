"""Catálogo do Fire ao Flow uma vez por dia, de madrugada.

O sync de catálogo (`catalogo_sync.run_catalogo_sync`) só rodava à mão (rota
`POST /api/admin/environments/{id}/flowpcp/sync-catalogo`); produto novo no Fire
não chegava ao Flow e o pedido com ele ficava "produto desconhecido" lá
(medido em 15/10/2026: 192 dos 424 produtos dos pedidos abertos da MM).

Regra (combinada com o Flow e o Samuel, 15/10/2026): por ambiente com o FlowPCP
habilitado E "Enviar catálogo ao Flow" (`flowpcp_catalogo_push`) ligado, o
catálogo inteiro (`fullSync=true`) vai uma vez por dia, dentro da hora
`PORTAL_FLOWPCP_CATALOGO_HORA` (padrão 5, horário de Brasília, junto da foto do
SP1). `PORTAL_FLOWPCP_CATALOGO_DIARIO` decide o modo: `simular` (padrão,
`dryRun=true`: o Flow só compara), `gravar` (`dryRun=false`) ou `desligado`.
Gravar nunca é padrão: só vira depois do ok do Samuel ao primeiro promote, para
o envio diário não gravar um recorte que ninguém conferiu. Fora das janelas de envio de propósito: é um POST só,
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


MODOS = ("simular", "gravar", "desligado")


def modo_do_catalogo() -> str:
    bruto = os.environ.get("PORTAL_FLOWPCP_CATALOGO_DIARIO", "").strip().lower()
    if not bruto:
        return "simular"
    if bruto not in MODOS:
        logger.error(f"PORTAL_FLOWPCP_CATALOGO_DIARIO inválido ({bruto!r}); usando 'simular'")
        return "simular"
    return bruto


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
    modo = modo_do_catalogo()
    if modo == "desligado" or agora.astimezone(FUSO).hour != hora:
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
            rep = sync(slug, dry_run=modo != "gravar", full_sync=True)
        except Exception as exc:  # noqa: BLE001 — a thread de envio não pode morrer
            _pausa[slug] = agora + PAUSA_FALHA
            logger.error(f"flowpcp.catalogo env={slug}: sync diário falhou: {exc!r}")
            continue
        enviados.append(slug)
        log = logger.warning if _precisa_atencao(rep) else logger.info
        log(f"flowpcp.catalogo env={slug}: sync diário ({modo}) enviado: {_resumo(rep)}")
    return enviados


_BLOCOS = (
    "composicao",
    "vinculoMes",
    "composicao_portal",
    "fora_do_envio_guardado",
    "fiscal_portal",
    "marcas_portal",
    "nota_portal",
)


def _precisa_atencao(rep: object) -> bool:
    """Trava do Flow ativa, leitura da composição com falha no Portal, kit com
    composição do ERP que deixou de vir (C3: apagar no Fire não chega ao Flow),
    kit congelado por quantidade divergente do nome (fica parado até o Fire mudar),
    ou campos fiscais ignorados pelo Flow (`fiscal_portal.capacidade_ok is False`:
    o envio fiscal está ligado e o Flow não devolveu `fiscal.v1`)."""
    extra = getattr(rep, "model_extra", None) or {}
    comp = extra.get("composicao") or {}
    portal = extra.get("composicao_portal") or {}
    fiscal = extra.get("fiscal_portal") or {}
    novos = (extra.get("marcas_portal") or {}, extra.get("nota_portal") or {})
    trava = comp.get("travaRemocaoAtiva") is True or comp.get("travaSubstituicaoAtiva") is True
    return bool(
        trava
        or comp.get("erpSemChave")
        or portal.get("leitura_ok") is False
        or portal.get("divergencia_nome")
        or fiscal.get("capacidade_ok") is False
        or any(
            b.get("capacidade_ok") is False
            or b.get("reenviar") is True
            or b.get("leitura_ok") is False
            for b in novos
        )
    )


def _resumo(rep: object) -> str:
    contagens = getattr(rep, "contagens", None)
    if contagens is None:
        return repr(rep)
    extras = {
        k: v
        for k, v in (getattr(rep, "model_extra", None) or {}).items()
        if isinstance(v, int | str) or k in _BLOCOS
    }
    volta = (getattr(rep, "model_extra", None) or {}).get("fora_do_envio_volta")
    if volta:
        extras["fora_do_envio_volta"] = len(volta)
    return f"contagens={contagens.model_dump(by_alias=True)} {extras}".strip()
