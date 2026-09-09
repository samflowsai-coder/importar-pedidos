"""A escada do roteamento: em que empresa este pedido entra?

    1. supplier_cnpj do documento casa com environments.cnpj  -> documento
    2. histórico do cliente no Fire, 12 meses, SE inequívoco  -> historico
    3. decisão lembrada para este cliente                     -> memoria
    4. nada resolveu, ou histórico ambíguo/indisponível       -> perguntar

A precedência é **estrita**. Cada degrau perde para o de cima, sem exceção:
documento é fato sobre este pedido, histórico é fato sobre o passado, memória é
julgamento humano. Quando um degrau mais forte contradiz um mais fraco, a
divergência viaja na `Decisao` — não é sobrescrita em silêncio.

Este módulo é **puro**: recebe `Deps` com as três leituras e não abre conexão
nenhuma. É o que permite testar a escada inteira sem banco, e é o que torna o
modo `observando` barato — rodar sem agir não custa quase nada.

A única exceção deliberada é `_historico_janela_ampla`: uma segunda consulta,
numa janela de 24 meses, que só enriquece a `explicacao` do degrau 2 com uma
divergência fora da janela de decisão (o caso Beira Rio — comprou da MM até
05/2025, migrou para a Nasmar). Ela roda só quando o degrau 2 já resolveu,
nunca influencia `env_slug`, e um erro nela nunca derruba o pedido.

Ele NUNCA devolve um ambiente default. `env_slug is None` com
`degrau == 'perguntar'` é uma resposta legítima e é a que o chamador tem que
saber tratar.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.erp.cnpj import cnpj_digits
from app.models.order import Order
from app.routing.historico import HistoricoAmbiente
from app.routing.historico import resolver as _resolver_historico
from app.utils.logger import logger

DEGRAUS: tuple[str, ...] = ("documento", "historico", "memoria", "perguntar")

# Janela mais larga que a do degrau 2 (12 meses), usada só para a divergência
# extra na explicação — ver `_historico_janela_ampla`.
_MESES_JANELA_AMPLA = 24


@dataclass(frozen=True)
class Decisao:
    env_slug: str | None
    degrau: str
    explicacao: str
    divergiu_de: str | None = None
    historico: tuple[HistoricoAmbiente, ...] = field(default_factory=tuple)

    @property
    def resolveu(self) -> bool:
        return self.env_slug is not None


@dataclass(frozen=True)
class Deps:
    env_por_cnpj: Callable[[str], str | None]
    historico: Callable[[Sequence[str | None]], tuple[HistoricoAmbiente, ...]]
    memoria: Callable[[str], str | None]


def _fmt(cnpj: str) -> str:
    if len(cnpj) != 14:
        return cnpj
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def cnpjs_do_pedido(order: Order) -> list[str]:
    """CNPJs que identificam o comprador: o do cabeçalho e os das lojas.

    Ordem preservada e sem repetição. As lojas entram porque numa planilha de
    desmembramento a identidade do comprador está nas colunas, não no
    cabeçalho — ver `DesmembramentoXlsParser`.
    """
    vistos: list[str] = []
    for bruto in [order.header.customer_cnpj, *(i.delivery_cnpj for i in order.items)]:
        c = cnpj_digits(bruto)
        if c and c not in vistos:
            vistos.append(c)
    return vistos


def _historico_janela_ampla(cnpjs: Sequence[str]) -> tuple[HistoricoAmbiente, ...]:
    """A mesma consulta do degrau 2, numa janela de 24 meses.

    Só existe para enriquecer a explicação do degrau 2 já resolvido — nunca
    é chamada para decidir. Fala com o Firebird de verdade em produção
    (mesmo caminho de `historico.consultar`); import local para manter o
    módulo puro para import, e nome de módulo separado para que o teste
    troque só esta função (`monkeypatch.setattr`) sem tocar banco nenhum.
    """
    from app.routing import historico as hist_mod

    return hist_mod.consultar(cnpjs, meses=_MESES_JANELA_AMPLA)


def _explicar_fora_da_janela(cnpjs: Sequence[str], hist_12: Sequence[HistoricoAmbiente]) -> str:
    """Trecho "atenção, ..." quando a janela de 24 meses revela compra em
    outro ambiente que a janela de 12 meses (a de decisão) não via mais — ou
    "" se não houver nada, ou a consulta falhar. Nunca levanta."""
    vistos = {h.env_slug for h in hist_12 if h.pedidos > 0}
    try:
        ampla = _historico_janela_ampla(cnpjs)
    except Exception as exc:  # noqa: BLE001 — enriquecimento não pode derrubar o pedido
        logger.warning("routing.ambiente.janela_ampla_falhou erro={!r}", exc)
        return ""
    fora = [h for h in ampla if h.pedidos > 0 and h.env_slug not in vistos]
    if not fora:
        return ""
    partes = ", ".join(f"{h.env_nome} ({h.pedidos} pedido(s), até {h.ultimo_em})" for h in fora)
    return f"; atenção, este cliente também já comprou de {partes}"


def _motivo_historico_nao_resolveu(hist: Sequence[HistoricoAmbiente]) -> str:
    """Distingue, pro operador que vai ter que escolher a empresa, "cliente
    novo" de "Firebird mudo" — são situações que pedem ações diferentes
    (nada vs. chamar o suporte / esperar a VPN)."""
    indisponiveis = [h.env_nome for h in hist if h.indisponivel]
    if indisponiveis:
        verbo = "não respondeu" if len(indisponiveis) == 1 else "não responderam"
        return f"o Firebird de {', '.join(indisponiveis)} {verbo} — histórico incompleto"
    if len([h for h in hist if h.pedidos > 0]) > 1:
        return "cliente já comprou de mais de uma empresa na janela de 12 meses"
    return "o pedido não traz o CNPJ do fornecedor e o cliente não tem histórico"


def ambiente_para(order: Order, deps: Deps) -> Decisao:
    """Resolve o ambiente do pedido. Nunca chuta."""
    cnpjs = cnpjs_do_pedido(order)
    cliente = cnpjs[0] if cnpjs else ""

    lembrado = deps.memoria(cliente) if cliente else None

    # Degrau 1 — o documento.
    fornecedor = cnpj_digits(order.header.supplier_cnpj)
    if fornecedor:
        env = deps.env_por_cnpj(fornecedor)
        if env:
            return Decisao(
                env_slug=env,
                degrau="documento",
                explicacao=f"Fornecedor {_fmt(fornecedor)} no pedido → ambiente {env}",
                divergiu_de=lembrado if lembrado and lembrado != env else None,
            )

    # Degrau 2 — o histórico, e só quando ele é inequívoco.
    hist = deps.historico(cnpjs) if cnpjs else ()
    do_historico = _resolver_historico(hist)
    if do_historico:
        h = next(x for x in hist if x.env_slug == do_historico)
        extra = _explicar_fora_da_janela(cnpjs, hist)
        return Decisao(
            env_slug=do_historico,
            degrau="historico",
            explicacao=(
                f"Histórico: {h.pedidos} pedido(s) em {h.env_nome}, último em {h.ultimo_em}{extra}"
            ),
            divergiu_de=lembrado if lembrado and lembrado != do_historico else None,
            historico=tuple(hist),
        )

    # Degrau 3 — a memória.
    if lembrado:
        return Decisao(
            env_slug=lembrado,
            degrau="memoria",
            explicacao=f"Escolha registrada para este cliente → ambiente {lembrado}",
            historico=tuple(hist),
        )

    # Degrau 4 — sem resposta. É um resultado, não uma falha.
    motivo = _motivo_historico_nao_resolveu(hist)
    return Decisao(
        env_slug=None,
        degrau="perguntar",
        explicacao=f"Ambiente não resolvido: {motivo}",
        historico=tuple(hist),
    )


def deps_padrao() -> Deps:
    """As três leituras reais. Import local: mantém o módulo puro para teste."""
    from app.persistence import decisao_ambiente_repo, environments_repo
    from app.routing import historico as hist_mod

    def env_por_cnpj(cnpj: str) -> str | None:
        env = environments_repo.find_by_cnpj(cnpj)
        return env["slug"] if env else None

    def memoria(cnpj: str) -> str | None:
        d = decisao_ambiente_repo.lembrada(cnpj)
        return d["env_slug"] if d else None

    return Deps(
        env_por_cnpj=env_por_cnpj,
        historico=lambda cnpjs: hist_mod.consultar(cnpjs),
        memoria=memoria,
    )
