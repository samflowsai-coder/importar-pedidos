"""Ganchos do lote intercompany no fluxo de envio ao Fire.

Chamados do request path depois que o Fire já aceitou o pedido: nunca
levantam. Falha aqui vira `logger.warning("lote: ...")` e o envio segue —
o pedido já está no Fire e a UI não pode ver erro por causa do lote.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from app.consolidators.janela import janela_de, proxima_janela
from app.exporters.firebird_exporter import FirebirdExportResult
from app.models.order import Order
from app.persistence import db, lotes_repo
from app.utils.logger import logger

# Quantas janelas à frente a perna pode andar procurando lote aberto. Lote
# fechado nunca reabre; mais de 8 janelas fechadas à frente da importação é
# dado incoerente, não atraso — vira erro no log em vez de lote longe demais.
_MAX_SALTOS = 8


def anexar_perna_de_origem(
    import_id: str,
    entry: dict,
    order: Order,
    result: FirebirdExportResult,
    request_env: dict | None,
    *,
    agora: datetime | None = None,
) -> int | None:
    """Anexa o pedido recém-enviado ao Fire ao lote da janela da importação.

    Só age quando o ambiente atual é origem de uma `lote_config` ativa. Janela
    pela data de `imports.imported_at`; se o lote dela já fechou, vai para a
    próxima janela com lote aberto. Devolve o `lote_id` ou `None`.
    """
    try:
        return _anexar(import_id, entry, order, result, request_env, agora or datetime.now())
    except Exception as exc:  # noqa: BLE001 — o envio ao Fire já foi; nada derruba
        logger.warning(f"lote: perna {import_id} não anexada: {type(exc).__name__}: {exc}")
        return None


def _anexar(
    import_id: str,
    entry: dict,
    order: Order,
    result: FirebirdExportResult,
    request_env: dict | None,
    agora: datetime,
) -> int | None:
    slug = (request_env or {}).get("slug")
    if not slug or result.fire_codigo is None:
        return None
    with db.connect_shared() as conn:
        cfg = lotes_repo.get_config(conn, slug)
        if cfg is None or not cfg["ativo"]:
            return None
        importado = date.fromisoformat(str(entry["imported_at"])[:10])
        lote_id = anexar_na_janela(
            conn,
            cfg,
            import_id=import_id,
            importado=importado,
            fire_codigo=result.fire_codigo,
            itens_fire=result.itens,
            order=order,
            agora=agora,
        )
        if lote_id is not None:
            return lote_id
    logger.error(
        f"lote: perna {import_id} ({slug}, importada {importado}) sem lote aberto "
        f"em {_MAX_SALTOS} janelas seguintes — não anexada"
    )
    return None


def anexar_na_janela(
    conn: sqlite3.Connection,
    cfg: dict,
    *,
    import_id: str,
    importado: date,
    fire_codigo: int,
    itens_fire: list[tuple[int, int]],
    order: Order,
    agora: datetime,
) -> int | None:
    """Anexa a perna ao lote da janela de `importado` (ou da próxima com lote aberto).

    Regra única do envio ao Fire e da varredura de recuperação do fechamento.
    Esgotou `_MAX_SALTOS` → `conn.rollback()` (não deixa os lotes vazios criados
    na procura) e devolve `None`; o log fica com o chamador. Use uma conexão
    só para esta perna: o rollback desfaz tudo que estiver pendente nela.
    """
    slug = cfg["env_origem_slug"]
    modo, dia_inicio = cfg["janela"], cfg["dia_inicio"]
    inicio, fim = janela_de(importado, modo=modo, dia_inicio=dia_inicio)
    for _ in range(_MAX_SALTOS + 1):
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug=slug,
            env_espelho_slug=cfg["env_espelho_slug"],
            inicio=inicio,
            fim=fim,
            modo_janela=modo,
            agora=agora,
        )
        try:
            lotes_repo.anexar_perna(
                conn,
                lote_id=lote["id"],
                import_id=import_id,
                env_origem_slug=slug,
                fire_codigo_origem=fire_codigo,
                itens_fire=itens_fire,
                pedido_cliente=order.header.order_number,
                cnpj_cliente_final=order.header.customer_cnpj,
                razao_cliente_final=order.header.customer_name,
                agora=agora,
            )
        except lotes_repo.LoteFechadoError:
            inicio, fim = proxima_janela(fim, modo=modo, dia_inicio=dia_inicio)
            continue
        return lote["id"]
    conn.rollback()
    return None
