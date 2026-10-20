"""Política da regra comercial de faturamento MM x Nasmar (shared).

Config única (empresa p/ SP e fora de SP, interruptor `ativa`) + redes fixas
com suas chaves (raiz de 8 dígitos ou CNPJ de 14). A regra nasce desligada.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.erp.cnpj import cnpj_digits
from app.persistence import environments_repo, router

if TYPE_CHECKING:
    from app.routing.regra import Politica

_TAMANHO = {"raiz": 8, "cnpj": 14}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ambiente_ativo(slug: str) -> bool:
    env = environments_repo.get_by_slug(slug)
    return bool(env and env.get("is_active"))


def config() -> dict[str, Any] | None:
    with router.shared_connect() as conn:
        r = conn.execute("SELECT * FROM regra_faturamento_config WHERE id = 1").fetchone()
    if not r:
        return None
    return {
        "env_sp": r["env_sp"],
        "env_fora_sp": r["env_fora_sp"],
        "ativa": bool(r["ativa"]),
        "alterado_por": r["alterado_por"],
        "alterado_em": r["alterado_em"],
    }


def listar_redes() -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        redes = conn.execute(
            "SELECT id, nome, env_slug FROM regra_faturamento_rede ORDER BY nome"
        ).fetchall()
        chaves = conn.execute(
            "SELECT rede_id, tipo, chave FROM regra_faturamento_chave ORDER BY tipo, chave"
        ).fetchall()
    por_rede: dict[int, list[dict[str, str]]] = {}
    for c in chaves:
        por_rede.setdefault(c["rede_id"], []).append({"tipo": c["tipo"], "chave": c["chave"]})
    return [
        {"id": r["id"], "nome": r["nome"], "env_slug": r["env_slug"],
         "chaves": por_rede.get(r["id"], [])}
        for r in redes
    ]


def set_config(*, env_sp: str, env_fora_sp: str, ativa: bool, por: str) -> None:
    if env_sp == env_fora_sp:
        raise ValueError("as empresas de SP e de fora de SP precisam ser diferentes")
    for slug in (env_sp, env_fora_sp):
        if not _ambiente_ativo(slug):
            raise ValueError(f"empresa {slug!r} não é um ambiente ativo")
    if ativa:
        for r in listar_redes():
            if not r["chaves"]:
                raise ValueError(f"a rede {r['nome']!r} está sem chave; cadastre antes de ligar")
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO regra_faturamento_config
                   (id, env_sp, env_fora_sp, ativa, alterado_por, alterado_em)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   env_sp = excluded.env_sp,
                   env_fora_sp = excluded.env_fora_sp,
                   ativa = excluded.ativa,
                   alterado_por = excluded.alterado_por,
                   alterado_em = excluded.alterado_em""",
            (env_sp, env_fora_sp, 1 if ativa else 0, por, _now()),
        )


def salvar_rede(
    *,
    nome: str,
    env_slug: str,
    chaves: list[tuple[str, str]],
    por: str,
    rede_id: int | None = None,
) -> int:
    nome = nome.strip()
    if not nome:
        raise ValueError("nome da rede é obrigatório")
    norm: list[tuple[str, str]] = []
    for tipo, chave in chaves:
        if tipo not in _TAMANHO:
            raise ValueError(f"tipo de chave inválido: {tipo!r}")
        digitos = cnpj_digits(chave)
        if len(digitos) != _TAMANHO[tipo]:
            raise ValueError(
                f"chave {tipo} precisa de {_TAMANHO[tipo]} dígitos: {chave!r}"
            )
        norm.append((tipo, digitos))
    with router.shared_connect() as conn:
        for tipo, chave in norm:
            outra = conn.execute(
                """SELECT r.nome FROM regra_faturamento_chave c
                   JOIN regra_faturamento_rede r ON r.id = c.rede_id
                   WHERE c.tipo = ? AND c.chave = ? AND (? IS NULL OR r.id != ?)""",
                (tipo, chave, rede_id, rede_id),
            ).fetchone()
            if outra:
                raise ValueError(f"a chave {chave} já pertence à rede {outra['nome']!r}")
        if rede_id is None:
            cur = conn.execute(
                """INSERT INTO regra_faturamento_rede (nome, env_slug, alterado_por, alterado_em)
                   VALUES (?, ?, ?, ?)""",
                (nome, env_slug, por, _now()),
            )
            rede_id = int(cur.lastrowid)
        else:
            conn.execute(
                """UPDATE regra_faturamento_rede
                   SET nome = ?, env_slug = ?, alterado_por = ?, alterado_em = ?
                   WHERE id = ?""",
                (nome, env_slug, por, _now(), rede_id),
            )
            conn.execute("DELETE FROM regra_faturamento_chave WHERE rede_id = ?", (rede_id,))
        conn.executemany(
            "INSERT INTO regra_faturamento_chave (rede_id, tipo, chave) VALUES (?, ?, ?)",
            [(rede_id, t, c) for t, c in set(norm)],
        )
    return rede_id


def remover_rede(rede_id: int) -> None:
    with router.shared_connect() as conn:
        conn.execute("DELETE FROM regra_faturamento_rede WHERE id = ?", (rede_id,))


def politica() -> Politica | None:
    """Política vigente, ou None quando a regra está desligada/sem config."""
    cfg = config()
    if not cfg or not cfg["ativa"]:
        return None
    from app.routing.regra import Politica, RedeFixa

    redes = tuple(
        RedeFixa(
            nome=r["nome"],
            env_slug=r["env_slug"],
            raizes=frozenset(c["chave"] for c in r["chaves"] if c["tipo"] == "raiz"),
            cnpjs=frozenset(c["chave"] for c in r["chaves"] if c["tipo"] == "cnpj"),
        )
        for r in listar_redes()
    )
    return Politica(env_sp=cfg["env_sp"], env_fora_sp=cfg["env_fora_sp"], redes=redes)
