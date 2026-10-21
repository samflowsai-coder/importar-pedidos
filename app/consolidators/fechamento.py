"""Fechamento do lote intercompany: consolida as pernas e cria o pedido espelho.

Roda numa thread do processo web (`loop`, a cada 5 min): no Windows do cliente
o worker/APScheduler não roda. Fecha todo lote cuja janela venceu
(`lotes_repo.lotes_vencidos`), então servidor fora na segunda 8h fecha quando
voltar. O botão "fechar agora" chama `fechar_lote` direto.

O fechamento NÃO grava no Fire. Cria a linha de `imports` do espelho no
ambiente espelho (MM) em `parsed`, e o pedido passa pela revisão e pelo
"Cadastrar no Fire" de sempre.

Antes de fechar, a varredura de recuperação anexa pernas da origem que foram
enviadas ao Fire pelo portal mas ficaram fora do lote (o anexo do request path
falhou e só deixou warning). Pernas que entraram no Fire por fora do portal
(`found_in_fire`, importador Excel) ficam fora: sem `CORPO_VENDAS.CODIGO` por
item não há rastro do lote fiscal.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.consolidators import integracao
from app.consolidators.lote import Consolidado, PernaDoLote, consolidar
from app.models.order import Order, OrderHeader
from app.observability.trace import current_trace_id, new_trace_id
from app.persistence import context as env_context
from app.persistence import db, environments_repo, erp_vinculo_repo, lotes_repo, repo
from app.state import EventSource, LifecycleEvent, transition
from app.utils.logger import logger

INTERVALO_S = 300.0
# 'erro' não é fechado: cada ciclo tenta de novo (dado corrigido fecha).
_ABERTOS = ("aberto", "aguardando_fator", "erro")
_FECHADOS = ("fechado", "enviado")
_CARENCIA = timedelta(minutes=10)
# Serializa a thread e o botão "fechar agora" dentro do processo. Reentrante
# porque `fechar_vencidos` segura o lock e chama `fechar_lote`.
_LOCK = threading.RLock()


class _FechamentoInvalidoError(Exception):
    """Dado do lote que impede fechar: vira `marcar_erro` com a mensagem."""


def habilitado() -> bool:
    return os.environ.get("PORTAL_LOTES") != "0"


def _resultado(
    resultado: str, *, import_id_espelho: str | None = None, motivo: str | None = None
) -> dict:
    return {"resultado": resultado, "import_id_espelho": import_id_espelho, "motivo": motivo}


# --- fechar um lote -----------------------------------------------------------


def fechar_lote(lote_id: int, *, por: str, agora: datetime | None = None) -> dict:
    """Fecha um lote. Erro de dado vira `erro` com o lote marcado; falha de
    infraestrutura (SQLite) propaga e o próximo ciclo tenta de novo.

    Fechado por uma pessoa ("fechar agora", `por != "sistema"`): roda antes a
    varredura de recuperação da config do lote, como o ciclo faz."""
    agora = agora or datetime.now()
    with _LOCK:
        if por != "sistema":
            _recuperar_do_lote(lote_id, agora)
        return _fechar_lote(lote_id, por=por, agora=agora)


def _recuperar_do_lote(lote_id: int, agora: datetime) -> int:
    with db.connect_shared() as conn:
        lote = lotes_repo.get(conn, lote_id)
        cfg = lotes_repo.get_config(conn, lote["env_origem_slug"]) if lote else None
    if cfg is None or not cfg["ativo"]:
        return 0
    return _recuperar(cfg, agora)


def _fechar_lote(lote_id: int, *, por: str, agora: datetime) -> dict:
    with db.connect_shared() as conn:
        lote = lotes_repo.get(conn, lote_id)
        if lote is None:
            return _resultado("erro", motivo=f"lote {lote_id} não existe")
        if lote["status"] in _FECHADOS:
            return _resultado("ja_fechado", import_id_espelho=lote["import_id_espelho"])
        if lote["status"] not in _ABERTOS:
            return _resultado("erro", motivo=lote.get("erro_motivo") or f"lote em {lote['status']}")
        pernas = lotes_repo.pernas_do_lote(conn, lote_id)
        if not pernas:
            return _resultado("vazio")
        if lote["fator_preco_aplicado"] is None:
            lotes_repo.marcar_aguardando_fator(conn, lote_id)
            return _resultado("aguardando_fator")
        cfg = lotes_repo.get_config(conn, lote["env_origem_slug"])

    try:
        if environments_repo.get_by_slug(lote["env_espelho_slug"]) is None:
            raise _FechamentoInvalidoError(
                f"ambiente espelho {lote['env_espelho_slug']!r} não existe"
            )
        consolidado = _consolidar(lote, cfg, pernas)
    except (_FechamentoInvalidoError, ValueError, InvalidOperation) as exc:
        motivo = str(exc) or type(exc).__name__
        with db.connect_shared() as conn:
            lotes_repo.marcar_erro(conn, lote_id=lote_id, motivo=motivo, agora=agora)
        logger.error(f"lote: {lote['chave_lote']} não fechou: {motivo}")
        return _resultado("erro", motivo=motivo)

    # Fecha PRIMEIRO (banco compartilhado, commitado) e só então materializa o
    # espelho: fechar recusado nunca deixa linha `lote-<chave>` no MM.
    import_id_espelho = f"lote-{lote['chave_lote']}"
    with db.connect_shared() as conn:
        ok = lotes_repo.fechar(
            conn,
            lote_id=lote_id,
            import_id_espelho=import_id_espelho,
            composicao=_serializar(consolidado.composicao),
            pernas_esperadas=len(pernas),
            por=por,
            agora=agora,
        )
        atual = lotes_repo.get(conn, lote_id)
    if not ok:
        if atual["status"] in _ABERTOS:
            motivo = "perna nova chegou durante o fechamento; tenta no próximo ciclo"
            logger.warning(f"lote: {lote['chave_lote']} {motivo}")
            return _resultado("erro", motivo=motivo)
        if atual["status"] in _FECHADOS:
            return _resultado("ja_fechado", import_id_espelho=atual["import_id_espelho"])
        return _resultado("erro", motivo=atual.get("erro_motivo") or f"lote em {atual['status']}")

    logger.info(
        f"lote: {lote['chave_lote']} fechado — {len(pernas)} pernas, "
        f"{len(consolidado.order.items)} linhas, espelho {import_id_espelho} em "
        f"{lote['env_espelho_slug']}"
    )
    try:
        materializar_espelho(atual, agora=agora)
    except Exception as exc:  # noqa: BLE001 — lote já fechado; o próximo ciclo repara
        logger.error(
            f"lote: espelho {import_id_espelho} não materializado, fica para o próximo "
            f"ciclo: {type(exc).__name__}: {exc}"
        )
    return _resultado("fechado", import_id_espelho=import_id_espelho)


def _consolidar(lote: dict, cfg: dict | None, pernas: list[dict]) -> Consolidado:
    """Mesma entrada (pernas em ordem de id, fator/modo congelados) no fechamento e
    na materialização: a composição gravada é a do pedido materializado."""
    if cfg is None:
        raise _FechamentoInvalidoError(f"sem lote_config para {lote['env_origem_slug']!r}")
    return consolidar(
        _carregar_pernas(lote["env_origem_slug"], pernas),
        env_origem_slug=lote["env_origem_slug"],
        cnpj_revenda=cfg["cnpj_revenda"],
        nome_revenda=cfg["nome_revenda"],
        chave_lote=lote["chave_lote"],
        fator_preco=Decimal(lote["fator_preco_aplicado"]),
        modo_preco=lote["modo_preco_aplicado"],
    )


def _reconstruir(lote: dict, cfg: dict | None, pernas: list[dict]) -> Consolidado:
    consolidado = _consolidar(lote, cfg, pernas)
    if _serializar(consolidado.composicao) != lote["composicao"]:
        raise RuntimeError(
            f"composição de {lote.get('import_id_espelho')} diverge da gravada no fechamento"
        )
    return consolidado


def consolidado_do_lote(lote: dict) -> Consolidado:
    """O pedido espelho (itens, composição e OBS) que o lote fechado gera hoje,
    conferido contra a composição gravada no fechamento. Falha propaga."""
    with db.connect_shared() as conn:
        pernas = lotes_repo.pernas_do_lote(conn, lote["id"])
        cfg = lotes_repo.get_config(conn, lote["env_origem_slug"])
    return _reconstruir(lote, cfg, pernas)


def previa_do_lote(lote: dict) -> Consolidado:
    """O pedido espelho que o lote geraria com o fator/modo já congelados nele.

    Serve a tela (lote aberto ou fechado): não compara com a composição gravada
    nem escreve nada. Falha propaga (snapshot ilegível, config ausente)."""
    with db.connect_shared() as conn:
        pernas = lotes_repo.pernas_do_lote(conn, lote["id"])
        cfg = lotes_repo.get_config(conn, lote["env_origem_slug"])
    return _consolidar(lote, cfg, pernas)


def _serializar(composicao: list) -> list[list[dict]]:
    return [
        [
            {"import_id": o.import_id, "indice": o.indice, "quantidade": str(o.quantidade)}
            for o in linha
        ]
        for linha in composicao
    ]


def _carregar_pernas(env_origem_slug: str, pernas: list[dict]) -> list[PernaDoLote]:
    env = environments_repo.get_by_slug(env_origem_slug)
    if env is None:
        raise _FechamentoInvalidoError(f"ambiente de origem {env_origem_slug!r} não existe")
    out: list[PernaDoLote] = []
    with env_context.active_env(env["id"], env["slug"]):
        for perna in pernas:
            ident = perna["import_id"]
            entry = repo.get_import(ident)
            if entry is None:
                raise _FechamentoInvalidoError(f"perna {ident} não existe em {env_origem_slug}")
            try:
                order = Order.model_validate(entry.get("snapshot"))
            except Exception as exc:  # noqa: BLE001 — snapshot ilegível é dado, não bug
                raise _FechamentoInvalidoError(
                    f"perna {ident}: snapshot ilegível ({type(exc).__name__})"
                ) from exc
            out.append(PernaDoLote(ident, order, perna["fire_codigo_origem"]))
    return out


def _tem_evento_imported(import_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM order_lifecycle_events WHERE import_id = ? AND event_type = ? LIMIT 1",
            (import_id, LifecycleEvent.IMPORTED.value),
        ).fetchone()
    return row is not None


def materializar_espelho(lote: dict, *, agora: datetime) -> bool:
    """Cria a linha `lote-<chave>` do lote fechado no ambiente espelho. Idempotente.

    Linha e evento `imported` já existem → no-op (não sobrescreve edição feita na
    revisão, como troca de produto). Reconstrói o pedido das pernas do lote
    (congeladas: `anexar_perna` recusa lote fechado) com o fator/modo do lote e
    confere a composição contra a gravada no fechamento. Devolve True se gravou.
    Falha propaga: quem chama loga e o próximo ciclo tenta de novo.
    """
    import_id = lote.get("import_id_espelho")
    if lote["status"] != "fechado" or not import_id:
        return False
    env = environments_repo.get_by_slug(lote["env_espelho_slug"])
    if env is None:
        raise _FechamentoInvalidoError(f"ambiente espelho {lote['env_espelho_slug']!r} não existe")
    with env_context.active_env(env["id"], env["slug"]):
        existe = repo.get_import(import_id) is not None
        tem_evento = _tem_evento_imported(import_id)
    if existe and tem_evento:
        return False

    with db.connect_shared() as conn:
        pernas = lotes_repo.pernas_do_lote(conn, lote["id"])
        cfg = lotes_repo.get_config(conn, lote["env_origem_slug"])
    consolidado = _reconstruir(lote, cfg, pernas)
    order = consolidado.order
    if lote.get("closed_at"):
        fechado_em = datetime.fromisoformat(lote["closed_at"])
        order = order.model_copy(
            update={
                "header": order.header.model_copy(
                    update={"issue_date": fechado_em.strftime("%d/%m/%Y")}
                )
            }
        )

    chave = lote["chave_lote"]
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": import_id,
                "environment_id": env["id"],
                "source_filename": f"Lote {chave}.lote",
                "imported_at": agora.isoformat(timespec="seconds"),
                "order_number": chave,
                "customer_cnpj": cfg["cnpj_revenda"],
                "customer": cfg["nome_revenda"],
                "output_files": [],
                "status": "success",
                "error": None,
                "snapshot": order.model_dump(),
                "portal_status": "parsed",
                "trace_id": current_trace_id() or new_trace_id(),
            }
        )
        repo.append_audit(
            import_id,
            "imported_to_portal",
            {
                "source": "lote_intercompany",
                "chave_lote": chave,
                "pernas": len(pernas),
                "linhas": len(order.items),
                "fator": lote["fator_preco_aplicado"],
                "modo": lote["modo_preco_aplicado"],
            },
        )
        _confirmar_sem_preco(import_id, chave, order)
        if not tem_evento:
            transition(
                import_id,
                LifecycleEvent.IMPORTED,
                source=EventSource.SYSTEM,
                payload={"source": "lote_intercompany", "chave_lote": chave},
            )
    return True


def _confirmar_sem_preco(import_id: str, chave: str, order: Order) -> None:
    """O espelho nasce com o "sem preço no Fire" confirmado pelo lote (BACKLOG 3.8).

    `PRECO_VENDA` da MM é vazio, então toda linha do espelho cairia em
    `no_price_in_fire` e pediria a mesma confirmação toda segunda. O preço do
    espelho já é auditável pela composição (`integracao._divergencias` confere
    antes do envio). Só o ack: divergência e preço ausente no pedido seguem
    bloqueando em `is_blocking`. Linha sem EAN nem código não é coberta e
    continua pedindo confirmação. Roda dentro do `active_env` do espelho.
    """
    itens = [
        {"ean": it.ean, "product_code": it.product_code, "fire_product_id": None}
        for it in order.items
        if it.ean or it.product_code
    ]
    repo.set_sem_preco_ack(import_id, by_email=f"lote:{chave}", items=itens)
    repo.append_audit(
        import_id,
        "sem_preco_acknowledged",
        {"origem": "lote", "chave_lote": chave, "items": itens},
    )


# --- varredura de recuperação -------------------------------------------------


def _recuperar(cfg: dict, agora: datetime) -> int:
    """Anexa pernas enviadas ao Fire pelo portal que ficaram fora do lote. Nunca levanta."""
    slug = cfg["env_origem_slug"]
    try:
        with db.connect_shared() as conn:
            # Piso = o último religamento do lote (envio feito com o lote desligado
            # não entra). Não o máximo com a 1ª perna anexada: toda perna anexa
            # depois da ativação, e o anexo que falhou logo após ligar ficaria
            # abaixo do piso para sempre. A 1ª perna anexada (não o início da
            # janela mais antiga: backlog recuaria o piso) só vale para config
            # sem o carimbo.
            desde = cfg.get("ativo_desde")
            if not desde:
                desde = conn.execute(
                    "SELECT MIN(created_at) FROM intercompany_lote_item WHERE env_origem_slug = ?",
                    (slug,),
                ).fetchone()[0]
            if not desde:
                return 0
            no_lote = {
                r[0]
                for r in conn.execute(
                    "SELECT import_id FROM intercompany_lote_item WHERE env_origem_slug = ?",
                    (slug,),
                )
            }
            # Espelho nunca é perna (config torta com origem == espelho fecharia o laço).
            no_lote |= {
                r[0]
                for r in conn.execute(
                    "SELECT import_id_espelho FROM intercompany_lote "
                    "WHERE import_id_espelho IS NOT NULL"
                )
            }
        env = environments_repo.get_by_slug(slug)
        if env is None:
            return 0
        # Carência: o request path anexa logo depois do envio; não disputar com ele.
        ate = (agora - _CARENCIA).isoformat(timespec="seconds")
        with env_context.active_env(env["id"], env["slug"]), db.connect() as conn:
            ids = conn.execute(
                """
                SELECT id FROM imports
                WHERE portal_status = 'sent_to_fire'
                  AND sent_to_fire_at IS NOT NULL AND fire_codigo IS NOT NULL
                  AND sent_to_fire_at >= ? AND sent_to_fire_at < ?
                ORDER BY sent_to_fire_at, id
                """,
                (desde, ate),
            ).fetchall()
            candidatas = []
            for (ident,) in ids:
                if ident in no_lote:
                    continue
                row = conn.execute(
                    """
                    SELECT id, imported_at, fire_codigo, order_number, customer_cnpj,
                           customer_name, snapshot_json
                    FROM imports WHERE id = ?
                    """,
                    (ident,),
                ).fetchone()
                c = dict(row)
                c["itens_fire"] = erp_vinculo_repo.vinculos_do_import(conn, ident)
                candidatas.append(c)
    except Exception as exc:  # noqa: BLE001 — a varredura não derruba o fechamento
        logger.warning(f"lote: varredura de {slug} falhou: {type(exc).__name__}: {exc}")
        return 0

    recuperadas = 0
    for c in candidatas:
        try:
            with db.connect_shared() as conn:
                lote_id = integracao.anexar_na_janela(
                    conn,
                    cfg,
                    import_id=c["id"],
                    importado=date.fromisoformat(str(c["imported_at"])[:10]),
                    fire_codigo=int(c["fire_codigo"]),
                    itens_fire=c["itens_fire"],
                    order=_order_da_perna(c),
                    agora=agora,
                )
        except Exception as exc:  # noqa: BLE001 — uma perna ruim não para as outras
            logger.warning(
                f"lote: perna {c['id']} ({slug}) não recuperada: {type(exc).__name__}: {exc}"
            )
            continue
        if lote_id is None:
            logger.error(f"lote: perna {c['id']} ({slug}) sem lote aberto — não recuperada")
            continue
        recuperadas += 1
        logger.warning(
            f"lote: perna recuperada {c['id']} ({slug}, Fire {c['fire_codigo']}, "
            f"{len(c['itens_fire'])} itens vinculados) -> lote {lote_id}"
        )
    return recuperadas


def _order_da_perna(c: dict) -> Order:
    """Cabeçalho da perna para o `intercompany_lote_item`. Snapshot ilegível cai
    nas colunas de `imports`; o fechamento então marca o lote em erro, visível."""
    try:
        return Order.model_validate(json.loads(c["snapshot_json"]))
    except Exception:  # noqa: BLE001
        return Order(
            header=OrderHeader(
                order_number=c["order_number"],
                customer_cnpj=c["customer_cnpj"],
                customer_name=c["customer_name"],
            ),
            items=[],
        )


# --- ciclo --------------------------------------------------------------------


def fechar_vencidos(agora: datetime | None = None) -> dict:
    """Varredura de recuperação (configs ativas), fecha os lotes vencidos (de
    qualquer config: `ativo` só barra perna nova) e repara espelho que faltou."""
    agora = agora or datetime.now()
    out = {
        "fechados": 0,
        "aguardando_fator": 0,
        "vazios": 0,
        "erros": 0,
        "recuperadas": 0,
        "materializados": 0,
    }
    contador = {"fechado": "fechados", "aguardando_fator": "aguardando_fator", "vazio": "vazios"}
    with _LOCK:
        with db.connect_shared() as conn:
            configs = lotes_repo.listar_configs(conn, so_ativas=True)
        for cfg in configs:
            out["recuperadas"] += _recuperar(cfg, agora)
        with db.connect_shared() as conn:
            vencidos = lotes_repo.lotes_vencidos(conn, agora)
        for lote in vencidos:
            try:
                r = fechar_lote(lote["id"], por="sistema", agora=agora)
            except Exception as exc:  # noqa: BLE001 — um lote não derruba os outros
                logger.error(f"lote: {lote['chave_lote']} falhou: {type(exc).__name__}: {exc}")
                out["erros"] += 1
                continue
            if r["resultado"] == "erro":
                out["erros"] += 1
            elif r["resultado"] in contador:
                out[contador[r["resultado"]]] += 1
        out["materializados"] = _reparar_espelhos(agora, ja_fechados={lt["id"] for lt in vencidos})
    return out


def _enviado_por_fora(lote: dict, agora: datetime) -> bool:
    """Lote `fechado` cujo espelho já está no Fire (a conclusão do envio não marcou
    o lote): marca `enviado`, com `lote_fiscal_ok` = o rastro do SP1 existe."""
    import_id = lote.get("import_id_espelho")
    env = environments_repo.get_by_slug(lote["env_espelho_slug"])
    if env is None or not import_id:
        return False
    with env_context.active_env(env["id"], env["slug"]), db.connect() as conn:
        row = conn.execute(
            "SELECT fire_codigo FROM imports WHERE id = ? AND fire_codigo IS NOT NULL",
            (import_id,),
        ).fetchone()
        if row is None:
            return False
        rastro = (
            conn.execute(
                "SELECT 1 FROM erp_lote_perna WHERE chave_lote = ? LIMIT 1",
                (lote["chave_lote"],),
            ).fetchone()
            is not None
        )
    with db.connect_shared() as conn:
        marcou = lotes_repo.marcar_enviado(
            conn,
            lote_id=lote["id"],
            fire_codigo_espelho=int(row[0]),
            lote_fiscal_ok=rastro,
            agora=agora,
        )
    if marcou:
        logger.warning(
            f"lote: {lote['chave_lote']} já estava no Fire (nº {row[0]}): marcado enviado "
            f"no reparo, rastro {'ok' if rastro else 'ausente'}"
        )
    return marcou


def _reparar_espelhos(agora: datetime, *, ja_fechados: set[int]) -> int:
    """Lote `fechado` sem a linha do espelho (ou sem o evento `imported`) no MM;
    espelho que já foi ao Fire marca o lote `enviado`."""
    with db.connect_shared() as conn:
        fechados = [
            lotes_repo.get(conn, r[0])
            for r in conn.execute(
                "SELECT id FROM intercompany_lote WHERE status = 'fechado' ORDER BY id"
            ).fetchall()
            if r[0] not in ja_fechados
        ]
    feitos = 0
    for lote in fechados:
        try:
            if _enviado_por_fora(lote, agora):
                continue
            if materializar_espelho(lote, agora=agora):
                feitos += 1
                logger.warning(f"lote: espelho {lote['import_id_espelho']} materializado no reparo")
        except Exception as exc:  # noqa: BLE001 — fica para o próximo ciclo
            logger.error(
                f"lote: espelho {lote['import_id_espelho']} segue sem materializar: "
                f"{type(exc).__name__}: {exc}"
            )
    return feitos


def loop() -> None:
    logger.info("lote: thread de fechamento iniciada")
    while True:
        try:
            fechar_vencidos()
        except Exception as exc:  # noqa: BLE001 — a thread daemon não pode morrer
            logger.error(f"lote: ciclo de fechamento falhou: {exc!r}")
        time.sleep(INTERVALO_S)
