"""Degrau 2 do roteamento: onde este cliente já comprou.

Consulta os Firebird de todos os ambientes ativos e devolve, por ambiente,
quantos pedidos aquele cliente tem na janela e qual o mais recente.

Duas regras que não se negociam:

1. **Ambíguo não responde.** Cliente com pedido em dois ambientes dentro da
   janela devolve `None` — não o de maior volume. Medido: 1 cliente em 277
   (spec, fato 15). É o caso que não pode ser chutado, não o caso comum.
2. **Erro não vira ausência.** Se o Firebird de um ambiente não responde, ele
   entra na tupla marcado `indisponivel=True` — nunca como `pedidos=0`
   silencioso. `resolver` recusa responder sempre que houver algum ambiente
   indisponível: esse ambiente mudo poderia ter sido exatamente o que
   tornaria o caso ambíguo, e ambíguo não responde. Contar "não sei" como
   "não tem" transformaria uma VPN caída em roteamento errado com confiança.

A janela de 12 meses é uma escolha, não um fato (spec, "Riscos"): ela é o que
resolve a Beira Rio, que comprou da MM até 05/2025 e migrou para a Nasmar.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from app.erp import queries
from app.erp.cnpj import cnpj_digits
from app.utils.logger import logger


@dataclass(frozen=True)
class HistoricoAmbiente:
    env_slug: str
    env_nome: str
    pedidos: int
    ultimo_em: str | None
    indisponivel: bool = False


def _normalizar_data(valor: object) -> str | None:
    """`ultimo_em` sempre em `YYYY-MM-DD`, venha o driver com `date`, `datetime`
    ou string. `MAX(V.DATA_PEDIDO)` volta `datetime` em alguns casos (mesmo
    problema tratado em `app/erp/fire_reconcile.py::_parse_data`); sem
    normalizar, o formato mudaria conforme o driver — e a Task 9 exibe este
    campo direto na UI.
    """
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    texto = str(valor).strip()
    return texto[:10] if texto else None


def _janela_desde(meses: int, hoje: date | None) -> str:
    """Data de corte da janela, em ISO. `meses` para trás a partir de hoje.

    O clamp do dia não é preciosismo: 31/03 menos 12 meses cai em 31/03, mas
    31/05 menos 3 meses cairia em 31/02 e `date()` levantaria.
    """
    if meses < 0:
        raise ValueError("_janela_desde exige meses >= 0")
    ref = hoje or date.today()
    ano = ref.year - (meses // 12)
    mes = ref.month - (meses % 12)
    if mes <= 0:
        mes += 12
        ano -= 1
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(ref.day, ultimo_dia)).isoformat()


def _contar_no_fire(env: dict, cnpjs: list[str], desde: str) -> tuple[int, str | None]:
    """Consulta real ao Firebird do ambiente. Somente leitura."""
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    cfg = environments_repo.to_fb_config(env)
    if not cfg.get("path"):
        raise ValueError(f"ambiente {env['slug']} sem fb_path")
    sql = queries.count_pedidos_cliente_desde_sql(len(cnpjs))
    with FirebirdConnection().connect_with_config(cfg) as conn:
        cur = conn.cursor()
        cur.execute(sql, (desde, *cnpjs))
        row = cur.fetchone()
    if not row:
        return 0, None
    total = int(row[0] or 0)
    return total, _normalizar_data(row[1])


def consultar(
    cnpjs: Sequence[str | None],
    *,
    meses: int = 12,
    hoje: date | None = None,
    envs: Sequence[dict] | None = None,
    contar: Callable[[dict, list[str], str], tuple[int, str | None]] | None = None,
) -> tuple[HistoricoAmbiente, ...]:
    """Histórico do cliente em cada ambiente ativo, na janela de `meses`.

    `envs` e `contar` são injetáveis para teste; em produção vêm de
    `environments_repo.list_active()` e do Firebird.
    """
    limpos = [c for c in (cnpj_digits(x) for x in cnpjs) if c]
    if not limpos:
        return ()

    if envs is None:
        from app.persistence import environments_repo

        envs = environments_repo.list_active()
    consulta = contar or _contar_no_fire
    desde = _janela_desde(meses, hoje)

    saida: list[HistoricoAmbiente] = []
    for env in envs:
        try:
            pedidos, ultimo = consulta(env, limpos, desde)
        except Exception as exc:  # noqa: BLE001 — ver regra 2 no docstring
            logger.warning("routing.historico.indisponivel env={} erro={!r}", env["slug"], exc)
            saida.append(
                HistoricoAmbiente(
                    env_slug=env["slug"],
                    env_nome=env.get("name") or env["slug"],
                    pedidos=0,
                    ultimo_em=None,
                    indisponivel=True,
                )
            )
            continue
        saida.append(
            HistoricoAmbiente(
                env_slug=env["slug"],
                env_nome=env.get("name") or env["slug"],
                pedidos=int(pedidos or 0),
                ultimo_em=ultimo,
            )
        )
    return tuple(saida)


def resolver(hist: Sequence[HistoricoAmbiente]) -> str | None:
    """O ambiente do histórico, ou `None` se ele não for inequívoco.

    Um ambiente `indisponivel` recusa a resposta antes de olhar a contagem:
    ele poderia ter sido exatamente o segundo pedido que tornaria o caso
    ambíguo, e ambíguo não responde.
    """
    if any(h.indisponivel for h in hist):
        return None
    com_pedido = [h for h in hist if h.pedidos > 0]
    if len(com_pedido) != 1:
        return None
    return com_pedido[0].env_slug
