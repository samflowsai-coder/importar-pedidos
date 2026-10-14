"""Vigia de uma base do ERP: lê, compara com o último estado, enfileira eventos.

Ordem de I/O (spec SP1 §3.2): o ERP é lido FORA de qualquer transação do
SQLite; a gravação de estado + marca do seq + eventos é UMA transação no fim.
Ou o ciclo inteiro entra na fila, ou nada entra — nunca seq consumido sem
evento, nunca evento sem estado.

Quem chama segura a trava da base (`runner.py`): não existem duas vigias da
mesma base ao mesmo tempo neste processo.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from app.erp_eventos import eco, lote_fiscal
from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.contrato import ContratoInvalidoError
from app.erp_eventos.eventos import Vinculo, montar_eventos
from app.erp_eventos.modelo import PedidoErp, esta_aberto, para_estado
from app.erp_eventos.mudancas import calcular_mudancas
from app.persistence import erp_estado_repo, erp_vinculo_repo, router
from app.persistence.erp_bases_repo import BaseErpConfig
from app.utils.logger import logger


class LeitorErp(Protocol):
    def abertos(self) -> list[PedidoErp]: ...

    def por_codigo(self, codigos: list[int]) -> dict[int, PedidoErp]: ...


# erp_codigo → CNPJ do cliente real (14 dígitos) ou None
ResolverClientes = Callable[[list[PedidoErp]], dict[int, str | None]]
# erp_item_codigo → produto na base produtora, ou None
ResolverProdutores = Callable[[list[PedidoErp]], dict[int, int | None]]
# grava vínculos de item (XLSX) para pedidos vistos pela primeira vez
CasarVinculos = Callable[[list[PedidoErp]], None]


@dataclass(frozen=True)
class ResultadoVigia:
    base: str
    lidos: int
    eventos: int
    fora_do_contrato: int
    adiados: int


def vigiar(
    cfg: BaseErpConfig,
    *,
    leitor: LeitorErp,
    agora: datetime,
    resolver_clientes: ResolverClientes,
    resolver_produtores: ResolverProdutores,
    casar_vinculos: CasarVinculos,
) -> ResultadoVigia:
    observado_em = formatar_instante(agora)

    # 1. ERP: abertos + releitura de quem estava aberto e não veio.
    abertos = {p.erp_codigo: p for p in leitor.abertos()}
    with router.env_connect(cfg.env_slug) as conn:
        estados = erp_estado_repo.carregar_estados(conn, cfg.base)
    sumidos = sorted(c for c, (_, sit) in estados.items() if sit == "aberto" and c not in abertos)
    relidos = leitor.por_codigo(sumidos) if sumidos else {}
    atuais: dict[int, PedidoErp] = {**relidos, **abertos}

    # 2. Enriquecimento (cada um com o próprio I/O, antes da transação final).
    novos = [p for c, p in atuais.items() if c not in estados or estados[c][1] == "excluido"]
    casar_vinculos(novos)
    atuais = {
        p.erp_codigo: p
        for p in enriquecer(
            list(atuais.values()),
            resolver_clientes=resolver_clientes,
            resolver_produtores=resolver_produtores,
        )
    }

    # 3. Uma transação: estado + seq + fila.
    total_eventos = fora = adiados = 0
    with router.env_connect(cfg.env_slug) as conn:
        segurar_intermediario = cfg.papel == "produtora" and lote_fiscal.reserva_ativa(
            conn, cfg.base, agora
        )
        for codigo in sorted(set(atuais) | set(sumidos)):
            atual = atuais.get(codigo)
            anterior, situacao = estados.get(codigo, (None, None))
            novo = anterior is None or situacao == "excluido"
            if (
                novo
                and atual is not None
                and segurar_intermediario
                and atual.cliente_erp_cnpj in cfg.intermediarios
            ):
                adiados += 1  # lote fiscal sendo gravado: o lote_fiscal tem que entrar antes
                continue
            try:
                n = _processar_pedido(conn, cfg, codigo, anterior, situacao, atual, observado_em)
            except ContratoInvalidoError as exc:
                fora += 1
                logger.warning(
                    f"erp_eventos: pedido {cfg.base}:{codigo} fora do contrato, fica para a "
                    f"próxima vigia — {exc}"
                )
                continue
            total_eventos += n
        erp_estado_repo.registrar_vigia(conn, cfg.base, observado_em)

    logger.info(
        f"erp_eventos: vigia '{cfg.base}' lidos={len(atuais)} eventos={total_eventos} "
        f"fora_do_contrato={fora} adiados={adiados}"
    )
    return ResultadoVigia(cfg.base, len(atuais), total_eventos, fora, adiados)


def enriquecer(
    pedidos: list[PedidoErp],
    *,
    resolver_clientes: ResolverClientes,
    resolver_produtores: ResolverProdutores,
) -> list[PedidoErp]:
    """Preenche cliente real e produto da produtora (vigia e foto usam o mesmo)."""
    clientes = resolver_clientes(pedidos)
    produtores = resolver_produtores(pedidos)
    return [
        replace(
            p,
            cliente_real_cnpj=clientes.get(p.erp_codigo),
            itens=tuple(
                replace(i, produto_produtor=produtores.get(i.erp_item_codigo)) for i in p.itens
            ),
        )
        for p in pedidos
    ]


def _processar_pedido(
    conn: sqlite3.Connection,
    cfg: BaseErpConfig,
    codigo: int,
    anterior: PedidoErp | None,
    situacao: str | None,
    atual: PedidoErp | None,
    observado_em: str,
) -> int:
    mudancas = calcular_mudancas(anterior, anterior_excluido=situacao == "excluido", atual=atual)
    pedido = atual if atual is not None else anterior
    if pedido is None:
        return 0

    if mudancas:
        vinculos = erp_vinculo_repo.vinculos_por_item(
            conn, cfg.base, [i.erp_item_codigo for i in pedido.itens]
        )
        imports = {v.import_id for v in vinculos.values()}
        correlacao = None
        data = next((m for m in mudancas if m.tipo == "data_entrega_alterada"), None)
        if data is not None and data.depois is not None:
            correlacao = eco.buscar_correlacao(conn, cfg.base, codigo, data.depois)
        primeiro = erp_estado_repo.proximo_seq(conn, cfg.base, codigo)
        eventos = montar_eventos(
            tenant=cfg.tenant_id,
            base=cfg.base,
            observado_em=observado_em,
            pedido=pedido,
            mudancas=mudancas,
            primeiro_seq=primeiro,
            vinculos={k: Vinculo(v.import_id, v.indice_original) for k, v in vinculos.items()},
            import_do_pedido=next(iter(imports)) if len(imports) == 1 else None,
            correlacao_data_entrega=correlacao,
        )
        erp_estado_repo.avancar_seq(conn, cfg.base, codigo, primeiro + len(eventos) - 1)
        erp_estado_repo.enfileirar(conn, eventos, observado_em)

    if atual is None:
        nova_situacao = "excluido"
    else:
        nova_situacao = "aberto" if esta_aberto(atual.status) else "encerrado"
    if (
        mudancas
        or anterior is None
        or situacao != nova_situacao
        or para_estado(pedido) != para_estado(anterior)
    ):
        erp_estado_repo.gravar_estado(conn, cfg.base, pedido, nova_situacao, observado_em)
    return len(mudancas)
