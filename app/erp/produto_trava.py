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
  3. `sem_codigo_fire` — só quando sai planilha (`export_mode` "xlsx" ou "both"): o
     produto decidido não tem `CODPROD_ALTERN`, e a planilha só aponta produto por ele.
  4. `troca_no_fire` — só quando sai planilha: o importador de Excel do Fire casa por
     PREFIXO (BACKLOG 2.15) e gravaria outro SEQ com esse código, ou não deu para
     conferir (`seq_previsto` None).

`produtos_decididos` monta, do MESMO check que a trava aprovou, o que vai ao Fire:
os vínculos do `depara_apply.aplicar` (a planilha leva o `CODPROD_ALTERN` do produto
decidido, com o código do cliente na OBS) e o `produtos_por_item` do insert (SEQ).
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


def _produto(item) -> dict:
    produto = item.get("produto") if isinstance(item, dict) else None
    return produto if isinstance(produto, dict) else {}


def _itens_pendentes(check: dict) -> list[dict]:
    pendentes: list[dict] = []
    for i, item in enumerate(check.get("items") or []):
        produto = _produto(item)
        estado = produto.get("estado") or "ausente"
        if estado == "ok":
            continue
        pendentes.append({"index": i, "estado": estado, "motivo": produto.get("motivo")})
    return pendentes


def candidato_decidido(produto: dict) -> dict | None:
    """O candidato do SEQ decidido (no `ok`, `candidatos` traz só ele)."""
    candidatos = [c for c in produto.get("candidatos") or [] if isinstance(c, dict)]
    for c in candidatos:
        if c.get("seq") is not None and str(c.get("seq")) == str(produto.get("fire_seq")):
            return c
    return None


def codigo_decidido(produto: dict) -> str | None:
    """`CODPROD_ALTERN` do produto decidido, lido do Fire na hora do check."""
    c = candidato_decidido(produto)
    codigo = ((c or {}).get("codprod_altern") or "").strip()
    return codigo or None


def produtos_decididos(check: dict | None) -> tuple[list[dict], dict[int, int]]:
    """(vínculos para `depara_apply.aplicar`, {posição: SEQ}) de todo item `ok`.

    Todo item `ok` entra, mesmo quando o código do cliente já é o `CODPROD_ALTERN`
    (a OBS ganha "ref cliente: ..." e a planilha leva o mesmo código). `fire_ean` é
    None: o EAN do cliente fica (no `ok` ele é do produto decidido ou não existe no
    Fire). Sem `CODPROD_ALTERN`, o código do cliente fica: só o insert, que entra pelo
    SEQ, aceita esse item (a planilha trava em `sem_codigo_fire`)."""
    vinculos: list[dict] = []
    mapa: dict[int, int] = {}
    for i, item in enumerate((check or {}).get("items") or []):
        produto = _produto(item)
        if produto.get("estado") != "ok" or produto.get("fire_seq") is None:
            continue
        seq = int(produto["fire_seq"])
        orig = item.get("product_code") or item.get("ean") or ""
        vinculos.append(
            {
                "item_index": i,
                "fire_produto_id": seq,
                "fire_codigo": codigo_decidido(produto) or item.get("product_code"),
                "fire_ean": None,
                "orig": orig,
            }
        )
        mapa[i] = seq
    return vinculos, mapa


def _itens_planilha(check: dict) -> tuple[list[dict], list[dict]]:
    """(sem código no Fire, troca no importador) dos itens `ok`."""
    sem_codigo: list[dict] = []
    trocas: list[dict] = []
    for i, item in enumerate(check.get("items") or []):
        produto = _produto(item)
        if produto.get("estado") != "ok":
            continue
        seq = produto.get("fire_seq")
        codigo = codigo_decidido(produto)
        if codigo is None:
            sem_codigo.append(
                {
                    "index": i,
                    "estado": "sem_codigo_fire",
                    "motivo": f"O produto {seq} não tem código no Fire; "
                    "a planilha não consegue apontá-lo.",
                }
            )
            continue
        # Chave ausente = check que não conferiu a planilha: não dá para afirmar.
        troca = (
            produto["troca_no_fire"]
            if "troca_no_fire" in produto
            else {"seq_previsto": None, "codigo": codigo}
        )
        if not troca:
            continue
        previsto = troca.get("seq_previsto") if isinstance(troca, dict) else None
        motivo = (
            f"O importador do Fire trocaria este código pelo produto {previsto}."
            if previsto is not None
            else f"O código {codigo} do produto {seq} não pode ser conferido na planilha do Fire."
        )
        trocas.append({"index": i, "estado": "troca_no_fire", "motivo": motivo})
    return sem_codigo, trocas


def avaliar(check: dict, *, env: dict | None, export_mode: str) -> TravaProduto:
    """Decide se o pedido pode seguir para o ERP. Ver o docstring do módulo.

    `export_mode`: "xlsx" e "both" geram planilha e somam as regras dela
    (`sem_codigo_fire`, `troca_no_fire`); "db" (só insert, pelo SEQ) não.
    """
    m = modo(env)
    if m == "desligado":
        return TravaProduto(bloqueado=False, reason=None, itens=[])

    itens = _itens_pendentes(check)
    planilha = export_mode in ("xlsx", "both")
    sem_codigo, trocas = _itens_planilha(check) if planilha else ([], [])
    if m == "observando":
        return TravaProduto(bloqueado=False, reason=None, itens=itens + sem_codigo + trocas)

    # ligado
    if not check.get("available") or check.get("produto_conferencia_incompleta"):
        return TravaProduto(bloqueado=True, reason="fire_indisponivel", itens=itens)
    if itens:
        return TravaProduto(bloqueado=True, reason="produto_a_identificar", itens=itens)
    if sem_codigo:
        return TravaProduto(bloqueado=True, reason="sem_codigo_fire", itens=sem_codigo)
    if trocas:
        return TravaProduto(bloqueado=True, reason="troca_no_fire", itens=trocas)
    return TravaProduto(bloqueado=False, reason=None, itens=[])
