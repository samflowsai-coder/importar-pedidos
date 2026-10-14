"""Vínculo exato item do ERP ↔ item do Portal.

Dois caminhos, o mesmo resultado (`erp_item_vinculo`):

- **insert** (EXPORT_MODE=db/both): o exportador conhece o `CORPO_VENDAS.CODIGO`
  de cada item no INSERT e grava o par direto.
- **xlsx** (EXPORT_MODE=xlsx, o caminho de produção hoje): a operadora importa
  no ERP a planilha que o Portal gerou. O Portal guardou o que cada arquivo
  levou, linha a linha (`erp_xlsx_linha`); quando a vigia vê o pedido pela
  primeira vez, casa as linhas do arquivo com os itens do ERP POR POSIÇÃO e só
  aceita se tudo bate (contagem, quantidade, cliente, número). Qualquer dúvida
  = sem vínculo: o Flow cai na heurística dele (spec §5.3.2). Nunca chuta.

O índice é sempre a posição do item em `order.items` — a mesma posição no
array `itens` que foi ao /recebimento (`mapper.py` itera `order.items` em
ordem; o Flow grava `source_id_externo = externalId:posição`).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.erp.cnpj import cnpj_digits
from app.erp.numero_pedido import variantes
from app.erp_eventos.canonico import decimal_canonico, formatar_instante
from app.erp_eventos.modelo import PedidoErp
from app.exporters.erp_exporter import ERPExporter
from app.models.order import Order
from app.persistence import db, erp_bases_repo, erp_vinculo_repo
from app.persistence.erp_vinculo_repo import LinhaXlsx
from app.utils.logger import logger

# Comparar também o preço por posição no casamento pela planilha. Desligado
# até a medição M1 (docs/ai/modules/erp_eventos.md) dizer se o importador de
# Excel do ERP grava o preço da planilha ou o da tabela de preço.
COMPARAR_PRECO = False

# PEDIDO_CLIENTE no ERP tem 20 posições (firebird_exporter.py corta em [:20]).
_LARGURA_PEDIDO_CLIENTE = 20


@dataclass(frozen=True)
class ArquivoXlsx:
    import_id: str
    arquivo: str
    linhas: tuple[LinhaXlsx, ...]


def _dec(texto: str | None) -> Decimal | None:
    if texto is None:
        return None
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


def _numero_bate(numero_portal: str | None, numero_erp: str | None) -> bool:
    if not numero_portal or not numero_erp:
        return False
    alvo = numero_erp.strip()
    return alvo in variantes(numero_portal) or alvo in variantes(
        numero_portal[:_LARGURA_PEDIDO_CLIENTE]
    )


def agrupar_arquivos(linhas: list[LinhaXlsx]) -> list[ArquivoXlsx]:
    grupos: dict[tuple[str, str], list[LinhaXlsx]] = defaultdict(list)
    for lx in linhas:
        grupos[(lx.import_id, lx.arquivo)].append(lx)
    return [
        ArquivoXlsx(imp, arq, tuple(sorted(ls, key=lambda x: x.linha)))
        for (imp, arq), ls in sorted(grupos.items())
    ]


def casar_por_posicao(
    pedido: PedidoErp, candidatos: list[ArquivoXlsx], *, comparar_preco: bool
) -> tuple[str, list[tuple[int, int]]] | None:
    """(import_id, [(erp_item_codigo, indice_original)]) se EXATAMENTE um arquivo casa."""
    itens = sorted(pedido.itens, key=lambda i: i.erp_item_codigo)
    casados = []
    for arq in candidatos:
        if len(arq.linhas) != len(itens):
            continue
        if not all(_numero_bate(lx.pedido_cliente, pedido.pedido_cliente) for lx in arq.linhas):
            continue
        cnpj_arquivo = {cnpj_digits(lx.cnpj_cliente) for lx in arq.linhas}
        if cnpj_arquivo != {pedido.cliente_erp_cnpj or ""}:
            continue
        ok = True
        for lx, it in zip(arq.linhas, itens, strict=True):
            if _dec(lx.quantidade) != it.qtd:
                ok = False
                break
            if comparar_preco and _dec(lx.preco_unitario) != it.preco_unitario:
                ok = False
                break
        if ok:
            casados.append(arq)
    if len(casados) != 1:
        return None
    arq = casados[0]
    return arq.import_id, [
        (it.erp_item_codigo, lx.indice_original) for lx, it in zip(arq.linhas, itens, strict=True)
    ]


def casar_novos_por_xlsx(
    conn: sqlite3.Connection,
    *,
    base: str,
    pedidos: list[PedidoErp],
    comparar_preco: bool,
    quantidade_enviada: Callable[[str, int], Decimal | None],
    desde: str,
    agora: str,
) -> int:
    """Grava vínculo 'xlsx' para cada pedido novo que casa sem ambiguidade.

    `quantidade_enviada(import_id, indice)` devolve a quantidade que o
    /recebimento levou naquele índice (payload guardado no outbox); só vincula
    se bate com a linha do XLSX — prova de que o índice aponta para o mesmo
    item que o Flow tem. Sem push ao Flow, sem vínculo.
    """
    arquivos = agrupar_arquivos(erp_vinculo_repo.linhas_xlsx_desde(conn, desde))
    gravados = 0
    for pedido in pedidos:
        codigos = [i.erp_item_codigo for i in pedido.itens]
        if not codigos or erp_vinculo_repo.vinculos_por_item(conn, base, codigos):
            continue  # sem itens, ou vínculo 'insert' já existe
        candidatos = [
            a for a in arquivos if _numero_bate(a.linhas[0].pedido_cliente, pedido.pedido_cliente)
        ]
        achado = casar_por_posicao(pedido, candidatos, comparar_preco=comparar_preco)
        if achado is None:
            continue
        import_id, pares = achado
        por_item = {i.erp_item_codigo: i for i in pedido.itens}
        if any(
            quantidade_enviada(import_id, indice) != por_item[item].qtd for item, indice in pares
        ):
            continue
        erp_vinculo_repo.gravar_vinculos(
            conn,
            base=base,
            erp_codigo=pedido.erp_codigo,
            import_id=import_id,
            pares=pares,
            origem="xlsx",
            agora=agora,
        )
        logger.info(
            f"erp_eventos: vínculo origem=xlsx base={base} erp_codigo={pedido.erp_codigo} "
            f"import={import_id} itens={len(pares)}"
        )
        gravados += 1
    return gravados


# ── gravação na origem (chamado por app/web/server.py) ─────────────────────
# Best-effort, como o push ao Flow: falhar aqui só tira o vínculo exato deste
# pedido (o Flow cai na heurística); nunca derruba o export que já deu certo.


def registrar_planilhas(import_id: str, order: Order, paths: list[Path]) -> None:
    """Guarda o que cada XLSX levou. `paths` é o retorno de `ERPExporter.export`
    do mesmo `order`; as linhas vêm de `linhas_por_arquivo`, na mesma ordem."""
    try:
        conteudo = ERPExporter().linhas_por_arquivo(order)
        if len(conteudo) != len(paths):
            logger.warning(
                f"erp_eventos: {len(paths)} XLSX e {len(conteudo)} grupos (import={import_id}) — sem registro"
            )
            return
        arquivos = [
            (path, rows, indices) for path, (rows, indices) in zip(paths, conteudo, strict=True)
        ]
        linhas = [
            LinhaXlsx(
                import_id=import_id,
                arquivo=path.name,
                linha=n,
                indice_original=indice,
                pedido_cliente=(row.pedido or "").strip() or None,
                cnpj_cliente=cnpj_digits(row.cnpj_cliente) or None,
                quantidade=decimal_canonico(row.quantidade) if row.quantidade is not None else None,
                preco_unitario=decimal_canonico(row.preco_unitario)
                if row.preco_unitario is not None
                else None,
            )
            for path, rows, indices in arquivos
            for n, (row, indice) in enumerate(zip(rows, indices, strict=True), 1)
        ]
        with db.connect() as conn:
            erp_vinculo_repo.gravar_linhas_xlsx(
                conn, import_id, linhas, formatar_instante(datetime.now(UTC))
            )
    except Exception as exc:  # noqa: BLE001 — best-effort (ver acima)
        logger.warning(f"erp_eventos: linhas do XLSX não registradas (import={import_id}): {exc!r}")


def registrar_insert(
    import_id: str, *, environment_id: str, erp_codigo: int, itens: list[tuple[int, int]]
) -> None:
    """Vínculo 'insert': o exportador acabou de gravar estes CORPO_VENDAS.CODIGO."""
    try:
        cfg = erp_bases_repo.obter(environment_id)
        if cfg is None or not itens:
            return
        with db.connect() as conn:
            erp_vinculo_repo.gravar_vinculos(
                conn,
                base=cfg.base,
                erp_codigo=erp_codigo,
                import_id=import_id,
                pares=itens,
                origem="insert",
                agora=formatar_instante(datetime.now(UTC)),
            )
        logger.info(
            f"erp_eventos: vínculo origem=insert base={cfg.base} erp_codigo={erp_codigo} "
            f"import={import_id} itens={len(itens)}"
        )
    except Exception as exc:  # noqa: BLE001 — best-effort (ver acima)
        logger.warning(
            f"erp_eventos: vínculo do INSERT não registrado (import={import_id}): {exc!r}"
        )
