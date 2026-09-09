"""Degrau 3 do roteamento: a escolha que um humano já fez para este cliente.

Tabela transversal (`app_shared.db`) — a decisão de roteamento acontece **antes**
de existir ambiente ativo, então ela não pode morar em `app_state_<slug>.db`.

Três coisas que este módulo NÃO é:

- não é cadastro curado: ninguém precisa preencher, ninguém precisa revisar;
- não é fonte de verdade: perde para o documento e para o histórico, sempre;
- não é pré-requisito: vazia, o Portal funciona igual — só pergunta mais.

Ela cresce com clientes novos, uma vez cada, e vira redundante assim que o
pedido entra no Fire (aí o histórico responde). O que sobra é auditoria:
"quem decidiu isso, quando" em uma linha.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.erp.cnpj import cnpj_digits
from app.persistence import router

_FIELDS = (
    "cnpj_cliente",
    "env_slug",
    "decidido_por",
    "decidido_em",
    "divergiu_em",
    "divergiu_de",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lembrar(*, cnpj_cliente: str, env_slug: str, por: str) -> None:
    """Grava (ou substitui) a decisão para este CNPJ, com autor e data."""
    digits = cnpj_digits(cnpj_cliente)
    if not digits:
        return
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO decisao_ambiente
                   (cnpj_cliente, env_slug, decidido_por, decidido_em)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cnpj_cliente) DO UPDATE SET
                   env_slug = excluded.env_slug,
                   decidido_por = excluded.decidido_por,
                   decidido_em = excluded.decidido_em,
                   divergiu_em = NULL,
                   divergiu_de = NULL""",
            (digits, env_slug, por, _now()),
        )


def lembrada(cnpj_cliente: str) -> dict[str, Any] | None:
    digits = cnpj_digits(cnpj_cliente)
    if not digits:
        return None
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM decisao_ambiente WHERE cnpj_cliente = ?", (digits,)
        ).fetchone()
    return {k: row[k] for k in _FIELDS} if row else None


def marcar_divergencia(*, cnpj_cliente: str, de: str) -> None:
    """Um degrau mais forte contradisse a memória. Marca, não reescreve.

    Reescrever esconderia que houve conflito; o valor lembrado continua sendo o
    julgamento humano registrado, e a marca é o que faz alguém ir olhar.
    """
    digits = cnpj_digits(cnpj_cliente)
    if not digits:
        return
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE decisao_ambiente SET divergiu_em = ?, divergiu_de = ? WHERE cnpj_cliente = ?",
            (_now(), de, digits),
        )


def listar(limit: int = 200) -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM decisao_ambiente ORDER BY decidido_em DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [{k: r[k] for k in _FIELDS} for r in rows]
