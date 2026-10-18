#!/usr/bin/env python3
"""NÃO rode com --gravar sem o ok explícito do Samuel. Saneia as marcas do Flow
na tabela PRODUTOS_MARCA do Fire.

Compara as marcas ativas do Flow (`MARCAS_FLOW_2026_10_17`) com o Fire. O padrão
é dry-run: só mostra o plano. Com `--gravar` (exige `--backup-servidor`): refaz o
plano, recusa se houver grafia parecida, faz o backup gbak, grava numa única
transação, relê e confere, e só então dá commit.

  python tools/sanear_marcas_fire.py <slug>
  python tools/sanear_marcas_fire.py <slug> --gravar --backup-servidor 'C:\\Fire\\backup\\marcas-20261017.fbk'

Marca nova depois de 17/10/2026 é cadastro manual (decisão do Samuel).
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Contrato §8.3: as 17 marcas ativas do Flow em 17/10/2026. O apóstrofo de
# "Sam's Club" é U+0027 (confirmado pelo Flow).
MARCAS_FLOW_2026_10_17 = [
    "Act Vitta", "Act Vitta Lisa", "Artwalk", "Authentic Feet", "CompraMais",
    "Kolosh", "Magic Feet", "Molekinha", "Molekinho", "Nasmar", "NBA Store",
    "Netshoes", "Oxer", "Riachuelo", "Sam's Club", "San Bovo", "Studio Z",
]


def normalizar(nome: str) -> str:
    """Regra do Flow: trim, espaços colapsados, maiúsculas; acento fica."""
    return re.sub(r"\s+", " ", nome or "").strip().upper()


def chave_solta(nome: str) -> str:
    """`normalizar` sem acento e sem apóstrofo/pontuação. Só para avisar de
    grafia parecida, nunca para decidir que duas marcas são a mesma."""
    n = unicodedata.normalize("NFKD", normalizar(nome))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", "", n)).strip()


@dataclass
class Plano:
    ja_existem: list[tuple[str, int]] = field(default_factory=list)
    parecidas: list[tuple[str, str, int]] = field(default_factory=list)
    inserir: list[tuple[int, str]] = field(default_factory=list)


def planejar(existentes: list[tuple[int, str | None]], desejadas: list[str]) -> Plano:
    """Classifica cada desejada: já existe (regra do Flow), grafia parecida
    (existe só pela chave solta) ou a inserir com código MAX+1 em sequência."""
    plano = Plano()
    ordenadas = sorted(existentes, key=lambda e: e[0])
    proximo = max((c for c, _ in existentes), default=0) + 1
    for nome in desejadas:
        exata = next((c for c, m in ordenadas if m and normalizar(m) == normalizar(nome)), None)
        if exata is not None:
            plano.ja_existem.append((nome, exata))
            continue
        alvo = chave_solta(nome)
        parecida = next(
            ((m, c) for c, m in ordenadas if m and chave_solta(m) == alvo), None
        )
        if parecida is not None:
            plano.parecidas.append((nome, parecida[0], parecida[1]))
            continue
        plano.inserir.append((proximo, nome))
        proximo += 1
    return plano


_SQL_LER = "SELECT CODIGO, MARCA FROM PRODUTOS_MARCA ORDER BY CODIGO"
_SQL_INSERIR = "INSERT INTO PRODUTOS_MARCA (CODIGO, MARCA) VALUES (?, ?)"


def _ler(conn) -> list[tuple[int, str | None]]:
    cur = conn.cursor()
    cur.execute(_SQL_LER)
    return [(int(c), m) for c, m in cur.fetchall()]


def _imprimir(plano: Plano, out=print) -> None:
    for nome, codigo in plano.ja_existem:
        out(f"  JÁ EXISTE   {codigo:>4}  {nome}")
    for nome, existente, codigo in plano.parecidas:
        out(f"  PARECIDA    {codigo:>4}  {nome}  ~  {existente}")
    for codigo, nome in plano.inserir:
        out(f"  INSERIR     {codigo:>4}  {nome}")


def executar(
    cfg: dict,
    *,
    gravar: bool,
    backup_servidor: str | None,
    desejadas: list[str] | None = None,
    conn_factory=None,
    backup=None,
    out=print,
) -> int:
    """Roda o saneamento e devolve o código de saída. Dublês entram por
    `conn_factory` (objeto com connect_somente_leitura/connect_with_config) e
    `backup` (callable gbak)."""
    desejadas = MARCAS_FLOW_2026_10_17 if desejadas is None else desejadas
    if conn_factory is None:
        from app.erp.connection import FirebirdConnection

        conn_factory = FirebirdConnection()
    if gravar and backup is None:
        from app.erp.backup import gbak as backup

    with conn_factory.connect_somente_leitura(cfg) as conn:
        plano = planejar(_ler(conn), desejadas)
    _imprimir(plano, out)

    if not gravar:
        out("Dry-run: nada gravado.")
        return 0
    if plano.parecidas:
        out("Há marcas com grafia parecida no Fire. Revise antes de gravar; nada foi gravado.")
        return 1
    if not plano.inserir:
        out("Nada a inserir.")
        return 0

    backup(cfg, backup_servidor)  # levanta se falhar: nada é gravado sem backup
    out(f"Backup concluído em {backup_servidor}.")

    with conn_factory.connect_with_config(cfg) as conn:
        # Refaz o plano dentro da transação de escrita: se o banco mudou desde o
        # dry-run, aborta em vez de gravar sobre um plano velho.
        if planejar(_ler(conn), desejadas).inserir != plano.inserir:
            conn.rollback()
            out("O banco mudou desde o plano. Nada foi gravado; rode de novo.")
            return 1
        try:
            cur = conn.cursor()
            for codigo, nome in plano.inserir:
                cur.execute(_SQL_INSERIR, (codigo, nome))
            lidas = {c: m for c, m in _ler(conn)}
            faltando = [n for c, n in plano.inserir if lidas.get(c) != n]
            if faltando:
                conn.rollback()
                out(f"Conferência falhou ({len(faltando)} marca(s) divergentes). Rollback feito.")
                return 1
            conn.commit()
        except Exception as exc:
            conn.rollback()
            out(f"Falha na gravação ({type(exc).__name__}). Rollback feito.")
            return 1
    out(f"Gravado: {len(plano.inserir)} marca(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Saneamento das marcas do Flow no Fire")
    parser.add_argument("slug")
    parser.add_argument("--gravar", action="store_true")
    parser.add_argument("--backup-servidor", help="Caminho do .fbk NO servidor do Fire")
    args = parser.parse_args(argv)
    if args.gravar and not args.backup_servidor:
        parser.error("--gravar exige --backup-servidor")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(args.slug)
    if env is None:
        print(f"Ambiente '{args.slug}' não encontrado.")
        return 2
    return executar(
        environments_repo.to_fb_config(env),
        gravar=args.gravar,
        backup_servidor=args.backup_servidor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
