"""Configuração SP1 por ambiente: a base do ERP que ele é, o papel dela e a
credencial própria (`Bearer fpe_…`) para as rotas de eventos e foto do Flow.

Credencial cifrada por (ambiente, base) via `secret_store`, como a senha do
Firebird. Nunca volta em `public_view`. Semântica de `token` no `salvar`
igual à do `service_token` do FlowPCP: None mantém, "" limpa, valor troca.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.erp.cnpj import cnpj_digits
from app.persistence import router
from app.security import secret_store
from app.utils.logger import logger

BASE_REGEX = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOKEN_REGEX = re.compile(r"^fpe_[A-Za-z0-9_-]{43}$")
UUID_REGEX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PAPEIS = ("produtora", "intermediaria")


class ConfigErpInvalidaError(ValueError):
    pass


@dataclass(frozen=True)
class BaseErpConfig:
    environment_id: str
    env_slug: str
    base: str
    papel: str
    tenant_id: str
    destino_url: str
    token: str | None
    foto_hora: int | None
    ativo: bool
    # CNPJs que são intermediário fiscal para esta base: o
    # `intercompany_cnpj` do próprio ambiente (na MM, a Nasmar no ambiente mm).
    intermediarios: frozenset[str]
    revenda_slug: str | None
    # Há credencial gravada, mas não decifra (chave do secret_store trocada):
    # para a tela dizer "salve de novo" em vez de "nenhuma credencial".
    token_ilegivel: bool = False


def _agora() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _validar(
    base: str, papel: str, tenant_id: str, destino_url: str, foto_hora: int | None
) -> None:
    if not BASE_REGEX.match(base):
        raise ConfigErpInvalidaError("base: use minúsculas, dígitos e _ (ex.: mm, nasmar)")
    if papel not in PAPEIS:
        raise ConfigErpInvalidaError("papel: produtora ou intermediaria")
    if not UUID_REGEX.match(tenant_id):
        raise ConfigErpInvalidaError("tenant_id: UUID do tenant no Flow")
    local = destino_url.startswith(("http://localhost", "http://127.0.0.1"))
    if not (destino_url.startswith("https://") or local):
        raise ConfigErpInvalidaError("destino_url: https:// (http só para localhost)")
    if foto_hora is not None and not 0 <= foto_hora <= 23:
        raise ConfigErpInvalidaError("foto_hora: 0 a 23")


def salvar(
    environment_id: str,
    *,
    base: str,
    papel: str,
    tenant_id: str,
    destino_url: str,
    foto_hora: int | None,
    ativo: bool,
    token: str | None = None,
) -> None:
    destino_url = destino_url.rstrip("/")
    _validar(base, papel, tenant_id, destino_url, foto_hora)
    if token and not TOKEN_REGEX.match(token):
        raise ConfigErpInvalidaError("token: formato fpe_… gerado pelo script do Flow")
    agora = _agora()
    with router.shared_connect() as conn:
        atual = conn.execute(
            "SELECT base, token_enc FROM erp_bases WHERE environment_id = ?", (environment_id,)
        ).fetchone()
        if token is None:
            token_enc = atual["token_enc"] if atual else None
        else:
            token_enc = secret_store.encrypt(token) if token else None
        # Trocar a base de um ambiente apaga a linha antiga: base é identidade
        # no Flow, não um rótulo.
        conn.execute("DELETE FROM erp_bases WHERE environment_id = ?", (environment_id,))
        outro = conn.execute(
            "SELECT e.slug FROM erp_bases b JOIN environments e ON e.id = b.environment_id "
            "WHERE b.tenant_id = ? AND b.base = ?",
            (tenant_id, base),
        ).fetchone()
        if outro:
            raise ConfigErpInvalidaError(
                f"base '{base}' já é usada pelo ambiente '{outro[0]}' neste tenant do Flow"
            )
        conn.execute(
            """
            INSERT INTO erp_bases (environment_id, base, papel, tenant_id, destino_url,
                                   token_enc, foto_hora, ativo, criado_em, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                environment_id,
                base,
                papel,
                tenant_id,
                destino_url,
                token_enc,
                foto_hora,
                1 if ativo else 0,
                agora,
                agora,
            ),
        )


def _montar(row) -> BaseErpConfig:
    token = secret_store.decrypt(row["token_enc"]) if row["token_enc"] else None
    inter = cnpj_digits(row["intercompany_cnpj"])
    return BaseErpConfig(
        environment_id=row["environment_id"],
        env_slug=row["slug"],
        base=row["base"],
        papel=row["papel"],
        tenant_id=row["tenant_id"],
        destino_url=row["destino_url"],
        token=token,
        foto_hora=row["foto_hora"],
        ativo=bool(row["ativo"]),
        intermediarios=frozenset({inter}) if len(inter) == 14 else frozenset(),
        revenda_slug=(row["intercompany_env_slug"] or "").strip() or None,
        token_ilegivel=bool(row["token_enc"]) and token is None,
    )


_SELECT = """
    SELECT b.*, e.slug, e.intercompany_cnpj, e.intercompany_env_slug
    FROM erp_bases b JOIN environments e ON e.id = b.environment_id
"""


def obter(environment_id: str) -> BaseErpConfig | None:
    with router.shared_connect() as conn:
        row = conn.execute(_SELECT + " WHERE b.environment_id = ?", (environment_id,)).fetchone()
    return _montar(row) if row else None


def listar_ativas() -> list[BaseErpConfig]:
    """Bases ligadas, de ambientes ativos, com credencial. Ordem estável por slug."""
    with router.shared_connect() as conn:
        rows = conn.execute(
            _SELECT + " WHERE b.ativo = 1 AND e.is_active = 1 ORDER BY e.slug"
        ).fetchall()
    saida = []
    for cfg in (_montar(r) for r in rows):
        if cfg.token:
            saida.append(cfg)
        else:
            # Ligada sem credencial legível (nunca salva, ou chave do
            # secret_store trocada): não envia, e diz por quê a cada volta.
            logger.warning(f"erp_eventos: base '{cfg.base}' ligada sem credencial — ignorada")
    return saida


def produtora_do_tenant(tenant_id: str) -> BaseErpConfig | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            _SELECT + " WHERE b.tenant_id = ? AND b.papel = 'produtora' AND e.is_active = 1",
            (tenant_id,),
        ).fetchone()
    return _montar(row) if row else None


def public_view(cfg: BaseErpConfig | None) -> dict[str, Any] | None:
    if cfg is None:
        return None
    return {
        "base": cfg.base,
        "papel": cfg.papel,
        "tenant_id": cfg.tenant_id,
        "destino_url": cfg.destino_url,
        "foto_hora": cfg.foto_hora,
        "ativo": cfg.ativo,
        "tem_token": bool(cfg.token),
        "token_ilegivel": cfg.token_ilegivel,
    }
