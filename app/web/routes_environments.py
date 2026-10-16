"""Admin CRUD de ambientes — `/api/admin/environments/*`.

Todas as rotas exigem usuário admin (`require_admin`). Não exigem
ambiente selecionado: é exatamente daqui que o admin cria/edita os
ambientes que outros usuários poderão selecionar.

Senha do Firebird:
- Cifrada via `secret_store` ao gravar (POST/PATCH)
- Nunca retornada (GET retorna `public_view` sem `fb_password_enc`)
- PATCH: `fb_password=None` mantém atual, `""` limpa, valor substitui

Endpoint `POST /{id}/test` valida pastas + tenta conexão Firebird.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.erp_eventos.canonico import formatar_instante
from app.erp_eventos.foto import dia_local
from app.persistence import environments_repo, erp_bases_repo, erp_estado_repo, erp_foto_repo
from app.persistence import router as db_router
from app.web.auth import require_admin

router = APIRouter(prefix="/api/admin/environments", tags=["admin", "environments"])


class CreateEnvRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=31)
    name: str = Field(..., min_length=1)
    watch_dir: str = Field(..., min_length=1)
    output_dir: str = Field(..., min_length=1)
    fb_path: str = Field(..., min_length=1)
    fb_host: str | None = None
    fb_port: str | None = None
    fb_user: str = "SYSDBA"
    fb_charset: str = "WIN1252"
    fb_password: str | None = None
    # CNPJ da empresa do ambiente — chave do roteamento pelo documento.
    cnpj: str | None = None


class UpdateEnvRequest(BaseModel):
    """slug propositalmente ausente — imutável após criação."""

    name: str | None = None
    watch_dir: str | None = None
    output_dir: str | None = None
    fb_path: str | None = None
    fb_host: str | None = None
    fb_port: str | None = None
    fb_user: str | None = None
    fb_charset: str | None = None
    # None = mantém senha atual; "" = limpa; valor = substitui
    fb_password: str | None = None
    # None = mantém CNPJ atual; "" = limpa; valor = substitui (sempre em dígitos)
    cnpj: str | None = None
    # Figura fiscal (CODFIGFISCAL). None = mantém o valor atual (sem forma de
    # limpar por aqui hoje — mesma limitação de app/persistence/environments_repo.update).
    fiscal_codfigfiscal: int | None = None


class FlowPCPConfigRequest(BaseModel):
    """Config da ponte FlowPCP por ambiente. Token cifrado via secret_store."""

    enabled: bool = False
    base_url: str | None = None
    tenant_id: str | None = None
    timezone: str = "America/Sao_Paulo"
    dry_run: bool = False
    poll_interval_s: int = Field(default=30, ge=5)
    request_timeout_s: float = Field(default=30.0, gt=0)
    # Gate do envio de catálogo ao Flow (OFF = sync só atualiza a cópia local)
    catalogo_push: bool = False
    # Filtro da extração: OFF = todo PRODUTOS (hoje); ON = só subgrupo MEIAS
    # (depende da marcação no Fire — Parte 2 do rollout)
    catalogo_apenas_meias: bool = False
    # Gate do envio de clientes ao Flow (OFF = sync só atualiza a cópia local)
    clientes_push: bool = False
    # None = mantém token atual; "" = limpa; valor = substitui
    service_token: str | None = None


class IntercompanyConfigRequest(BaseModel):
    """De-para de cliente intercompany. Vazio em qualquer campo = desligado."""

    cnpj: str | None = None
    revenda_slug: str | None = None


def _cnpj_taken_message(cnpj: str | None) -> str:
    """Mensagem 409 útil: nomeia o ambiente que já usa o CNPJ.

    `find_by_cnpj` basta: o índice UNIQUE é escopado a `is_active=1` (mesmo
    WHERE de `find_by_cnpj`), então quem colide é sempre um ambiente ativo —
    um desativado nunca dispara `CnpjTaken`, porque soltou o CNPJ ao sair do
    roteamento.
    """
    outro = environments_repo.find_by_cnpj(cnpj)
    if outro:
        return f"CNPJ já usado pelo ambiente '{outro['name']}' ({outro['slug']})."
    return "CNPJ já usado por outro ambiente."


@router.get("")
def list_environments(_=Depends(require_admin)):
    return environments_repo.list_all()


@router.post("", status_code=201)
def create_environment(payload: CreateEnvRequest, _=Depends(require_admin)):
    try:
        return environments_repo.create(**payload.model_dump())
    except environments_repo.SlugTaken:
        raise HTTPException(409, "Slug já existe — escolha outro.") from None
    except environments_repo.CnpjTaken as exc:
        raise HTTPException(409, _cnpj_taken_message(str(exc))) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/{env_id}")
def get_environment(env_id: str, _=Depends(require_admin)):
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    return env


@router.patch("/{env_id}")
def update_environment(env_id: str, payload: UpdateEnvRequest, _=Depends(require_admin)):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    try:
        return environments_repo.update(env_id, **payload.model_dump())
    except environments_repo.CnpjTaken as exc:
        raise HTTPException(409, _cnpj_taken_message(str(exc))) from None


@router.put("/{env_id}/flowpcp")
def set_environment_flowpcp(env_id: str, payload: FlowPCPConfigRequest, _=Depends(require_admin)):
    """Grava a config FlowPCP do ambiente (token cifrado). `service_token`:
    omitir/None mantém o atual, "" limpa, valor substitui."""
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    return environments_repo.set_flowpcp_config(env_id, **payload.model_dump())


@router.put("/{env_id}/intercompany")
def set_environment_intercompany(
    env_id: str, payload: IntercompanyConfigRequest, _=Depends(require_admin)
):
    """Grava o de-para de cliente intercompany do ambiente.

    `revenda_slug` vem do `<select>` na tela (populado com os ambientes reais),
    mas normaliza minúsculas aqui como defesa mínima — slugs de ambiente são
    sempre lowercase (`environments_repo.SLUG_RE`)."""
    if environments_repo.get(env_id) is None:
        raise HTTPException(status_code=404, detail="Ambiente não encontrado")
    data = payload.model_dump()
    if data["revenda_slug"]:
        data["revenda_slug"] = data["revenda_slug"].strip().lower()
    return environments_repo.set_intercompany_config(env_id, **data)


class ErpBaseRequest(BaseModel):
    """SP1: esta empresa como uma base do ERP para o Flow (rotas de eventos e foto).

    `token`: credencial `fpe_…` gerada no Flow para (tenant, base). None
    mantém a atual, "" limpa, valor substitui. Nunca volta no GET."""

    base: str = Field(..., min_length=1, max_length=32)
    papel: str
    tenant_id: str
    destino_url: str
    foto_hora: int | None = Field(default=None, ge=0, le=23)
    ativo: bool = False
    token: str | None = None


def _erp_base_view(env_id: str) -> dict:
    cfg = erp_bases_repo.obter(env_id)
    view = {"config": erp_bases_repo.public_view(cfg), "envio": None, "fila": {}, "foto_hoje": None}
    if cfg is None:
        return view
    with db_router.env_connect(cfg.env_slug) as conn:
        envio = erp_estado_repo.obter_envio(conn, cfg.base)
        view["fila"] = erp_estado_repo.contar(conn, cfg.base)
        foto = erp_foto_repo.foto_do_dia(conn, cfg.base, dia_local(datetime.now(UTC)))
    view["envio"] = {
        "bloqueio": envio.bloqueio,
        "ultimo_http": envio.ultimo_http,
        "ultimo_erro": envio.ultimo_erro,
        "ultimo_202_em": envio.ultimo_202_em,
        "ultima_vigia_em": envio.ultima_vigia_em,
        "proxima_tentativa_em": envio.proxima_tentativa_em,
    }
    view["foto_hoje"] = (
        {"situacao": foto.situacao, "paginas": foto.total_paginas, "motivo": foto.motivo}
        if foto
        else None
    )
    return view


@router.get("/{env_id}/erp-base")
def get_environment_erp_base(env_id: str, _=Depends(require_admin)):
    if environments_repo.get(env_id) is None:
        raise HTTPException(status_code=404, detail="Ambiente não encontrado")
    return _erp_base_view(env_id)


@router.put("/{env_id}/erp-base")
def set_environment_erp_base(env_id: str, payload: ErpBaseRequest, _=Depends(require_admin)):
    if environments_repo.get(env_id) is None:
        raise HTTPException(status_code=404, detail="Ambiente não encontrado")
    try:
        erp_bases_repo.salvar(env_id, **payload.model_dump())
    except erp_bases_repo.ConfigErpInvalidaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cfg = erp_bases_repo.obter(env_id)
    # Credencial nova resolve o 401: tira o bloqueio de credencial.
    if payload.token and cfg is not None:
        with db_router.env_connect(cfg.env_slug) as conn:
            if erp_estado_repo.obter_envio(conn, cfg.base).bloqueio == "credencial":
                erp_estado_repo.desbloquear(conn, cfg.base, formatar_instante(datetime.now(UTC)))
    return _erp_base_view(env_id)


@router.post("/{env_id}/erp-base/desbloquear")
def unblock_environment_erp_base(env_id: str, _=Depends(require_admin)):
    """Retoma o envio de uma base bloqueada (400/4xx/lote grande) depois que a
    causa foi corrigida. O próximo lote repete os mesmos eventos."""
    cfg = erp_bases_repo.obter(env_id)
    if cfg is None:
        raise HTTPException(status_code=404, detail="Base do ERP não configurada")
    with db_router.env_connect(cfg.env_slug) as conn:
        erp_estado_repo.desbloquear(conn, cfg.base, formatar_instante(datetime.now(UTC)))
    return _erp_base_view(env_id)


_SEQ = re.compile(r"[0-9]{1,10}")


def _seqs_fora_do_envio(valores: list[str] | None) -> frozenset[str]:
    """`excluir` pode vir repetido e com vírgulas; só dígitos ASCII (até 10).
    Normaliza o zero à esquerda: o fire_produto_id é str(SEQ) inteiro."""
    seqs: set[str] = set()
    for valor in valores or []:
        for parte in valor.split(","):
            parte = parte.strip()
            if not _SEQ.fullmatch(parte):
                raise HTTPException(422, f"excluir: '{parte}' não é um código do Fire (SEQ)")
            seqs.add(str(int(parte)))
    return frozenset(seqs)


@router.post("/{env_id}/flowpcp/sync-catalogo")
def sync_catalogo_flowpcp(
    env_id: str,
    apply: bool = False,
    excluir: list[str] | None = Query(default=None, max_length=4000),
    confirmar_remocao: bool = False,
    confirmar_substituicao: bool = False,
    _=Depends(require_admin),
):
    """Full-load do catálogo (produtos Fire → FlowPCP), direção IDA.

    Lê TODOS os produtos (`PRODUTOS`) do Fire do ambiente e empurra pro Flow.
    - `apply=false` (default): **dry-run** — reconcilia e devolve o relatório,
      NÃO escreve no catálogo do Flow. Seguro pra rodar quando quiser.
    - `apply=true`: **promove** — o Flow linka + grava de verdade (Fase 1). Exige
      o `/catalogo` do Flow com o promote no ar (senão devolve 422 → 502 aqui).
    - `excluir=3174,3381`: SEQs que não vão ao Flow nesta rodada (cadastro com
      problema no Fire); a cópia local continua com tudo.
    - confirmar_remocao=1 / confirmar_substituicao=1: liberam as travas de composição
      do Flow; só com apply=true (o diário nunca manda).
    Blocking (Firebird + HTTP) → FastAPI roda esta rota `def` no threadpool.
    """
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    if not env.get("flowpcp_enabled"):
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    import httpx

    from app.integrations.flowpcp.catalogo_composicao import ExcluirGuardadoInvalidoError
    from app.integrations.flowpcp.catalogo_sync import (
        CatalogoLocalResult,
        ConfirmacaoSemApplyError,
        ForaDoEnvioInexistenteError,
        run_catalogo_sync,
    )
    from app.integrations.flowpcp.client import (
        FlowPCPApplyEmAndamentoError,
        FlowPCPClientError,
    )

    if (confirmar_remocao or confirmar_substituicao) and not apply:
        raise HTTPException(422, "Liberar trava só vale junto com apply=true (gravar)")
    fora = _seqs_fora_do_envio(excluir)
    try:
        rep = run_catalogo_sync(
            env["slug"],
            dry_run=not apply,
            full_sync=True,
            fora_do_envio=fora,
            confirmar_remocao=confirmar_remocao,
            confirmar_substituicao=confirmar_substituicao,
        )
    except ForaDoEnvioInexistenteError as exc:
        detalhe = f"Nada foi enviado ao Flow: {exc}"
        if exc.guardados:
            detalhe += (
                f". Os SEQs {', '.join(exc.guardados)} vêm da exclusão guardada "
                f"({exc.variavel}): corrija a variável"
            )
        raise HTTPException(422, detalhe) from exc
    except FlowPCPApplyEmAndamentoError as exc:
        raise HTTPException(
            409,
            "O Flow já está gravando um catálogo deste ambiente. Espere terminar e simule de novo.",
        ) from exc
    except (ExcluirGuardadoInvalidoError, ConfirmacaoSemApplyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except httpx.TimeoutException as exc:
        if isinstance(exc, httpx.ConnectTimeout | httpx.PoolTimeout):
            msg = "Não consegui conectar ao Flow; nada foi gravado. Tente de novo."
        elif apply:
            msg = (
                "O Flow não respondeu a tempo. A gravação pode ainda estar em andamento no Flow: "
                "espere alguns minutos e simule de novo antes de gravar outra vez."
            )
        else:
            msg = "O Flow não respondeu a tempo. Tente simular de novo."
        raise HTTPException(504, msg) from exc
    except Exception as exc:  # noqa: BLE001 — vira erro HTTP legível pro operador
        status = getattr(exc, "status_code", None)
        if (
            apply
            and isinstance(exc, FlowPCPClientError)
            and isinstance(status, int)
            and status >= 500
        ):
            raise HTTPException(
                502,
                f"O Flow respondeu com erro ao gravar ({status}). Ele pode ter gravado parte: "
                "simule antes de gravar de novo.",
            ) from exc
        raise HTTPException(502, f"Falha no sync de catálogo: {exc}") from exc
    if rep is None:
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")
    if isinstance(rep, CatalogoLocalResult):
        # Gate OFF: catálogo atualizado só no importador — nada foi ao Flow.
        return {"local_only": True, "itens": rep.itens, "extraido_em": rep.extraido_em}
    corpo = rep.model_dump()
    if fora:
        corpo["fora_do_envio"] = sorted(fora, key=int)
    return corpo


@router.post("/{env_id}/flowpcp/sync-clientes")
def sync_clientes_flowpcp(env_id: str, apply: bool = False, _=Depends(require_admin)):
    """Carga de clientes ativos (Fire → FlowPCP), direção IDA.

    Lê os clientes com pedido nos últimos 12 meses do Fire do ambiente.
    - `apply=false` (default): dry-run — reconcilia/relatório, não grava no Flow.
    - `apply=true`: promove (exige o `/clientes` do Flow no ar).
    A cópia local (`clientes_fire`) é sempre atualizada. Blocking → threadpool.
    """
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    if not env.get("flowpcp_enabled"):
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    from app.integrations.flowpcp.clientes_sync import run_clientes_sync

    try:
        res = run_clientes_sync(env["slug"], dry_run=not apply, full_sync=False)
    except Exception as exc:  # noqa: BLE001 — vira erro HTTP legível pro operador
        raise HTTPException(502, f"Falha na carga de clientes: {exc}") from exc
    if res is None:
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    body = {
        "local_only": res.reconciliacao is None,
        "skipped_empty": res.skipped_empty,
        "itens": res.itens,
        "extraido_em": res.extraido_em,
        "descartados_cpf": res.descartados_cpf,
        "descartados_invalidos": res.descartados_invalidos,
        "colisoes_dedup": res.colisoes_dedup,
    }
    if res.reconciliacao is not None:
        body["reconciliacao"] = res.reconciliacao.model_dump()
    return body


@router.post("/{env_id}/flowpcp/sync-materiais")
def sync_materiais_flowpcp(env_id: str, apply: bool = False, _=Depends(require_admin)):
    """Carga de material (Fire → FlowPCP), spec 2026-10-16 §6.2. `apply=false` (padrão):
    o Flow só calcula o relatório. `apply=true` exige a capacidade `materiais.v1` do Flow
    (422 antes de enviar se faltar). A cópia local (`materiais_fire`) é sempre atualizada."""
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    if not env.get("flowpcp_enabled"):
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    from app.integrations.flowpcp import materiais_sync
    from app.integrations.flowpcp.client import FlowPCPApplyEmAndamentoError

    try:
        res = materiais_sync.run_materiais_sync(env["slug"], dry_run=not apply, full_sync=True)
    except (materiais_sync.ApplySemCapacidadeError, materiais_sync.MateriaisDesligadoError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except FlowPCPApplyEmAndamentoError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — vira erro HTTP legível pro operador
        raise HTTPException(502, f"Falha na carga de materiais: {exc}") from exc
    if res is None:
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")
    if isinstance(res, materiais_sync.MateriaisLocalResult):
        return {"local_only": True, "itens": res.itens, "extraido_em": res.extraido_em}
    return res.model_dump(by_alias=True)


_PLANO_MAX_BYTES = 2048


@router.get("/{env_id}/flowpcp/cadastro-volta")
def cadastro_volta_flowpcp(env_id: str, _=Depends(require_admin)):
    """Livro da volta Flow → Fire (spec 2026-10-16 §6.3). Só leitura do SQLite do ambiente."""
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")

    from app.integrations.flowpcp import cadastro_volta
    from app.persistence import cadastro_volta_repo

    with db_router.env_connect(env["slug"]) as conn:
        por_estado = cadastro_volta_repo.contar_por_estado(conn)
        ultimos = cadastro_volta_repo.listar(conn, limite=50)
    for linha in ultimos:
        plano = linha.get("plano_json")
        linha["plano_truncado"] = bool(plano) and len(plano) > _PLANO_MAX_BYTES
        if linha["plano_truncado"]:
            linha["plano_json"] = plano[:_PLANO_MAX_BYTES]
    return {"modo": cadastro_volta.modo(env["slug"]), "por_estado": por_estado, "ultimos": ultimos}


@router.delete("/{env_id}", status_code=204)
def delete_environment(env_id: str, _=Depends(require_admin)):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "Ambiente não encontrado")
    environments_repo.soft_delete(env_id)
    return Response(status_code=204)


@router.post("/{env_id}/test")
def test_environment(env_id: str, _=Depends(require_admin)):
    """Valida pastas + tenta conexão Firebird. Não levanta — retorna status."""
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")

    watch_ok = Path(env["watch_dir"]).is_dir()
    output_ok = Path(env["output_dir"]).is_dir()
    fb_ok, fb_err = _try_firebird(env)

    return {
        "watch_dir": env["watch_dir"],
        "watch_dir_ok": watch_ok,
        "output_dir": env["output_dir"],
        "output_dir_ok": output_ok,
        "firebird_ok": fb_ok,
        "firebird_error": fb_err,
    }


def _try_firebird(env: dict) -> tuple[bool, str | None]:
    try:
        from app.erp.connection import FirebirdConnection

        cfg = environments_repo.to_fb_config(env)
        with FirebirdConnection().connect_with_config(cfg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
        return True, None
    except Exception as e:  # noqa: BLE001 — explicitly capturing all errors for diagnostics
        return False, str(e)
