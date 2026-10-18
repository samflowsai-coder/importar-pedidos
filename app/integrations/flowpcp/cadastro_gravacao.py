"""Volta Flow -> Fire nos modos de gravação (`criar`, `criar_e_atualizar`).

Chamado por `cadastro_volta.processar_ambiente`, que já reenviou as pendências e
conferiu a P0. Segue as regras de ouro do contrato P4a (rev. 4):

- `(cadastroId, versao)` no livro decide antes do ERP: o que já tem resultado só
  reenvia o corpo guardado, e só quando a espera venceu; o ERP não é chamado de novo.
- A triagem (validação, livro, 9b, escopo) roda para todos os itens sem abrir o
  ERP; a conexão de escrita só abre se algum item sair "a gravar" (ou, antes do
  GET, se há linha interrompida para `cadastro_recuperacao`, que só lê).
- O SEQ reservado vai ao disco (commit do SQLite) antes do INSERT (`ao_reservar`).
- Resultado incerto (conexão caída depois do comando, livro que não aceitou a
  reserva) fica `em_andamento` para a recuperação; nunca vira erro nem resposta.
- A mensagem ao Flow sai sempre de `mensagem_para_flow`/`mensagem_nome_existe`; o
  texto técnico (sem valor recebido) fica só no livro e no log.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.erp import cadastro_fire
from app.integrations.flowpcp import cadastro_recuperacao, capacidades
from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_resultado import (
    Desfecho,
    corpo_erro,
    corpo_gravado,
    ler_resposta,
    mensagem_nome_existe,
    mensagem_para_flow,
)
from app.integrations.flowpcp.cadastro_schema import CadastroPendente, validar_item
from app.observability import metrics
from app.persistence import cadastro_volta_repo as repo
from app.utils.logger import logger

_ACOES = ("criar", "atualizar", "inativar")
_TIPOS = ("simples", "kit", "pack", "material")
_PENDENTES = ("gravado_sem_volta", "erro_a_enviar")
# Já têm desfecho (ou uma pessoa decide): o item reentregue não é processado.
_PULA_NO_LIVRO = frozenset(
    {"incerto", "orfao", "orfao_fechado", "confirmado", "erro_registrado", "superado"}
)
# atualizar: `ean` e `codigoNfe` null = limpar no ERP; os demais null = não mexer (§2.1).
_LIMPA_COM_NULL = frozenset({"ean", "codigoNfe"})
_FECHA = {
    Desfecho.CONFIRMADO: "confirmado",
    Desfecho.ERRO_REGISTRADO: "erro_registrado",
    Desfecho.TRANSITORIO_REGISTRADO: "transitorio",
    Desfecho.ORFAO: "orfao",
    Desfecho.SUPERADO: "superado",
}
_GRAVADO = {"criar": "criado", "atualizar": "atualizado", "inativar": "inativado"}
_ESPERA_BUG = timedelta(minutes=60)
_ESPERA_CREDENCIAL = timedelta(minutes=5)
_ROTULO_SEGURO = re.compile(r"^[a-z_]{1,40}$")
# validar_item emite códigos sem frase própria: a área de cadastro lê a de contrato.
_FRASE_DO_CODIGO = {"componente_invalido": "contrato_invalido"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _espera(tentativas: int, retry_after: int | None) -> timedelta:
    base = timedelta(minutes=min(2 ** max(tentativas, 0), 60))
    return max(base, timedelta(seconds=retry_after or 0))


@dataclass
class _Lote:
    slug: str
    modo: str
    env_conn: Any
    client: Any
    relogio: Callable[[], datetime]
    resumo: dict = field(default_factory=dict)
    nomes: dict = field(default_factory=dict)
    marcas: dict = field(default_factory=dict)
    # Credencial recusada ou resultado incerto no ERP: nada mais neste ciclo.
    parar: bool = False

    def conta(self, chave: str) -> None:
        self.resumo[chave] = self.resumo.get(chave, 0) + 1

    def log(self, cid: str, versao: int, texto: str) -> str:
        return f"flowpcp.cadastro_volta env={self.slug}: {cid} v{versao}: {texto}"


# --- envio ao Flow ---------------------------------------------------------


def _resposta(status_http: int | None, corpo: dict | None) -> str:
    """Rótulo curto da resposta para o livro: status e o código do Flow, se for vocabulário."""
    if status_http is None:
        return "sem_resposta"
    corpo = corpo if isinstance(corpo, dict) else {}
    rotulo = corpo.get("status") or corpo.get("error")
    if isinstance(rotulo, str) and _ROTULO_SEGURO.fullmatch(rotulo):
        return f"{status_http}:{rotulo}"
    return str(status_http)


def _enviar(lote: _Lote, cid: str, versao: int, corpo: dict, tentativas: int) -> Desfecho:
    """POST do resultado e o estado que a resposta dá (pelo `status`, nunca pela `acao`)."""
    r = lote.client.post_cadastro_resultado(cid, corpo)
    d = ler_resposta(corpo["resultado"], r.status_http, r.corpo)
    conn, agora, resposta = lote.env_conn, lote.relogio(), _resposta(r.status_http, r.corpo)
    if d in _FECHA:
        if not repo.marcar_desfecho(conn, cid, versao, _FECHA[d], resposta=resposta):
            logger.warning(lote.log(cid, versao, f"desfecho {d.value} não aplicado no livro"))
        if d is Desfecho.ORFAO:
            logger.error(lote.log(
                cid, versao,
                f"produto órfão no ERP (código {corpo.get('erpProdutoId')}): o Flow não "
                f"registrou o código ({resposta}); a operação resolve",
            ))
    else:
        alerta = None
        if d is Desfecho.REPETIR:
            ate = agora + _espera(tentativas, r.retry_after)
        elif d is Desfecho.BUG_CORPO:
            ate, alerta = agora + _ESPERA_BUG, "bug_corpo"
            logger.error(lote.log(cid, versao, f"Flow recusou o corpo do resultado ({resposta})"))
        else:  # CREDENCIAL
            ate = agora + _ESPERA_CREDENCIAL
            logger.error(lote.log(cid, versao, "Flow recusou a credencial; envios parados"))
        if not repo.adiar(conn, cid, versao, ate=_iso(ate), alerta=alerta, resposta=resposta):
            logger.warning(lote.log(cid, versao, "adiamento não aplicado no livro"))
    conn.commit()
    resultado = "transitorio" if d is Desfecho.TRANSITORIO_REGISTRADO else d.value
    metrics.cadastro_volta_total.labels(env=lote.slug, resultado=resultado).inc()
    if d is Desfecho.CREDENCIAL:
        lote.parar = True
    return d


def _vencida(lote: _Lote, linha: dict) -> bool:
    proxima = linha.get("proxima_tentativa_em")
    return proxima is None or proxima <= _iso(lote.relogio())


def _reenviar_linha(lote: _Lote, linha: dict) -> Desfecho | None:
    cid, versao = linha["cadastro_id"], linha["versao"]
    try:
        corpo = json.loads(linha["corpo_json"])
        if not isinstance(corpo, dict) or "resultado" not in corpo:
            raise ValueError("corpo sem resultado")
    except (TypeError, ValueError):
        logger.error(lote.log(cid, versao, "corpo guardado ilegível; não reenviado"))
        if not repo.adiar(lote.env_conn, cid, versao, ate=_iso(lote.relogio() + _ESPERA_BUG),
                          alerta="bug_corpo"):
            logger.warning(lote.log(cid, versao, "adiamento não aplicado no livro"))
        lote.env_conn.commit()
        return None
    return _enviar(lote, cid, versao, corpo, linha.get("tentativas") or 0)


def reenviar_pendencias(slug: str, env_conn, client, *, agora: datetime) -> dict:
    """Reenvia os resultados guardados cuja espera venceu. -> contagem por desfecho.
    Para no primeiro `credencial` (o resto espera o próximo ciclo)."""
    lote = _Lote(slug, "", env_conn, client, lambda: agora)
    contagem: dict[str, int] = {}
    for linha in repo.pendencias_de_envio(env_conn, _iso(agora), cv.LIMITE_PENDENCIAS):
        d = _reenviar_linha(lote, linha)
        if d is None:
            continue
        contagem[d.value] = contagem.get(d.value, 0) + 1
        if lote.parar:
            break
    return contagem


# --- erro ------------------------------------------------------------------


def _erro(
    lote: _Lote,
    cid: str,
    versao: int,
    tipo: str,
    codigo: str,
    tecnico: str,
    *,
    mensagem: str | None = None,
    rollback_comprovado: bool,
) -> None:
    """Linha `em_andamento` deste ciclo -> `erro_a_enviar` -> POST.
    `rollback_comprovado=True` só quando nada pode ter comitado no ERP (nenhum
    comando de escrita foi enviado, ou o ERP respondeu com erro e desfez)."""
    corpo = corpo_erro(versao, tipo, codigo, mensagem or mensagem_para_flow(codigo))
    conn = lote.env_conn
    if not repo.marcar_erro_a_enviar(
        conn, cid, versao, codigo=codigo, mensagem=tecnico[:500],
        corpo_json=json.dumps(corpo, ensure_ascii=False),
        rollback_comprovado=rollback_comprovado,
    ):
        conn.commit()
        logger.error(lote.log(cid, versao, f"erro {codigo} não aplicado no livro"))
        return
    conn.commit()
    lote.conta("erros")
    logger.warning(lote.log(cid, versao, f"recusado: {codigo} ({tipo})"))
    if not lote.parar:
        _enviar(lote, cid, versao, corpo, 0)


def _reservar(lote: _Lote, cid, versao, *, produto_id, acao, tipo, plano) -> bool:
    ok = repo.reservar(
        lote.env_conn, cadastro_id=cid, versao=versao, produto_id=produto_id, acao=acao,
        tipo=tipo, modo=lote.modo,
        plano_json=json.dumps(plano, ensure_ascii=False) if plano is not None else None,
    )
    lote.env_conn.commit()
    if not ok:
        lote.conta("pulados")
        logger.warning(lote.log(cid, versao, "lease de outro ciclo; pulado"))
    return ok


def _responder_erro(lote: _Lote, cid, versao, item_ou_bruto, tipo, codigo, tecnico, *,
                    mensagem=None) -> None:
    """Erro de um item que não chegou ao ERP: reserva a linha e responde."""
    if isinstance(item_ou_bruto, CadastroPendente):
        item = item_ou_bruto
        produto_id, acao, tipo_item = str(item.produtoId), item.acao, item.tipo
    else:
        produto_id = _uuid(item_ou_bruto.get("produtoId"))
        acao = item_ou_bruto.get("acao") if item_ou_bruto.get("acao") in _ACOES else None
        tipo_item = item_ou_bruto.get("tipo") if item_ou_bruto.get("tipo") in _TIPOS else None
    if _reservar(lote, cid, versao, produto_id=produto_id, acao=acao, tipo=tipo_item, plano=None):
        _erro(lote, cid, versao, tipo, codigo, tecnico, mensagem=mensagem,
              rollback_comprovado=True)


# --- triagem (sem ERP) -----------------------------------------------------


def _uuid(valor: object) -> str | None:
    if not isinstance(valor, str):
        return None
    try:
        return str(UUID(valor))
    except ValueError:
        return None


def _ids(item: CadastroPendente | None, bruto: object) -> tuple[str, int] | None:
    if item is not None:
        return str(item.cadastroId), item.versao
    if not isinstance(bruto, dict):
        return None
    cid, versao = _uuid(bruto.get("cadastroId")), cv._versao_do_bruto(bruto)
    return (cid, versao) if cid and versao >= 1 else None


def _segue_do_livro(lote: _Lote, linha: dict) -> bool:
    """True = a linha deste `(cadastroId, versao)` deixa o item seguir na triagem."""
    cid, versao, estado = linha["cadastro_id"], linha["versao"], linha["estado"]
    if estado in _PENDENTES:  # regra de ouro 1: só o corpo guardado, nunca o ERP
        lote.conta("ja_no_livro")
        if _vencida(lote, linha):
            _reenviar_linha(lote, linha)
        return False
    if estado == "em_andamento":  # a recuperação resolve
        lote.conta("pulados")
        return False
    if estado in _PULA_NO_LIVRO:
        lote.conta("ja_no_livro")
        logger.warning(lote.log(cid, versao, f"reentregue com {estado} no livro; pulado"))
        return False
    return True


def _livre_pela_9b(lote: _Lote, item: CadastroPendente) -> bool:
    """Spec 9b: `criar` de produto que já tem `criar` que pode ter gravado no livro."""
    outra = repo.criar_do_produto(lote.env_conn, str(item.produtoId))
    if outra is None:
        return True
    cid, versao = str(item.cadastroId), item.versao
    estado, seq = outra["estado"], outra["erp_produto_id"] or outra["seq_reservado"]
    lote.conta("pulados")
    if estado == "gravado_sem_volta":
        # Reenvia o gravado da versão que gravou; esta versão não recebe resposta.
        if _vencida(lote, outra):
            _reenviar_linha(lote, outra)
        return False
    if estado == "em_andamento":
        return False
    if estado in ("orfao", "orfao_fechado"):  # fechado pela operação: o produto segue no ERP
        try:
            mensagem = mensagem_para_flow("ja_criado_no_erp", detalhe=outra["erp_produto_id"])
        except ValueError:
            logger.error(lote.log(cid, versao, "9b: órfão sem código válido no livro; pulado"))
            return False
        _responder_erro(
            lote, cid, versao, item, "definitivo", "ja_criado_no_erp",
            f"produto já criado no ERP pela v{outra['versao']} (SEQ {seq}), sem vínculo no Flow",
            mensagem=mensagem,
        )
        return False
    logger.error(lote.log(
        cid, versao,
        f"9b: criar de produto com criar {estado} no livro (v{outra['versao']}, SEQ {seq}); "
        "não grava nem responde (resolver pela ferramenta do operador)",
    ))
    return False


def _triar(lote: _Lote, bruto: object, vistos: set) -> CadastroPendente | None:
    """Decide o item sem abrir o ERP. Devolve o item só se ele sai "a gravar"."""
    item, codigo, tecnico = validar_item(bruto)
    ids = _ids(item, bruto)
    if ids is None:
        lote.conta("pulados")
        logger.warning(
            f"flowpcp.cadastro_volta env={lote.slug}: item sem cadastroId/versao legível "
            f"ignorado ({codigo})"
        )
        return None
    if ids in vistos:
        lote.conta("pulados")
        return None
    vistos.add(ids)
    cid, versao = ids
    linha = repo.buscar(lote.env_conn, cid, versao)
    if linha is not None and not _segue_do_livro(lote, linha):
        return None
    if item is None:
        mensagem = mensagem_para_flow(_FRASE_DO_CODIGO.get(codigo, codigo))
        _responder_erro(lote, cid, versao, bruto, "definitivo", codigo, tecnico, mensagem=mensagem)
        return None
    if item.tipo == "material":
        _responder_erro(lote, cid, versao, item, "definitivo", "tipo_nao_suportado",
                        "material ainda não é gravado pelo Portal")
        return None
    if item.acao == "inativar" and not cv.inativar_ligado(lote.slug):
        lote.conta("pulados")
        logger.warning(lote.log(cid, versao, f"inativar sem {cv.VAR_INATIVAR}; não processado"))
        return None
    if item.acao == "criar":
        return item if _livre_pela_9b(lote, item) else None
    no_escopo = cv._no_escopo(lote.env_conn, item) or repo.seq_criado_pelo_portal(
        lote.env_conn, item.erpProdutoId
    )
    if not no_escopo:
        _responder_erro(lote, cid, versao, item, "definitivo", "seq_fora_do_escopo",
                        "erpProdutoId fora do catálogo e não criado pelo Portal")
        return None
    return item


# --- gravação (ERP aberto) ---------------------------------------------------


def _campos(item: CadastroPendente, marcas: dict) -> dict:
    """Colunas do Fire. `criar`: só o que veio. `atualizar`: null = não mexer, salvo
    `ean`/`codigoNfe` (limpar). `marcaErp` null = não mexer em MARCA."""
    if item.acao == "inativar":
        return {}
    campos: dict = {}
    for wire, coluna in cv.COLUNAS.items():
        valor = getattr(item, wire)
        if valor is not None:
            campos[coluna] = valor
        elif item.acao == "atualizar" and wire in _LIMPA_COM_NULL:
            campos[coluna] = None
    campos["GRUPO"] = int(item.grupoErp.codigo)
    if item.marcaErp is not None:
        campos["MARCA"] = marcas[item.marcaErp.codigo]
    return campos


def _componentes(item: CadastroPendente) -> list[tuple[int, float]] | None:
    if item.acao == "inativar" or item.componentes is None:
        return None
    return [(int(c.erpProdutoId), c.quantidade) for c in item.componentes]


def _plano(item: CadastroPendente, campos: dict, componentes) -> dict:
    """Exatamente o que vai ao gravador (a recuperação compara com o ERP)."""
    return {
        "acao": item.acao,
        "tabela": "PRODUTOS",
        "tipo": item.tipo,
        "seq": item.erpProdutoId,
        "campos": campos,
        "componentes": None
        if componentes is None
        else [{"erpProdutoId": str(f), "quantidade": q} for f, q in componentes],
    }


def _conferir(lote: _Lote, fire, item: CadastroPendente):
    """Conferência contra o ERP (só SELECT), antes da reserva. -> None (segue),
    ("erro", tipo, codigo, tecnico, mensagem), ("proprio", seq, linha) ou ("caiu",)."""
    if item.acao == "criar":
        seqs = lote.nomes.get(cv._normaliza_nome(item.nome))
        if seqs:
            for seq in seqs:
                linha = repo.linha_do_produto_com_seq(lote.env_conn, str(item.produtoId), seq)
                if linha is not None:
                    return ("proprio", seq, linha)
            return ("erro", "definitivo", "nome_existe",
                    f"já existe produto com esse nome no ERP (SEQ {seqs[0]})",
                    mensagem_nome_existe(seqs[0]))
    try:
        falha = cv._contra_o_fire(fire, item, {}, lote.marcas)  # nome já conferido acima
    except Exception as exc:  # noqa: BLE001
        try:
            cv._select(fire, cv._SQL_SONDA, ())
        except Exception:  # noqa: BLE001
            return ("caiu",)
        return ("erro", "transitorio", "consulta_erp_falhou",
                f"consulta ao ERP falhou ({type(exc).__name__})", None)
    if falha:
        return ("erro", "definitivo", falha[0], falha[1], None)
    return None


def _ausente_no_fire(lote: _Lote, fire, cid: str, versao: int) -> bool:
    """Depois de um erro do ERP num `criar`: o SEQ reservado comprovadamente não tem
    produto (nada comitou). Sem SEQ reservado, o INSERT nem foi enviado."""
    linha = repo.buscar(lote.env_conn, cid, versao)
    seq = linha["seq_reservado"] if linha else None
    if seq is None:
        return True
    try:
        return cadastro_fire.ler_produto(fire, int(seq)) is None
    except Exception:  # noqa: BLE001 — sem prova, fica para a recuperação
        return False


def _executar(lote: _Lote, fire, item: CadastroPendente, campos, componentes):
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    if item.acao == "criar":

        def _ao_reservar(seq: int) -> None:
            ok = repo.marcar_seq(conn, cid, versao, seq)
            conn.commit()  # o SEQ no disco antes do INSERT
            if not ok:
                raise RuntimeError("linha do livro não está em_andamento")

        return cadastro_fire.criar(
            fire, campos=campos, componentes=componentes,
            hoje=lote.relogio().astimezone().date(), ao_reservar=_ao_reservar,
        )
    if item.acao == "atualizar":
        return cadastro_fire.atualizar(
            fire, seq=int(item.erpProdutoId), campos=campos, componentes=componentes
        )
    return cadastro_fire.inativar(fire, seq=int(item.erpProdutoId))


def _gravar_item(lote: _Lote, fire, item: CadastroPendente) -> None:
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    # De novo aqui: outra versão do mesmo produto pode ter gravado neste mesmo lote.
    if item.acao == "criar" and not _livre_pela_9b(lote, item):
        return
    conferencia = _conferir(lote, fire, item)
    campos = componentes = None
    if conferencia is None:
        campos, componentes = _campos(item, lote.marcas), _componentes(item)
    plano = _plano(item, campos, componentes) if campos is not None else None
    if not _reservar(lote, cid, versao, produto_id=str(item.produtoId), acao=item.acao,
                     tipo=item.tipo, plano=plano):
        return
    if conferencia is not None:
        _sem_gravar(lote, cid, versao, conferencia)
        return
    try:
        gravacao = _executar(lote, fire, item, campos, componentes)
    except cadastro_fire.ReservaFalhouError:
        lote.parar = True
        logger.error(lote.log(cid, versao, "livro não aceitou o SEQ; nada inserido, lote parado"))
        return
    except cadastro_fire.SeqTomadoError:
        _erro(lote, cid, versao, "transitorio", "erp_ocupado", "SEQ tomado nas tentativas",
              rollback_comprovado=True)
        return
    except LookupError as exc:
        if item.acao == "criar":  # só atualizar/inativar levantam "SEQ não existe"
            _falha_do_erp(lote, fire, item, exc)
        elif _rollback_provado(lote, fire, cid, versao, "SEQ não existe no ERP"):
            _erro(lote, cid, versao, "definitivo", "seq_inexistente", "SEQ não existe no ERP",
                  rollback_comprovado=True)
        return
    except Exception as exc:  # noqa: BLE001 — classificado abaixo; nunca str(exc) ao Flow
        _falha_do_erp(lote, fire, item, exc)
        return
    seq = str(gravacao.seq)
    corpo = corpo_gravado(versao, seq, lote.relogio())
    if not repo.marcar_gravado(conn, cid, versao, erp_produto_id=seq, corpo_json=json.dumps(corpo)):
        conn.commit()
        lote.parar = True
        logger.error(lote.log(cid, versao, f"gravado no ERP (SEQ {seq}) mas o livro não mudou; "
                                           "lote parado"))
        return
    conn.commit()
    lote.conta("gravados")
    metrics.cadastro_volta_total.labels(env=lote.slug, resultado=_GRAVADO[item.acao]).inc()
    if item.acao == "criar":  # o 2º item com o mesmo nome no lote vira nome_existe
        lote.nomes[cv._normaliza_nome(item.nome)] = [gravacao.seq]
    if not lote.parar:
        _enviar(lote, cid, versao, corpo, 0)


def _sem_gravar(lote: _Lote, cid: str, versao: int, conferencia: tuple) -> None:
    """Item reservado que a conferência barrou: nada foi enviado ao ERP."""
    if conferencia[0] == "erro":
        _, tipo, codigo, tecnico, mensagem = conferencia
        _erro(lote, cid, versao, tipo, codigo, tecnico, mensagem=mensagem,
              rollback_comprovado=True)
    elif conferencia[0] == "proprio":
        _, seq, linha = conferencia
        if not repo.marcar_desfecho(lote.env_conn, cid, versao, "incerto",
                                    alerta="nome_do_proprio_produto"):
            logger.warning(lote.log(cid, versao, "incerto não aplicado no livro"))
        lote.env_conn.commit()
        lote.conta("incertos")
        logger.error(lote.log(
            cid, versao,
            f"nome já existe no ERP no SEQ {seq}, que o livro liga a este mesmo produto "
            f"({linha['cadastro_id']} v{linha['versao']}, {linha['estado']}); incerto, "
            "nem grava nem responde",
        ))
    else:  # "caiu": a conexão caiu numa leitura; nenhum comando de escrita saiu
        _erro(lote, cid, versao, "transitorio", "conexao_recusada", "conexão caiu na conferência",
              rollback_comprovado=True)
        lote.parar = True


def _rollback_provado(lote: _Lote, fire, cid: str, versao: int, tecnico: str) -> bool:
    """O gravador já tentou o rollback, mas relança a exceção original mesmo quando
    ele falha. Repete aqui e só segue com prova: sem ela, o pendente poderia ser
    comitado depois; a linha fica `em_andamento` e o lote para."""
    if cadastro_fire.desfazer(fire):
        return True
    lote.parar = True
    logger.error(lote.log(cid, versao, f"{tecnico} e o rollback não confirmou; fica para a "
                                       "recuperação, lote parado"))
    return False


def _falha_do_erp(lote: _Lote, fire, item: CadastroPendente, exc: BaseException) -> None:
    cid, versao = str(item.cadastroId), item.versao
    classe = cadastro_fire.classificar_erro(exc)
    tecnico = f"ERP: {classe} ({type(exc).__name__})"
    if classe == "conexao":
        # Pode ter comitado: a recuperação confere (regra de ouro 3).
        lote.parar = True
        logger.error(lote.log(cid, versao, f"{tecnico} depois do comando; incerto, lote parado"))
        return
    if not _rollback_provado(lote, fire, cid, versao, tecnico):
        return
    if item.acao == "criar" and not _ausente_no_fire(lote, fire, cid, versao):
        lote.parar = True
        logger.error(lote.log(cid, versao, f"{tecnico} e o SEQ reservado existe; fica para a "
                                           "recuperação, lote parado"))
        return
    if classe in ("lock", "colisao"):
        _erro(lote, cid, versao, "transitorio", "erp_ocupado", tecnico, rollback_comprovado=True)
    else:
        _erro(lote, cid, versao, "definitivo", "erp_recusou", tecnico, rollback_comprovado=True)


def _recusar_conexao(lote: _Lote, itens: list[CadastroPendente], exc: BaseException) -> None:
    logger.error(
        f"flowpcp.cadastro_volta env={lote.slug}: ERP de escrita indisponível "
        f"({type(exc).__name__}); {len(itens)} item(ns) voltam como transitório"
    )
    for item in itens:
        if lote.parar:
            return
        cid, versao = str(item.cadastroId), item.versao
        if _reservar(lote, cid, versao, produto_id=str(item.produtoId), acao=item.acao,
                     tipo=item.tipo, plano=None):
            _erro(lote, cid, versao, "transitorio", "conexao_recusada",
                  f"conexão com o ERP falhou ({type(exc).__name__})", rollback_comprovado=True)


def _recuperar(slug: str, env_conn, fire_conn, agora: datetime) -> dict | None:
    """Antes do GET: linhas que um processo caído deixou no meio. Só lê o ERP, pela
    conexão bloqueante (INSERT pendente de outro processo = lock, não ausência)."""
    if not cadastro_recuperacao.ha_o_que_recuperar(env_conn, agora=agora):
        return None
    try:
        with ExitStack() as stack:
            fire = fire_conn if fire_conn is not None else stack.enter_context(
                cv._abrir_fire_recuperacao(slug)
            )
            return cadastro_recuperacao.recuperar(slug, env_conn, fire, agora=agora)
    except Exception as exc:  # noqa: BLE001 — as linhas ficam para o próximo ciclo
        logger.error(f"flowpcp.cadastro_volta env={slug}: recuperação adiada, ERP indisponível "
                     f"({type(exc).__name__})")
        return {"erro": type(exc).__name__}


def atualizar_sem_volta(slug: str, env_conn) -> None:
    """Gauge das linhas gravadas no Fire que o Flow ainda não aceitou. Nunca levanta."""
    try:
        por_estado = repo.contar_por_estado(env_conn)
    except Exception as exc:  # noqa: BLE001 — métrica não derruba o ciclo
        logger.error(f"flowpcp.cadastro_volta env={slug}: contagem do livro falhou "
                     f"({type(exc).__name__})")
        return
    n = por_estado.get("gravado_sem_volta", 0) + por_estado.get("erro_a_enviar", 0)
    metrics.cadastro_volta_sem_volta.labels(env=slug).set(n)


def _aguardando_modo(slug: str, m: str, client) -> int | None:
    """Itens aprovados que este modo não grava, lidos sem reservar (Emenda 1, I9).
    Aproximado: em `criar` entra o `atualizar` derivado de `criar`, que este modo grava.
    `None` quando o Flow não respondeu; o ciclo segue."""
    consumidas = cv._acoes(slug, m)
    nao_consumidas = tuple(a for a in ("criar", "atualizar", "inativar") if a not in consumidas)
    if not nao_consumidas:
        return 0
    try:
        resp = client.list_cadastros_pendentes(
            limite=cv.LIMITE_FILA, acoes=nao_consumidas, simular=True
        )
        return len(resp.cadastros)
    except Exception as exc:  # noqa: BLE001 — a contagem é cortesia: nunca derruba o ciclo
        if getattr(exc, "status_code", None) == 400:
            cv._avisa_uma_vez(
                f"aguardando-modo-400:{slug}",
                f"flowpcp.cadastro_volta env={slug}: o Flow recusou simular com acoes "
                "(contrato rev. 3); \"aguardando modo\" fica sem contagem",
                nivel="WARNING",
            )
        else:
            logger.warning(f"flowpcp.cadastro_volta env={slug}: contagem do que aguarda "
                           f"outro modo falhou ({type(exc).__name__})")
        return None


def processar_gravacao(slug: str, m: str, client, env_conn, fire_conn, *,
                       relogio: Callable[[], datetime]) -> dict:
    try:
        return _processar_gravacao(slug, m, client, env_conn, fire_conn, relogio=relogio)
    finally:
        atualizar_sem_volta(slug, env_conn)


def _processar_gravacao(slug: str, m: str, client, env_conn, fire_conn, *,
                        relogio: Callable[[], datetime]) -> dict:
    inicio = relogio()
    recuperacao = _recuperar(slug, env_conn, fire_conn, inicio)
    try:
        resp = client.list_cadastros_pendentes(limite=cv.LIMITE_GRAVACAO, acoes=cv._acoes(slug, m))
    except ValueError as exc:  # JSON ou envelope inválido
        logger.error(f"flowpcp.cadastro_volta env={slug}: resposta da fila inválida: "
                     f"{type(exc).__name__}")
        return {"modo": m, "erro_transitorio": "resposta_invalida"}
    except Exception as exc:  # noqa: BLE001 — Flow fora não derruba o ciclo
        logger.error(f"flowpcp.cadastro_volta env={slug}: leitura da fila falhou: "
                     f"{type(exc).__name__}")
        return {"modo": m, "erro_transitorio": f"fila do Flow: {type(exc).__name__}"}
    brutos = list(resp.cadastros)
    cv._avisa_chaves(slug, brutos)
    resumo = {
        "modo": m,
        "lidos": len(brutos),
        "capacidade_ok": capacidades.CADASTROS in capacidades.de(resp),
        "gravados": 0,
        "erros": 0,
        "ja_no_livro": 0,
        "pulados": 0,
    }
    if recuperacao is not None:
        resumo["recuperacao"] = recuperacao
    metrics.cadastro_volta_pendentes.labels(env=slug).set(len(brutos))
    metrics.cadastro_volta_mais_antigo.labels(env=slug).set(cv._idade_mais_antiga(brutos))
    if not resumo["capacidade_ok"]:
        if brutos:
            logger.error(f"flowpcp.cadastro_volta env={slug}: fila sem {capacidades.CADASTROS}; "
                         f"{len(brutos)} item(ns) não processados")
        return resumo
    resumo["aguardando_modo"] = _aguardando_modo(slug, m, client)
    aguardando = resumo["aguardando_modo"]
    if aguardando is None:
        try:
            metrics.cadastro_volta_aguardando_modo.remove(slug)
        except KeyError:
            pass
    else:
        metrics.cadastro_volta_aguardando_modo.labels(env=slug).set(aguardando)
    lote = _Lote(slug, m, env_conn, client, relogio, resumo)
    vistos: set = set()
    a_gravar = []
    for bruto in brutos:
        if lote.parar:
            return resumo
        item = _triar(lote, bruto, vistos)
        if item is not None:
            a_gravar.append(item)
    if not a_gravar or lote.parar:
        return resumo
    with ExitStack() as stack:
        try:
            fire = fire_conn if fire_conn is not None else stack.enter_context(
                cv._abrir_fire_escrita(slug)
            )
            lote.nomes = cv._nomes_do_fire(fire)
            lote.marcas = cv._marcas_do_fire(fire)
        except Exception as exc:  # noqa: BLE001 — nada foi enviado ao ERP
            _recusar_conexao(lote, a_gravar, exc)
            return resumo
        for n, item in enumerate(a_gravar):
            if lote.parar:
                break
            if relogio() - inicio >= cv.PRAZO_LOTE:
                logger.warning(f"flowpcp.cadastro_volta env={slug}: prazo do lote; "
                               f"{len(a_gravar) - n} item(ns) ficam para a reentrega do Flow")
                break
            _gravar_item(lote, fire, item)
    return resumo
