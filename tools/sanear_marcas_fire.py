#!/usr/bin/env python3
"""NÃO rode com --gravar sem o ok explícito do Samuel. Saneia as marcas do Flow
na tabela PRODUTOS_MARCA do Fire.

Compara as marcas ativas do Flow (`MARCAS_FLOW_2026_10_17`) com o Fire. O padrão
é dry-run: só mostra o plano. Com `--gravar` (exige `--backup-servidor`): refaz o
plano, recusa se houver grafia parecida, faz o backup gbak, grava numa única
transação, relê e confere, e só então dá commit.

  python tools/sanear_marcas_fire.py <slug>
  python tools/sanear_marcas_fire.py <slug> --gravar --backup-servidor 'C:\\Fire\\backup\\marcas-20261017.fbk'

Marca nova depois de 17/10/2026 é cadastro manual (decisão do Samuel); as 3 de 23/10 (Kings,
Tennis Station, Centauro) foram autorizadas e entram por `--so`.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# Rodando como script, a raiz do projeto não está no sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.erp import cadastro_fire, queries  # noqa: E402

# Contrato §8.3: as 17 marcas ativas do Flow em 17/10/2026. O apóstrofo de
# "Sam's Club" é U+0027 (confirmado pelo Flow).
MARCAS_FLOW_2026_10_17 = [
    "Act Vitta", "Act Vitta Lisa", "Artwalk", "Authentic Feet", "CompraMais",
    "Kolosh", "Magic Feet", "Molekinha", "Molekinho", "Nasmar", "NBA Store",
    "Netshoes", "Oxer", "Riachuelo", "Sam's Club", "San Bovo", "Studio Z",
]

# Marcas pedidas pelo Flow depois de 17/10 (Kings, Tennis Station e Centauro; mesma grafia do
# Flow). O Samuel autorizou o cadastro em 23/10/2026. "Lojas Silva" e "Polo Match" também existem
# no Flow como marca ativa, mas ficam FORA daqui até ele decidir.
MARCAS_NOVAS_2026_10_23 = ["Kings", "Tennis Station", "Centauro"]

# Tudo o que esta ferramenta pode cadastrar. O padrão (sem --so) é a lista inteira.
MARCAS_DESEJADAS = MARCAS_FLOW_2026_10_17 + MARCAS_NOVAS_2026_10_23


def normalizar(nome: str) -> str:
    """Regra do Flow: trim, espaços colapsados, maiúsculas; acento fica."""
    return re.sub(r"\s+", " ", nome or "").strip().upper()


def chave_solta(nome: str) -> str:
    """`normalizar` sem acento e sem apóstrofo/pontuação/espaço. Só para avisar de
    grafia parecida, nunca para decidir que duas marcas são a mesma."""
    n = unicodedata.normalize("NFKD", normalizar(nome))
    n = "".join(c for c in n if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", n)


def selecionar_marcas(so: str | None) -> list[str]:
    """`--so "A,B"`: só essas marcas, com a grafia da lista (caixa e espaços do argumento não
    importam). Sem `--so`, a lista inteira. Nome fora da lista é erro: esta ferramenta não cadastra
    marca que não foi decidida."""
    if so is None:
        return list(MARCAS_DESEJADAS)
    por_chave = {normalizar(m): m for m in MARCAS_DESEJADAS}
    textos = [" ".join(n.split()) for n in so.split(",") if n.strip()]
    if not textos:
        raise ValueError("--so vazio")
    fora = [t for t in textos if normalizar(t) not in por_chave]
    if fora:
        raise ValueError(f"marca(s) fora da lista desta ferramenta: {', '.join(fora)}")
    return [por_chave[normalizar(t)] for t in textos]


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


_SQL_TRIGGERS = (
    "SELECT TRIM(RDB$TRIGGER_NAME), COALESCE(RDB$TRIGGER_INACTIVE, 0) FROM RDB$TRIGGERS "
    "WHERE RDB$RELATION_NAME = 'PRODUTOS_MARCA' AND COALESCE(RDB$SYSTEM_FLAG, 0) = 0"
)
_SQL_GERADORES = (
    "SELECT TRIM(RDB$GENERATOR_NAME) FROM RDB$GENERATORS "
    "WHERE RDB$SYSTEM_FLAG = 0 AND RDB$GENERATOR_NAME CONTAINING 'MARCA'"
)


def _ler(conn) -> list[tuple[int, str | None]]:
    cur = conn.cursor()
    cur.execute(queries.LIST_PRODUTOS_MARCA)
    return [(int(c), m) for c, m in cur.fetchall()]


def _ler_automatismos(conn) -> tuple[list[tuple[str, bool]], list[str]]:
    """Triggers em PRODUTOS_MARCA (nome, inativo) e geradores com MARCA no
    nome: qualquer um pode dar outro código ao INSERT, e o plano usa MAX+1
    literal. Só trigger ativo recusa; inativo apenas avisa."""
    cur = conn.cursor()
    cur.execute(_SQL_TRIGGERS)
    triggers = [(str(r[0]), bool(r[1])) for r in cur.fetchall()]
    cur.execute(_SQL_GERADORES)
    geradores = [str(r[0]) for r in cur.fetchall()]
    return triggers, geradores


def _imprimir(plano: Plano, out=print) -> None:
    for nome, codigo in plano.ja_existem:
        out(f"  JÁ EXISTE   {codigo:>4}  {nome}")
    for nome, existente, codigo in plano.parecidas:
        out(f"  PARECIDA    {codigo:>4}  {nome}  ~  {existente}")
    for codigo, nome in plano.inserir:
        out(f"  INSERIR     {codigo:>4}  {nome}")
    out(
        f"Resumo: {len(plano.ja_existem)} já existem, "
        f"{len(plano.parecidas)} parecidas, {len(plano.inserir)} a inserir."
    )


def _curta(exc: BaseException, cfg: dict) -> str:
    """`Tipo: mensagem curta`, sem a senha."""
    texto = " ".join(str(exc).split())
    senha = cfg.get("password") or ""
    if senha:
        texto = texto.replace(senha, "***")
    return f"{type(exc).__name__}: {texto[:160]}"


class RollbackFalhouError(RuntimeError):
    """Levantada DENTRO do `with` da conexão para a transação não seguir como
    se tivesse sido desfeita. Se o gerenciador de conexão também falhar no
    rollback dele, a exceção dele substitui esta; por isso `estado` guarda o
    fato à parte, e o relato ao operador não depende da cadeia de exceções."""


def _desfazer_ou_levantar(conn, estado: dict, causa: str) -> None:
    if not cadastro_fire.desfazer(conn):
        estado["incerto"] = causa
        raise RollbackFalhouError(f"rollback falhou; resultado incerto ({causa})")


def _falha_backup(exc: BaseException, cfg: dict, out) -> None:
    """Primeira linha da exceção no cabeçalho; o resto do log inteiro, uma
    linha por vez, com a senha mascarada."""
    senha = cfg.get("password") or ""
    linhas = [ln.replace(senha, "***") if senha else ln for ln in str(exc).splitlines()] or [""]
    out(f"Backup falhou ({type(exc).__name__}: {linhas[0]}). Nada foi gravado.")
    for ln in linhas[1:]:
        out(f"  {ln}")


def executar(
    cfg: dict,
    *,
    gravar: bool,
    backup_servidor: str | None,
    slug: str = "",
    desejadas: list[str] | None = None,
    conn_factory=None,
    backup=None,
    out=print,
) -> int:
    """Roda o saneamento e devolve o código de saída. Dublês entram por
    `conn_factory` (objeto com connect_somente_leitura/connect_escrita_cadastro) e
    `backup` (callable gbak que devolve a linha de prova)."""
    desejadas = MARCAS_DESEJADAS if desejadas is None else desejadas
    if conn_factory is None:
        from app.erp.connection import FirebirdConnection

        conn_factory = FirebirdConnection()
    if gravar and backup is None:
        from app.erp.backup import gbak as backup

    out(f"Ambiente {slug} · {cfg.get('host')}:{cfg.get('port')} · {cfg.get('path')}")
    try:
        with conn_factory.connect_somente_leitura(cfg) as conn:
            plano = planejar(_ler(conn), desejadas)
            triggers, geradores = _ler_automatismos(conn)
    except Exception as exc:  # noqa: BLE001
        out(f"Leitura falhou ({_curta(exc, cfg)}). Nada foi gravado.")
        return 1
    _imprimir(plano, out)
    ativos = [n for n, inativo in triggers if not inativo]
    nomes = [f"{n} (inativo)" if inativo else n for n, inativo in triggers]
    out(f"Triggers em PRODUTOS_MARCA: {', '.join(nomes) or 'nenhum'}")
    out(f"Geradores com MARCA no nome: {', '.join(geradores) or 'nenhum'}")

    if not gravar:
        if plano.parecidas:
            out("Atenção: há grafia parecida; o --gravar vai recusar até a revisão.")
        if ativos or geradores:
            out("Atenção: há trigger/gerador; o --gravar vai recusar.")
        out("Dry-run: nada gravado.")
        return 0
    if plano.parecidas:
        out("Há marcas com grafia parecida no Fire. Revise antes de gravar; nada foi gravado.")
        return 1
    if ativos or geradores:
        out("Há trigger ou gerador ligado a PRODUTOS_MARCA; o código planejado não é garantido. Nada foi gravado.")
        return 1
    if not plano.inserir:
        out("Nada a inserir.")
        return 0

    try:
        prova = backup(cfg, backup_servidor)
    except Exception as exc:  # noqa: BLE001
        _falha_backup(exc, cfg, out)
        return 1
    out(f"Backup concluído em {backup_servidor}. Linha final do gbak: {prova}")

    resultado = 0
    estado: dict = {}
    try:
        with conn_factory.connect_escrita_cadastro(cfg) as conn:
            resultado = _gravar(conn, plano, desejadas, cfg, out, estado)
    except Exception as exc:  # noqa: BLE001
        if "incerto" in estado:
            out(estado["incerto"])
            out("Gravação falhou e o rollback não confirmou. Resultado incerto: rode o dry-run para conferir.")
            return 1
        out(f"Gravação falhou ({_curta(exc, cfg)}). Rode o dry-run para conferir.")
        return 1
    return resultado


def _gravar(conn, plano: Plano, desejadas: list[str], cfg: dict, out, estado: dict) -> int:
    """Uma transação: replaneja, insere, relê, confere, commit."""
    try:
        # Se o banco mudou desde o dry-run, aborta em vez de gravar sobre plano velho.
        if planejar(_ler(conn), desejadas).inserir != plano.inserir:
            _desfazer_ou_levantar(conn, estado, "O banco mudou desde o plano.")
            out("O banco mudou desde o plano. Nada foi gravado; rode de novo.")
            return 1
        cadastro_fire.inserir_marcas(conn, plano.inserir)
        lidas = dict(_ler(conn))
        faltando = [n for c, n in plano.inserir if lidas.get(c) != n]
    except RollbackFalhouError:
        raise
    except Exception as exc:  # noqa: BLE001
        if cadastro_fire.classificar_erro(exc) == "colisao":
            msg = "Outro cadastro tomou o código; rode de novo."
        else:
            msg = f"Falha na gravação ({_curta(exc, cfg)})."
        _desfazer_ou_levantar(conn, estado, msg)
        out(f"{msg} Rollback feito.")
        return 1
    if faltando:
        msg = f"Conferência falhou ({len(faltando)} marca(s) divergentes)."
        _desfazer_ou_levantar(conn, estado, msg)
        out(f"{msg} Rollback feito.")
        return 1
    try:
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        _desfazer_ou_levantar(conn, estado, f"Commit falhou ({_curta(exc, cfg)}).")
        out(f"Commit falhou ({_curta(exc, cfg)}); resultado incerto; rode o dry-run para conferir.")
        return 1
    out(f"Gravado: {len(plano.inserir)} marca(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Saneamento das marcas do Flow no Fire")
    parser.add_argument("slug")
    parser.add_argument("--gravar", action="store_true")
    parser.add_argument("--backup-servidor", help="Caminho do .fbk NO servidor do Fire")
    parser.add_argument(
        "--so",
        help='Só estas marcas, separadas por vírgula (ex.: "Kings,Tennis Station,Centauro")',
    )
    args = parser.parse_args(argv)
    if args.gravar and not args.backup_servidor:
        parser.error("--gravar exige --backup-servidor")
    try:
        desejadas = selecionar_marcas(args.so)
    except ValueError as exc:
        parser.error(str(exc))

    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(args.slug)
    if env is None:
        print(f"Ambiente '{args.slug}' não encontrado.")
        return 2
    return executar(
        environments_repo.to_fb_config(env),
        gravar=args.gravar,
        backup_servidor=args.backup_servidor,
        slug=args.slug,
        desejadas=desejadas,
    )


if __name__ == "__main__":
    raise SystemExit(main())
