"""`produtoProdutor`: o produto, na base PRODUTORA, que atende cada item (spec SP1 §3A.2).

- Base produtora: é o próprio `CODPRODUTO` do item.
- Base intermediária: só quando o item tem vínculo com um pedido do Portal.
  O Portal leu o documento do cliente (EAN, código); procura esse produto no
  catálogo da produtora e aceita só casamento ÚNICO — EAN primeiro, depois
  código alternativo, sempre exato (`TRIM(...) = ?`, nunca prefixo: o
  importador de Excel do ERP já troca produto por prefixo, não repetimos o
  erro). Sem vínculo ou sem casamento único → None (o Flow abre pendência).

O resultado fica em `erp_item_vinculo.produto_produtor`; um None é tentado de
novo depois de `NOVA_TENTATIVA` (o catálogo da produtora pode ganhar o EAN).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.erp import queries
from app.erp.connection import FirebirdConnection
from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.modelo import PedidoErp
from app.models.order import Order
from app.persistence import context as env_context
from app.persistence import environments_repo, erp_bases_repo, erp_vinculo_repo, repo, router
from app.persistence.erp_bases_repo import BaseErpConfig
from app.utils.logger import logger

NOVA_TENTATIVA = timedelta(hours=24)
_BLOCO = 200


def _unicos(linhas: list[tuple]) -> dict[str, int]:
    """chave → SEQ, só para chaves com UM produto (catálogo com EAN/código duplicado não resolve)."""
    seqs: dict[str, set[int]] = defaultdict(set)
    for r in linhas:
        seqs[(r[0] or "").strip()].add(int(r[1]))
    return {k: next(iter(v)) for k, v in seqs.items() if k and len(v) == 1}


def _consultar(cur, sql_de_n, chaves: list[str]) -> dict[str, int]:
    linhas: list[tuple] = []
    for i in range(0, len(chaves), _BLOCO):
        bloco = chaves[i : i + _BLOCO]
        cur.execute(sql_de_n(len(bloco)), bloco)
        linhas.extend(cur.fetchall())
    return _unicos(linhas)


def resolver(
    cfg: BaseErpConfig, pedidos: list[PedidoErp], *, agora: datetime | None = None
) -> dict[int, int | None]:
    if cfg.papel == "produtora":
        return {i.erp_item_codigo: i.cod_produto for p in pedidos for i in p.itens}

    agora = agora or datetime.now(UTC)
    itens = [i.erp_item_codigo for p in pedidos for i in p.itens]
    with router.env_connect(cfg.env_slug) as conn:
        vinculos = erp_vinculo_repo.vinculos_por_item(conn, cfg.base, itens)
    saida = {k: v.produto_produtor for k, v in vinculos.items()}
    limite = formatar_instante(agora - NOVA_TENTATIVA)
    pendentes = [
        v
        for v in vinculos.values()
        if v.produto_produtor is None
        and (v.produtor_resolvido_em is None or v.produtor_resolvido_em < limite)
    ]
    if not pendentes:
        return saida

    # documento original de cada item pendente
    chave_por_item: dict[int, tuple[str | None, str | None]] = {}
    with env_context.active_env(cfg.environment_id, cfg.env_slug):
        snapshots: dict[str, Order | None] = {}
        for v in pendentes:
            if v.import_id not in snapshots:
                entry = repo.get_import(v.import_id)
                snap = (entry or {}).get("snapshot")
                snapshots[v.import_id] = Order.model_validate(snap) if snap else None
            order = snapshots[v.import_id]
            if order is not None and v.indice_original < len(order.items):
                it = order.items[v.indice_original]
                chave_por_item[v.erp_item_codigo] = (
                    (it.ean or "").strip() or None,
                    (it.product_code or "").strip() or None,
                )

    produtora = erp_bases_repo.produtora_do_tenant(cfg.tenant_id)
    produtora_env = environments_repo.get(produtora.environment_id) if produtora else None
    if produtora_env is None:
        logger.warning(
            f"erp_eventos: tenant {cfg.tenant_id} sem base produtora configurada — produtoProdutor fica nulo"
        )
        return saida

    eans = sorted({e for e, _ in chave_por_item.values() if e})
    codigos = sorted({c for _, c in chave_por_item.values() if c})
    try:
        with FirebirdConnection().connect_somente_leitura(
            environments_repo.to_fb_config(produtora_env)
        ) as fb:
            cur = fb.cursor()
            try:
                por_ean = _consultar(cur, queries.find_products_by_eans_sql, eans) if eans else {}
                por_codigo = (
                    _consultar(cur, queries.find_products_by_codes_sql, codigos) if codigos else {}
                )
            finally:
                cur.close()
    except Exception as exc:  # noqa: BLE001 — sem produtor não é erro do pedido; tenta na próxima vigia
        logger.warning(f"erp_eventos: leitura do catálogo da base produtora falhou: {exc!r}")
        return saida

    resolvidos: dict[int, int | None] = {}
    for v in pendentes:
        ean, codigo = chave_por_item.get(v.erp_item_codigo, (None, None))
        resolvidos[v.erp_item_codigo] = (por_ean.get(ean) if ean else None) or (
            por_codigo.get(codigo) if codigo else None
        )
    with router.env_connect(cfg.env_slug) as conn:
        erp_vinculo_repo.gravar_produtor(conn, cfg.base, resolvidos, formatar_instante(agora))
    saida.update(resolvidos)
    return saida
