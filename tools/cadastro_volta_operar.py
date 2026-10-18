"""Ferramenta do operador para as linhas `incerto` e `orfao` do livro da volta de cadastro.

NUNCA escreve no ERP nem no Flow: o ERP é lido por `connect_somente_leitura` e só o
livro (SQLite do ambiente) muda. O resultado adotado segue para o Flow pelo ciclo
normal (`reenviar_pendencias`). Sem `--confirmar`, toda ação só mostra o que faria.

    .venv/bin/python tools/cadastro_volta_operar.py <slug> listar
    .venv/bin/python tools/cadastro_volta_operar.py <slug> adotar <cadastroId> <versao> <seq> --operador NOME [--confirmar]
    .venv/bin/python tools/cadastro_volta_operar.py <slug> liberar <cadastroId> <versao> --operador NOME [--confirmar]
    .venv/bin/python tools/cadastro_volta_operar.py <slug> fechar-orfao <cadastroId> <versao> --operador NOME --nota TEXTO [--confirmar]
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_CAMPOS_ERP = ("SEQ", "DESCRICAO", "UNIDADE", "GRUPO", "CODIGO_EAN13", "CLASSIF_FISCAL", "DATA_CAD")
_ESTADOS_LISTA = ("incerto", "orfao", "transitorio")


def _positivo(texto: str) -> int:
    valor = int(texto)
    if valor <= 0:
        raise argparse.ArgumentTypeError("deve ser um inteiro positivo")
    return valor


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="slug do ambiente (environments)")
    sub = ap.add_subparsers(dest="comando", required=True)

    sub.add_parser("listar", help="incerto, orfao e transitorio com SEQ")

    def _comum(p: argparse.ArgumentParser, *, nota: bool = False) -> None:
        p.add_argument("cadastro_id")
        p.add_argument("versao", type=_positivo)
        p.add_argument("--operador", required=True)
        if nota:
            p.add_argument("--nota", required=True)
        p.add_argument("--confirmar", action="store_true", help="aplica; sem isso só mostra")

    p = sub.add_parser("adotar", help="adota um SEQ que existe no ERP")
    _comum(p)
    p.add_argument("seq", type=_positivo)
    _comum(sub.add_parser("liberar", help="libera para nova tentativa (prova de ausência)"))
    _comum(sub.add_parser("fechar-orfao", help="fecha um órfão com nota"), nota=True)
    return ap


@contextmanager
def _livro_real(slug: str):
    from app.persistence import router

    with router.env_connect(slug) as conn:
        yield conn


@contextmanager
def _erp_real(slug: str):
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(slug)
    if env is None:
        raise LookupError("ambiente não encontrado")
    with FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env)) as fire:
        yield fire


def _erro(msg: str) -> int:
    print(f"ERRO: {msg}", file=sys.stderr)
    return 1


def _listar(livro) -> int:
    from app.persistence import cadastro_volta_repo as repo

    total = 0
    for estado in _ESTADOS_LISTA:
        linhas = repo.listar_por_estado(livro, estado, 200, so_com_seq=estado == "transitorio")
        for r in linhas:
            total += 1
            print(
                f"{estado:<11} {r['cadastro_id']} v{r['versao']} acao={r['acao']} "
                f"seq_reservado={r['seq_reservado']} erp_produto_id={r['erp_produto_id']} "
                f"tentativas={r['tentativas']} alerta={r['alerta']} em={r['atualizado_em']}"
            )
    if total == 0:
        print("nenhuma linha incerto, orfao ou transitorio com SEQ.")
    return 0


def _plano(linha: dict) -> dict:
    try:
        plano = json.loads(linha.get("plano_json") or "{}")
    except ValueError:
        return {}
    campos = plano.get("campos") if isinstance(plano, dict) else None
    return campos if isinstance(campos, dict) else {}


def _pode_ter_gravado(linha: dict) -> bool:
    return linha["estado"] == "incerto" or (
        linha["estado"] in ("liberado", "transitorio") and linha["seq_reservado"] is not None
    )


def _adotar(livro, abrir_erp, slug, a) -> int:
    from app.erp import cadastro_fire
    from app.integrations.flowpcp.cadastro_resultado import corpo_gravado
    from app.persistence import cadastro_volta_repo as repo

    linha = repo.buscar(livro, a.cadastro_id, a.versao)
    if linha is None:
        return _erro("linha não existe no livro.")
    if not _pode_ter_gravado(linha):
        return _erro(f"adotar só vale para incerto ou liberado/transitorio com SEQ (estado: {linha['estado']}).")
    with abrir_erp(slug) as fire:
        lido = cadastro_fire.ler_produto(fire, a.seq)
    if lido is None:
        return _erro(f"o SEQ {a.seq} não existe no ERP; nada a adotar.")
    plano = _plano(linha)
    print(f"{a.cadastro_id} v{a.versao} ({linha['estado']}) -> adotar SEQ {a.seq}")
    print(f"  {'campo':<16} {'ERP (lido)':<40} plano")
    for campo in _CAMPOS_ERP:
        print(f"  {campo:<16} {lido.get(campo)!s:<40} {plano.get(campo, '-')}")
    if not a.confirmar:
        print("Confira os campos acima. Nada foi alterado; repita com --confirmar.")
        return 0
    try:
        corpo = corpo_gravado(a.versao, str(a.seq), datetime.now(UTC))
    except ValueError:
        return _erro(f"SEQ {a.seq} não serve como erpProdutoId (1 a 9 dígitos).")
    if not repo.adotar_manual(
        livro, a.cadastro_id, a.versao, seq=a.seq, operador=a.operador, corpo_json=json.dumps(corpo)
    ):
        return _erro("a linha mudou de estado; nada foi alterado.")
    print("Adotado: gravado_sem_volta. O ciclo normal envia ao Flow.")
    return 0


def _liberar(livro, abrir_erp, slug, a) -> int:
    from app.erp import cadastro_fire
    from app.integrations.flowpcp import cadastro_recuperacao as rec
    from app.persistence import cadastro_volta_repo as repo

    linha = repo.buscar(livro, a.cadastro_id, a.versao)
    if linha is None:
        return _erro("linha não existe no livro.")
    if not _pode_ter_gravado(linha):
        return _erro(f"liberar só vale para incerto ou liberado/transitorio com SEQ (estado: {linha['estado']}).")
    seq = linha["seq_reservado"]
    if seq is not None:
        with abrir_erp(slug) as fire:
            lido = cadastro_fire.ler_produto(fire, int(seq))
        if lido is not None:
            return _erro(f"o SEQ {seq} EXISTE no ERP ({lido.get('DESCRICAO')}); não é seguro liberar. Use adotar.")
        print(f"Prova: o SEQ {seq} não existe no ERP.")
    else:
        iniciado = rec._iniciado(linha)
        if iniciado is None:
            return _erro("linha sem instante de início legível; não há janela para conferir.")
        dias = rec._dias_locais(iniciado)
        with abrir_erp(slug) as fire:
            cands = cadastro_fire.candidatos_por_data(
                fire, de=dias[0] - rec._FOLGA_FUSO, ate=dias[-1] + rec._FOLGA_FUSO
            )
        print(f"Sem SEQ reservado: {len(cands)} produto(s) cadastrado(s) na janela da reserva.")
        for c in cands:
            print(f"  SEQ {c['SEQ']} | {c['DESCRICAO']} | grupo {c['GRUPO']} | cad. {c['DATA_CAD']}")
        print("Confira que nenhum deles é este cadastro.")
    if not a.confirmar:
        print("Nada foi alterado; repita com --confirmar.")
        return 0
    if not repo.liberar_manual(livro, a.cadastro_id, a.versao, operador=a.operador):
        return _erro("a linha mudou de estado; nada foi alterado.")
    print("Liberado: o próximo ciclo pode reservar de novo.")
    return 0


def _fechar_orfao(livro, a) -> int:
    from app.persistence import cadastro_volta_repo as repo

    linha = repo.buscar(livro, a.cadastro_id, a.versao)
    if linha is None:
        return _erro("linha não existe no livro.")
    if linha["estado"] != "orfao":
        return _erro(f"fechar-orfao só vale para orfao (estado: {linha['estado']}).")
    print(f"{a.cadastro_id} v{a.versao} (orfao, SEQ {linha['erp_produto_id']}) -> fechar. Nota: {a.nota[:500]}")
    if not a.confirmar:
        print("Nada foi alterado; repita com --confirmar.")
        return 0
    if not repo.fechar_orfao(livro, a.cadastro_id, a.versao, operador=a.operador, nota=a.nota):
        return _erro("a linha mudou de estado; nada foi alterado.")
    print("Órfão fechado (final).")
    return 0


def main(argv: list[str] | None = None, *, abrir_livro=None, abrir_erp=None) -> int:
    a = _parser().parse_args(argv)
    abrir_livro = abrir_livro or _livro_real
    abrir_erp = abrir_erp or _erp_real
    try:
        with abrir_livro(a.slug) as livro:
            if a.comando == "listar":
                return _listar(livro)
            if a.comando == "adotar":
                return _adotar(livro, abrir_erp, a.slug, a)
            if a.comando == "liberar":
                return _liberar(livro, abrir_erp, a.slug, a)
            return _fechar_orfao(livro, a)
    except Exception as e:  # noqa: BLE001 - nunca despejar a mensagem (pode ter credencial)
        return _erro(f"falha ({type(e).__name__}); nada foi alterado além do já informado.")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
