"""Recuperação de cadastro interrompido na volta Flow -> Fire (spec §6.3 "Recuperação").

Uma linha `em_andamento` antiga é um processo que caiu no meio da gravação daquele
item (a reserva acontece no início da gravação). Para cada uma, o ERP é LIDO e a
linha sai por uma das portas do livro:

- adotar (`marcar_gravado`): o produto do plano está no ERP; o envio fica para
  `reenviar_pendencias`;
- liberar (`liberar_comprovado`): está comprovado que o nosso produto não está lá;
  o item volta pela fila do Flow;
- `incerto`: não dá para saber; uma pessoa resolve.

Nunca grava no ERP e nunca adota no chute. `transitorio` com SEQ (erro que saiu sem
prova de rollback) volta antes a `em_andamento` e passa pelas mesmas regras.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from app.erp import cadastro_fire
from app.integrations.flowpcp.cadastro_resultado import corpo_gravado
from app.persistence import cadastro_volta_repo as repo
from app.utils.logger import logger

# A linha recém-reservada pelo próprio ciclo não é tocada.
IDADE_MINIMA_RECUPERACAO = timedelta(minutes=2)
# `DATA_CAD` é a data local do INSERT, segundos depois da reserva (`iniciado_em`, UTC).
# Reserva até esta folga antes da meia-noite local procura também no dia seguinte.
_VIRADA_DO_DIA = timedelta(minutes=10)
_CONFERIDAS = ("DESCRICAO", "UNIDADE", "GRUPO", "CODIGO_EAN13", "CLASSIF_FISCAL")
_CONTAGENS = ("adotado", "liberado", "incerto", "reaplicar")


def _txt(valor: object) -> str:
    return str(valor).strip() if valor is not None else ""


def _int(valor: object) -> int | None:
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


def _confere_seq(fire, seq: int, plano: dict) -> tuple:
    lido = cadastro_fire.ler_produto(fire, seq)
    if lido is None:
        return ("liberar", f"SEQ {seq} não existe no ERP")
    if not _bate(lido, plano.get("campos") or {}):
        return ("liberar", f"SEQ {seq} tem outro produto no ERP")
    esperada = _composicao_do_plano(plano)
    if esperada is None:
        return ("incerto", "composição do plano ilegível", [seq])
    lida = sorted((f, round(q, 6)) for f, q in cadastro_fire.ler_composicao(fire, seq))
    if lida != esperada:
        return ("incerto", "produto igual no ERP com composição diferente", [seq])
    return ("adotar", seq)


def _decidir(fire, linha: dict, iniciado: datetime) -> tuple:
    """-> ("adotar", seq) | ("liberar", motivo) | ("reaplicar", motivo) |
    ("incerto", motivo, candidatos). Só lê o ERP."""
    acao, seq = linha["acao"], linha["seq_reservado"]
    if acao in ("atualizar", "inativar"):
        return ("reaplicar", f"{acao} é idempotente")
    if linha["plano_json"] is None and seq is None:
        # Sem plano a linha foi reservada só para responder erro: nada saiu ao ERP.
        return ("liberar", "reserva sem plano e sem SEQ")
    if acao != "criar":
        return ("incerto", "ação desconhecida no livro", [seq] if seq is not None else [])
    try:
        plano = json.loads(linha["plano_json"])
    except (TypeError, ValueError):
        plano = None
    if not isinstance(plano, dict) or not isinstance(plano.get("campos"), dict):
        return ("incerto", "plano ilegível no livro", [seq] if seq is not None else [])
    if seq is not None:
        return _confere_seq(fire, int(seq), plano)
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
    decisao = _confere_seq(fire, candidatos[0], plano)
    if decisao[0] == "liberar":  # o nome está lá: nunca libera por campo diferente
        return ("incerto", "produto com o nome difere do plano", candidatos)
    return decisao


def _antigas(linhas: list[dict], limite: datetime) -> list[tuple[dict, datetime]]:
    saida = []
    for linha in linhas:
        iniciado = _iniciado(linha)
        if iniciado is not None and iniciado <= limite:
            saida.append((linha, iniciado))
    return saida


def ha_o_que_recuperar(env_conn, *, agora: datetime) -> bool:
    limite = agora - IDADE_MINIMA_RECUPERACAO
    return bool(_antigas(repo.em_andamento(env_conn), limite)
                or _antigas(repo.transitorios_com_seq(env_conn), limite))


def _aplicar(slug: str, env_conn, linha: dict, decisao: tuple, agora: datetime, contagem: dict):
    cid, versao = linha["cadastro_id"], linha["versao"]
    prefixo = f"flowpcp.cadastro_volta env={slug}: {cid} v{versao}: recuperação"
    if decisao[0] == "adotar":
        try:
            corpo = corpo_gravado(versao, str(decisao[1]), agora)
        except ValueError:
            decisao = ("incerto", "código do ERP fora do formato", [decisao[1]])
    if decisao[0] == "adotar":
        chave, texto = "adotado", f"adotou o SEQ {decisao[1]}"
        ok = repo.marcar_gravado(env_conn, cid, versao, erp_produto_id=str(decisao[1]),
                                 corpo_json=json.dumps(corpo))
    elif decisao[0] in ("liberar", "reaplicar"):
        chave = "liberado" if decisao[0] == "liberar" else "reaplicar"
        texto = f"liberou ({decisao[1]})"
        ok = repo.liberar_comprovado(env_conn, cid, versao)
    else:
        chave, texto = "incerto", f"incerto ({decisao[1]}; SEQs candidatos {decisao[2]})"
        ok = repo.marcar_desfecho(env_conn, cid, versao, "incerto", alerta="recuperacao")
    env_conn.commit()
    if not ok:
        logger.error(f"{prefixo}: {texto} não aplicado no livro")
        return
    contagem[chave] += 1
    if chave == "incerto":
        logger.error(f"{prefixo}: {texto}; nem grava nem responde, a operação resolve")
    else:
        logger.warning(f"{prefixo}: {texto}")


def recuperar(slug: str, env_conn, fire_conn, *, agora: datetime) -> dict:
    """Resolve as linhas interrompidas há mais de `IDADE_MINIMA_RECUPERACAO`.
    -> contagens `adotado`, `liberado`, `incerto`, `reaplicar`. Leitura do ERP que
    falha para a recuperação neste ciclo (as linhas ficam como estão)."""
    contagem = dict.fromkeys(_CONTAGENS, 0)
    limite = agora - IDADE_MINIMA_RECUPERACAO
    for linha, _ in _antigas(repo.transitorios_com_seq(env_conn), limite):
        ok = repo.retomar_transitorio_com_seq(env_conn, linha["cadastro_id"], linha["versao"])
        env_conn.commit()
        if ok:
            logger.warning(f"flowpcp.cadastro_volta env={slug}: {linha['cadastro_id']} "
                           f"v{linha['versao']}: transitório com SEQ volta para a recuperação")
    for linha, iniciado in _antigas(repo.em_andamento(env_conn), limite):
        try:
            decisao = _decidir(fire_conn, linha, iniciado)
        except Exception as exc:  # noqa: BLE001 — sem leitura, sem decisão
            logger.error(f"flowpcp.cadastro_volta env={slug}: {linha['cadastro_id']} "
                         f"v{linha['versao']}: recuperação parada, leitura do ERP falhou "
                         f"({type(exc).__name__})")
            break
        _aplicar(slug, env_conn, linha, decisao, agora, contagem)
    return contagem
