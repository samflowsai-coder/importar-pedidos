"""Conversa da trava de preço com o Flow (contrato precos v1), na thread `flowpcp-envio`.

`rodar(agora)` é chamado a cada volta de `envio_portal.ciclo()`. Para cada
ambiente ativo com a trava em `observando` ou `ligado` e `environments.cnpj`
válido, com a config do Flow de `preco_fonte_slug or slug` (Nasmar fala com o
Flow da MM e manda o CNPJ dela como `empresaFaturadora`):

1. **Tabela** (rota a), a cada `INTERVALO_TABELA`: páginas até `temMais: false`,
   cada uma numa transação (`precos_repo.aplicar_pagina`). `reiniciar: true` ou
   cópia de outra empresa → `precos_repo.reiniciar` e recomeça sem cursor.
   Falha → nada aplicado daquela página, cursor parado.
2. **Só em `ligado`**, a cada `INTERVALO_APROVACAO` e só se a última página
   completa trouxe `precos.aprovacao.v1` em `capacidades`: envia as revisões
   pendentes e as retiradas (rota b), lê as decisões da empresa, grava e
   confirma (rota c).

Respostas da rota (b): 201/200 → `enviada` (2xx fora do contrato também: o Flow
gravou); 409 da vigente → piso e reenvio na mesma volta; 409 de revisão que já
não é a vigente → fica `substituida`; 422 de conteúdo → `recusada_flow`; 413 e
400 `payload_invalido` (e corpo inválido aqui) → bloqueio local daquela revisão;
403, 400/401/404 e outros → pausa só da aprovação (a tabela segue); 429/5xx/rede
→ para o envio desta volta (retiradas e decisões seguem). Pausa e transitório
nunca tocam a revisão. Na tabela, 400 `cursor_invalido` com cursor salvo
reinicia a cópia como o `reiniciar`.

Estado por ambiente (`geradoEm` da cópia completa, último erro, contagem de
`pendente_envio`/`bloqueada_local`/`recusada_flow`) vai ao log quando muda
(`flowpcp.precos estado`).
`PORTAL_FLOWPCP_PRECOS=0` desliga tudo (ausente = ligado).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from app.erp import preco_regra, preco_trava
from app.integrations.flowpcp import precos_schema
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.config import FlowPCPConfig, flowpcp_config_for_slug
from app.persistence import environments_repo, precos_repo, router
from app.persistence import preco_revisao_repo as revs
from app.utils.logger import logger

INTERVALO_TABELA = timedelta(seconds=60)
INTERVALO_APROVACAO = timedelta(seconds=30)
PAUSA = timedelta(minutes=15)
LIMITE_TABELA = 500
LIMITE_DECISOES = 100
LOTE_ENVIO = 50
MAX_PAGINAS = 200
MAX_REINICIOS = 2
MAX_VOLTAS_DECISOES = 20
MAX_REENVIOS = 3
CAPACIDADE_APROVACAO = "precos.aprovacao.v1"
MOTIVO_RETIRADA_PADRAO = "pedido retirado no Portal"
BLOQUEIO_CORPO_LOCAL = "corpo fora do contrato, não enviado ao Flow"
BLOQUEIO_CORPO_FLOW = "corpo fora do contrato (o Flow não aceitou o formato)"
BLOQUEIO_GRANDE = "pedido grande demais para o Flow (corpo acima de 2 MB)"
_TRANSITORIOS = frozenset({429, 500, 502, 503, 504})
_RECUSAS_422 = frozenset(
    {"rede_desconhecida", "produto_desconhecido", "empresa_faturadora_desconhecida"}
)
_MIN = datetime.min.replace(tzinfo=UTC)

# Estado da thread, por slug (memória do processo; reinício = tudo devido).
_ultima_tabela: dict[str, datetime] = {}
_ultima_aprovacao: dict[str, datetime] = {}
_pausa_tabela: dict[str, datetime] = {}
_pausa_aprovacao: dict[str, datetime] = {}
_capacidades: dict[str, frozenset[str]] = {}
_ultimo_erro: dict[str, str] = {}
_estado_logado: dict[str, tuple] = {}
_avisos: set[tuple[str, str]] = set()


def habilitado() -> bool:
    return os.environ.get("PORTAL_FLOWPCP_PRECOS", "1").strip() != "0"


def _reset_estado() -> None:
    for d in (
        _ultima_tabela,
        _ultima_aprovacao,
        _pausa_tabela,
        _pausa_aprovacao,
        _capacidades,
        _ultimo_erro,
        _estado_logado,
    ):
        d.clear()
    _avisos.clear()


def _cliente(cfg: FlowPCPConfig) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def _avisar_uma_vez(slug: str, chave: str, texto: str) -> None:
    if (slug, chave) not in _avisos:
        _avisos.add((slug, chave))
        logger.warning(f"flowpcp.precos env={slug}: {texto}")


def _pausar(slug: str, agora: datetime, *, tudo: bool, motivo: str) -> None:
    ate = agora + PAUSA
    _pausa_aprovacao[slug] = ate
    if tudo:
        _pausa_tabela[slug] = ate
    _ultimo_erro[slug] = motivo
    logger.error(
        f"flowpcp.precos env={slug}: {motivo} — "
        f"{'ambiente' if tudo else 'aprovação de preço'} em pausa por "
        f"{int(PAUSA.total_seconds() // 60)} min"
    )


def _transitorio(slug: str, rota: str, exc: FlowPCPClientError) -> None:
    _ultimo_erro[slug] = f"{rota}: {exc.status_code or 'rede'} {exc.error or ''}".strip()
    logger.warning(f"flowpcp.precos env={slug} {rota}: falha passageira, próximo ciclo: {exc}")


def _eh_transitorio(exc: FlowPCPClientError) -> bool:
    """Rede, 429/5xx, e 2xx sem objeto JSON (proxy, manutenção: o Flow não recebeu)."""
    return (
        exc.status_code is None
        or exc.status_code in _TRANSITORIOS
        or exc.error == "resposta_sem_json"
    )


def _fora_do_contrato_2xx(exc: FlowPCPClientError) -> bool:
    """2xx com objeto JSON que não bate com o schema: o Flow aceitou o pedido.
    2xx sem objeto JSON é `resposta_sem_json` (transitório), nunca este caminho."""
    return exc.error == "resposta_fora_do_contrato" and 200 <= (exc.status_code or 0) < 300


def _falha_aprovacao(slug: str, rota: str, exc: FlowPCPClientError, agora: datetime) -> bool:
    """Falha nas rotas (b)/(c) que não é do conteúdo: 403 = ordem de ligar; o resto
    (400/401/404, tenant inativo...) = configuração ou bug. Pausa SÓ a aprovação:
    a tabela tem tratamento e pausa próprios. Transitório só espera o próximo
    ciclo. Devolve True se pausou."""
    if _eh_transitorio(exc):
        _transitorio(slug, rota, exc)
        return False
    motivo = f"{rota}: {exc.status_code} {exc.error or 'sem código'}"
    _pausar(slug, agora, tudo=False, motivo=motivo)
    return True


def rodar(agora: datetime) -> None:
    """Uma volta: todo ambiente ativo com a trava ≠ desligado. Um ambiente ruim
    não derruba os outros."""
    agora = agora.astimezone(UTC)  # naive = hora local do servidor
    for env in environments_repo.list_active():
        if not preco_trava.trava_ativa(env):
            continue
        try:
            _rodar_ambiente(env, agora)
        except Exception as exc:  # noqa: BLE001 — a thread não pode morrer
            _ultimo_erro[env["slug"]] = f"falha interna: {type(exc).__name__}"
            logger.error(f"flowpcp.precos env={env['slug']} falhou: {exc!r}")


def _rodar_ambiente(env: dict[str, Any], agora: datetime) -> None:
    slug = env["slug"]
    ligado = env.get("preco_trava_modo") == "ligado"
    tabela = (
        agora - _ultima_tabela.get(slug, _MIN) >= INTERVALO_TABELA
        and _pausa_tabela.get(slug, _MIN) <= agora
    )
    aprovacao = (
        ligado
        and agora - _ultima_aprovacao.get(slug, _MIN) >= INTERVALO_APROVACAO
        and _pausa_aprovacao.get(slug, _MIN) <= agora
    )
    if not (tabela or aprovacao):
        return
    empresa = preco_regra.normalizar_cnpj(env.get("cnpj"))
    if empresa is None:
        _avisar_uma_vez(slug, "sem_cnpj", "CNPJ da empresa não cadastrado: nada sincroniza")
        return
    fonte = env.get("preco_fonte_slug") or slug
    cfg = flowpcp_config_for_slug(fonte)
    if cfg is None or not (cfg.base_url and cfg.tenant_id and cfg.service_token):
        _avisar_uma_vez(
            slug, f"sem_flow:{fonte}", f"integração com o Flow de {fonte!r} desligada ou incompleta"
        )
        return
    client = _cliente(cfg)
    try:
        with router.env_connect(slug) as conn:
            if tabela:
                _ultima_tabela[slug] = agora
                _sincronizar_tabela(conn, client, slug, empresa, agora)
            if aprovacao and _pausa_aprovacao.get(slug, _MIN) <= agora:
                _ultima_aprovacao[slug] = agora
                _aprovacao(conn, client, slug, empresa, agora)
            _logar_estado(conn, slug)
    finally:
        fechar = getattr(client, "close", None)
        if callable(fechar):
            fechar()


# ─── (a) tabela ─────────────────────────────────────────────────────────────


def _sincronizar_tabela(conn, client, slug: str, empresa: str, agora: datetime) -> None:
    estado = precos_repo.estado_sync(conn)
    cursor = estado.cursor
    if estado.empresa_cnpj is not None and estado.empresa_cnpj != empresa:
        logger.warning(f"flowpcp.precos env={slug}: cópia de outra empresa faturadora, reiniciando")
        precos_repo.reiniciar(conn, empresa_cnpj=empresa)
        cursor = None
    reinicios = 0
    for _ in range(MAX_PAGINAS):
        try:
            pagina = client.get_precos_tabela(empresa, cursor, LIMITE_TABELA)
        except FlowPCPClientError as exc:
            if exc.status_code == 400 and exc.error == "cursor_invalido" and cursor is not None:
                reinicios += 1
                if reinicios > MAX_REINICIOS:
                    _pausar(slug, agora, tudo=True, motivo="tabela: cursor inválido em laço")
                    return
                logger.warning(f"flowpcp.precos env={slug}: cursor recusado pelo Flow, reiniciando")
                precos_repo.reiniciar(conn, empresa_cnpj=empresa)
                cursor = None
                continue
            if _eh_transitorio(exc):
                _transitorio(slug, "tabela", exc)
            else:
                _pausar(
                    slug, agora, tudo=True, motivo=f"tabela: {exc.status_code} {exc.error or ''}"
                )
            return
        if pagina.reiniciar:
            reinicios += 1
            if reinicios > MAX_REINICIOS:
                _pausar(slug, agora, tudo=True, motivo="tabela: reiniciar em laço")
                return
            logger.warning(f"flowpcp.precos env={slug}: o Flow pediu reiniciar a cópia")
            precos_repo.reiniciar(conn, empresa_cnpj=empresa)
            if cursor is not None:
                cursor = None
                continue
        if pagina.empresaFaturadoraCnpj != empresa:
            _pausar(slug, agora, tudo=True, motivo="tabela: página de outra empresa faturadora")
            return
        precos_repo.aplicar_pagina(
            conn, pagina.model_dump(mode="json", by_alias=True), empresa_cnpj=empresa
        )
        cursor = pagina.cursor
        if not pagina.temMais:
            _capacidades[slug] = frozenset(pagina.capacidades)
            _ultimo_erro.pop(slug, None)
            return
    logger.warning(f"flowpcp.precos env={slug}: tabela com mais de {MAX_PAGINAS} páginas; segue")


# ─── (b) e (c) ──────────────────────────────────────────────────────────────


def _aprovacao(conn, client, slug: str, empresa: str, agora: datetime) -> None:
    capacidades = _capacidades.get(slug)
    if capacidades is None:
        return  # a tabela ainda não completou neste processo
    if CAPACIDADE_APROVACAO not in capacidades:
        _pausar(slug, agora, tudo=False, motivo=f"o Flow não declara {CAPACIDADE_APROVACAO}")
        return
    if not _enviar_revisoes(conn, client, slug, agora):
        return
    if not _enviar_retiradas(conn, client, slug, empresa, agora):
        return
    _ler_decisoes(conn, client, slug, empresa, agora)


def _texto_recusa(exc: FlowPCPClientError) -> str:
    detalhes = exc.details or {}
    if exc.error == "produto_desconhecido":
        ids = detalhes.get("erpProdutoIds")
        lista = ", ".join(str(i) for i in ids) if isinstance(ids, list) and ids else "?"
        return f"produto sem cadastro no catálogo do Flow: {lista}"
    if exc.error == "rede_desconhecida":
        return "cliente sem rede de preço ativa no Flow"
    return "empresa faturadora não cadastrada no Flow"


def _revisao_vigente_flow(exc: FlowPCPClientError) -> int | None:
    v = (exc.details or {}).get("revisaoVigente")
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else None


def _enviar_revisoes(conn, client, slug: str, agora: datetime) -> bool:
    """Revisões `pendente_envio` (só a vigente de cada pedido, fora as bloqueadas
    localmente). Um pedido ruim não para os outros. Transitório encerra o envio
    desta volta (retiradas e decisões seguem). False = pausou: parar a volta."""
    fila = revs.pendentes_envio(conn, LOTE_ENVIO)
    reenvios: dict[str, int] = {}
    while fila:
        r = fila.pop(0)
        imp, n, corpo = r["import_id"], int(r["revisao"]), r["corpo"]
        try:
            precos_schema.validar_envio(corpo)
        except ValueError as exc:  # o portão valida antes de gravar: defesa
            logger.error(f"flowpcp.precos env={slug} {imp}/{n}: corpo fora do contrato: {exc}")
            revs.marcar_bloqueio_local(conn, imp, n, BLOQUEIO_CORPO_LOCAL, em=agora)
            continue
        if not revs.marcar_tentada(conn, imp, n, em=agora):
            continue
        try:
            resposta = client.post_precos_aprovacao(corpo)
        except FlowPCPClientError as exc:
            acao = _erro_do_pedido(conn, slug, imp, n, exc, agora)
            if acao == "parar":
                return False
            if acao == "transitorio":
                return True
            if acao == "reenviar" and reenvios.get(imp, 0) < MAX_REENVIOS:
                reenvios[imp] = reenvios.get(imp, 0) + 1
                nova = revs.vigente(conn, imp)
                if nova is not None and nova["situacao"] == "pendente_envio":
                    fila.insert(0, nova)
            continue
        revs.marcar_enviada(conn, imp, n, resposta.model_dump(mode="json"), em=agora)
    return True


def _erro_do_pedido(
    conn, slug: str, imp: str, n: int, exc: FlowPCPClientError, agora: datetime
) -> str:
    """Devolve "parar" | "transitorio" | "seguir" | "reenviar" (decisão 7 do adendo)."""
    if _fora_do_contrato_2xx(exc):
        revs.marcar_enviada(conn, imp, n, {"bruto": exc.body}, em=agora)
        _avisar_uma_vez(slug, "resposta_b", "resposta 2xx da aprovação fora do contrato; aceita")
        return "seguir"
    if exc.status_code == 409 and exc.error in ("revisao_obsoleta", "conteudo_divergente"):
        atual = revs.vigente(conn, imp)
        if atual is None or int(atual["revisao"]) != n:
            logger.info(
                f"flowpcp.precos env={slug} {imp}/{n}: {exc.error}, mas a revisão já foi "
                "substituída aqui; não reenvia"
            )
            return "seguir"
        piso = n if exc.error == "conteudo_divergente" else _revisao_vigente_flow(exc)
        if piso is None:
            _pausar(slug, agora, tudo=False, motivo="aprovação: 409 sem revisaoVigente")
            return "parar"
        nova = revs.subir_piso(conn, imp, piso, agora=agora)
        logger.info(f"flowpcp.precos env={slug} {imp}: {exc.error}, reenvia como revisão {nova}")
        return "reenviar"
    if exc.status_code == 413 or (exc.status_code == 400 and exc.error == "payload_invalido"):
        texto = BLOQUEIO_GRANDE if exc.status_code == 413 else BLOQUEIO_CORPO_FLOW
        revs.marcar_bloqueio_local(conn, imp, n, texto, em=agora)
        logger.warning(f"flowpcp.precos env={slug} {imp}/{n} bloqueada aqui: {texto}")
        return "seguir"
    if exc.status_code == 422 and exc.error in _RECUSAS_422:
        texto = _texto_recusa(exc)
        revs.marcar_recusa_flow(conn, imp, n, texto, em=agora)
        logger.warning(f"flowpcp.precos env={slug} {imp}/{n} recusada pelo Flow: {texto}")
        return "seguir"
    return "parar" if _falha_aprovacao(slug, "aprovação", exc, agora) else "transitorio"


def _motivo_retirada(texto: str | None) -> str:
    motivo = (texto or "").strip() or MOTIVO_RETIRADA_PADRAO
    while len(motivo.encode("utf-16-le")) // 2 > 500:
        motivo = motivo[:-1].rstrip()
    return motivo


def _enviar_retiradas(conn, client, slug: str, empresa: str, agora: datetime) -> bool:
    """Retiradas pendentes. 409 `revisao_obsoleta` → reenvia com a `revisaoVigente`
    do Flow (nunca pausa por isso); 200 com `aprovacaoId` nulo = feito. Retirada
    sem saída (inválida, 409 sem revisão nova) loga uma vez e fica para depois.
    Transitório encerra as retiradas desta volta. False = pausou."""
    for r in revs.retiradas_pendentes(conn, LOTE_ENVIO):
        imp, n = r["import_id"], int(r["revisao"])
        base = {
            "schema": "precos.aprovacao.v1",
            "tipo": "retirada",
            "externalId": imp,
            "empresaFaturadoraCnpj": (r["corpo"] or {}).get("empresaFaturadoraCnpj") or empresa,
            "motivo": _motivo_retirada(r.get("retirada_motivo")),
        }
        alvo = n
        for _ in range(2):
            corpo = {**base, "revisao": alvo}
            try:
                precos_schema.validar_envio(corpo)
            except ValueError as exc:
                _avisar_uma_vez(
                    slug,
                    f"retirada_invalida:{imp}:{alvo}",
                    f"retirada {imp}/{alvo} inválida: {exc}",
                )
                break
            try:
                client.post_precos_aprovacao(corpo)
            except FlowPCPClientError as exc:
                vigente_flow = _revisao_vigente_flow(exc)
                if exc.status_code == 409 and exc.error == "revisao_obsoleta":
                    if vigente_flow is not None and vigente_flow != alvo:
                        alvo = vigente_flow
                        continue
                    _avisar_uma_vez(
                        slug,
                        f"retirada_409:{imp}:{alvo}",
                        f"retirada {imp}/{alvo}: 409 sem revisão nova; tenta no próximo ciclo",
                    )
                    break
                if _fora_do_contrato_2xx(exc):
                    _avisar_uma_vez(
                        slug, "resposta_b", "resposta 2xx da aprovação fora do contrato; aceita"
                    )
                    revs.marcar_retirada_enviada(conn, imp, n, em=agora)
                    break
                return not _falha_aprovacao(slug, "retirada", exc, agora)
            revs.marcar_retirada_enviada(conn, imp, n, em=agora)
            break
    return True


def _ler_decisoes(conn, client, slug: str, empresa: str, agora: datetime) -> None:
    """Lê sem cursor, grava, confirma e repete até vir vazia (decisão 13).
    Decisão que volta depois de confirmada aqui não prende a volta."""
    vistas: set[str] = set()
    for _ in range(MAX_VOLTAS_DECISOES):
        try:
            pagina = client.get_precos_decisoes(empresa, None, LIMITE_DECISOES)
        except FlowPCPClientError as exc:
            _falha_aprovacao(slug, "decisões", exc, agora)
            return
        novas = [d for d in pagina.decisoes if d.decisaoId.lower() not in vistas]
        if not novas:
            return
        confirmacoes = []
        for d in novas:
            vistas.add(d.decisaoId.lower())
            if d.empresaFaturadoraCnpj != empresa:
                logger.warning(
                    f"flowpcp.precos env={slug}: decisão {d.decisaoId} de outra empresa "
                    "faturadora; não grava, confirma descartada"
                )
                confirmacoes.append({"decisaoId": d.decisaoId, "resultado": "descartada"})
                continue
            resultado = revs.gravar_decisao(conn, d.model_dump(mode="json"), agora=agora)
            confirmacoes.append({"decisaoId": d.decisaoId, "resultado": resultado})
        if not confirmacoes:
            return
        corpo = {
            "schema": "precos.decisoes.confirmacao.v1",
            "empresaFaturadoraCnpj": empresa,
            "confirmacoes": confirmacoes,
        }
        try:
            precos_schema.ConfirmacaoDecisoesV1.model_validate(corpo)
        except ValueError as exc:
            logger.error(f"flowpcp.precos env={slug}: confirmação fora do contrato: {exc}")
            return
        try:
            resposta = client.post_precos_confirmar(corpo)
        except FlowPCPClientError as exc:
            if _fora_do_contrato_2xx(exc):
                # O Flow confirmou; se não, a decisão volta na leitura e é reconfirmada.
                _avisar_uma_vez(slug, "resposta_c", "resposta 2xx do confirmar fora do contrato")
                revs.marcar_confirmada(conn, [c["decisaoId"] for c in confirmacoes], em=agora)
                continue
            _falha_aprovacao(slug, "confirmação", exc, agora)
            return
        situacoes = {x.decisaoId.lower(): x.situacao for x in resposta.resultados}
        feitas = []
        for c in confirmacoes:
            situacao = situacoes.get(c["decisaoId"].lower())
            if situacao is None:
                logger.warning(f"flowpcp.precos env={slug}: confirmar sem {c['decisaoId']}")
                continue
            if situacao in ("conflito", "nao_encontrada"):
                logger.warning(
                    f"flowpcp.precos env={slug}: decisão {c['decisaoId']} {situacao} no "
                    f"confirmar ({c['resultado']}); não reenvia"
                )
            feitas.append(c["decisaoId"])
        revs.marcar_confirmada(conn, feitas, em=agora)


# ─── estado ─────────────────────────────────────────────────────────────────


def _logar_estado(conn, slug: str) -> None:
    completo = precos_repo.estado_sync(conn).completo_em
    conta = revs.contagem_por_situacao(conn)
    estado = (
        completo.isoformat() if completo else None,
        _ultimo_erro.get(slug),
        conta.get("pendente_envio", 0),
        conta.get("recusada_flow", 0),
        conta.get("bloqueada_local", 0),
    )
    if _estado_logado.get(slug) == estado:
        return
    _estado_logado[slug] = estado
    logger.info(
        f"flowpcp.precos estado env={slug} gerado_em={estado[0]} ultimo_erro={estado[1]} "
        f"pendente_envio={estado[2]} recusada_flow={estado[3]} bloqueada_local={estado[4]}"
    )
