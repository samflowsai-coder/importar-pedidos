"""Ganchos do lote intercompany no fluxo de envio ao Fire.

Parte 1 (perna de origem) e `concluir_envio_espelho` rodam depois que o Fire
já aceitou o pedido: nunca levantam. Falha aqui vira log e o envio segue —
o pedido já está no Fire e a UI não pode ver erro por causa do lote.

Parte 2 (perna espelho) roda ANTES do insert: `preparar_envio_espelho` pode
bloquear o envio (lote alterado depois do fechamento, lote fora de `fechado`)
e reserva o `lote_fiscal` do SP1, que `abortar_envio_espelho` desfaz se o
insert não acontecer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from app.consolidators.janela import janela_de, proxima_janela
from app.erp_eventos import lote_fiscal
from app.exporters.firebird_exporter import FirebirdExportResult
from app.models.order import Order
from app.persistence import (
    db,
    environments_repo,
    erp_bases_repo,
    lotes_repo,
    repo,
    router,
)
from app.persistence.erp_bases_repo import BaseErpConfig
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


# --- parte 2: envio da perna espelho -------------------------------------------


@dataclass
class EnvioEspelho:
    """Espelho liberado para o insert. `reservado` vira False quando a reserva do
    `lote_fiscal` é consumida (registro) ou cancelada — abortar fica idempotente.
    `concluido` marca que o Fire aceitou o insert: dali em diante exceção no
    request path não cancela mais nada (o rastro já foi gravado ou auditado)."""

    lote: dict
    obs: str
    reservado: bool
    concluido: bool = False


@dataclass(frozen=True)
class BloqueioEspelho:
    motivo: str
    detalhe: dict

    @property
    def mensagem(self) -> str:
        if self.motivo == "lote_alterado":
            return (
                "Pedido de lote alterado depois do fechamento (quantidade, preço ou "
                "número de linhas). O lote fiscal é rastreado por linha: desfaça a "
                "alteração para cadastrar no Fire."
            )
        if self.motivo.startswith("lote_status_"):
            status = self.motivo.removeprefix("lote_status_")
            return f"Lote {self.detalhe.get('chave_lote')} está '{status}', não 'fechado'."
        if self.motivo == "ambiente_errado":
            return (
                f"Pedido do lote {self.detalhe.get('chave_lote')} só vai ao Fire pela empresa "
                f"{self.detalhe.get('env_espelho_slug')}."
            )
        if self.motivo == "reserva_falhou":
            return "Não foi possível reservar o lote fiscal do SP1. Tente de novo."
        return f"Lote {self.detalhe.get('chave_lote')} não pôde ser conferido: {self.motivo}."


class _SemRastroError(Exception):
    """Falta uma peça do rastro por item; a mensagem é o motivo da auditoria."""


def e_espelho(import_id: str) -> bool:
    with db.connect_shared() as conn:
        return lotes_repo.lote_do_espelho(conn, import_id) is not None


def _base_sp1(slug: str) -> BaseErpConfig | None:
    """Base SP1 ligada do ambiente; desligada (`ativo = 0`) conta como sem SP1."""
    env = environments_repo.get_by_slug(slug)
    cfg = erp_bases_repo.obter(env["id"]) if env else None
    return cfg if cfg is not None and cfg.ativo else None


def _dec(valor: float | None) -> Decimal | None:
    return None if valor is None else Decimal(str(valor))


def _divergencias(order: Order, esperado: Order, composicao: list) -> list[dict]:
    """Linhas cuja quantidade ou preço não é o do fechamento. Produto pode mudar
    (de-para na revisão); quantidade, preço e número de linhas não."""
    if not (len(order.items) == len(esperado.items) == len(composicao)):
        return [
            {
                "linhas": len(order.items),
                "linhas_no_fechamento": len(composicao),
            }
        ]
    out = []
    for i, (item, alvo, origens) in enumerate(
        zip(order.items, esperado.items, composicao, strict=True)
    ):
        soma = sum((Decimal(str(o["quantidade"])) for o in origens), Decimal("0"))
        qtd, preco = _dec(item.quantity), _dec(item.unit_price)
        if qtd != soma or qtd != _dec(alvo.quantity) or preco != _dec(alvo.unit_price):
            out.append(
                {
                    "linha": i,
                    "quantidade": item.quantity,
                    "quantidade_no_fechamento": str(soma),
                    "preco": item.unit_price,
                    "preco_no_fechamento": alvo.unit_price,
                }
            )
    return out


def preparar_envio_espelho(
    import_id: str,
    order: Order,
    request_env: dict | None,
) -> EnvioEspelho | BloqueioEspelho | None:
    """`None` = não é espelho (caminho de sempre). Roda depois do portão de preço
    (`preco_trava.decidir_envio`)."""
    with db.connect_shared() as conn:
        lote = lotes_repo.lote_do_espelho(conn, import_id)
    if lote is None:
        return None
    base = {"chave_lote": lote["chave_lote"], "lote_id": lote["id"]}
    if (request_env or {}).get("slug") != lote["env_espelho_slug"]:
        return BloqueioEspelho(
            "ambiente_errado",
            {
                **base,
                "env_espelho_slug": lote["env_espelho_slug"],
                "env_request": (request_env or {}).get("slug"),
            },
        )
    if lote["status"] != "fechado":
        return BloqueioEspelho(f"lote_status_{lote['status']}", base)

    from app.consolidators import fechamento  # fechamento importa este módulo

    try:
        esperado = fechamento.consolidado_do_lote(lote)
    except Exception as exc:  # noqa: BLE001 — sem conferir não envia
        logger.error(f"lote: {lote['chave_lote']} não conferido: {type(exc).__name__}: {exc}")
        return BloqueioEspelho("lote_invalido", {**base, "erro": f"{type(exc).__name__}: {exc}"})
    divergentes = _divergencias(order, esperado.order, lote["composicao"])
    if divergentes:
        return BloqueioEspelho("lote_alterado", {**base, "linhas": divergentes})

    slug = lote["env_espelho_slug"]
    cfg = _base_sp1(slug)
    if cfg is None:
        return EnvioEspelho(lote=lote, obs=esperado.obs, reservado=False)
    try:
        with router.env_connect(slug) as conn:
            # Reserva que sobrou de uma tentativa interrompida não trava o reenvio.
            lote_fiscal.cancelar_reserva(conn, chave_lote=lote["chave_lote"])
            lote_fiscal.reservar_lote(
                conn,
                chave_lote=lote["chave_lote"],
                base_espelho=cfg.base,
                agora=datetime.now(UTC),
            )
    except Exception as exc:  # noqa: BLE001 — sem reserva o rastro do SP1 se perde
        logger.error(f"lote: reserva de {lote['chave_lote']} falhou: {type(exc).__name__}: {exc}")
        return BloqueioEspelho("reserva_falhou", {**base, "erro": f"{type(exc).__name__}: {exc}"})
    return EnvioEspelho(lote=lote, obs=esperado.obs, reservado=True)


def abortar_envio_espelho(envio: EnvioEspelho, request_env: dict | None) -> None:  # noqa: ARG001
    """Insert não aconteceu: cancela a reserva do `lote_fiscal`. Nunca levanta."""
    if not envio.reservado:
        return
    chave = envio.lote["chave_lote"]
    try:
        with router.env_connect(envio.lote["env_espelho_slug"]) as conn:
            lote_fiscal.cancelar_reserva(conn, chave_lote=chave)
        envio.reservado = False
    except Exception as exc:  # noqa: BLE001 — reserva vence sozinha em 1 h
        logger.error(f"lote: reserva de {chave} não cancelada: {type(exc).__name__}: {exc}")


@contextmanager
def guardar_reserva(envio: EnvioEspelho | None, request_env: dict | None) -> Iterator[None]:
    """Exceção ANTES de o Fire aceitar o insert cancela a reserva e propaga como
    sempre. Depois de `concluir_envio_espelho` não cancela: o espelho está no Fire."""
    try:
        yield
    except BaseException:
        if envio is not None and not envio.concluido:
            abortar_envio_espelho(envio, request_env)
        raise


def _pernas_sp1(lote: dict, result: FirebirdExportResult) -> list[lote_fiscal.PernaLote]:
    cfg_origem = _base_sp1(lote["env_origem_slug"])
    if cfg_origem is None:
        raise _SemRastroError("sp1_origem_desligado")
    with db.connect_shared() as conn:
        pernas = {p["import_id"]: p for p in lotes_repo.pernas_do_lote(conn, lote["id"])}
    linha_do_indice = {indice: codigo for codigo, indice in result.itens}
    saida = []
    for i, origens in enumerate(lote["composicao"]):
        linha, produto = linha_do_indice.get(i), result.produtos.get(i)
        if linha is None or produto is None:
            raise _SemRastroError(f"linha {i} do espelho sem CODIGO ou produto no Fire")
        for o in origens:
            perna = pernas.get(o["import_id"])
            if perna is None:
                raise _SemRastroError(f"perna {o['import_id']} fora do lote")
            item_origem = next((c for c, idx in perna["itens_fire"] if idx == o["indice"]), None)
            if item_origem is None:
                raise _SemRastroError(
                    f"item {o['indice']} da perna {o['import_id']} sem CODIGO no Fire de origem"
                )
            saida.append(
                lote_fiscal.PernaLote(
                    base_origem=cfg_origem.base,
                    erp_codigo_origem=int(perna["fire_codigo_origem"]),
                    erp_item_codigo_origem=int(item_origem),
                    produto_produtor=int(produto),
                    quantidade=Decimal(str(o["quantidade"])),
                    linha_lote=int(linha),
                )
            )
    return saida


def _registrar(envio: EnvioEspelho, result: FirebirdExportResult) -> None:
    lote = envio.lote
    slug = lote["env_espelho_slug"]
    cfg = _base_sp1(slug)
    if cfg is None:
        raise _SemRastroError("sp1_desligado")
    pernas = _pernas_sp1(lote, result)
    # Transação única: exceção dentro do bloco não comita nada (nem o registro
    # parcial); a reserva fica e quem chama a cancela numa conexão nova.
    with router.env_connect(slug) as conn:
        lote_fiscal.registrar_lote_fiscal(
            conn,
            tenant=cfg.tenant_id,
            chave_lote=lote["chave_lote"],
            base_espelho=cfg.base,
            erp_codigo_espelho=int(result.fire_codigo),
            pernas=pernas,
            agora=datetime.now(UTC),
        )
    envio.reservado = False  # o registro consome a reserva


def concluir_envio_espelho(
    envio: EnvioEspelho, result: FirebirdExportResult, request_env: dict | None
) -> None:
    """Fire aceitou o espelho: registra o `lote_fiscal` e marca o lote `enviado`.
    Nunca levanta — falha no rastro vira `lote_fiscal_ok = 0` + auditoria."""
    envio.concluido = True
    lote = envio.lote
    import_id = lote["import_id_espelho"]
    try:
        motivo = None
        if not envio.reservado:
            motivo = "sp1_desligado"
        else:
            try:
                _registrar(envio, result)
            except Exception as exc:  # noqa: BLE001
                motivo = str(exc) if isinstance(exc, _SemRastroError) else repr(exc)
                abortar_envio_espelho(envio, request_env)
        with db.connect_shared() as conn:
            marcou = lotes_repo.marcar_enviado(
                conn,
                lote_id=lote["id"],
                fire_codigo_espelho=int(result.fire_codigo),
                lote_fiscal_ok=motivo is None,
                agora=datetime.now(),
            )
        if not marcou:
            logger.error(f"lote: {lote['chave_lote']} enviado ao Fire mas não estava 'fechado'")
        if motivo is not None:
            repo.append_audit(import_id, "lote_fiscal_falhou", {"motivo": motivo})
            log = logger.info if motivo == "sp1_desligado" else logger.error
            log(f"lote: {lote['chave_lote']} sem lote_fiscal do SP1: {motivo}")
    except Exception as exc:  # noqa: BLE001 — o espelho já está no Fire
        logger.error(
            f"lote: conclusão do envio de {lote['chave_lote']} falhou: {type(exc).__name__}: {exc}"
        )
        try:
            repo.append_audit(
                import_id,
                "lote_fiscal_falhou",
                {"motivo": f"conclusao_falhou: {type(exc).__name__}: {exc}"},
            )
        except Exception as exc_audit:  # noqa: BLE001
            logger.error(f"lote: auditoria de {lote['chave_lote']} falhou: {exc_audit!r}")
