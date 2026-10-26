"""Consulta por pedido: o que o portal sabe do envio de um pedido ao Flow.

Módulo puro: recebe a conexão do ambiente, só lê (`erp_pedido_estado`,
`erp_eventos_saida`, `erp_envio_lote`, `imports`), não abre conexão nem fala
com o Fire ou o Flow (spec "Envio ao Flow: consulta e Enviar agora" §1).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from app.erp_eventos.modelo import esta_aberto
from app.persistence import erp_envio_lote_repo
from app.utils.janelas_envio import FUSO, proxima_janela

ROTULOS: dict[str, str] = {
    "pedido_visto": "Pedido novo",
    "pedido_excluido": "Pedido excluído",
    "status_alterado": "Status mudou",
    "item_incluido": "Item incluído",
    "item_removido": "Item removido",
    "quantidade_alterada": "Quantidade mudou",
    "preco_alterado": "Preço mudou",
    "data_entrega_alterada": "Data de entrega mudou",
    "faturamento_detectado": "Faturado",
    "lote_fiscal": "Lote fiscal",
}

# Motivos de bloqueio do envio (fonte única; a tela de ambiente passa a ler da API).
BLOQUEIOS: dict[str, str] = {
    "credencial": "o Flow recusou a credencial (401). Salve uma credencial nova.",
    "lote_invalido": "o Flow recusou um lote inteiro (400). Corrija a causa e retome.",
    "unidade_grande": "um pedido tem mais itens do que cabe num lote. Fale com o suporte.",
    "lote_grande_demais": "um lote passou do tamanho máximo. Fale com o suporte.",
}

_DIAS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_SEM_ORIGEM = "origem não registrada"


def texto_bloqueio(bloqueio: str, ultimo_http: int | None) -> str:
    return BLOQUEIOS.get(bloqueio) or f"HTTP {ultimo_http}."


def rotulo(tipo: str) -> str:
    return ROTULOS.get(tipo, tipo)


@dataclass(frozen=True)
class PedidoResumo:
    erp_codigo: int
    pedido_cliente: str | None
    cliente_codigo: int | None
    cliente_cnpj: str | None
    status: str | None
    visto_em: str


@dataclass(frozen=True)
class EventoView:
    tipo: str
    rotulo: str
    detalhe: str
    observado_em: str
    situacao_tela: str  # na_fila | enviado | aceito | duplicado | recusado
    enviado_em: str | None
    origem_lote: str | None
    respondido_em: str | None
    motivo: str | None


@dataclass(frozen=True)
class PedidoDetalhe:
    resumo: PedidoResumo
    itens: int
    aberto: bool
    leitura_em: str | None
    leitura_tipo: str  # ultima_leitura | ultima_mudanca
    origem_pedido: str  # portal | fire
    cliente_nome: str | None
    eventos: list[EventoView]
    frase: str
    retidos: bool


def _resumo(r: sqlite3.Row | tuple) -> PedidoResumo:
    e = json.loads(r[0])
    return PedidoResumo(
        erp_codigo=e["erp_codigo"],
        pedido_cliente=e.get("pedido_cliente"),
        cliente_codigo=e.get("cliente_erp_codigo"),
        cliente_cnpj=e.get("cliente_erp_cnpj"),
        status=e.get("status"),
        visto_em=r[1],
    )


_PC = "UPPER(TRIM(json_extract(estado_json, '$.pedido_cliente'))) = UPPER(TRIM(?))"


def buscar(conn: sqlite3.Connection, base: str, q: str, *, limite: int = 50) -> list[PedidoResumo]:
    q = q.strip()
    if q.isascii() and q.isdigit() and len(q) <= 18:
        onde, params = f"(erp_codigo = ? OR {_PC})", [int(q), q]
    else:
        onde, params = _PC, [q]
    rows = conn.execute(
        f"SELECT estado_json, visto_em FROM erp_pedido_estado WHERE base = ? AND {onde} "
        "ORDER BY visto_em DESC, erp_codigo DESC LIMIT ?",
        [base, *params, limite],
    ).fetchall()
    return [_resumo(r) for r in rows]


# ── formatação (horário de São Paulo) ─────────────────────────────────────


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(FUSO)


def _origem_lote(reg: erp_envio_lote_repo.LoteRegistro) -> str:
    if reg.origem == "janela":
        return f"Janela das {_local(reg.enviado_em).hour}h"
    if reg.origem == "foto":
        return "Antes da foto"
    if reg.origem == "manual":
        return f"Manual · {reg.quem}"
    return _SEM_ORIGEM


def _plural_recusado(n: int) -> str:
    return f"{n} recusado" if n == 1 else f"{n} recusados"


def rotulo_lote(reg: erp_envio_lote_repo.LoteRegistro) -> dict:
    """Origem e resultado de um lote, prontos para a tela (a tela só exibe)."""
    if reg.respondido_em is None:
        res, tom = "Enviando…", "neutro"  # lote em voo: a chamada ao Flow ainda não voltou
    elif reg.http is None:
        res, tom = "Sem resposta (rede)", "erro"
    elif not 200 <= reg.http < 300:
        res, tom = f"HTTP {reg.http}", "erro"
    elif reg.eventos > 0 and not (reg.aceitos or reg.duplicados or reg.rejeitados):
        res, tom = "Sem resultado", "aviso"
    elif reg.rejeitados > 0:
        res, tom = _plural_recusado(reg.rejeitados), "erro"
    else:
        res, tom = "Aceito", "ok"
    return {"origem_rotulo": _origem_lote(reg), "resultado_rotulo": res, "resultado_tom": tom}


def _dm(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.day:02d}/{x.month:02d}"


def _antes_depois(p: dict, fmt=lambda v: v) -> str:
    def f(v):
        return "" if v is None else fmt(v)

    return f"{f(p.get('antes'))} → {f(p.get('depois'))}"


def _detalhe(tipo: str, payload: dict, itens_visto: int) -> str:
    if tipo == "pedido_visto":
        return f"{itens_visto} {'item' if itens_visto == 1 else 'itens'}"
    if tipo in ("status_alterado", "quantidade_alterada", "preco_alterado"):
        return _antes_depois(payload)
    if tipo == "data_entrega_alterada":
        return _antes_depois(payload, _dm)
    return ""


def _situacao_tela(situacao: str, enviado_em: str | None) -> str:
    if situacao == "pendente":
        return "enviado" if enviado_em else "na_fila"
    return "recusado" if situacao == "rejeitado" else situacao


def _proxima_texto(agora: datetime) -> str:
    prox = proxima_janela(agora).astimezone(FUSO)
    dias = (prox.date() - agora.astimezone(FUSO).date()).days
    hora = prox.strftime("%H:%M")
    if dias == 0:
        return f"hoje às {hora}"
    if dias == 1:
        return f"amanhã às {hora}"
    return f"{_DIAS[prox.weekday()]} às {hora}"


def _plural(n: int) -> str:
    return f"{n} evento" if n == 1 else f"{n} eventos"


def _frase(
    eventos: list[EventoView], bloqueio: str | None, ultimo_http: int | None, agora: datetime
) -> str:
    if bloqueio:
        return f"O envio está parado: {texto_bloqueio(bloqueio, ultimo_http)}"
    sem_resposta = sum(1 for e in eventos if e.situacao_tela in ("na_fila", "enviado"))
    recusados = sum(1 for e in eventos if e.situacao_tela == "recusado")
    if recusados:
        frase = (
            f"O Flow recusou {_plural(recusados)}. Recusa não é reenviada: corrija a causa "
            "no Fire; a próxima mudança gera um evento novo."
        )
        if sem_resposta:
            outro = "Outro 1 evento" if sem_resposta == 1 else f"Outros {sem_resposta} eventos"
            frase += f" {outro} ainda na fila."
        return frase
    if sem_resposta:
        return (
            f"O Flow ainda não recebeu {_plural(sem_resposta)}. "
            f"Saem na janela de {_proxima_texto(agora)} ou agora pelo Enviar agora."
        )
    if not eventos:
        return ""
    ultimo = max(
        (e for e in eventos if e.situacao_tela in ("aceito", "duplicado") and e.respondido_em),
        key=lambda e: e.respondido_em,
        default=None,
    )
    sufixo = ""
    if ultimo is not None:
        d = _local(ultimo.respondido_em)
        origem = ultimo.origem_lote or _SEM_ORIGEM
        sufixo = f" Último aceite: {d.day:02d}/{d.month:02d} {d:%H:%M}, {origem}."
    return f"O Flow aceitou todos os eventos deste pedido.{sufixo}"


def detalhe(
    conn: sqlite3.Connection,
    base: str,
    erp_codigo: int,
    *,
    ultima_vigia_em: str | None,
    bloqueio: str | None,
    agora: datetime,
    ultimo_http: int | None = None,
) -> PedidoDetalhe:
    est = conn.execute(
        "SELECT estado_json, visto_em, situacao, atualizado_em FROM erp_pedido_estado "
        "WHERE base = ? AND erp_codigo = ?",
        (base, erp_codigo),
    ).fetchone()
    if est is None:
        raise LookupError(f"pedido {erp_codigo} sem estado na base {base}")
    resumo = _resumo(est)
    linhas = conn.execute(
        "SELECT tipo, unidade, payload_json, observado_em, situacao, motivo, lote_id, "
        "enviado_em, respondido_em FROM erp_eventos_saida WHERE base = ? AND erp_codigo = ? "
        "ORDER BY ordem",
        (base, erp_codigo),
    ).fetchall()
    excluido = est[2] == "excluido" or any(r[0] == "pedido_excluido" for r in linhas)
    aberto = esta_aberto(resumo.status or "") and not excluido
    lotes = erp_envio_lote_repo.origens(conn, sorted({r[6] for r in linhas if r[6]}))
    itens_visto: dict[str, int] = {}
    for r in linhas:
        if r[0] == "item_incluido":
            itens_visto[r[1]] = itens_visto.get(r[1], 0) + 1

    eventos = [
        EventoView(
            tipo=r[0],
            rotulo=rotulo(r[0]),
            detalhe=_detalhe(r[0], json.loads(r[2]), itens_visto.get(r[1], 0)),
            observado_em=r[3],
            situacao_tela=_situacao_tela(r[4], r[7]),
            enviado_em=r[7],
            origem_lote=(
                None if not r[6] else (_origem_lote(lotes[r[6]]) if r[6] in lotes else _SEM_ORIGEM)
            ),
            respondido_em=r[8],
            motivo=r[5],
        )
        for r in linhas
    ]
    frase = _frase(eventos, bloqueio, ultimo_http, agora)

    cli = conn.execute(
        "SELECT customer_name FROM imports WHERE fire_codigo = ? ORDER BY imported_at DESC LIMIT 1",
        (erp_codigo,),
    ).fetchone()
    estado = json.loads(est[0])
    return PedidoDetalhe(
        resumo=resumo,
        itens=len(estado.get("itens", [])),
        aberto=aberto,
        leitura_em=ultima_vigia_em if aberto else est[3],
        leitura_tipo="ultima_leitura" if aberto else "ultima_mudanca",
        origem_pedido="portal" if cli is not None else "fire",
        cliente_nome=(cli[0] or None) if cli is not None else None,
        eventos=eventos,
        frase=frase,
        retidos=not linhas,
    )
