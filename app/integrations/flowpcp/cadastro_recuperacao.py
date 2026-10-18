"""Recuperação de cadastro interrompido na volta Flow -> Fire (spec §6.3 "Recuperação").

Uma linha `em_andamento` antiga é um processo que caiu no meio da gravação daquele
item (a reserva acontece no início da gravação). Para cada uma, o ERP é LIDO e a
linha sai por uma das portas do livro:

- adotar (`marcar_gravado`): o produto do plano está no ERP; o envio fica para
  `reenviar_pendencias`;
- liberar (`liberar_comprovado`): está comprovado que o nosso produto não está lá;
  o item volta pela fila do Flow;
- `incerto`: não dá para saber; uma pessoa resolve.

Nunca grava no ERP e nunca adota no chute. Liberar um produto que existe faz ele ser
gravado de novo, então "é outro produto" exige prova forte (descrição diferente ou
cadastro anterior à reserva); qualquer outra divergência é `incerto`. A leitura
corre em `connect_leitura_bloqueante`: um INSERT pendente de outro processo vira
conflito de lock, a recuperação para e nada é marcado. Todo SQL passa por `_SoSelect`.

`transitorio` com SEQ (erro que saiu sem prova de rollback) passa pelas mesmas
regras; a retomada e a porta vão no mesmo commit do SQLite.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.erp import cadastro_fire
from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_resultado import corpo_gravado
from app.persistence import cadastro_volta_repo as repo
from app.utils.logger import logger

# Maior que `PRAZO_LOTE` (8 min) com folga: um ciclo vivo para de iniciar itens aos
# 8 min, então uma linha mais velha que isto não é de uma gravação em curso no mesmo
# processo. Outro processo travado na rede ainda pode estar no meio: é a leitura
# bloqueante que impede de ler a ausência dele como prova (trava entre processos: backlog).
IDADE_MINIMA_RECUPERACAO = cv.PRAZO_LOTE + timedelta(minutes=7)
# `DATA_CAD` é a data local do INSERT, segundos depois da reserva (`iniciado_em`, UTC).
# Reserva até esta folga antes da meia-noite local procura também no dia seguinte.
_VIRADA_DO_DIA = timedelta(minutes=10)
_CONFERIDAS = ("DESCRICAO", "UNIDADE", "GRUPO", "CODIGO_EAN13", "CLASSIF_FISCAL")
_CONTAGENS = ("adotado", "liberado", "incerto", "reaplicar")


class _SoSelect:
    """A conexão do ERP vista pela recuperação: só SELECT passa, commit nunca."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _CursorSoSelect(self._conn.cursor())

    def commit(self):
        raise RuntimeError("cadastro_recuperacao: só SELECT é permitido no Fire")


class _CursorSoSelect:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params: tuple = ()):
        if not sql.lstrip().upper().startswith("SELECT"):
            raise RuntimeError("cadastro_recuperacao: só SELECT é permitido no Fire")
        return self._cur.execute(sql, params)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        close = getattr(self._cur, "close", None)
        if close:
            close()


def _txt(valor: object) -> str:
    return str(valor).strip() if valor is not None else ""


def _int(valor: object) -> int | None:
    """Inteiro, ou None (ilegível). `8.0` e `Decimal("8.000")` valem 8; `8.5` não."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float | Decimal):
        try:
            inteiro = int(valor)
        except (ValueError, OverflowError):  # nan, inf
            return None
        return inteiro if valor == inteiro else None
    try:
        return int(_txt(valor))
    except ValueError:
        return None


def _bate(lido: dict, plano: dict) -> bool:
    """As cinco colunas conferidas, aparadas (`None` = vazio); `GRUPO` como inteiro."""
    for coluna in _CONFERIDAS:
        if coluna == "GRUPO":
            grupo = _int(lido.get(coluna))
            if grupo is None or grupo != _int(plano.get(coluna)):
                return False
        elif _txt(lido.get(coluna)) != _txt(plano.get(coluna)):
            return False
    return True


def _outro_produto(lido: dict, plano: dict, iniciado: datetime) -> str | None:
    """Prova forte de que o produto no SEQ não é o nosso, ou None."""
    descricao = lido.get("DESCRICAO")
    if descricao is not None and _txt(descricao).casefold() != _txt(
        plano.get("DESCRICAO")
    ).casefold():
        return "descrição diferente"
    data_cad = lido.get("DATA_CAD")
    if isinstance(data_cad, datetime):
        data_cad = data_cad.date()
    if isinstance(data_cad, date) and data_cad < _dias_locais(iniciado)[0]:
        return "cadastrado antes da reserva"
    return None


def _composicao_do_plano(plano: dict) -> list[tuple[int, float]] | None:
    """Composição que o gravador mandou (sem lista = nenhuma). None = ilegível."""
    componentes = plano.get("componentes")
    if componentes is None:
        return []
    try:
        return sorted((int(c["erpProdutoId"]), round(float(c["quantidade"]), 6))
                      for c in componentes)
    except (TypeError, ValueError, KeyError):
        return None


def _iniciado(linha: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(linha["iniciado_em"]).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _dias_locais(iniciado: datetime) -> list[date]:
    """Datas locais em que o INSERT pode ter caído (mesma conversão do gravador)."""
    return sorted({iniciado.astimezone().date(), (iniciado + _VIRADA_DO_DIA).astimezone().date()})


def _confere_seq(fire, seq: int, plano: dict, iniciado: datetime) -> tuple:
    """-> ("adotar", seq) | ("liberar", motivo, causa) | ("incerto", motivo, [seq])."""
    lido = cadastro_fire.ler_produto(fire, seq)
    if lido is None:
        return ("liberar", f"SEQ {seq} não existe no ERP", "ausente")
    campos = plano.get("campos") or {}
    outro = _outro_produto(lido, campos, iniciado)
    if outro is not None:
        return ("liberar", f"SEQ {seq} tem outro produto no ERP ({outro})", "outro")
    if not _bate(lido, campos):
        return ("incerto", "mesma descrição no ERP com outro campo diferente ou ilegível", [seq])
    esperada = _composicao_do_plano(plano)
    if esperada is None:
        return ("incerto", "composição do plano ilegível", [seq])
    lida = sorted((f, round(q, 6)) for f, q in cadastro_fire.ler_composicao(fire, seq))
    if lida != esperada:
        return ("incerto", "produto igual no ERP com composição diferente", [seq])
    return ("adotar", seq)


def _ligado_a_outra_linha(env_conn, linha: dict, seq: int) -> bool:
    return (
        repo.seq_criado_pelo_portal(env_conn, str(seq))
        or str(seq) in repo.seqs_em_aberto(env_conn)
        or repo.linha_do_produto_com_seq(env_conn, linha["produto_id"], seq) is not None
    )


def _por_nome(fire, env_conn, linha: dict, plano: dict, iniciado: datetime) -> tuple:
    campos = plano["campos"]
    nome, grupo, unidade = campos.get("DESCRICAO"), _int(campos.get("GRUPO")), campos.get("UNIDADE")
    if not isinstance(nome, str) or grupo is None or not isinstance(unidade, str):
        return ("incerto", "plano sem nome, grupo ou unidade para a busca", [])
    candidatos = sorted({
        s
        for dia in _dias_locais(iniciado)
        for s in cadastro_fire.buscar_por_nome(fire, nome=nome, grupo=grupo, unidade=unidade,
                                               data_cad=dia)
    })
    if not candidatos:
        return ("liberar", "nenhum produto com o nome no dia da reserva")
    if len(candidatos) > 1:
        return ("incerto", "mais de um produto com o nome no dia da reserva", candidatos)
    if _ligado_a_outra_linha(env_conn, linha, candidatos[0]):
        return ("incerto", "produto com o nome já ligado a outra linha do livro", candidatos)
    decisao = _confere_seq(fire, candidatos[0], plano, iniciado)
    if decisao[0] != "liberar":
        return decisao
    if decisao[2] == "ausente":
        return ("incerto", "produto com o nome sumiu do ERP entre as consultas", candidatos)
    return ("incerto", "produto com o nome difere do plano", candidatos)  # nunca libera


def _decidir(fire, env_conn, linha: dict, iniciado: datetime | None) -> tuple:
    """-> ("adotar", seq) | ("liberar", motivo, ...) | ("reaplicar", motivo) |
    ("incerto", motivo, candidatos). Só lê o ERP."""
    acao, seq = linha["acao"], linha["seq_reservado"]
    seqs = [seq] if seq is not None else []
    if iniciado is None:
        return ("incerto", "iniciado_em ilegível no livro", seqs)
    if acao in ("atualizar", "inativar"):
        return ("reaplicar", f"{acao} é idempotente")
    if linha["plano_json"] is None and seq is None:
        # Sem plano a linha foi reservada só para responder erro: nada saiu ao ERP.
        return ("liberar", "reserva sem plano e sem SEQ")
    if acao != "criar":
        return ("incerto", "ação desconhecida no livro", seqs)
    try:
        plano = json.loads(linha["plano_json"])
    except (TypeError, ValueError):
        plano = None
    if not isinstance(plano, dict) or not isinstance(plano.get("campos"), dict):
        return ("incerto", "plano ilegível no livro", seqs)
    if seq is not None:
        return _confere_seq(fire, int(seq), plano, iniciado)
    return _por_nome(fire, env_conn, linha, plano, iniciado)


def _a_recuperar(env_conn, agora: datetime) -> list[tuple[dict, datetime | None, bool]]:
    """(linha, iniciado, é transitório). `iniciado_em` ilegível conta como antiga."""
    limite = agora - IDADE_MINIMA_RECUPERACAO
    saida = []
    for transitorio, linhas in ((True, repo.transitorios_com_seq(env_conn)),
                                (False, repo.em_andamento(env_conn))):
        for linha in linhas:
            iniciado = _iniciado(linha)
            if iniciado is None or iniciado <= limite:
                saida.append((linha, iniciado, transitorio))
    return saida


def ha_o_que_recuperar(env_conn, *, agora: datetime) -> bool:
    return bool(_a_recuperar(env_conn, agora))


def _aplicar(slug: str, env_conn, linha: dict, decisao: tuple, agora: datetime,
             contagem: dict, *, transitorio: bool) -> None:
    cid, versao = linha["cadastro_id"], linha["versao"]
    prefixo = f"flowpcp.cadastro_volta env={slug}: {cid} v{versao}: recuperação"
    if decisao[0] == "adotar":
        try:
            corpo = corpo_gravado(versao, str(decisao[1]), agora)
        except ValueError:
            decisao = ("incerto", "código do ERP fora do formato", [decisao[1]])
    # Transitório: a retomada só vale junto com a porta (um commit só).
    ok = not transitorio or repo.retomar_transitorio_com_seq(env_conn, cid, versao)
    if decisao[0] == "adotar":
        chave, texto = "adotado", f"adotou o SEQ {decisao[1]}"
        ok = ok and repo.marcar_gravado(env_conn, cid, versao, erp_produto_id=str(decisao[1]),
                                        corpo_json=json.dumps(corpo))
    elif decisao[0] in ("liberar", "reaplicar"):
        chave = "liberado" if decisao[0] == "liberar" else "reaplicar"
        texto = f"liberou ({decisao[1]})"
        ok = ok and repo.liberar_comprovado(env_conn, cid, versao)
    else:
        chave, texto = "incerto", f"incerto ({decisao[1]}; SEQs candidatos {decisao[2]})"
        ok = ok and repo.marcar_desfecho(env_conn, cid, versao, "incerto", alerta="recuperacao")
    if not ok:
        env_conn.rollback()
        logger.error(f"{prefixo}: {texto} não aplicado no livro")
        return
    env_conn.commit()
    contagem[chave] += 1
    if chave == "incerto":
        logger.error(f"{prefixo}: {texto}; nem grava nem responde, a operação resolve")
    else:
        logger.warning(f"{prefixo}: {texto}")


def recuperar(slug: str, env_conn, fire_conn, *, agora: datetime) -> dict:
    """Resolve as linhas interrompidas há mais de `IDADE_MINIMA_RECUPERACAO`.
    -> contagens `adotado`, `liberado`, `incerto`, `reaplicar`, e `parado` (tipo da
    exceção) quando uma falha de conexão ou de lock para a recuperação neste ciclo:
    as linhas que faltam ficam como estão. Outra falha de leitura vira `incerto` só
    naquela linha."""
    contagem: dict = dict.fromkeys(_CONTAGENS, 0)
    fire = _SoSelect(fire_conn)
    for linha, iniciado, transitorio in _a_recuperar(env_conn, agora):
        cid, versao = linha["cadastro_id"], linha["versao"]
        try:
            decisao = _decidir(fire, env_conn, linha, iniciado)
        except Exception as exc:  # noqa: BLE001 — sem leitura, sem decisão
            tipo = type(exc).__name__
            if cadastro_fire.classificar_erro(exc) in ("conexao", "lock"):
                contagem["parado"] = tipo
                logger.error(f"flowpcp.cadastro_volta env={slug}: {cid} v{versao}: recuperação "
                             f"parada, leitura do ERP falhou ({tipo}); nada marcado")
                break
            seq = linha["seq_reservado"]
            decisao = ("incerto", f"leitura do ERP falhou ({tipo})",
                       [seq] if seq is not None else [])
        _aplicar(slug, env_conn, linha, decisao, agora, contagem, transitorio=transitorio)
    return contagem
