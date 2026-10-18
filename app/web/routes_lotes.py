"""API do lote semanal intercompany (Nasmar -> MM).

Config e fator global são do admin; ler, informar o fator de um lote que está
esperando e fechar agora são do operador. O fechamento em si mora em
`app.consolidators.fechamento`; aqui só se valida a borda e se monta a tela.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import (
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.consolidators import fechamento
from app.consolidators.janela import MODOS, instante_fechamento, janela_de, proxima_janela
from app.consolidators.lote import MODOS_PRECO, preco_espelho
from app.erp.cnpj import cnpj_digits
from app.persistence import context as env_context
from app.persistence import db, environments_repo, lotes_repo, repo
from app.persistence import router as db_router
from app.utils.logger import logger
from app.web.auth import User, require_admin, require_user

router = APIRouter()

_EXEMPLO = Decimal("16.12")
_CENTAVO = Decimal("0.01")
_LIMITE_FORA = 20


def _fator_decimal(v: Any) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
        raise ValueError("fator deve ser número")
    try:
        d = Decimal(str(v))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("fator inválido") from exc
    if not d.is_finite() or not (Decimal("0") <= d < Decimal("1")):
        raise ValueError("fator fora de faixa (use 0 <= fator < 1)")
    return d


class _ComFator(BaseModel):
    fator: Decimal
    modo: str

    @field_validator("fator", mode="before")
    @classmethod
    def _fator(cls, v: Any) -> Decimal:
        return _fator_decimal(v)

    @field_validator("modo")
    @classmethod
    def _modo(cls, v: str) -> str:
        if v not in MODOS_PRECO:
            raise ValueError(f"modo deve ser um de {MODOS_PRECO}")
        return v


class FatorGlobalRequest(_ComFator):
    env_origem_slug: str


class FatorLoteRequest(_ComFator):
    pass


class ConfigRequest(BaseModel):
    env_origem_slug: str
    env_espelho_slug: str
    nome_revenda: str = Field(min_length=1, max_length=100)
    janela: str
    dia_inicio: StrictInt = Field(ge=0, le=6)
    hora_fechamento: StrictInt = Field(ge=0, le=23)
    ativo: StrictBool

    @field_validator("nome_revenda", mode="before")
    @classmethod
    def _nome(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("janela")
    @classmethod
    def _janela(cls, v: str) -> str:
        if v not in MODOS:
            raise ValueError(f"janela deve ser uma de {MODOS}")
        return v

    @model_validator(mode="after")
    def _origem_diferente_do_espelho(self) -> ConfigRequest:
        if self.env_origem_slug == self.env_espelho_slug:
            raise ValueError("o ambiente espelho deve ser diferente do de origem")
        return self


def _exemplo(fator: Decimal, modo: str) -> dict:
    para = preco_espelho(_EXEMPLO, fator=fator, modo=modo).quantize(
        _CENTAVO, rounding=ROUND_HALF_UP
    )
    return {"de": float(_EXEMPLO), "para": float(para)}


def _config_da_tela(conn) -> dict | None:
    configs = lotes_repo.listar_configs(conn, so_ativas=False)
    if not configs:
        return None
    return next((c for c in configs if c["ativo"]), configs[0])


def _valor(slug: str, import_id: str) -> float | None:
    """Valor da perna na origem (preço da Nasmar). Snapshot ilegível = sem valor."""
    env = environments_repo.get_by_slug(slug)
    if env is None:
        return None
    try:
        with env_context.active_env(env["id"], env["slug"]):
            entry = repo.get_import(import_id)
        itens = ((entry or {}).get("snapshot") or {}).get("items") or []
        total = sum(
            (Decimal(str(i.get("quantity") or 0)) * Decimal(str(i.get("unit_price") or 0)))
            for i in itens
        )
        return float(total.quantize(_CENTAVO, rounding=ROUND_HALF_UP))
    except Exception:  # noqa: BLE001 — tela degrada, não cai
        return None


def _previa(lote: dict) -> list[dict] | None:
    if lote["fator_preco_aplicado"] is None:
        return None
    try:
        consolidado = fechamento.previa_do_lote(lote)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"lotes: prévia do lote {lote['id']} indisponível: {type(exc).__name__}")
        return None
    return [
        {
            "product_code": i.product_code,
            "ean": i.ean,
            "description": i.description,
            "quantity": i.quantity,
            "unit_price": i.unit_price,
            "delivery_date": i.delivery_date,
        }
        for i in consolidado.order.items
    ]


def _resumo_aberto(lote: dict, cfg: dict, pernas: list[dict]) -> dict:
    previa = _previa(lote)
    total = None
    if previa is not None:
        total = float(
            sum(
                (Decimal(str(i["quantity"] or 0)) * Decimal(str(i["unit_price"] or 0)))
                for i in previa
            ).quantize(_CENTAVO, rounding=ROUND_HALF_UP)
        )
    return {
        "id": lote["id"],
        "chave": lote["chave_lote"],
        "janela_inicio": lote["janela_inicio"],
        "janela_fim": lote["janela_fim"],
        "fecha_em": instante_fechamento(
            date.fromisoformat(lote["janela_fim"]), hora=cfg["hora_fechamento"]
        ).isoformat(timespec="minutes"),
        "status": lote["status"],
        "pernas": len(pernas),
        "clientes": len({p["cnpj_cliente_final"] or p["import_id"] for p in pernas}),
        "linhas_previstas": None if previa is None else len(previa),
        "total_previsto": total,
        "fator": lote["fator_preco_aplicado"],
        "modo": lote["modo_preco_aplicado"],
    }


def _fora_do_lote(cfg: dict, inicio: str, fim: str, conn) -> dict:
    """Pedidos da origem que chegaram ao Fire na janela e não são perna de lote
    nenhum: digitados no Fire/Excel, não serão refaturados na MM."""
    env = environments_repo.get_by_slug(cfg["env_origem_slug"])
    if env is None:
        return {"total": 0, "itens": []}
    em_lote = {
        r[0]
        for r in conn.execute(
            "SELECT import_id FROM intercompany_lote_item WHERE env_origem_slug = ?",
            (cfg["env_origem_slug"],),
        )
    }
    with env_context.active_env(env["id"], env["slug"]), db.connect() as econn:
        rows = econn.execute(
            """
            SELECT id, order_number, customer_name, sent_to_fire_at, reconciled_at
            FROM imports
            WHERE environment_id = ?
              AND ( substr(sent_to_fire_at, 1, 10) BETWEEN ? AND ?
                 OR (portal_status = 'found_in_fire'
                     AND substr(reconciled_at, 1, 10) BETWEEN ? AND ?))
            ORDER BY COALESCE(sent_to_fire_at, reconciled_at) DESC, id
            """,
            (env["id"], inicio, fim, inicio, fim),
        ).fetchall()
    fora = [r for r in rows if r[0] not in em_lote]
    return {
        "total": len(fora),
        "itens": [
            {
                "import_id": r[0],
                "order_number": r[1],
                "customer_name": r[2],
                "quando": r[3] or r[4],
            }
            for r in fora[:_LIMITE_FORA]
        ],
    }


@router.get("/api/lotes")
def get_lotes(_=Depends(require_user)):
    hoje = date.today()
    with db_router.shared_connect() as conn:
        cfg = _config_da_tela(conn)
        recentes = lotes_repo.listar(conn, limite=20)
        out: dict[str, Any] = {
            "config": cfg,
            "fator_vigente": None,
            "proximo_fator": None,
            "aberto": None,
            "pendentes": [],
            "fora_do_lote": {"total": 0, "itens": []},
            "recentes": recentes,
        }
        if cfg is None:
            return out
        slug = cfg["env_origem_slug"]
        vigente = lotes_repo.fator_vigente(conn, slug, hoje)
        if vigente:
            out["fator_vigente"] = {"fator": str(vigente[0]), "modo": vigente[1]}
        inicio_prox, _ = proxima_janela(
            janela_de(hoje, modo=cfg["janela"], dia_inicio=cfg["dia_inicio"])[1],
            modo=cfg["janela"],
            dia_inicio=cfg["dia_inicio"],
        )
        prox = lotes_repo.fator_vigente(conn, slug, inicio_prox)
        if prox and prox != vigente:
            out["proximo_fator"] = {
                "fator": str(prox[0]),
                "modo": prox[1],
                "vigente_desde": inicio_prox.isoformat(),
            }
        j_hoje = janela_de(hoje, modo=cfg["janela"], dia_inicio=cfg["dia_inicio"])
        da_origem = [lt for lt in recentes if lt["env_origem_slug"] == slug]
        aberto = next(
            (
                lt
                for lt in da_origem
                if lt["status"] == "aberto" and lt["janela_inicio"] == j_hoje[0].isoformat()
            ),
            None,
        )
        out["pendentes"] = [
            _resumo_aberto(lt, cfg, lotes_repo.pernas_do_lote(conn, lt["id"]))
            for lt in da_origem
            if lt["status"] == "aguardando_fator"
        ]
        if aberto is not None:
            pernas = lotes_repo.pernas_do_lote(conn, aberto["id"])
            out["aberto"] = _resumo_aberto(aberto, cfg, pernas)
            ini, fim = aberto["janela_inicio"], aberto["janela_fim"]
        else:
            ini, fim = j_hoje[0].isoformat(), j_hoje[1].isoformat()
        out["fora_do_lote"] = _fora_do_lote(cfg, ini, fim, conn)
    return out


@router.get("/api/lotes/{lote_id}")
def get_lote(lote_id: int, _=Depends(require_user)):
    with db_router.shared_connect() as conn:
        lote = lotes_repo.get(conn, lote_id)
        if lote is None:
            raise HTTPException(status_code=404, detail="Lote não encontrado")
        pernas = lotes_repo.pernas_do_lote(conn, lote_id)
    lote["pernas"] = [
        {
            "import_id": p["import_id"],
            "pedido": p["pedido_cliente"],
            "cliente_final": p["razao_cliente_final"],
            "cnpj_cliente_final": p["cnpj_cliente_final"],
            "fire_codigo_origem": p["fire_codigo_origem"],
            "valor": _valor(p["env_origem_slug"], p["import_id"]),
        }
        for p in pernas
    ]
    lote["previa"] = _previa(lote)
    return lote


@router.put("/api/lotes/config")
def put_config(payload: ConfigRequest, user: User = Depends(require_admin)):
    origem = environments_repo.get_by_slug(payload.env_origem_slug)
    espelho = environments_repo.get_by_slug(payload.env_espelho_slug)
    if origem is None or espelho is None:
        raise HTTPException(status_code=400, detail="Ambiente de origem ou espelho não existe")
    cnpj_revenda = cnpj_digits(origem.get("cnpj"))
    if not cnpj_revenda:
        raise HTTPException(status_code=400, detail="O ambiente de origem não tem CNPJ cadastrado")
    inter = cnpj_digits(espelho.get("intercompany_cnpj"))
    if inter and inter != cnpj_revenda:
        raise HTTPException(
            status_code=400,
            detail="O CNPJ da revenda difere do intercompany configurado no ambiente espelho",
        )
    try:
        with db_router.shared_connect() as conn:
            return lotes_repo.save_config(
                conn,
                env_origem_slug=payload.env_origem_slug,
                env_espelho_slug=payload.env_espelho_slug,
                cnpj_revenda=cnpj_revenda,
                nome_revenda=payload.nome_revenda,
                janela=payload.janela,
                dia_inicio=payload.dia_inicio,
                hora_fechamento=payload.hora_fechamento,
                ativo=payload.ativo,
                por=user.email,
                agora=datetime.now(),
            )
    except lotes_repo.JanelaComLoteAbertoError as exc:
        raise HTTPException(
            status_code=409,
            detail="Há lote aberto com pernas: feche-o antes de trocar a janela",
        ) from exc


@router.post("/api/lotes/fator")
def post_fator_global(payload: FatorGlobalRequest, user: User = Depends(require_admin)):
    with db_router.shared_connect() as conn:
        cfg = lotes_repo.get_config(conn, payload.env_origem_slug)
        if cfg is None:
            raise HTTPException(status_code=404, detail="Sem configuração de lote para a origem")
        vigente_desde, _ = proxima_janela(
            janela_de(date.today(), modo=cfg["janela"], dia_inicio=cfg["dia_inicio"])[1],
            modo=cfg["janela"],
            dia_inicio=cfg["dia_inicio"],
        )
        lotes_repo.registrar_fator(
            conn,
            env_origem_slug=payload.env_origem_slug,
            fator=payload.fator,
            modo=payload.modo,
            vigente_desde=vigente_desde,
            por=user.email,
            agora=datetime.now(),
        )
    return {
        "vigente_desde": vigente_desde.isoformat(),
        "exemplo": _exemplo(payload.fator, payload.modo),
    }


@router.post("/api/lotes/{lote_id}/fator")
def post_fator_lote(lote_id: int, payload: FatorLoteRequest, user: User = Depends(require_user)):
    with db_router.shared_connect() as conn:
        lote = lotes_repo.get(conn, lote_id)
        if lote is None:
            raise HTTPException(status_code=404, detail="Lote não encontrado")
        if lote["status"] != "aguardando_fator":
            raise HTTPException(status_code=409, detail="O lote não está aguardando fator")
        try:
            lotes_repo.informar_fator(
                conn,
                lote_id=lote_id,
                fator=payload.fator,
                modo=payload.modo,
                por=user.email,
                agora=datetime.now(),
            )
        except lotes_repo.LoteFechadoError as exc:
            raise HTTPException(status_code=409, detail="O lote já fechou") from exc
    return {"exemplo": _exemplo(payload.fator, payload.modo)}


@router.post("/api/lotes/{lote_id}/fechar")
def post_fechar(lote_id: int, user: User = Depends(require_user)):
    with db_router.shared_connect() as conn:
        if lotes_repo.get(conn, lote_id) is None:
            raise HTTPException(status_code=404, detail="Lote não encontrado")
    r = fechamento.fechar_lote(lote_id, por=user.email)
    if r["resultado"] == "aguardando_fator":
        raise HTTPException(status_code=409, detail="Informe o fator antes de fechar o lote")
    if r["resultado"] == "vazio":
        raise HTTPException(status_code=409, detail="Lote sem pernas: nada a fechar")
    if r["resultado"] == "erro":
        raise HTTPException(status_code=409, detail=r["motivo"] or "Lote não fechou")
    return r
