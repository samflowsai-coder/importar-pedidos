from __future__ import annotations

import re
import threading
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.erp import queries
from app.erp.connection import FirebirdConnection
from app.erp.exceptions import (
    FirebirdClientNotFoundError,
    FirebirdError,
    FirebirdOrderAlreadyExistsError,
)
from app.erp.fiscal import perfil_para
from app.erp.mapper import FireSistemasMapper, _item_total, _parse_date
from app.models.order import ERPRow, Order
from app.utils.logger import logger

# GET_NEXT_CABVENDAS_CODIGO / GET_NEXT_CORPOVENDAS_CODIGO use MAX(CODIGO)+1, which
# is not atomic. Serialize inserts process-wide until we migrate to a Firebird
# generator.
_insert_lock = threading.Lock()


class FirebirdExportResult:
    def __init__(
        self,
        order_number: str | None,
        items_inserted: int,
        fire_codigo: int | None = None,
        skipped: bool = False,
        skip_reason: str | None = None,
    ) -> None:
        self.order_number = order_number
        self.items_inserted = items_inserted
        self.fire_codigo = fire_codigo
        self.skipped = skipped
        self.skip_reason = skip_reason

    def to_dict(self) -> dict:
        return {
            "order_number": self.order_number,
            "items_inserted": self.items_inserted,
            "fire_codigo": self.fire_codigo,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }

    def __repr__(self) -> str:
        if self.skipped:
            return f"<FirebirdExportResult skipped={self.skip_reason!r}>"
        return (
            f"<FirebirdExportResult order={self.order_number!r} "
            f"items={self.items_inserted} fire_codigo={self.fire_codigo}>"
        )


def _to_erp_rows(order: Order) -> list[ERPRow]:
    """Flatten Order to ERPRow list (mirrors ERPExporter logic)."""
    from app.exporters.erp_exporter import ERPExporter
    exporter = ERPExporter()
    all_rows: list[ERPRow] = []
    for bucket in exporter._group_by_delivery(order).values():
        all_rows.extend(exporter._to_erp_rows(order, bucket))
    return all_rows


def _valor_total(rows: list[ERPRow]) -> Decimal:
    """Soma dos MESMOS totais por item que vao pra CORPO_VENDAS.TOTAL
    (_item_total, em app.erp.mapper) — o cabecalho bate com a soma dos
    proprios itens em vez de recalcular a regra em paralelo. Converte pra
    Decimal na borda via str() (nunca o float direto) e quantiza em 2 casas
    (escala de CAB_VENDAS.VALOR_TOTAL); o item mantem as 4 casas dele.
    """
    total = Decimal("0")
    for row in rows:
        total += Decimal(str(_item_total(row)))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _menor_dt_entrega(rows: list[ERPRow]) -> date | None:
    """Menor data de entrega entre os itens do pedido, ignorando quem nao
    tem data; None se nenhum item tiver. Mesma regra que o desenho do lote
    intercompany usa pro pedido consolidado (regra da casa).
    """
    datas = [d for d in (_parse_date(row.data_entrega) for row in rows) if d is not None]
    return min(datas) if datas else None


class FirebirdExporter:
    """
    Writes an Order directly into the Fire Sistemas Firebird database.

    Inserts into CAB_VENDAS (header) + CORPO_VENDAS (items).
    Schema verified against BKP_MM2_CONFECCAO_TERCA.fbk (April 2026).
    """

    def __init__(self, *, env: dict | None = None) -> None:
        """env: ambiente atual (dict de environments_repo). Quando presente,
        usa connect_with_config(to_fb_config(env)). Quando None, cai no
        comportamento legado (env vars FB_*) para compatibilidade.
        """
        self._conn = FirebirdConnection()
        self._mapper = FireSistemasMapper()
        self._env = env

    def _open(self):
        """Context manager para conexão FB do ambiente correto."""
        if self._env is not None:
            from app.persistence import environments_repo  # avoid cycle
            cfg = environments_repo.to_fb_config(self._env)
            return self._conn.connect_with_config(cfg)
        return self._conn.connect()

    def _is_configured(self) -> bool:
        if self._env is not None:
            return bool((self._env.get("fb_path") or "").strip())
        return self._conn.is_configured()

    def export(
        self,
        order: Order,
        *,
        override_client_id: int | None = None,
    ) -> FirebirdExportResult:
        """Insere o pedido em CAB_VENDAS / CORPO_VENDAS.

        `override_client_id`: quando o usuário escolheu manualmente o cliente
        no portal (recovery do CLIENT_NOT_FOUND), pula o lookup por CNPJ e
        usa esse codigo direto — mas ainda valida via FIND_CLIENT_BY_CODIGO
        para evitar FK quebrada caso o cliente tenha sido inativado.
        """
        if not self._is_configured():
            logger.warning("Firebird não configurado para o ambiente — exportação ignorada.")
            return FirebirdExportResult(
                order_number=order.header.order_number,
                items_inserted=0,
                skipped=True,
                skip_reason="FB_DATABASE_NOT_SET",
            )

        try:
            with _insert_lock, self._open() as conn:
                return self._insert_order(conn, order, override_client_id)
        except FirebirdOrderAlreadyExistsError as exc:
            logger.warning(str(exc))
            return FirebirdExportResult(
                order_number=order.header.order_number,
                items_inserted=0,
                skipped=True,
                skip_reason="ORDER_ALREADY_EXISTS",
            )
        except FirebirdClientNotFoundError as exc:
            logger.warning(str(exc))
            return FirebirdExportResult(
                order_number=order.header.order_number,
                items_inserted=0,
                skipped=True,
                skip_reason="CLIENT_NOT_FOUND",
            )
        except FirebirdError as exc:
            logger.error(f"Firebird export falhou [{order.header.order_number}]: {exc}")
            raise

    # ── Internal ─────────────────────────────────────────────────────────────

    def _insert_order(
        self,
        conn,
        order: Order,
        override_client_id: int | None = None,
    ) -> FirebirdExportResult:
        cur = conn.cursor()

        # 1. Resolve client FK. Prefer manual override if set; still validate
        #    against CADASTRO so a stale codigo (cliente inativado entre
        #    seleção e envio) surfaces como CLIENT_NOT_FOUND, não FK error.
        if override_client_id is not None:
            client_id = self._validate_client_id(cur, override_client_id)
        else:
            client_id = self._find_client(cur, order.header.customer_cnpj)
        if client_id is None:
            raise FirebirdClientNotFoundError(
                order.header.customer_cnpj or "",
                order.header.customer_name,
            )

        # 2. Idempotency — PEDIDO_CLIENTE + CLIENTE uniquely identifies a retailer order
        pedido_cliente = (order.header.order_number or "")[:20]
        if pedido_cliente:
            cur.execute(queries.CHECK_ORDER_EXISTS, (pedido_cliente, client_id))
            if cur.fetchone()[0] > 0:
                raise FirebirdOrderAlreadyExistsError(pedido_cliente)

        # 3. Get next PK for CAB_VENDAS
        cur.execute(queries.GET_NEXT_CABVENDAS_CODIGO)
        header_pk: int = cur.fetchone()[0]

        # 4. Insert header. valor_total soma os itens em Decimal e dt_entrega
        #    e a menor data de entrega entre eles (precisa dos erp_rows antes
        #    do insert do cabecalho); perfil vem do ambiente atual
        #    (CODFIGFISCAL difere MM x Nasmar), default medido se ausente.
        #    OBS fica None aqui — carrega a lista de pedidos de origem do
        #    lote intercompany, que e um call-site futuro.
        erp_rows = _to_erp_rows(order)
        valor_total = _valor_total(erp_rows)
        dt_entrega = _menor_dt_entrega(erp_rows)
        perfil = perfil_para(self._env)
        header_params = self._mapper.order_to_cabvendas(
            order,
            header_pk,
            client_id,
            perfil=perfil,
            valor_total=valor_total,
            dt_entrega=dt_entrega,
        )
        # ULT_ALT_USER repete ULT_INS_USER (ultimo elemento da tupla) — nao
        # entra no mapper pra um erro de ordem na tupla nao passar despercebido.
        cur.execute(queries.INSERT_CAB_VENDAS, (*header_params, header_params[-1]))
        cur.execute(queries.UPDATE_CODPED_PAI, (header_pk,))
        logger.debug(
            f"CAB_VENDAS inserido: CODIGO={header_pk} "
            f"PEDIDO_CLIENTE={pedido_cliente!r} CLIENTE={client_id}"
        )

        # 5. Insert items. Mesmo `perfil` do cabecalho (ICMS_PORC, REDUCAO,
        #    CFOP_PRINCIPAL sao por ambiente, nao por item). CFOP_PRINCIPAL
        #    e o 16o valor do INSERT — anexado aqui, fora da tupla do mapper
        #    (que tem 15 elementos).
        items_inserted = 0
        for row in erp_rows:
            product_seq, unid = self._find_product(cur, row)
            cur.execute(queries.GET_NEXT_CORPOVENDAS_CODIGO)
            item_pk: int = cur.fetchone()[0]
            item_params = self._mapper.item_to_corpovendas(
                row, item_pk, header_pk, product_seq, perfil=perfil, unid=unid
            )
            cur.execute(queries.INSERT_CORPO_VENDAS, (*item_params, perfil.cfop_principal))
            items_inserted += 1

        cur.close()
        logger.info(
            f"Firebird: pedido {pedido_cliente!r} inserido — {items_inserted} item(s) "
            f"(CAB_VENDAS.CODIGO={header_pk})"
        )
        return FirebirdExportResult(
            order_number=order.header.order_number,
            items_inserted=items_inserted,
            fire_codigo=header_pk,
        )

    def _find_client(self, cur, cnpj: str | None) -> int | None:
        if not cnpj:
            return None
        digits = re.sub(r"\D", "", cnpj)
        if not digits:
            return None
        cur.execute(queries.FIND_CLIENT_BY_CNPJ, (digits,))
        row = cur.fetchone()
        return row[0] if row else None

    def _validate_client_id(self, cur, codigo: int) -> int | None:
        """Confirma que o codigo ainda existe e está ativo em CADASTRO."""
        cur.execute(queries.FIND_CLIENT_BY_CODIGO, (int(codigo),))
        row = cur.fetchone()
        return row[0] if row else None

    def _find_product(self, cur, row: ERPRow) -> tuple[int | None, str]:
        """Retorna (product_seq, unid). `unid` vem de PRODUTOS.UNIDADE no
        cadastro do Fire — "UN" quando o produto nao e encontrado ou a
        coluna vem vazia/NULL (kits cadastrados usam 'KIT').
        """
        # Try EAN first, then alternative code
        if row.ean:
            cur.execute(queries.FIND_PRODUCT_BY_EAN, (row.ean,))
            result = cur.fetchone()
            if result:
                return result[0], self._unid_from_row(result)

        if row.codigo_produto:
            cur.execute(queries.FIND_PRODUCT_BY_CODE, (row.codigo_produto,))
            result = cur.fetchone()
            if result:
                return result[0], self._unid_from_row(result)

        logger.warning(
            f"Produto não encontrado no ERP — EAN={row.ean!r} "
            f"código={row.codigo_produto!r}. Inserindo sem FK."
        )
        return None, "UN"

    @staticmethod
    def _unid_from_row(result: tuple) -> str:
        """result = (SEQ, DESCRICAO, PRECO_VENDA, UNIDADE)."""
        unid = (result[3] or "").strip()
        return unid or "UN"
