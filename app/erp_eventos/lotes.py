"""Regras puras do envio: cortar o lote, ler a resposta, decidir o que fazer após o HTTP.

Nada aqui fala com rede ou banco — `drenagem.py` aplica as decisões.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

MAX_EVENTOS = 500
# 2 MB é o teto do Flow para o corpo inteiro. A conta aqui é sobre o JSON
# compacto que fica na fila; o envelope e a reserializaçào do httpx cabem nos
# 10% de folga.
MAX_BYTES = 1_800_000
_ENVELOPE_BYTES = 200


@dataclass(frozen=True)
class Pendente:
    ordem: int
    id: str
    unidade: str
    payload_json: str


class UnidadeGrandeDemaisError(Exception):
    """Um `pedido_visto` + itens que sozinho estoura o lote. Não dá para cumprir o contrato."""

    def __init__(self, unidade: str, eventos: int, tamanho: int) -> None:
        super().__init__(f"unidade {unidade} com {eventos} eventos / {tamanho} bytes")
        self.unidade = unidade


def proximo_lote(
    pendentes: list[Pendente], *, max_eventos: int = MAX_EVENTOS, max_bytes: int = MAX_BYTES
) -> list[Pendente]:
    """Maior prefixo de `pendentes` (já em `ordem`) que cabe no lote sem partir unidade.

    Unidades são contíguas na fila (a vigia grava o pedido inteiro de uma vez),
    então "partir unidade" é cortar entre dois pendentes com a mesma `unidade`.
    """
    lote: list[Pendente] = []
    tamanho = _ENVELOPE_BYTES
    i = 0
    while i < len(pendentes):
        j = i
        while j < len(pendentes) and pendentes[j].unidade == pendentes[i].unidade:
            j += 1
        unidade = pendentes[i:j]
        bytes_unidade = sum(len(p.payload_json.encode("utf-8")) + 1 for p in unidade)
        if len(unidade) > max_eventos or _ENVELOPE_BYTES + bytes_unidade > max_bytes:
            if lote:
                return lote
            raise UnidadeGrandeDemaisError(pendentes[i].unidade, len(unidade), bytes_unidade)
        if len(lote) + len(unidade) > max_eventos or tamanho + bytes_unidade > max_bytes:
            return lote
        lote.extend(unidade)
        tamanho += bytes_unidade
        i = j
    return lote


def dividir_lote(lote: list[Pendente]) -> tuple[list[Pendente], list[Pendente]] | None:
    """Metade sem partir unidade (resposta 413). None se o lote é uma unidade só."""
    fronteiras = [k for k in range(1, len(lote)) if lote[k].unidade != lote[k - 1].unidade]
    if not fronteiras:
        return None
    meio = min(fronteiras, key=lambda k: abs(k - len(lote) / 2))
    return lote[:meio], lote[meio:]


_SITUACOES = frozenset({"aceito", "duplicado", "rejeitado"})


def ler_resultados(enviados: list[str], corpo: dict) -> dict[str, tuple[str, str | None]]:
    """Casa a resposta 202 por `id`, nunca por posição.

    Só devolve ids que foram enviados neste lote e vieram com situação
    válida. Id enviado e ausente da resposta fica fora do dicionário: segue
    pendente e vai de novo (o Flow deduplica por id).
    """
    enviados_set = set(enviados)
    saida: dict[str, tuple[str, str | None]] = {}
    for r in corpo.get("resultados") or []:
        rid = r.get("id") if isinstance(r, dict) else None
        situacao = r.get("situacao") if isinstance(r, dict) else None
        if rid in enviados_set and situacao in _SITUACOES and rid not in saida:
            motivo = r.get("motivo")
            saida[rid] = (situacao, str(motivo)[:500] if motivo is not None else None)
    return saida


@dataclass(frozen=True)
class Decisao:
    acao: str  # "aplicar" | "dividir" | "esperar" | "bloquear"
    espera_s: float = 0.0
    bloqueio: str | None = None


ESPERA_BASE_S = 30.0
ESPERA_MAX_S = 900.0


def espera_crescente(falhas_seguidas: int) -> float:
    """30 s, 60 s, 120 s ... até 15 min."""
    return min(ESPERA_BASE_S * (2 ** max(falhas_seguidas, 0)), ESPERA_MAX_S)


def retry_after_s(valor: str | None, agora: datetime) -> float | None:
    if not valor:
        return None
    texto = valor.strip()
    if texto.isdigit():
        return float(texto)
    try:
        quando = parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        return None
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=UTC)
    return max((quando - agora).total_seconds(), 0.0)


def decidir(
    status: int | None, *, retry_after: str | None, falhas_seguidas: int, agora: datetime
) -> Decisao:
    """`status=None` = falha de rede/timeout."""
    if status == 202:
        return Decisao("aplicar")
    if status == 413:
        return Decisao("dividir")
    if status == 401:
        return Decisao("bloquear", bloqueio="credencial")
    if status == 400:
        return Decisao("bloquear", bloqueio="lote_invalido")
    if status == 429:
        pedida = retry_after_s(retry_after, agora) or 0.0
        return Decisao("esperar", espera_s=max(pedida, espera_crescente(falhas_seguidas)))
    if status is not None and 400 <= status < 500:
        # 403/404/422...: reenviar o mesmo corpo daria a mesma resposta. Para e avisa.
        return Decisao("bloquear", bloqueio=f"http_{status}")
    return Decisao("esperar", espera_s=espera_crescente(falhas_seguidas))
