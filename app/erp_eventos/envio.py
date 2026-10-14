"""HTTP das duas rotas novas do Flow (plano do Flow, Tasks 5 e 6):
`POST /api/portal-pedidos/eventos` e `POST /api/portal-pedidos/snapshot-erp`.

Credencial própria da base: `Authorization: Bearer fpe_…` + `X-Tenant-Id`.
Sem retry dentro do cliente (`max_attempts=1`): quem decide esperar, dividir
ou bloquear é a drenagem, com estado persistido — retry escondido aqui
reenviaria lote em laço sem ninguém ver.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.http.client import OutboundClient
from app.http.policies import RetryPolicy

EVENTOS_PATH = "/api/portal-pedidos/eventos"
FOTO_PATH = "/api/portal-pedidos/snapshot-erp"
SCHEMA_EVENTOS = "pedido.evento_erp.v1"


@dataclass(frozen=True)
class RespostaHttp:
    status: int | None  # None = rede/timeout
    corpo: dict | None
    retry_after: str | None
    erro: str | None = None


class ClienteErpEventos:
    def __init__(
        self,
        *,
        destino_url: str,
        tenant_id: str,
        token: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = OutboundClient(
            base_url=destino_url,
            timeout=timeout,
            retry_policy=RetryPolicy(max_attempts=1),
            default_headers={
                "Authorization": f"Bearer {token}",
                "X-Tenant-Id": tenant_id,
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def enviar_lote(self, lote_id: str, eventos: list[dict]) -> RespostaHttp:
        return self._post(
            EVENTOS_PATH, {"schema": SCHEMA_EVENTOS, "loteId": lote_id, "eventos": eventos}
        )

    def enviar_pagina(self, pagina: dict) -> RespostaHttp:
        return self._post(FOTO_PATH, pagina)

    def _post(self, path: str, corpo: dict) -> RespostaHttp:
        try:
            resp = self._http.post_json(path, json=corpo)
        except httpx.HTTPError as exc:
            return RespostaHttp(None, None, None, erro=type(exc).__name__)
        try:
            dados = resp.json()
        except ValueError:
            dados = None
        return RespostaHttp(
            resp.status_code,
            dados if isinstance(dados, dict) else None,
            resp.headers.get("Retry-After"),
            erro=None if resp.is_success else (resp.text or "")[:500],
        )


class Ritmo:
    """Intervalo mínimo entre POSTs para o mesmo (tenant, rota), compartilhado
    entre bases. 3 s = 20/min, abaixo do teto de 30/min por tenant do Flow
    (plano do Flow, Task 4) — vale para a primeira carga e para a foto."""

    def __init__(
        self,
        intervalo_s: float = 3.0,
        *,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._intervalo = intervalo_s
        self._relogio = relogio
        self._dormir = dormir
        self._ultimo: dict[tuple[str, str], float] = {}
        self._trava = threading.Lock()

    def aguardar(self, tenant: str, rota: str) -> None:
        with self._trava:
            chave = (tenant, rota)
            agora = self._relogio()
            falta = self._ultimo.get(chave, -1e18) + self._intervalo - agora
            if falta > 0:
                self._dormir(falta)
                agora += falta
            self._ultimo[chave] = agora
