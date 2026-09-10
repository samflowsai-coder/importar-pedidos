"""A escada do roteamento: em que empresa este pedido entra?

    1. supplier_cnpj do documento casa com environments.cnpj  -> documento
    2. histórico do cliente no Fire, 12 meses, SE inequívoco  -> historico
    3. decisão lembrada para este cliente                     -> memoria
    4. nada resolveu, ou histórico ambíguo/indisponível       -> perguntar

A precedência é **estrita**. Cada degrau perde para o de cima, sem exceção:
documento é fato sobre este pedido, histórico é fato sobre o passado, memória é
julgamento humano. Quando um degrau mais forte contradiz um mais fraco, a
divergência viaja na `Decisao` — não é sobrescrita em silêncio.

Este módulo é **puro**: toda leitura passa por `Deps`, e nenhum import de
persistência aparece no topo do arquivo — só dentro de `deps_padrao()`. É o
que permite testar a escada inteira sem banco, e é o que torna o modo
`observando` barato — rodar sem agir não custa quase nada.

`Deps.historico_amplo` é opcional (default `None`) e cobre só um
enriquecimento: uma segunda consulta, numa janela de 24 meses, usada apenas
para anexar na explicação do degrau 2 uma divergência que a janela de 12
meses (a de decisão) já não vê mais — o caso Beira Rio, que comprou da MM até
05/2025 e migrou para a Nasmar. Sem esse campo (`None`), a explicação some a
parte extra e **nenhum I/O adicional acontece** — é o que mantém o modo
`observando` barato mesmo com o degrau 2 resolvendo toda hora. Essa segunda
consulta nunca influencia `env_slug` (que já está fixado pela janela de 12
meses antes dela ser chamada), e um erro nela vira `logger.warning` e some —
nunca derruba o pedido.

Contratos que quem monta `Deps` e renderiza a `Decisao` (o wiring — hoje as
Tasks 10/11) precisa conhecer, porque não dá pra descobrir sozinho em
produção:

- **Exceção de `env_por_cnpj`, `historico` ou `memoria` propaga.** Este
  módulo não blinda essas três leituras — só `historico_amplo`, que é
  enriquecimento, não decisão. Se o Firebird ou o SQLite caírem no meio de
  uma leitura que decide, a exceção sobe crua pra fora de `ambiente_para`;
  virar 5xx, retry ou "perguntar" é decisão de quem chama, não deste módulo.
- **A memória é lida pela chave `cnpjs_do_pedido(order)[0]`.** Num
  desmembramento sem CNPJ no cabeçalho, essa chave é o CNPJ da PRIMEIRA
  loja do pedido, não um "CNPJ do cliente" canônico. Quem GRAVA a decisão
  lembrada tem que usar exatamente essa mesma chave, ou a decisão nunca
  mais é reencontrada.
- **`explicacao` mistura slug e nome legível.** Os degraus documento e
  memória só têm o SLUG à mão (`env_por_cnpj`/`memoria` devolvem
  `str | None`, sem nome); o degrau histórico mostra o NOME porque
  `HistoricoAmbiente` já carrega os dois. Resolver slug → nome de forma
  consistente pro operador (ex.: via `environments_repo`) é trabalho da
  camada que renderiza a `Decisao`, não deste módulo.

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
# extra na explicação — ver `Deps.historico_amplo` e `deps_padrao()`.
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
    # Opcional. `None` (default) = enriquecimento desligado, zero I/O extra —
    # ver docstring do módulo. Quando presente, é chamado só depois que o
    # degrau 2 já decidiu, nunca para decidir.
    historico_amplo: Callable[[Sequence[str | None]], tuple[HistoricoAmbiente, ...]] | None = None


def _fmt(cnpj: str) -> str:
    if len(cnpj) != 14:
        return cnpj
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def cnpjs_do_pedido(order: Order) -> list[str]:
    """CNPJs que identificam o comprador: o do cabeçalho e os das lojas.

    Ordem preservada e sem repetição. As lojas entram porque numa planilha de
    desmembramento a identidade do comprador está nas colunas, não no
    cabeçalho — ver `DesmembramentoXlsParser`.

    `resultado[0]`, quando existe, é a chave usada por `deps.memoria` em
    `ambiente_para` — ver contrato no docstring do módulo.
    """
    vistos: list[str] = []
    for bruto in [order.header.customer_cnpj, *(i.delivery_cnpj for i in order.items)]:
        c = cnpj_digits(bruto)
        if c and c not in vistos:
            vistos.append(c)
    return vistos


def _explicar_fora_da_janela(
    cnpjs: Sequence[str],
    hist_12: Sequence[HistoricoAmbiente],
    historico_amplo: Callable[[Sequence[str | None]], tuple[HistoricoAmbiente, ...]] | None,
) -> str:
    """Trecho "atenção, ..." quando a janela ampla (`historico_amplo`) revela
    compra em outro ambiente que a janela de 12 meses (a de decisão) já não
    via mais. Devolve "" — sem chamar `historico_amplo` — quando ele é
    `None`; e devolve "" também se não houver nada a mostrar ou se a
    consulta falhar. Nunca levanta."""
    if historico_amplo is None:
        return ""
    vistos = {h.env_slug for h in hist_12 if h.pedidos > 0}
    try:
        ampla = historico_amplo(cnpjs)
    except Exception as exc:  # noqa: BLE001 — enriquecimento não pode derrubar o pedido
        logger.warning("routing.ambiente.janela_ampla_falhou erro={!r}", exc)
        return ""
    fora = [h for h in ampla if h.pedidos > 0 and h.env_slug not in vistos]
    if not fora:
        return ""
    partes = ", ".join(f"{h.env_nome} ({h.pedidos} pedido(s), até {h.ultimo_em})" for h in fora)
    return f"; atenção, este cliente também já comprou de {partes}"


def _motivo_historico_nao_resolveu(cnpjs: Sequence[str], hist: Sequence[HistoricoAmbiente]) -> str:
    """Distingue, pro operador que vai ter que escolher a empresa, três
    situações que pedem ações diferentes: pedido sem CNPJ nenhum (não há o
    que consultar), Firebird mudo (chamar o suporte / esperar a VPN
    voltar), e cliente novo sem histórico (nada a fazer, é esperado)."""
    if not cnpjs:
        return "o pedido não traz CNPJ de cliente nem de fornecedor"
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
            # `env` é o SLUG — `env_por_cnpj` (Callable[[str], str | None]) não
            # carrega o nome legível. O degrau 2 mostra `env_nome` porque
            # `HistoricoAmbiente` já traz os dois; aqui e no degrau 3 (memória)
            # só o slug está à mão. Resolver slug → nome pro operador é
            # responsabilidade de quem renderiza a `Decisao` (o wiring), não
            # deste módulo — ver docstring do módulo.
            return Decisao(
                env_slug=env,
                degrau="documento",
                # "Fornecedor X" afirmaria POSIÇÃO (que o CNPJ veio do campo
                # fornecedor do documento) — `detectar_fornecedor` garante só
                # PRESENÇA (único CNPJ de ambiente no texto, papel nenhum
                # verificado). Ver docstring de `app/routing/documento.py`.
                explicacao=f"CNPJ {_fmt(fornecedor)} identificado no pedido → ambiente {env}",
                divergiu_de=lembrado if lembrado and lembrado != env else None,
            )

    # Degrau 2 — o histórico, e só quando ele é inequívoco.
    hist = deps.historico(cnpjs) if cnpjs else ()
    do_historico = _resolver_historico(hist)
    if do_historico:
        h = next(x for x in hist if x.env_slug == do_historico)
        extra = _explicar_fora_da_janela(cnpjs, hist, deps.historico_amplo)
        ultimo = f", último em {h.ultimo_em}" if h.ultimo_em else ""
        return Decisao(
            env_slug=do_historico,
            degrau="historico",
            explicacao=f"Histórico: {h.pedidos} pedido(s) em {h.env_nome}{ultimo}{extra}",
            divergiu_de=lembrado if lembrado and lembrado != do_historico else None,
            historico=tuple(hist),
        )

    # Degrau 3 — a memória.
    if lembrado:
        # `lembrado` também é slug, pelo mesmo motivo do degrau 1 — ver
        # comentário lá.
        return Decisao(
            env_slug=lembrado,
            degrau="memoria",
            explicacao=f"Escolha registrada para este cliente → ambiente {lembrado}",
            historico=tuple(hist),
        )

    # Degrau 4 — sem resposta. É um resultado, não uma falha.
    motivo = _motivo_historico_nao_resolveu(cnpjs, hist)
    return Decisao(
        env_slug=None,
        degrau="perguntar",
        explicacao=f"Ambiente não resolvido: {motivo}",
        historico=tuple(hist),
    )


def decidir(order: Order, *, origem: str) -> tuple[str, Decisao | None]:
    """(modo, Decisao|None) — o wiring padrão de `ambiente_para` pros dois
    lugares que hoje ligam o roteamento: `app/web/server.py` (commit do
    preview, um humano na tela) e `app/worker/jobs/scan_environments.py`
    (watcher, sem humano nenhum). Extraído pra cá porque as duas cópias
    eram idênticas exceto pelo prefixo do log — e os dois chamadores já
    importam este módulo, que é o domínio comum dos dois.

    Em `'desligado'` o roteador nem é chamado. Fora disso, uma exceção
    vinda da escada (`ambiente_para`/`deps_padrao` — Firebird ou SQLite fora
    do ar) NUNCA propaga daqui pra fora; blindar é decisão desta função, não
    do módulo puro acima:

    - `'observando'`: vira log e `decisao=None` — do ponto de vista de quem
      chama é como se o modo fosse `'desligado'` PARA ESTE PEDIDO. Evidência
      é importante, o pedido é mais.
    - `'ligado'`: vira um degrau `'perguntar'` com a falha explicada. Nunca
      cai de volta pro ambiente "de sempre" (cookie ou pasta varrida) em
      silêncio — cada chamador já trata `decisao.resolveu is False` como
      "preciso de uma resposta" (o web pergunta ao operador; o worker retém
      o arquivo). Por este contrato, quando `modo == LIGADO` o retorno
      NUNCA é `(modo, None)` — só varia entre uma `Decisao` resolvida e uma
      não resolvida.

    `origem` é só o prefixo do evento de log (`"web"`/`"scan"`), pra achar
    de onde veio a falha no log agregado.
    """
    from app.persistence import roteamento_repo
    from app.utils.logger import logger

    modo = roteamento_repo.modo()
    if modo == roteamento_repo.DESLIGADO:
        return modo, None
    try:
        return modo, ambiente_para(order, deps_padrao())
    except Exception as exc:  # noqa: BLE001 — roteador não pode derrubar o chamador nem decidir errado em silêncio
        logger.warning("{}.decidir_falhou modo={} erro={!r}", origem, modo, exc)
        if modo == roteamento_repo.LIGADO:
            return modo, Decisao(
                env_slug=None,
                degrau="perguntar",
                explicacao=f"Ambiente não resolvido: falha ao consultar o roteador ({exc})",
            )
        return modo, None


def deps_padrao() -> Deps:
    """As três leituras reais. Import local: mantém o módulo puro para teste."""
    from app.persistence import decisao_ambiente_repo, environments_repo
    from app.routing import historico as hist_mod

    def env_por_cnpj(cnpj: str) -> str | None:
        env = environments_repo.find_by_cnpj(cnpj)
        return env["slug"] if env else None

    def memoria(cnpj: str) -> str | None:
        d = decisao_ambiente_repo.lembrada(cnpj)
        if not d:
            return None
        # Decisão que aponta pra ambiente desativado não é decisão usável —
        # sem este filtro o degrau 3 "resolvia" pra um ambiente que ninguém
        # pode mais escolher, e o commit em 'ligado' travava num 412 sem
        # seletor na tela. Cai pra 'perguntar' em vez disso.
        env = environments_repo.get_by_slug(d["env_slug"])
        return d["env_slug"] if env and env.get("is_active") else None

    return Deps(
        env_por_cnpj=env_por_cnpj,
        historico=lambda cnpjs: hist_mod.consultar(cnpjs),
        memoria=memoria,
        historico_amplo=lambda cnpjs: hist_mod.consultar(cnpjs, meses=_MESES_JANELA_AMPLA),
    )
