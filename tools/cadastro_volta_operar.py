"""Ferramenta do operador para as linhas `incerto` e `orfao` do livro da volta de cadastro.

NUNCA escreve no ERP nem no Flow: só o livro (SQLite do ambiente) muda, e o resultado
adotado segue para o Flow pelo ciclo normal (`reenviar_pendencias`). Sem `--confirmar`,
toda ação só mostra o que faria.

- `adotar` prova PRESENÇA: lê o ERP por `connect_somente_leitura` (transação READ ONLY).
  Recusa se o SEQ já é de outra linha do livro, se difere do SEQ reservado, se o produto
  foi cadastrado antes da reserva ou não existe. Divergência de campo/composição exige
  `--aceito-divergencia`.
- `liberar` prova AUSÊNCIA, pelo mesmo caminho da recuperação: `connect_leitura_bloqueante`
  (NO_REC_VERSION; um INSERT pendente de outro processo vira conflito de lock em vez de
  "não existe"), recusa motor >= 4.0 ou de versão ilegível, e linha mais nova que
  `IDADE_MINIMA_RECUPERACAO`. Essa conexão abre transação WRITE por exigência do
  NO_REC_VERSION, mas só passa SELECT (guarda `SoSelect`) e sai SEMPRE em rollback: é a
  mesma decisão já aceita na recuperação.
- `fechar-orfao` só depois de o Flow estar resolvido (o produto vinculado, ou a área de
  cadastro decidiu): enquanto `orfao`, o SEQ fica fora da ida do catálogo; fechado, ele
  volta a ser oferecido ao Flow, que criaria outro produto se nada foi vinculado.

    .venv/bin/python tools/cadastro_volta_operar.py <slug> listar
    .venv/bin/python tools/cadastro_volta_operar.py <slug> adotar <cadastroId> <versao> <seq> --operador NOME [--aceito-divergencia] [--confirmar]
    .venv/bin/python tools/cadastro_volta_operar.py <slug> liberar <cadastroId> <versao> --operador NOME [--candidatos-conferidos 4005,4006 | --nenhum-candidato] [--confirmar]
    .venv/bin/python tools/cadastro_volta_operar.py <slug> fechar-orfao <cadastroId> <versao> --operador NOME --nota TEXTO [--confirmar]
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_ESTADOS_LISTA = ("incerto", "orfao", "transitorio", "liberado")
_LIMITE_LISTA = 50
_TODAS = 100_000
_RISCO = (
    "Se algum destes for este cadastro, liberar faz o Portal gravar o produto de novo "
    "no ERP (duplicado)."
)


class _RecusaError(Exception):
    """Mensagem segura da própria ferramenta (nunca carrega texto vindo do ERP)."""


def _positivo(texto: str) -> int:
    valor = int(texto)
    if valor <= 0:
        raise argparse.ArgumentTypeError("deve ser um inteiro positivo")
    return valor


def _nome(texto: str) -> str:
    texto = texto.strip()
    if not texto:
        raise argparse.ArgumentTypeError("não pode ser vazio")
    return texto


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="slug do ambiente (environments)")
    sub = ap.add_subparsers(dest="comando", required=True)

    sub.add_parser("listar", help="incerto, orfao e transitorio/liberado com SEQ")

    def _comum(p: argparse.ArgumentParser, *, nota: bool = False) -> None:
        p.add_argument("cadastro_id")
        p.add_argument("versao", type=_positivo)
        p.add_argument("--operador", required=True, type=_nome)
        if nota:
            p.add_argument("--nota", required=True, type=_nome)
        p.add_argument("--confirmar", action="store_true", help="aplica; sem isso só mostra")

    p = sub.add_parser("adotar", help="adota um SEQ que existe no ERP")
    _comum(p)
    p.add_argument("seq", type=_positivo)
    p.add_argument("--aceito-divergencia", action="store_true",
                   help="aceita campos/composição diferentes do plano")
    p = sub.add_parser("liberar", help="libera para nova tentativa (prova de ausência)")
    _comum(p)
    p.add_argument("--candidatos-conferidos", help="SEQs da janela, separados por vírgula")
    p.add_argument("--nenhum-candidato", action="store_true", help="a janela está vazia")
    _comum(sub.add_parser("fechar-orfao", help="fecha um órfão com nota; só depois de o "
                                        "Flow estar resolvido (o SEQ volta à ida)"), nota=True)
    return ap


@contextmanager
def _livro_real(slug: str):
    from app.persistence import environments_repo, router

    # Antes de abrir: `env_connect` com slug errado criaria um app_state_<slug>.db vazio.
    if environments_repo.get_by_slug(slug) is None:
        raise _RecusaError(f"ambiente {slug!r} não encontrado; nada foi aberto.")
    from app.persistence import cadastro_volta_repo as repo

    with router.env_connect(slug) as conn:
        repo.duravel(conn)  # o que a ferramenta muda no livro não se perde numa queda
        yield conn


def _abrir_real(slug: str, metodo: str):
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    env = environments_repo.get_by_slug(slug)
    if env is None:
        raise _RecusaError("ambiente não encontrado.")
    return getattr(FirebirdConnection(), metodo)(environments_repo.to_fb_config(env))


def _erp_real(slug: str):
    """Leitura do ERP para `adotar` (prova de presença): transação READ ONLY."""
    return _abrir_real(slug, "connect_somente_leitura")


def _erp_bloqueante_real(slug: str):
    """Leitura do ERP para `liberar` (prova de ausência): mesma conexão da recuperação."""
    return _abrir_real(slug, "connect_leitura_bloqueante")


def _erro(msg: str) -> int:
    print(f"ERRO: {msg}", file=sys.stderr)
    return 1


def _listar(livro) -> int:
    from app.persistence import cadastro_volta_repo as repo

    total = 0
    for estado in _ESTADOS_LISTA:
        linhas = repo.listar_por_estado(livro, estado, _TODAS, so_com_seq=estado in ("transitorio", "liberado"))
        for r in linhas[:_LIMITE_LISTA]:
            total += 1
            print(
                f"{estado:<11} {r['cadastro_id']} v{r['versao']} acao={r['acao']} "
                f"seq_reservado={r['seq_reservado']} erp_produto_id={r['erp_produto_id']} "
                f"tentativas={r['tentativas']} alerta={r['alerta']} em={r['atualizado_em']}"
            )
        if len(linhas) > _LIMITE_LISTA:
            print(f"-- {estado}: mostrando {_LIMITE_LISTA} de {len(linhas)}")
    if total == 0:
        print("nenhuma linha incerto, orfao, transitorio ou liberado com SEQ.")
    return 0


def _plano(linha: dict) -> dict:
    try:
        plano = json.loads(linha.get("plano_json") or "{}")
    except ValueError:
        return {}
    return plano if isinstance(plano, dict) else {}


def _pode_ter_gravado(linha: dict) -> bool:
    return linha["estado"] == "incerto" or (
        linha["estado"] in ("liberado", "transitorio") and linha["seq_reservado"] is not None
    )


def _linha_valida(livro, a, verbo: str):
    from app.persistence import cadastro_volta_repo as repo

    linha = repo.buscar(livro, a.cadastro_id, a.versao)
    if linha is None:
        raise _RecusaError("linha não existe no livro.")
    if not _pode_ter_gravado(linha):
        raise _RecusaError(
            f"{verbo} só vale para incerto ou liberado/transitorio com SEQ (estado: {linha['estado']})."
        )
    return linha


def _commit(livro) -> None:
    try:
        livro.commit()
    except Exception as exc:
        try:
            livro.rollback()
        except Exception:  # noqa: BLE001 - o rollback é só tentativa
            pass
        raise _RecusaError(f"falha ao gravar no livro ({type(exc).__name__}); nada foi alterado.") from exc


def _adotar(livro, abrir_erp, slug, a) -> int:
    from app.erp import cadastro_fire
    from app.integrations.flowpcp import cadastro_recuperacao as rec
    from app.integrations.flowpcp.cadastro_resultado import corpo_gravado
    from app.persistence import cadastro_volta_repo as repo

    linha = _linha_valida(livro, a, "adotar")
    if linha["seq_reservado"] is not None and int(linha["seq_reservado"]) != a.seq:
        raise _RecusaError(f"a linha reservou o SEQ {linha['seq_reservado']}, não o {a.seq}.")
    if rec.ligado_a_outra_linha(livro, a.seq, linha):
        raise _RecusaError(f"o SEQ {a.seq} já pertence a outra linha do livro; não é o produto desta.")
    with abrir_erp(slug) as fire:
        fire = rec.SoSelect(fire)
        lido = cadastro_fire.ler_produto(fire, a.seq)
        if lido is None:
            raise _RecusaError(f"o SEQ {a.seq} não existe no ERP; nada a adotar.")
        composicao = cadastro_fire.ler_composicao(fire, a.seq)
    antes = rec.cadastrado_antes_da_reserva(lido, linha)
    if antes is None:
        raise _RecusaError("DATA_CAD do produto ou início da reserva ilegível; não dá para provar que é o nosso.")
    if antes:
        raise _RecusaError(f"o produto do SEQ {a.seq} foi cadastrado antes da reserva; não é o nosso.")
    plano = _plano(linha)
    campos = plano.get("campos") if isinstance(plano.get("campos"), dict) else {}
    divergentes = rec.confere(lido, plano, composicao)
    print(f"{a.cadastro_id} v{a.versao} ({linha['estado']}) -> adotar SEQ {a.seq}")
    print(f"  {'campo':<16} {'ERP (lido)':<40} plano")
    for campo in cadastro_fire.CAMPOS_CONFERENCIA:
        marca = "  <-- DIVERGE" if campo in divergentes else ""
        print(f"  {campo:<16} {lido.get(campo)!s:<40} {campos.get(campo, '-')}{marca}")
    marca = "  <-- DIVERGE" if "COMPOSICAO" in divergentes else ""
    print(f"  {'COMPOSICAO':<16} {sorted(composicao)!s:<40} {plano.get('componentes', '-')}{marca}")
    print("Conferência: " + ("BATE com o plano." if not divergentes else "DIVERGE em " + ", ".join(divergentes) + "."))
    if not a.confirmar:
        print("Confira os campos acima. Nada foi alterado; repita com --confirmar.")
        return 0
    if divergentes and not a.aceito_divergencia:
        raise _RecusaError("há divergência; para adotar mesmo assim use --confirmar --aceito-divergencia.")
    try:
        corpo = corpo_gravado(a.versao, str(a.seq), datetime.now(UTC))
    except ValueError:
        raise _RecusaError(f"SEQ {a.seq} não serve como erpProdutoId (1 a 9 dígitos).") from None
    if not repo.adotar_manual(
        livro, a.cadastro_id, a.versao, seq=a.seq, operador=a.operador, corpo_json=json.dumps(corpo)
    ):
        raise _RecusaError("a linha mudou de estado; nada foi alterado.")
    _commit(livro)
    print("Adotado: gravado_sem_volta. O ciclo normal envia ao Flow.")
    return 0


def _ler_candidatos(texto: str | None) -> list[int] | None:
    if texto is None:
        return None
    try:
        return sorted({int(x) for x in texto.split(",") if x.strip()})
    except ValueError:
        raise _RecusaError("--candidatos-conferidos deve ser uma lista de SEQs separada por vírgula.") from None


def _liberar(livro, abrir_erp_bloqueante, slug, a) -> int:
    from app.erp import cadastro_fire
    from app.integrations.flowpcp import cadastro_recuperacao as rec
    from app.persistence import cadastro_volta_repo as repo

    linha = _linha_valida(livro, a, "liberar")
    iniciado = rec.iniciado(linha)
    if iniciado is None:
        raise _RecusaError("linha sem instante de início legível; não há como provar a ausência.")
    if datetime.now(UTC) - iniciado < rec.IDADE_MINIMA_RECUPERACAO:
        raise _RecusaError("a linha é recente demais: pode haver gravação em curso. Tente mais tarde.")
    seq = linha["seq_reservado"]
    conferidos = _ler_candidatos(a.candidatos_conferidos)
    campos = _plano(linha).get("campos")
    campos = campos if isinstance(campos, dict) else {}

    def _prova(fire) -> tuple[list[dict] | None, str]:
        """Levanta _RecusaError se o produto pode estar no ERP; devolve (candidatos sem
        SEQ, nota da prova)."""
        if seq is not None:
            lido = cadastro_fire.ler_produto(fire, int(seq))
            if lido is None:
                return None, f"Prova: o SEQ {seq} não existe no ERP."
            antes = rec.cadastrado_antes_da_reserva(lido, linha)
            if antes is None:
                raise _RecusaError(f"o SEQ {seq} existe no ERP e a data de cadastro é ilegível; não é seguro liberar.")
            if not antes:
                raise _RecusaError(f"o SEQ {seq} EXISTE no ERP; não é seguro liberar. Use adotar.")
            return None, (f"Prova: o produto no SEQ {seq} foi cadastrado antes da reserva; "
                          "não é este cadastro.")
        janela = rec.janela_da_reserva(linha)
        if janela is None:
            raise _RecusaError("linha sem instante de início legível; não há janela para conferir.")
        return cadastro_fire.candidatos_por_data(fire, de=janela[0], ate=janela[1]), ""

    with abrir_erp_bloqueante(slug) as bruto:
        fire = rec.SoSelect(bruto)
        if rec.motor_sem_bloqueio(slug, fire):
            raise _RecusaError("motor do ERP 4.0+ ou de versão ilegível: a leitura bloqueante não prova ausência.")
        cands, nota = _prova(fire)
        if seq is not None:
            print(nota)
        else:
            plano = _plano(linha)
            print("Plano deste cadastro:")
            for c in cadastro_fire.CAMPOS_CONFERENCIA[1:6]:
                print(f"  {c:<16} {campos.get(c, '-')}")
            if plano.get("componentes"):
                print(f"  {'COMPONENTES':<16} {plano['componentes']}")
            print(f"Sem SEQ reservado: {len(cands)} produto(s) cadastrado(s) na janela da reserva.")
            nome = str(campos.get("DESCRICAO") or "").strip().casefold()
            for c in cands:
                igual = "  <-- MESMO NOME" if nome and str(c["DESCRICAO"] or "").strip().casefold() == nome else ""
                print(f"  SEQ {c['SEQ']} | {c['DESCRICAO']} | grupo {c['GRUPO']} | cad. {c['DATA_CAD']}{igual}")
            print(_RISCO)
        if not a.confirmar:
            print("Nada foi alterado; repita com --confirmar" + (
                " --candidatos-conferidos <SEQs acima>" if cands else " --nenhum-candidato" if cands is not None else "") + ".")
            return 0
        if cands is not None:
            atuais = sorted({int(c["SEQ"]) for c in cands})
            informados = [] if a.nenhum_candidato else conferidos
            if informados is None or (a.nenhum_candidato and conferidos):
                raise _RecusaError("informe --candidatos-conferidos com os SEQs da janela (ou --nenhum-candidato se vazia).")
            if informados != atuais:
                raise _RecusaError("a lista informada difere da janela atual; confira de novo.")
            # Relê na hora de liberar: nada pode ter entrado desde a primeira leitura.
            novos, _ = _prova(fire)
            if sorted({int(c["SEQ"]) for c in novos}) != atuais:
                raise _RecusaError("a janela mudou durante a conferência; nada foi alterado.")
        else:
            _prova(fire)
        if not repo.liberar_manual(livro, a.cadastro_id, a.versao, seq=seq, operador=a.operador):
            raise _RecusaError("a linha mudou de estado; nada foi alterado.")
        _commit(livro)
    print("Liberado: o próximo ciclo pode reservar de novo.")
    return 0


def _fechar_orfao(livro, a) -> int:
    from app.persistence import cadastro_volta_repo as repo

    linha = repo.buscar(livro, a.cadastro_id, a.versao)
    if linha is None:
        raise _RecusaError("linha não existe no livro.")
    if linha["estado"] != "orfao":
        raise _RecusaError(f"fechar-orfao só vale para orfao (estado: {linha['estado']}).")
    print(f"{a.cadastro_id} v{a.versao} (orfao, SEQ {linha['erp_produto_id']}) -> fechar. Nota: {a.nota[:500]}")
    if not a.confirmar:
        print("Nada foi alterado; repita com --confirmar.")
        return 0
    if not repo.fechar_orfao(livro, a.cadastro_id, a.versao, operador=a.operador, nota=a.nota):
        raise _RecusaError("a linha mudou de estado; nada foi alterado.")
    _commit(livro)
    print("Órfão fechado (final).")
    return 0


def main(argv: list[str] | None = None, *, abrir_livro=None, abrir_erp=None, abrir_erp_bloqueante=None) -> int:
    a = _parser().parse_args(argv)
    abrir_livro = abrir_livro or _livro_real
    abrir_erp = abrir_erp or _erp_real
    abrir_erp_bloqueante = abrir_erp_bloqueante or _erp_bloqueante_real
    try:
        with abrir_livro(a.slug) as livro:
            if a.comando == "listar":
                return _listar(livro)
            if a.comando == "adotar":
                return _adotar(livro, abrir_erp, a.slug, a)
            if a.comando == "liberar":
                return _liberar(livro, abrir_erp_bloqueante, a.slug, a)
            return _fechar_orfao(livro, a)
    except _RecusaError as e:
        return _erro(str(e))
    except Exception as e:  # noqa: BLE001 - só o tipo: a mensagem pode ter credencial
        return _erro(f"falha ({type(e).__name__}); nada foi alterado.")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    raise SystemExit(main())
