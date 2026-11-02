"""Recuperação de cadastro interrompido na volta Flow -> Fire (spec §6.3 "Recuperação").

Uma linha `em_andamento` antiga é um processo que caiu no meio da gravação daquele
item (a reserva acontece no início da gravação). Para cada uma, o ERP é LIDO e a
linha sai por uma das portas do livro:

- adotar (`marcar_gravado`): o produto do plano está no ERP, com todos os campos
  conferidos batendo; o envio fica para `reenviar_pendencias`;
- liberar (`liberar_comprovado`): está comprovado que o nosso produto não está lá;
  o item volta pela fila do Flow;
- `incerto`: qualquer outro caso; uma pessoa resolve.

Nunca grava no ERP e nunca adota no chute. Liberar um produto que existe faz ele ser
gravado de novo, então a liberação só vale com prova: o SEQ não existe, ou o produto
no SEQ foi cadastrado com folga antes da reserva, ou (sem SEQ) nenhum produto, de
qualquer grupo, foi cadastrado na janela da reserva. Descrição diferente não prova
nada (renomeado na tela, trigger).

A leitura corre em `connect_leitura_bloqueante` (NO_REC_VERSION): um INSERT pendente
de outro processo vira conflito de lock, a recuperação para e nada é marcado. Por
isso ela só roda em motor < 4.0 (no 4.0+, ReadConsistency faz o read committed
ignorar o NO_REC_VERSION). Todo SQL passa por `SoSelect`.

`transitorio` com SEQ (erro que saiu sem prova de rollback) passa pelas mesmas
regras; a retomada e a porta vão no mesmo commit do SQLite.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.erp import cadastro_fire
from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_resultado import corpo_gravado
from app.observability import metrics
from app.persistence import cadastro_volta_repo as repo
from app.utils.logger import logger

# Maior que `PRAZO_LOTE` (8 min) com folga: um ciclo vivo para de iniciar itens aos
# 8 min, então uma linha mais velha que isto não é de uma gravação em curso no mesmo
# processo. Outro processo travado na rede ainda pode estar no meio: é a leitura
# bloqueante que impede de ler a ausência dele como prova (trava entre processos: backlog).
IDADE_MINIMA_RECUPERACAO = cv.PRAZO_LOTE + timedelta(minutes=7)
# `DATA_CAD` é a data local do INSERT, segundos depois da reserva (`iniciado_em`, UTC).
# Reserva até esta folga antes da meia-noite local conta também o dia seguinte.
_VIRADA_DO_DIA = timedelta(minutes=10)
# O processo que gravou pode estar noutro fuso que o que recupera: um dia de folga
# em volta dos dias locais, tanto na prova de "cadastrado antes" quanto na busca.
_FOLGA_FUSO = timedelta(days=1)
_CONFERIDAS = ("DESCRICAO", "UNIDADE", "GRUPO", "CODIGO_EAN13", "CLASSIF_FISCAL")
_CONTAGENS = ("adotado", "liberado", "incerto", "reaplicar")
_SQL_VERSAO = "SELECT RDB$GET_CONTEXT('SYSTEM', 'ENGINE_VERSION') FROM RDB$DATABASE"
_SO_SELECT = "cadastro_recuperacao: só SELECT é permitido no Fire"
# SELECT que trava ou escreve: `WITH LOCK` bloqueia a linha, `GEN_ID`/`NEXT VALUE FOR`
# avançam gerador. Comentário (`/*`, `--`) nunca é necessário aqui e esconderia SQL.
_SELECT_PROIBIDO = re.compile(
    r"\bWITH\s+LOCK\b|\bGEN_ID\s*\(|\bNEXT\s+VALUE\s+FOR\b|/\*|--", re.IGNORECASE
)
_motor_avisado: set[str] = set()


class SoSelect:
    """A conexão do ERP vista pela recuperação: só SELECT passa, commit nunca."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _CursorSoSelect(self._conn.cursor())

    def commit(self):
        raise RuntimeError(_SO_SELECT)


class _CursorSoSelect:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params: tuple = ()):
        if not sql.lstrip().upper().startswith("SELECT") or _SELECT_PROIBIDO.search(sql):
            raise RuntimeError(_SO_SELECT)
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


def _data(valor: object) -> date | None:
    if isinstance(valor, datetime):
        return valor.date()
    return valor if isinstance(valor, date) else None


def _colunas_divergentes(lido: dict, plano: dict) -> list[str]:
    """As cinco colunas conferidas, aparadas (`None` = vazio); `GRUPO` como inteiro;
    `DESCRICAO` só como texto não vazio. -> as colunas que não batem."""
    ruins = []
    for coluna in _CONFERIDAS:
        if coluna == "DESCRICAO":
            descricao = lido.get(coluna)
            if not isinstance(descricao, str) or not descricao.strip():
                ruins.append(coluna)
                continue
        if coluna == "GRUPO":
            grupo = _int(lido.get(coluna))
            if grupo is None or grupo != _int(plano.get(coluna)):
                ruins.append(coluna)
        elif _txt(lido.get(coluna)) != _txt(plano.get(coluna)):
            ruins.append(coluna)
    return ruins


def _bate(lido: dict, plano: dict) -> bool:
    return not _colunas_divergentes(lido, plano)


def confere(lido: dict, plano: dict, composicao_lida: list[tuple[int, float]] | None) -> list[str]:
    """Divergências entre o produto lido no ERP e o `plano` do livro: nomes das colunas
    e `COMPOSICAO`. `composicao_lida=None` não confere a composição. Lista vazia = bate."""
    campos = plano.get("campos") if isinstance(plano, dict) else None
    ruins = _colunas_divergentes(lido, campos if isinstance(campos, dict) else {})
    if composicao_lida is not None:
        esperada = _composicao_do_plano(plano if isinstance(plano, dict) else {})
        if esperada is None or sorted((f, round(q, 6)) for f, q in composicao_lida) != esperada:
            ruins.append("COMPOSICAO")
    return ruins


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


iniciado = _iniciado  # público: instante (UTC) em que a linha foi reservada, ou None


def _dias_locais(iniciado: datetime) -> list[date]:
    """Datas locais em que o INSERT pode ter caído (mesma conversão do gravador)."""
    return sorted({iniciado.astimezone().date(), (iniciado + _VIRADA_DO_DIA).astimezone().date()})


def janela_da_reserva(linha: dict) -> tuple[date, date] | None:
    """(de, até) em que um produto desta reserva pode ter sido cadastrado, com a folga
    de fuso; None se `iniciado_em` é ilegível. É a janela da busca por nome e a fronteira
    do "cadastrado antes da reserva"."""
    iniciado = _iniciado(linha)
    if iniciado is None:
        return None
    dias = _dias_locais(iniciado)
    return (dias[0] - _FOLGA_FUSO, dias[-1] + _FOLGA_FUSO)


def cadastrado_antes_da_reserva(lido: dict, linha: dict) -> bool | None:
    """True se `DATA_CAD` é anterior à janela (não pode ser o nosso produto); None se
    `DATA_CAD` ou `iniciado_em` é ilegível."""
    janela, data_cad = janela_da_reserva(linha), _data(lido.get("DATA_CAD"))
    if janela is None or data_cad is None:
        return None
    return data_cad < janela[0]


def ligado_a_outra_linha(env_conn, seq: int, linha: dict) -> bool:
    """O SEQ já está em `erp_produto_id` ou `seq_reservado` de OUTRA linha do livro, em
    qualquer estado (inclusive `orfao`/`orfao_fechado`): não é o produto desta."""
    return repo.outra_linha_com_seq(env_conn, seq, linha["cadastro_id"], linha["versao"])


def _adota_se_bate(fire, seq: int, lido: dict, plano: dict, candidatos: list) -> tuple:
    """-> ("adotar", seq) se os campos e a composição batem; senão ("incerto", ...)."""
    if confere(lido, plano, None):
        return ("incerto", "produto no ERP com campo diferente ou ilegível", candidatos)
    if _composicao_do_plano(plano) is None:
        return ("incerto", "composição do plano ilegível", candidatos)
    if confere(lido, plano, cadastro_fire.ler_composicao(fire, seq)):
        return ("incerto", "produto igual no ERP com composição diferente", candidatos)
    return ("adotar", seq)


def _confere_seq(fire, seq: int, plano: dict, linha: dict) -> tuple:
    lido = cadastro_fire.ler_produto(fire, seq)
    if lido is None:
        return ("liberar", f"SEQ {seq} não existe no ERP")
    antes = cadastrado_antes_da_reserva(lido, linha)
    if antes is None:  # o gravador sempre grava DATA_CAD: nulo ou ilegível = linha mexida
        return ("incerto", "DATA_CAD ilegível", [seq])
    if antes:
        return ("liberar", f"SEQ {seq} tem produto cadastrado antes da reserva")
    return _adota_se_bate(fire, seq, lido, plano, [seq])


def _por_nome(fire, env_conn, linha: dict, plano: dict) -> tuple:
    """Sem SEQ (o commit de `marcar_seq` se perdeu): o mais conservador possível. Janela
    vazia libera; um único produto na janela, com o nome exato e tudo batendo, adota;
    qualquer outro caso é `incerto`."""
    campos = plano["campos"]
    nome, grupo = campos.get("DESCRICAO"), _int(campos.get("GRUPO"))
    if not isinstance(nome, str) or not nome.strip() or grupo is None:
        return ("incerto", "plano sem nome ou grupo para a busca", [])
    de, ate = janela_da_reserva(linha)
    linhas = cadastro_fire.candidatos_por_data(fire, de=de, ate=ate)
    if not linhas:
        return ("liberar", "nenhum produto cadastrado na janela da reserva")
    seqs = sorted(r["SEQ"] for r in linhas)
    if len(linhas) > 1:
        return ("incerto", "mais de um produto cadastrado na janela da reserva", seqs)
    (lido,) = linhas
    descricao = lido["DESCRICAO"]
    if not isinstance(descricao, str) or not descricao.strip():
        return ("incerto", "produto sem descrição legível na janela da reserva", seqs)
    if _txt(descricao) != _txt(nome):
        return ("incerto", "produto com outro nome na janela da reserva", seqs)
    if ligado_a_outra_linha(env_conn, lido["SEQ"], linha):
        return ("incerto", "produto com o nome já ligado a outra linha do livro", seqs)
    return _adota_se_bate(fire, int(lido["SEQ"]), lido, plano, seqs)


def _decidir(fire, env_conn, linha: dict, iniciado: datetime | None) -> tuple:
    """-> ("adotar", seq) | ("liberar", motivo) | ("reaplicar", motivo) |
    ("incerto", motivo, candidatos). Só lê o ERP."""
    acao, seq = linha["acao"], linha["seq_reservado"]
    seqs = [seq] if seq is not None else []
    if iniciado is None:
        return ("incerto", "iniciado_em ilegível no livro", seqs)
    if acao in ("atualizar", "inativar", "excluir"):
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
        return _confere_seq(fire, int(seq), plano, linha)
    return _por_nome(fire, env_conn, linha, plano)


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


def motor_sem_bloqueio(slug: str, fire) -> bool:
    """True = a recuperação não roda: motor 4.0+ (ReadConsistency faz o read committed
    ignorar o NO_REC_VERSION), versão ilegível ou leitura da versão que falha."""
    detalhe = ""
    try:
        cur = fire.cursor()
        try:
            cur.execute(_SQL_VERSAO)
            linha = cur.fetchone()
        finally:
            cur.close()
        versao = linha[0] if linha else None
        principal = _int(versao.split(".", 1)[0]) if isinstance(versao, str) else None
        if principal is not None and principal < 4:
            return False
        detalhe = "versão 4.0 ou maior" if principal is not None else "versão ilegível"
    except Exception as exc:  # noqa: BLE001 — sem a versão, a leitura bloqueante não está provada
        detalhe = f"leitura da versão do motor falhou ({type(exc).__name__})"
    if slug not in _motor_avisado:
        _motor_avisado.add(slug)
        logger.error(f"flowpcp.cadastro_volta env={slug}: recuperação desligada "
                     f"(motor_sem_bloqueio: {detalhe}): sem a leitura bloqueante, INSERT "
                     "pendente de outro processo pareceria ausência. As linhas em_andamento ficam "
                     "paradas (nada grava de novo); conferir o SEQ com tools/produto_no_fire.py e "
                     "seguir 'Caminhos manuais' em docs/ai/modules/flowpcp.md")
    return True


def _desfazer_livro(env_conn, prefixo: str) -> None:
    """Rollback do SQLite que nunca sobe ao ciclo (seria lido como ERP indisponível)."""
    try:
        env_conn.rollback()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"{prefixo}: erro no livro ao desfazer ({type(exc).__name__})")


def _aplicar(slug: str, env_conn, linha: dict, decisao: tuple, agora: datetime,
             contagem: dict, *, transitorio: bool) -> None:
    cid, versao = linha["cadastro_id"], linha["versao"]
    prefixo = f"flowpcp.cadastro_volta env={slug}: {cid} v{versao}: recuperação"
    if decisao[0] == "adotar":
        try:
            corpo = corpo_gravado(versao, str(decisao[1]), agora)
        except ValueError:
            decisao = ("incerto", "código do ERP fora do formato", [decisao[1]])
    if decisao[0] == "adotar":
        chave, texto = "adotado", f"adotou o SEQ {decisao[1]}"
    elif decisao[0] in ("liberar", "reaplicar"):
        chave = "liberado" if decisao[0] == "liberar" else "reaplicar"
        texto = f"liberou ({decisao[1]})"
    else:
        chave, texto = "incerto", f"incerto ({decisao[1]}; SEQs candidatos {decisao[2]})"
    try:
        # Transitório: a retomada só vale junto com a porta (um commit só).
        ok = not transitorio or repo.retomar_transitorio_com_seq(env_conn, cid, versao)
        if chave == "adotado":
            ok = ok and repo.marcar_gravado(env_conn, cid, versao,
                                            erp_produto_id=str(decisao[1]),
                                            corpo_json=json.dumps(corpo))
        elif chave in ("liberado", "reaplicar"):
            ok = ok and repo.liberar_comprovado(env_conn, cid, versao)
        else:
            ok = ok and repo.marcar_desfecho(env_conn, cid, versao, "incerto",
                                             alerta="recuperacao")
        if ok:
            env_conn.commit()
    except Exception as exc:  # noqa: BLE001 — a linha volta a como estava
        _desfazer_livro(env_conn, prefixo)
        logger.error(f"{prefixo}: erro no livro ao aplicar '{chave}' ({type(exc).__name__}); "
                     "linha como estava")
        return
    if not ok:
        _desfazer_livro(env_conn, prefixo)
        logger.error(f"{prefixo}: {texto} não aplicado no livro")
        return
    contagem[chave] += 1
    metrics.cadastro_volta_total.labels(
        env=slug, resultado="liberado" if chave == "reaplicar" else chave
    ).inc()
    if chave == "incerto":
        logger.error(f"{prefixo}: {texto}; nem grava nem responde, a operação resolve")
    else:
        logger.warning(f"{prefixo}: {texto}")


def recuperar(slug: str, env_conn, fire_conn, *, agora: datetime) -> dict:
    """Resolve as linhas interrompidas há mais de `IDADE_MINIMA_RECUPERACAO`.
    -> contagens `adotado`, `liberado`, `incerto`, `reaplicar`, e `parado` (tipo da
    exceção) quando uma falha de conexão ou de lock para a recuperação neste ciclo:
    as linhas que faltam ficam como estão. Outra falha de leitura vira `incerto` só
    naquela linha. Motor sem leitura bloqueante: `{"parado": "motor_sem_bloqueio"}`."""
    fire = SoSelect(fire_conn)
    if motor_sem_bloqueio(slug, fire):
        return {"parado": "motor_sem_bloqueio"}
    contagem: dict = dict.fromkeys(_CONTAGENS, 0)
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
