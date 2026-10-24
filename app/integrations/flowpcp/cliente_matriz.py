"""Pedido de rede sem CNPJ no header (Riachuelo): vai ao Flow como UM pedido, de um cliente fixo.

O Fire guarda um pedido por loja e continua assim. O Flow só reconhece cliente por CNPJ, e sem CNPJ o
pedido entra com `cliente_id` nulo (sem preço nem aprovação por rede). A divisão por loja
(`recebimento_por_loja`) resolve o CNPJ, mas vira N pedidos e N aprovações. Aqui o Samuel escolheu
(24/10/2026) o caminho do meio: um cliente "matriz" fixo por rede, um pedido, uma aprovação; o vínculo
com o Fire segue por item (`import_id:posição`) e não depende do cliente.

`PORTAL_FLOWPCP_CLIENTE_MATRIZ_<SLUG>` = `termo:cnpj:nome`, várias separadas por `;`. O `termo` é
procurado como palavra inteira (sem caixa nem acento) no nome do cliente do header. Entrada inválida é ignorada com
WARNING: o pedido cai no comportamento de antes em vez de deixar de ser enviado.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass

from app.erp.cnpj import cnpj_digits, cnpj_dv_ok
from app.erp.depara_cliente import ResolucaoCliente
from app.models.order import Order
from app.utils.logger import logger

_PREFIXO = "PORTAL_FLOWPCP_CLIENTE_MATRIZ_"


@dataclass(frozen=True)
class ClienteMatriz:
    termo: str
    cnpj: str
    nome: str


def _var(slug: str) -> str:
    return _PREFIXO + re.sub(r"[^A-Z0-9]", "_", slug.upper())


def _dobrar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold()


def configuradas(slug: str) -> list[ClienteMatriz]:
    var = _var(slug)
    saida: list[ClienteMatriz] = []
    for bruto in os.environ.get(var, "").split(";"):
        if not bruto.strip():
            continue
        partes = [p.strip() for p in bruto.split(":", 2)]
        if len(partes) != 3 or not all(partes) or not cnpj_dv_ok(partes[1]):
            logger.warning(f"{var}: entrada ignorada (esperado termo:cnpj:nome com CNPJ válido)")
            continue
        saida.append(ClienteMatriz(partes[0], cnpj_digits(partes[1]), partes[2]))
    return saida


def para_pedido(
    slug: str | None, order: Order, resolucao: ResolucaoCliente | None = None
) -> ClienteMatriz | None:
    """A matriz do pedido, ou None. Só vale com o header SEM CNPJ e sem intercompany resolvido (o
    cliente real, nesses casos, já está identificado e vai no envio)."""
    if not slug:
        return None
    if cnpj_digits(order.header.customer_cnpj) or (resolucao is not None and resolucao.resolvido):
        return None
    nome = _dobrar(order.header.customer_name or "")
    return next(
        (
            c
            for c in configuradas(slug)
            if re.search(rf"(?<![a-z0-9]){re.escape(_dobrar(c.termo))}(?![a-z0-9])", nome)
        ),
        None,
    )
