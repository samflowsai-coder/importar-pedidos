"""Portão do casamento de produto por chave exata (`produto_chave_modo`).

Função pura: recebe o resultado da conferência (`check`, com a decisão por item em
`item["produto"]`) e o ambiente, e diz se o pedido pode seguir para o ERP.

- `desligado` (padrão): nada muda, nunca trava, não lista.
- `observando`: nunca trava, mas `itens` lista o que travaria (para medir antes de ligar).
- `ligado`: trava o pedido inteiro. Prioridade dos motivos:
  1. `fire_indisponivel` — o Fire não respondeu (`available` False) ou a conferência de
     vínculos/intercompany ficou incompleta (`produto_conferencia_incompleta`): sem ler
     tudo não dá para afirmar que o item está resolvido.
  2. `produto_a_identificar` — algum item com `produto.estado` diferente de `ok`
     (item sem `produto` conta como não resolvido).
  3. `troca_no_fire`, `sem_codigo_fire` — regras do export XLSX (Task 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

MODOS = ("desligado", "observando", "ligado")


@dataclass(frozen=True)
class TravaProduto:
    bloqueado: bool
    reason: str | None = None
    itens: list[dict] = field(default_factory=list)


def modo(env: dict | None) -> str:
    """Modo do ambiente; `desligado` se o env é None ou o valor é inválido."""
    valor = (env or {}).get("produto_chave_modo")
    return valor if valor in MODOS else "desligado"


def _itens_pendentes(check: dict) -> list[dict]:
    pendentes: list[dict] = []
    for i, item in enumerate(check.get("items") or []):
        produto = item.get("produto") if isinstance(item, dict) else None
        produto = produto if isinstance(produto, dict) else {}
        estado = produto.get("estado") or "ausente"
        if estado == "ok":
            continue
        pendentes.append({"index": i, "estado": estado, "motivo": produto.get("motivo")})
    return pendentes


def avaliar(check: dict, *, env: dict | None, export_mode: str) -> TravaProduto:
    """Decide se o pedido pode seguir para o ERP. Ver o docstring do módulo.

    `export_mode` ("xlsx" | "db" | "both") já é aceito: as regras específicas do
    XLSX (`troca_no_fire`, `sem_codigo_fire`) entram na Task 6, no ponto marcado abaixo.
    """
    m = modo(env)
    if m == "desligado":
        return TravaProduto(bloqueado=False, reason=None, itens=[])

    itens = _itens_pendentes(check)
    if m == "observando":
        return TravaProduto(bloqueado=False, reason=None, itens=itens)

    # ligado
    if not check.get("available") or check.get("produto_conferencia_incompleta"):
        return TravaProduto(bloqueado=True, reason="fire_indisponivel", itens=itens)
    if itens:
        return TravaProduto(bloqueado=True, reason="produto_a_identificar", itens=itens)
    # Task 6: com export_mode == "xlsx", "troca_no_fire" e "sem_codigo_fire" entram aqui.
    return TravaProduto(bloqueado=False, reason=None, itens=[])
