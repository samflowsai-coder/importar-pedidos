"""Volta Flow -> Fire dos cadastros aprovados, em modo `simular` (Etapa 1).

Regra da Etapa 1: este módulo só LÊ (a fila do Flow, a cópia local e o Fire por
conexão somente leitura); nunca grava no Fire nem confirma no Flow, e os modos de
escrita (`criar`, `criar_e_atualizar`) se recusam a iniciar até a Etapa 3.

Cada item da fila é validado à parte (`cadastro_schema.validar_item`), conferido
contra a cópia local e contra o Fire (só SELECT, sempre parametrizado) e vira uma
linha no livro `cadastro_volta`: `simulado` com o plano do que seria gravado, ou
`erro_definitivo` com código e mensagem sem o valor recebido. O par
`(cadastro_id, versao)` é o lease: o mesmo item não é processado duas vezes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta

from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp import capacidades
from app.integrations.flowpcp.cadastro_schema import CadastroPendente, validar_item
from app.integrations.flowpcp.catalogo_sync import _build_client
from app.integrations.flowpcp.config import enabled_flowpcp_envs, flowpcp_config_for_slug
from app.observability import metrics
from app.persistence import cadastro_volta_repo, environments_repo, materiais_fire_repo, router
from app.utils.logger import logger

MODOS = ("desligado", "simular", "criar", "criar_e_atualizar")
VAR_MODO = "PORTAL_FLOWPCP_CADASTRO_VOLTA"
INTERVALO = timedelta(minutes=5)
LIMITE_FILA = 20
_RECUSA_ESCRITA = "gravação exige P0 e cadastros.v1 (Etapa 3)"

_avisados: set[str] = set()
_ultima_volta: datetime | None = None

# wire -> coluna do Fire (PRODUTOS)
COLUNAS = {
    "nome": "DESCRICAO",
    "cor": "DESCRICAO2",
    "unidadeErp": "UNIDADE",
    "ean": "CODIGO_EAN13",
    "ncm": "CLASSIF_FISCAL",
    "tipoItemSped": "CODITEMSPED",
    "codigoNfe": "CODPARANFE",
    "codigoMes": "CODPROD_ALTERN",
}

_SQL_NOME = "SELECT FIRST 1 SEQ FROM PRODUTOS WHERE UPPER(TRIM(DESCRICAO)) = ?"
_SQL_SONDA = "SELECT 1 FROM RDB$DATABASE"
_SQL_COMPONENTE = "SELECT SEQ, BLOQUEADO FROM PRODUTOS WHERE SEQ = ?"


def _avisa_uma_vez(chave: str, msg: str) -> None:
    if chave in _avisados:
        return
    _avisados.add(chave)
    logger.error(msg)


def modo(slug: str) -> str:
    """Modo do ambiente em `PORTAL_FLOWPCP_CADASTRO_VOLTA` (`slug:modo,slug:modo`).
    Ausente ou inválido = `desligado` (o inválido loga ERROR uma vez por valor)."""
    bruto = os.environ.get(VAR_MODO, "")
    achado = "desligado"
    for parte in bruto.split(","):
        entrada = parte.strip()
        if not entrada:
            continue
        nome, sep, valor = entrada.partition(":")
        nome, valor = nome.strip().lower(), valor.strip().lower()
        if not sep or not nome or valor not in MODOS:
            _avisa_uma_vez(
                entrada.lower(),
                f"{VAR_MODO}: entrada inválida {entrada[:40]!r} (esperado slug:modo com modo em "
                f"{', '.join(MODOS)}); tratada como desligado",
            )
            continue
        if nome == slug.lower():
            achado = valor
    return achado


def _select(conn, sql: str, params: tuple):
    """Único caminho de leitura no Fire: só SELECT, sempre com parâmetros."""
    if not sql.lstrip().upper().startswith("SELECT"):
        raise RuntimeError("cadastro_volta: só SELECT é permitido no Fire")
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        close = getattr(cur, "close", None)
        if close:
            close()


def _no_escopo(env_conn, item: CadastroPendente) -> bool:
    seq = item.erpProdutoId
    if item.tipo == "material":
        return seq in materiais_fire_repo.seqs(env_conn)
    row = env_conn.execute(
        "SELECT 1 FROM catalogo_fire WHERE fire_produto_id = ?", (seq,)
    ).fetchone()
    return row is not None


def _contra_o_fire(fire_conn, item: CadastroPendente) -> tuple[str, str] | None:
    if item.acao == "criar":
        achou = _select(fire_conn, _SQL_NOME, (item.nome.strip().upper(),))
        if achou:
            return "nome_existe", f"já existe produto com esse nome no Fire (SEQ {achou[0][0]})"
    for i, comp in enumerate(item.componentes or []):
        linhas = _select(fire_conn, _SQL_COMPONENTE, (int(comp.erpProdutoId),))
        if not linhas:
            return "componente_invalido", f"componentes[{i}] não existe no Fire"
        if str(linhas[0][1] or "").strip().lower() == "sim":
            return "componente_invalido", f"componentes[{i}] está bloqueado no Fire"
    return None


def _plano(item: CadastroPendente) -> dict:
    campos = {
        coluna: getattr(item, wire)
        for wire, coluna in COLUNAS.items()
        if getattr(item, wire) is not None
    }
    campos["GRUPO"] = item.grupoErp.codigo
    return {
        "acao": item.acao,
        "tabela": "PRODUTOS",
        "tipo": item.tipo,
        "seq": item.erpProdutoId,
        "campos": {} if item.acao == "inativar" else campos,
        "componentes": [
            {"erpProdutoId": c.erpProdutoId, "quantidade": c.quantidade}
            for c in (item.componentes or [])
        ],
    }


_ID_SEGURO = re.compile(r"^[0-9A-Fa-f-]{1,64}$")


def _id_do_bruto(bruto: object) -> str:
    """Id para o livro de um item que não validou: só hex e hífen; o resto vira hash."""
    cru = bruto.get("cadastroId") if isinstance(bruto, dict) else None
    if isinstance(cru, str) and _ID_SEGURO.fullmatch(cru):
        return cru
    base = cru if cru is not None else bruto
    digest = hashlib.sha256(repr(base).encode("utf-8", "replace")).hexdigest()[:16]
    return f"sem-id-{digest}"


def _versao_do_bruto(bruto: object) -> int:
    v = bruto.get("versao") if isinstance(bruto, dict) else None
    return v if type(v) is int and v >= 1 else 0


def _idade_mais_antiga(brutos: list) -> float:
    agora = datetime.now().astimezone()
    maior = 0.0
    for b in brutos:
        txt = b.get("aprovadoEm") if isinstance(b, dict) else None
        if not isinstance(txt, str):
            continue
        try:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.astimezone()
        maior = max(maior, (agora - dt).total_seconds())
    return maior


def _decidir(bruto, env_conn, fire_conn):
    """-> (estado, item|None, codigo, mensagem, plano|None)."""
    item, codigo, msg = validar_item(bruto)
    if item is None:
        return "erro_definitivo", None, codigo, msg, None
    if item.acao != "criar" and not _no_escopo(env_conn, item):
        origem = "dos materiais" if item.tipo == "material" else "do catálogo"
        return (
            "erro_definitivo",
            item,
            "seq_fora_do_escopo",
            f"erpProdutoId não está na cópia local {origem}",
            None,
        )
    try:
        falha = _contra_o_fire(fire_conn, item)
    except Exception as exc:  # noqa: BLE001
        # Conexão perdida aborta o lote; erro só desta consulta rejeita só este item.
        try:
            _select(fire_conn, _SQL_SONDA, ())
        except Exception:  # noqa: BLE001
            raise exc from None
        return (
            "erro_definitivo",
            item,
            "consulta_fire_falhou",
            f"consulta ao Fire falhou ({type(exc).__name__})",
            None,
        )
    if falha:
        return "erro_definitivo", item, falha[0], falha[1][:500], None
    return "simulado", item, None, None, _plano(item)


def processar_ambiente(slug: str, *, _client=None, _fire_conn=None, _env_conn=None) -> dict:
    m = modo(slug)
    if m == "desligado":
        return {"modo": m}
    if m != "simular":
        logger.error(f"flowpcp.cadastro_volta env={slug}: modo {m!r} recusado: {_RECUSA_ESCRITA}")
        return {"modo": m, "recusado": _RECUSA_ESCRITA}

    cfg = None
    if _client is None:
        cfg = flowpcp_config_for_slug(slug)
        if cfg is None:
            return {"modo": m, "ignorado": "FlowPCP não habilitado"}
    client = _client or _build_client(cfg)
    try:
        try:
            resp = client.list_cadastros_pendentes(limite=LIMITE_FILA)
        except Exception as exc:  # noqa: BLE001 — Flow fora não derruba o ciclo
            logger.error(
                f"flowpcp.cadastro_volta env={slug}: leitura da fila falhou: {type(exc).__name__}"
            )
            return {"modo": m, "erro_transitorio": f"fila do Flow: {type(exc).__name__}"}
        brutos = list(resp.cadastros)
        resumo = {
            "modo": m,
            "lidos": len(brutos),
            "capacidade_ok": capacidades.CADASTROS in capacidades.de(resp),
            "simulados": 0,
            "erros": 0,
            "ja_no_livro": 0,
        }
        metrics.cadastro_volta_pendentes.labels(env=slug).set(len(brutos))
        metrics.cadastro_volta_mais_antigo.labels(env=slug).set(_idade_mais_antiga(brutos))
        if not brutos:
            return resumo
        try:
            return _processar_fila(slug, brutos, resumo, _fire_conn, _env_conn)
        except Exception as exc:  # noqa: BLE001 — Fire fora: nada registrado, tenta no próximo ciclo
            logger.error(
                f"flowpcp.cadastro_volta env={slug}: ciclo interrompido: {type(exc).__name__}"
            )
            resumo["erro_transitorio"] = type(exc).__name__
            return resumo
    finally:
        if _client is None:
            client.close()


def _processar_fila(slug, brutos, resumo, _fire_conn, _env_conn) -> dict:
    with ExitStack() as stack:
        if _fire_conn is not None:
            fire_conn = _fire_conn
        else:
            env = environments_repo.get_by_slug(slug)
            fire_conn = stack.enter_context(
                FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env))
            )
        env_conn = stack.enter_context(
            nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
        )
        # Fase 1: decide tudo (lê o Fire); se o Fire cair, nada foi para o livro.
        decisoes = []
        for bruto in brutos:
            cadastro_id, versao = _id_do_bruto(bruto), _versao_do_bruto(bruto)
            item, *_ = validar_item(bruto)
            if item is not None:
                cadastro_id, versao = str(item.cadastroId), item.versao
            if cadastro_volta_repo.existe(env_conn, cadastro_id, versao):
                decisoes.append((cadastro_id, versao, None))
                continue
            decisoes.append((cadastro_id, versao, _decidir(bruto, env_conn, fire_conn)))
        # Fase 2: registra.
        for cadastro_id, versao, dec in decisoes:
            if dec is None:
                resumo["ja_no_livro"] += 1
                metrics.cadastro_volta_total.labels(env=slug, resultado="ja_no_livro").inc()
                continue
            estado, item, codigo, msg, plano = dec
            novo = cadastro_volta_repo.registrar(
                env_conn,
                cadastro_id=cadastro_id,
                versao=versao,
                estado=estado,
                acao=item.acao if item else None,
                tipo=item.tipo if item else None,
                erro_codigo=codigo,
                erro_mensagem=msg,
                plano_json=json.dumps(plano, ensure_ascii=False) if plano else None,
            )
            if not novo:
                resumo["ja_no_livro"] += 1
                continue
            resumo["simulados" if estado == "simulado" else "erros"] += 1
            metrics.cadastro_volta_total.labels(env=slug, resultado=estado).inc()
            if estado == "erro_definitivo":
                logger.warning(
                    f"flowpcp.cadastro_volta env={slug}: {cadastro_id} v{versao} rejeitado: {codigo}"
                )
        commit = getattr(env_conn, "commit", None)
        if commit:
            commit()
    return resumo


def rodar() -> dict[str, dict]:
    """Todos os ambientes com a volta ligada. Nunca levanta."""
    resultado: dict[str, dict] = {}
    try:
        ambientes = list(enabled_flowpcp_envs())
    except Exception as exc:  # noqa: BLE001
        logger.error(f"flowpcp.cadastro_volta: listar ambientes falhou: {type(exc).__name__}")
        return resultado
    for slug in ambientes:
        if modo(slug) == "desligado":
            continue
        try:
            resultado[slug] = processar_ambiente(slug)
        except Exception as exc:  # noqa: BLE001 — um ambiente não derruba os outros
            logger.error(f"flowpcp.cadastro_volta env={slug}: falhou: {exc!r}")
            resultado[slug] = {"erro": type(exc).__name__}
    return resultado


def rodar_se_devido(agora: datetime) -> dict[str, dict] | None:
    """`rodar()` no máximo a cada `INTERVALO`; fora disso devolve None."""
    global _ultima_volta
    if _ultima_volta is not None and agora - _ultima_volta < INTERVALO:
        return None
    _ultima_volta = agora
    return rodar()
