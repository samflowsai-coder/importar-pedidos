"""Revisão do pedido na aprovação de preço e as decisões do Flow (contrato precos v1, §4 e §6).

Por pedido (`import_id` = `externalId`): `revisao` monotônica + `conteudo_hash`.
O portão (`preco_trava`) garante a revisão; a thread do Flow (Task 7.4) envia
as `pendente_envio`, grava a resposta, lê as decisões e confirma.

Situações da revisão: `pendente_envio` → `enviada` | `recusada_flow` (422);
`substituida` quando nasce outra; `retirada` quando o pedido sai da fila.
Bloqueio local (`bloqueio_local`, corpo que não pode ir ao Flow: fora do
contrato, 413, 400 `payload_invalido`): a revisão segue `pendente_envio` e
vigente, mas sai da fila de envio; só conteúdo novo cria a revisão seguinte,
ou o admin a devolve à fila (`desbloquear_locais`).
A vigente é a de maior número, se ela não estiver `substituida`/`retirada`.

Decisão (`preco_decisao`) é gravada para toda decisão recebida, com a
`confirmacao` que vai ao Flow: `aplicada` (da vigente local) ou `descartada`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import sqlite3

_ATIVAS = ("pendente_envio", "enviada", "recusada_flow")
_ATIVAS_SQL = "('pendente_envio', 'enviada', 'recusada_flow')"
# Revisão que o Flow pode conhecer: o POST saiu (`tentada_em`, resposta talvez
# perdida) ou voltou 201/200 (`enviada_em`).
_CONHECIDA_SQL = "(enviada_em IS NOT NULL OR tentada_em IS NOT NULL)"


def _linha(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["corpo"] = json.loads(d["corpo_json"]) if d.get("corpo_json") else None
    return d


def _piso(conn: sqlite3.Connection, import_id: str) -> int:
    row = conn.execute(
        "SELECT piso FROM preco_revisao_piso WHERE import_id = ?", (import_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def _maior(conn: sqlite3.Connection, import_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(revisao) FROM preco_revisao WHERE import_id = ?", (import_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def vigente(conn: sqlite3.Connection, import_id: str) -> dict[str, Any] | None:
    """A revisão de maior número, se ativa (pendente_envio | enviada | recusada_flow)."""
    row = conn.execute(
        "SELECT * FROM preco_revisao WHERE import_id = ? ORDER BY revisao DESC LIMIT 1",
        (import_id,),
    ).fetchone()
    if row is None or row["situacao"] not in _ATIVAS:
        return None
    return _linha(row)


def _nova(
    conn: sqlite3.Connection,
    import_id: str,
    corpo: dict[str, Any],
    conteudo_hash: str,
    agora: datetime,
) -> int:
    """Dentro de transação aberta: a ativa vira `substituida` e nasce
    `max(maior, piso) + 1` em `pendente_envio`."""
    numero = max(_maior(conn, import_id), _piso(conn, import_id)) + 1
    conn.execute(
        f"UPDATE preco_revisao SET situacao = 'substituida' "  # noqa: S608 — constante
        f"WHERE import_id = ? AND situacao IN {_ATIVAS_SQL}",
        (import_id,),
    )
    conn.execute(
        "INSERT INTO preco_revisao (import_id, revisao, conteudo_hash, corpo_json, situacao,"
        " criado_em) VALUES (?, ?, ?, ?, 'pendente_envio', ?)",
        (
            import_id,
            numero,
            conteudo_hash,
            json.dumps({**corpo, "revisao": numero}, ensure_ascii=False, sort_keys=True),
            agora.isoformat(),
        ),
    )
    return numero


def garantir_revisao(
    conn: sqlite3.Connection,
    import_id: str,
    corpo: dict[str, Any],
    conteudo_hash: str,
    *,
    agora: datetime,
) -> int:
    """Hash igual ao da vigente → a vigente. Diferente, nenhuma vigente, ou
    vigente `recusada_flow` (novo envio depois do 422, mesmo corpo) → nova
    revisão `max(maior, piso) + 1`; a anterior vira `substituida`.
    `corpo` vem sem `revisao` (o número é gravado no corpo da linha)."""
    with conn:
        atual = vigente(conn, import_id)
        if (
            atual is not None
            and atual["conteudo_hash"] == conteudo_hash
            and atual["situacao"] != "recusada_flow"
        ):
            return int(atual["revisao"])
        return _nova(conn, import_id, corpo, conteudo_hash, agora)


def subir_piso(
    conn: sqlite3.Connection,
    import_id: str,
    revisao_vigente_flow: int,
    *,
    agora: datetime | None = None,
) -> int | None:
    """409 do Flow (`revisao_obsoleta` com `revisaoVigente`, ou
    `conteudo_divergente` com a própria revisão): o piso sobe para
    `revisao_vigente_flow` e, se a vigente local não passa dele, ela renasce
    como `piso + 1` com o mesmo corpo. Devolve o número da vigente (None se
    não há vigente)."""
    agora = agora or datetime.now(UTC)
    with conn:
        conn.execute(
            "INSERT INTO preco_revisao_piso (import_id, piso) VALUES (?, ?)"
            " ON CONFLICT(import_id) DO UPDATE SET piso = MAX(piso, excluded.piso)",
            (import_id, int(revisao_vigente_flow)),
        )
        atual = vigente(conn, import_id)
        if atual is None:
            return None
        if int(atual["revisao"]) > _piso(conn, import_id):
            return int(atual["revisao"])
        corpo = {k: v for k, v in atual["corpo"].items() if k != "revisao"}
        return _nova(conn, import_id, corpo, atual["conteudo_hash"], agora)


def pendentes_envio(conn: sqlite3.Connection, limite: int) -> list[dict[str, Any]]:
    """Só a vigente de cada pedido, em `pendente_envio` (pedido retirado não
    tem vigente). Em rotação: pela última tentativa (`tentada_em`) ou, nunca
    tentada, pela criação; quem acabou de falhar vai para o fim."""
    rows = conn.execute(
        """
        SELECT r.* FROM preco_revisao r
         WHERE r.situacao = 'pendente_envio' AND r.bloqueio_local IS NULL
           AND r.revisao = (SELECT MAX(r2.revisao) FROM preco_revisao r2
                             WHERE r2.import_id = r.import_id)
         ORDER BY COALESCE(r.tentada_em, r.criado_em), r.import_id
         LIMIT ?
        """,
        (limite,),
    ).fetchall()
    return [_linha(r) for r in rows]


def marcar_tentada(conn: sqlite3.Connection, import_id: str, revisao: int, *, em: datetime) -> bool:
    """Chamar logo ANTES do POST da rota (b). Marca a revisão como possivelmente
    conhecida pelo Flow (resposta perdida, timeout) e guarda a hora da última
    tentativa (rotação da fila). False = ela não está mais em `pendente_envio`
    (substituída ou retirada): não enviar."""
    with conn:
        cur = conn.execute(
            "UPDATE preco_revisao SET tentada_em = ?"
            " WHERE import_id = ? AND revisao = ? AND situacao = 'pendente_envio'",
            (em.isoformat(), import_id, revisao),
        )
    return cur.rowcount > 0


def _garantir_retirada_da_conhecida(conn: sqlite3.Connection, import_id: str) -> None:
    """Dentro de transação: pedido retirado (última revisão `retirada`, nenhuma
    ativa) cuja última revisão conhecida pelo Flow ainda não está `retirada`
    (o 201 dela chegou depois do cancel) → ela passa a `retirada`, com o motivo
    da retirada, e entra na fila."""
    ultima = conn.execute(
        "SELECT situacao, retirada_em, retirada_motivo FROM preco_revisao"
        " WHERE import_id = ? ORDER BY revisao DESC LIMIT 1",
        (import_id,),
    ).fetchone()
    if ultima is None or ultima["situacao"] != "retirada":
        return
    conn.execute(
        f"""
        UPDATE preco_revisao SET situacao = 'retirada', retirada_em = ?, retirada_motivo = ?
         WHERE import_id = ? AND situacao = 'substituida'
           AND revisao = (SELECT MAX(revisao) FROM preco_revisao
                           WHERE import_id = ? AND {_CONHECIDA_SQL})
        """,  # noqa: S608 — constante
        (ultima["retirada_em"], ultima["retirada_motivo"], import_id, import_id),
    )


def marcar_enviada(
    conn: sqlite3.Connection,
    import_id: str,
    revisao: int,
    resposta: dict[str, Any] | None,
    *,
    em: datetime,
) -> None:
    """201/200 da rota (b). Registra que o Flow conhece a revisão (`enviada_em`,
    vale para a retirada) mesmo se ela já foi substituída ou retirada aqui,
    mas só muda a situação de `pendente_envio` (o 201 de uma revisão em voo
    não ressuscita a substituída)."""
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET enviada_em = ?, resposta_json = ?"
            " WHERE import_id = ? AND revisao = ? AND enviada_em IS NULL",
            (em.isoformat(), json.dumps(resposta, ensure_ascii=False), import_id, revisao),
        )
        cur = conn.execute(
            "UPDATE preco_revisao SET situacao = 'enviada'"
            " WHERE import_id = ? AND revisao = ? AND situacao = 'pendente_envio'",
            (import_id, revisao),
        )
        if cur.rowcount == 0:
            _garantir_retirada_da_conhecida(conn, import_id)


def marcar_recusa_flow(
    conn: sqlite3.Connection, import_id: str, revisao: int, erro: str, *, em: datetime
) -> None:
    """422 da rota (b): recusa definitiva daquela revisão (só de `pendente_envio`).
    O Flow não guardou essa revisão: `tentada_em` é limpo, para a retirada
    mirar a última revisão que ele de fato tem."""
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET situacao = 'recusada_flow', erro_definitivo = ?,"
            " resposta_json = ?, tentada_em = NULL"
            " WHERE import_id = ? AND revisao = ? AND situacao = 'pendente_envio'",
            (
                erro,
                json.dumps({"erro": erro, "em": em.isoformat()}, ensure_ascii=False),
                import_id,
                revisao,
            ),
        )


def marcar_bloqueio_local(
    conn: sqlite3.Connection, import_id: str, revisao: int, motivo: str, *, em: datetime
) -> None:
    """O corpo desta revisão não pode ir ao Flow (fora do contrato no Portal,
    413 `payload_grande`, 400 `payload_invalido`): sai da fila de envio, o portão
    bloqueia com `motivo` e `garantir_revisao` só cria outra se o conteúdo mudar.
    O Flow não guardou o corpo: `tentada_em` é limpo (só de `pendente_envio`)."""
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET bloqueio_local = ?, resposta_json = ?, tentada_em = NULL"
            " WHERE import_id = ? AND revisao = ? AND situacao = 'pendente_envio'",
            (
                motivo,
                json.dumps({"bloqueio_local": motivo, "em": em.isoformat()}, ensure_ascii=False),
                import_id,
                revisao,
            ),
        )


def desbloquear_locais(conn: sqlite3.Connection) -> int:
    """Admin pediu reenvio (`POST .../preco-trava/desbloquear`): as revisões
    `pendente_envio` com `bloqueio_local` voltam à fila de envio. Corpo que
    continua fora do contrato é bloqueado de novo na próxima volta. Devolve
    quantas saíram do bloqueio."""
    with conn:
        cur = conn.execute(
            "UPDATE preco_revisao SET bloqueio_local = NULL"
            " WHERE situacao = 'pendente_envio' AND bloqueio_local IS NOT NULL"
        )
    return cur.rowcount


def contagem_por_situacao(conn: sqlite3.Connection) -> dict[str, int]:
    """Revisões `pendente_envio` (na fila), `bloqueada_local` e `recusada_flow`."""
    rows = conn.execute(
        """
        SELECT CASE WHEN bloqueio_local IS NOT NULL THEN 'bloqueada_local'
                    ELSE situacao END, COUNT(*)
          FROM preco_revisao
         WHERE situacao IN ('pendente_envio', 'recusada_flow')
         GROUP BY 1
        """
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def gravar_decisao(
    conn: sqlite3.Connection, d: dict[str, Any], *, agora: datetime
) -> Literal["aplicada", "descartada"]:
    """Grava a decisão (formato do contrato, já validada). Idempotente por
    `decisaoId`: repetida devolve a confirmação já gravada. `aplicada` quando
    a revisão é a vigente local; senão `descartada`."""
    with conn:
        row = conn.execute(
            "SELECT confirmacao FROM preco_decisao WHERE decisao_id = ?", (d["decisaoId"],)
        ).fetchone()
        if row is not None:
            return row[0]
        atual = vigente(conn, d["externalId"])
        maior = _maior(conn, d["externalId"])
        if int(d["revisao"]) > maior:
            from app.utils.logger import logger

            # O Portal nunca criou essa revisão: sinal de restore de backup do SQLite.
            logger.warning(
                f"decisão de preço {d['decisaoId']} da revisão {d['revisao']} acima da maior "
                f"revisão local ({maior}) de {d['externalId']}: descartada; o banco do "
                "ambiente pode ter voltado de um backup"
            )
        confirmacao: Literal["aplicada", "descartada"] = (
            "aplicada"
            if atual is not None and int(atual["revisao"]) == int(d["revisao"])
            else "descartada"
        )
        if confirmacao == "aplicada" and not _decisao_bate_com_o_corpo(d, atual["corpo"] or {}):
            from app.utils.logger import logger

            logger.warning(
                f"decisão de preço {d['decisaoId']} não bate com o corpo da revisão "
                f"{d['externalId']}/{d['revisao']} (produto, item ou empresa): descartada"
            )
            confirmacao = "descartada"
        conn.execute(
            "INSERT INTO preco_decisao (decisao_id, import_id, revisao, item_chave, resultado,"
            " preco_aprovado_centavos, motivo_recusa, observacao, decidido_em, recebida_em,"
            " confirmacao) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                d["decisaoId"],
                d["externalId"],
                int(d["revisao"]),
                d["itemChave"],
                d["resultado"],
                d.get("precoAprovadoCentavos"),
                d.get("motivoRecusa"),
                d.get("observacao"),
                d["decididoEm"],
                agora.isoformat(),
                confirmacao,
            ),
        )
    return confirmacao


def _decisao_bate_com_o_corpo(d: dict[str, Any], corpo: dict[str, Any]) -> bool:
    """A decisão é do item e da empresa que a revisão mandou."""
    if d.get("empresaFaturadoraCnpj") != corpo.get("empresaFaturadoraCnpj"):
        return False
    for item in corpo.get("itens") or []:
        if item.get("itemChave") == d.get("itemChave"):
            return item.get("erpProdutoId") == d.get("erpProdutoId")
    return False


def _instante(texto: str) -> datetime:
    return datetime.fromisoformat(texto)


def decisoes_da_vigente(conn: sqlite3.Connection, import_id: str) -> dict[str, dict[str, Any]]:
    """Decisões aplicadas da revisão vigente, por `item_chave`. Duas para a
    mesma chave → vence a de maior `decidido_em` (instante); empate → maior
    `decisao_id` (independe da ordem de chegada)."""
    atual = vigente(conn, import_id)
    if atual is None:
        return {}
    rows = conn.execute(
        "SELECT * FROM preco_decisao WHERE import_id = ? AND revisao = ?"
        " AND confirmacao = 'aplicada'",
        (import_id, int(atual["revisao"])),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        antes = out.get(d["item_chave"])
        if antes is None or (_instante(d["decidido_em"]), d["decisao_id"]) > (
            _instante(antes["decidido_em"]),
            antes["decisao_id"],
        ):
            out[d["item_chave"]] = d
    return out


def confirmacoes_pendentes(conn: sqlite3.Connection, limite: int) -> list[dict[str, Any]]:
    """Decisões ainda não confirmadas ao Flow (`decisao_id`, `confirmacao`)."""
    rows = conn.execute(
        "SELECT * FROM preco_decisao WHERE confirmada_em IS NULL"
        " ORDER BY recebida_em, decisao_id LIMIT ?",
        (limite,),
    ).fetchall()
    return [dict(r) for r in rows]


def marcar_confirmada(conn: sqlite3.Connection, ids: list[str], *, em: datetime) -> None:
    if not ids:
        return
    marcas = ",".join("?" * len(ids))
    with conn:
        conn.execute(
            f"UPDATE preco_decisao SET confirmada_em = ? WHERE decisao_id IN ({marcas})",  # noqa: S608
            (em.isoformat(), *ids),
        )


def retirar(conn: sqlite3.Connection, import_id: str, motivo: str, *, em: datetime) -> None:
    """Pedido sai da fila (cancelado, reimportado, liberado pela tabela depois
    de ir ao Flow). Toda revisão ativa vira `retirada`; a retirada vai ao
    Flow só para a última revisão que ele pode conhecer (`enviada_em` ou
    `tentada_em`), mesmo que ela já estivesse substituída aqui. Nunca tentada
    → nada vai ao Flow.
    Idempotente. A retirada mira a última revisão conhecida pelo Flow; na 7.4,
    409 `revisao_obsoleta` na retirada → reenviar com `details.revisaoVigente`,
    e 200 com `aprovacaoId` nulo (externalId desconhecido) → dar como feita."""
    with conn:
        conn.execute(
            f"UPDATE preco_revisao SET situacao = 'retirada', retirada_em = ?,"  # noqa: S608
            f" retirada_motivo = ? WHERE import_id = ? AND situacao IN {_ATIVAS_SQL}",
            (em.isoformat(), motivo, import_id),
        )
        conn.execute(
            f"""
            UPDATE preco_revisao SET situacao = 'retirada', retirada_em = ?, retirada_motivo = ?
             WHERE import_id = ? AND situacao = 'substituida'
               AND revisao = (SELECT MAX(revisao) FROM preco_revisao
                               WHERE import_id = ? AND {_CONHECIDA_SQL})
            """,  # noqa: S608 — constante
            (em.isoformat(), motivo, import_id, import_id),
        )


def retiradas_pendentes(conn: sqlite3.Connection, limite: int) -> list[dict[str, Any]]:
    """Retiradas a enviar: a última revisão que o Flow pode conhecer, `retirada` e
    ainda não avisada, sem revisão ativa mais nova (pedido reaberto: a revisão
    nova já substitui a velha no Flow). A retirada mira a última revisão
    conhecida pelo Flow; na 7.4, 409 `revisao_obsoleta` → reenviar com
    `details.revisaoVigente`; 200 com `aprovacaoId` nulo → feita.
    As presas (`retirada_presa_em`, `marcar_retirada_presa`) vão para o fim da
    fila, a que falhou há mais tempo primeiro."""
    rows = conn.execute(
        f"""
        SELECT r.* FROM preco_revisao r
         WHERE r.situacao = 'retirada'
           AND (r.enviada_em IS NOT NULL OR r.tentada_em IS NOT NULL)
           AND r.retirada_enviada_em IS NULL
           AND r.revisao = (SELECT MAX(r2.revisao) FROM preco_revisao r2
                             WHERE r2.import_id = r.import_id
                               AND (r2.enviada_em IS NOT NULL OR r2.tentada_em IS NOT NULL))
           AND NOT EXISTS (SELECT 1 FROM preco_revisao r3
                            WHERE r3.import_id = r.import_id AND r3.revisao > r.revisao
                              AND r3.situacao IN {_ATIVAS_SQL})
         ORDER BY r.retirada_presa_em IS NOT NULL, r.retirada_presa_em, r.retirada_em,
                  r.import_id
         LIMIT ?
        """,  # noqa: S608 — constante
        (limite,),
    ).fetchall()
    return [_linha(r) for r in rows]


def marcar_retirada_enviada(
    conn: sqlite3.Connection, import_id: str, revisao: int, *, em: datetime
) -> None:
    """A retirada chegou ao Flow; deixa de ser presa."""
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET retirada_enviada_em = ?, retirada_presa_em = NULL"
            " WHERE import_id = ? AND revisao = ? AND situacao = 'retirada'",
            (em.isoformat(), import_id, revisao),
        )


def marcar_retirada_presa(
    conn: sqlite3.Connection, import_id: str, revisao: int, *, em: datetime
) -> None:
    """A retirada falhou nesta volta: vai para o fim da fila (ordem de
    `retiradas_pendentes`), também depois de reiniciar o processo."""
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET retirada_presa_em = ?"
            " WHERE import_id = ? AND revisao = ? AND situacao = 'retirada'",
            (em.isoformat(), import_id, revisao),
        )


def contar_retiradas_presas(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM preco_revisao WHERE retirada_presa_em IS NOT NULL"
    ).fetchone()
    return int(row[0])


def soltar_retiradas_presas(conn: sqlite3.Connection) -> int:
    """Esquece a ordem das presas (só ordem da fila: nenhuma retirada se perde)."""
    with conn:
        cur = conn.execute(
            "UPDATE preco_revisao SET retirada_presa_em = NULL WHERE retirada_presa_em IS NOT NULL"
        )
    return cur.rowcount


def somar_tentativa(conn: sqlite3.Connection, import_id: str, revisao: int) -> int:
    """Mais uma falha passageira no envio desta revisão; devolve o total."""
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET tentativas = tentativas + 1"
            " WHERE import_id = ? AND revisao = ?",
            (import_id, revisao),
        )
        row = conn.execute(
            "SELECT tentativas FROM preco_revisao WHERE import_id = ? AND revisao = ?",
            (import_id, revisao),
        ).fetchone()
    return int(row[0]) if row else 0


def zerar_tentativas(conn: sqlite3.Connection, import_id: str, revisao: int) -> None:
    with conn:
        conn.execute(
            "UPDATE preco_revisao SET tentativas = 0"
            " WHERE import_id = ? AND revisao = ? AND tentativas <> 0",
            (import_id, revisao),
        )
