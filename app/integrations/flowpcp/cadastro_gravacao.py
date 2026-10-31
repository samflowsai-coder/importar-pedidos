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
import sqlite3
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.erp import cadastro_fire, produto_referencias
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
from app.integrations.flowpcp.cadastro_schema import (
    LIMPA_COM_NULL,
    CadastroPendente,
    vai_ao_erp,
    validar_item,
)
from app.observability import metrics
from app.persistence import cadastro_exclusao_repo as backup_repo
from app.persistence import cadastro_volta_repo as repo
from app.utils.logger import logger

_ACOES = ("criar", "atualizar", "inativar", "excluir")
_TIPOS = ("simples", "kit", "pack", "material")
_PENDENTES = ("gravado_sem_volta", "erro_a_enviar")
# Já têm desfecho (ou uma pessoa decide): o item reentregue não é processado.
_PULA_NO_LIVRO = frozenset(
    {"incerto", "orfao", "orfao_fechado", "confirmado", "erro_registrado", "superado"}
)
_FECHA = {
    Desfecho.CONFIRMADO: "confirmado",
    Desfecho.ERRO_REGISTRADO: "erro_registrado",
    Desfecho.TRANSITORIO_REGISTRADO: "transitorio",
    Desfecho.ORFAO: "orfao",
    Desfecho.SUPERADO: "superado",
}
_GRAVADO = {"criar": "criado", "atualizar": "atualizado", "inativar": "inativado",
            "excluir": "excluido"}
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
    # Colunas do ERP que podem guardar um SEQ de produto, lidas do catálogo uma vez por ciclo,
    # só quando há `excluir` a gravar.
    alvos_de_uso: list | None = None
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


def _orfao_ou_superado(lote: _Lote, cid: str, versao: int, corpo_resposta) -> tuple:
    """409 de um `gravado`: órfão só se o Portal CRIOU o produto (contrato §5). Decide
    pela `acao` que o livro guardou (o que o Portal fez de fato): em `atualizar`/`inativar`
    o produto já existia no ERP e o 409 fecha como `superado`; em `excluir` não há produto
    novo sem vínculo, então também."""
    linha = repo.buscar(lote.env_conn, cid, versao)
    if (linha or {}).get("acao") not in ("atualizar", "inativar", "excluir"):
        return Desfecho.ORFAO, None
    erro = corpo_resposta.get("error") if isinstance(corpo_resposta, dict) else None
    if erro == "conflito_codigo":
        logger.error(lote.log(
            cid, versao,
            f"o Flow recusou o código {linha.get('erp_produto_id')} deste {linha['acao']} "
            "(conflito_codigo): a gravação no ERP ficou; a área de cadastro do Flow confere "
            "qual produto tem esse código",
        ))
        return Desfecho.SUPERADO, "conflito_codigo"
    return Desfecho.SUPERADO, None


def _enviar(lote: _Lote, cid: str, versao: int, corpo: dict, tentativas: int) -> Desfecho:
    """POST do resultado e o estado que a resposta dá: pelo `status`, nunca pela `acao`
    que o item supõe; só o 409 de um `gravado` olha a `acao` do livro (`_orfao_ou_superado`)."""
    r = lote.client.post_cadastro_resultado(cid, corpo)
    d = ler_resposta(corpo["resultado"], r.status_http, r.corpo)
    conn, agora, resposta = lote.env_conn, lote.relogio(), _resposta(r.status_http, r.corpo)
    alerta_fecho = None
    if d is Desfecho.ORFAO:
        d, alerta_fecho = _orfao_ou_superado(lote, cid, versao, r.corpo)
    if d in _FECHA:
        if not repo.marcar_desfecho(conn, cid, versao, _FECHA[d], alerta=alerta_fecho,
                                    resposta=resposta):
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
    if estado in ("incerto", "liberado", "transitorio"):
        caminho = "resolver pela ferramenta do operador"
    else:  # confirmado, erro_*, superado com SEQ: a ferramenta não age neles
        caminho = (f"conferir com tools/produto_no_fire.py <slug> --seq {seq} e seguir "
                   "\"Caminhos manuais\" em docs/ai/modules/flowpcp.md")
    logger.error(lote.log(
        cid, versao,
        f"9b: criar de produto com criar {estado} no livro (v{outra['versao']}, SEQ {seq}); "
        f"não grava nem responde ({caminho})",
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
    if item.acao == "excluir":
        return _triar_excluir(lote, item)
    no_escopo = cv._no_escopo(lote.env_conn, item) or repo.seq_criado_pelo_portal(
        lote.env_conn, item.erpProdutoId
    )
    if not no_escopo:
        _responder_erro(lote, cid, versao, item, "definitivo", "seq_fora_do_escopo",
                        "erpProdutoId fora do catálogo e não criado pelo Portal")
        return None
    if item.acao == "atualizar" and item.camposAlterados == []:
        _sem_mudanca(lote, item)
        return None
    return item


def _triar_excluir(lote: _Lote, item: CadastroPendente) -> CadastroPendente | None:
    """Portão e origem do `excluir`, sem abrir o ERP. Só o produto que um `criar` do Flow
    gravou no ERP é apagável; produto da fábrica é definitivo (nunca se apaga)."""
    cid, versao = str(item.cadastroId), item.versao
    if not cv.excluir_ligado(lote.slug):
        lote.conta("pulados")
        logger.warning(lote.log(cid, versao, f"excluir sem {cv.VAR_EXCLUIR}; não processado"))
        return None
    if lote.modo != "criar_e_atualizar":
        # O Flow só entrega `excluir` quando o Portal pede (e o Portal só pede neste modo), mas
        # a ação é destrutiva: a proteção não pode depender só do contrato do outro lado.
        lote.conta("pulados")
        logger.warning(lote.log(cid, versao, f"excluir fora do modo criar_e_atualizar "
                                             f"({lote.modo}); não processado"))
        return None
    if repo.criar_que_gravou_seq(lote.env_conn, item.erpProdutoId, str(item.produtoId)) is None:
        _responder_erro(lote, cid, versao, item, "definitivo", "produto_nao_criado_pelo_flow",
                        "o livro não tem um criar deste produto com esse código")
        return None
    return item


def _sem_mudanca(lote: _Lote, item: CadastroPendente) -> None:
    """`atualizar` com `camposAlterados: []` (rev. 5 §2.3): a correção não muda nada que
    vá ao ERP. Não abre nem chama o ERP; reserva, guarda o `gravado` com o código do item
    e responde, pelas mesmas portas do livro de um `atualizar` gravado."""
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    if not _reservar(lote, cid, versao, produto_id=str(item.produtoId), acao=item.acao,
                     tipo=item.tipo, plano=_plano(item, {}, None)):
        return
    corpo = corpo_gravado(versao, item.erpProdutoId, lote.relogio())
    if not repo.marcar_gravado(conn, cid, versao, erp_produto_id=item.erpProdutoId,
                               corpo_json=json.dumps(corpo)):
        conn.commit()
        logger.error(lote.log(cid, versao, "sem mudança para o ERP, mas o livro não mudou; "
                                           "fica para a recuperação"))
        return
    conn.commit()
    lote.conta("sem_mudanca")
    metrics.cadastro_volta_total.labels(env=lote.slug, resultado="sem_mudanca").inc()
    if not lote.parar:
        _enviar(lote, cid, versao, corpo, 0)


# --- gravação (ERP aberto) ---------------------------------------------------


def _campos(item: CadastroPendente, marcas: dict) -> dict:
    """Colunas do Fire. `criar`: só o que veio. `atualizar`: null = não mexer, salvo
    `ean`/`codigoNfe` (limpar). `marcaErp` null = não mexer em MARCA. `atualizar` com
    `camposAlterados` (rev. 5): só as colunas dos campos listados (`vai_ao_erp`)."""
    if item.acao in ("inativar", "excluir"):
        return {}
    campos: dict = {}
    for wire, coluna in cv.COLUNAS.items():
        if not vai_ao_erp(item, wire):
            continue
        valor = getattr(item, wire)
        if valor is not None:
            campos[coluna] = valor
        elif item.acao == "atualizar" and wire in LIMPA_COM_NULL:
            campos[coluna] = None
    if vai_ao_erp(item, "grupoErp"):
        campos["GRUPO"] = int(item.grupoErp.codigo)
    if item.marcaErp is not None and vai_ao_erp(item, "marcaErp"):
        campos["MARCA"] = marcas[item.marcaErp.codigo]
    return campos


def _componentes(item: CadastroPendente) -> list[tuple[int, float]] | None:
    if item.acao in ("inativar", "excluir") or item.componentes is None \
            or not vai_ao_erp(item, "componentes"):
        return None
    return [(int(c.erpProdutoId), c.quantidade) for c in item.componentes]


def _plano(item: CadastroPendente, campos: dict, componentes) -> dict:
    """Exatamente o que vai ao gravador (a recuperação compara com o ERP). Com
    `camposAlterados`, o plano diz quais campos a correção muda; lista vazia não vai ao ERP."""
    if item.camposAlterados == []:
        return {"acao": item.acao, "sem_mudanca": True}
    if item.acao == "excluir":
        return {"acao": "excluir", "tabela": "PRODUTOS", "tipo": item.tipo, "seq": item.erpProdutoId}
    plano = {
        "acao": item.acao,
        "tabela": "PRODUTOS",
        "tipo": item.tipo,
        "seq": item.erpProdutoId,
        "campos": campos,
        "componentes": None
        if componentes is None
        else [{"erpProdutoId": str(f), "quantidade": q} for f, q in componentes],
    }
    if item.camposAlterados is not None:
        plano["campos_alterados"] = list(item.camposAlterados)
    return plano


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
    outro = cv.outro_seq_com_o_nome(lote.nomes, item)
    if outro is not None:
        return ("erro", "definitivo", "nome_existe",
                f"atualizar: já existe outro produto com esse nome no ERP (SEQ {outro})",
                mensagem_nome_existe(outro))
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
    if item.acao == "excluir":
        _excluir_item(lote, fire, item)
        return
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


def _responder_gravado(lote: _Lote, item: CadastroPendente, seq: str) -> None:
    """`gravado` do `excluir` (apagado agora, ou já não existia): livro e envio."""
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    corpo = corpo_gravado(versao, seq, lote.relogio())
    if not repo.marcar_gravado(conn, cid, versao, erp_produto_id=seq, corpo_json=json.dumps(corpo)):
        conn.commit()
        lote.parar = True
        logger.error(lote.log(cid, versao, f"excluído no ERP (código {seq}) mas o livro não "
                                           "mudou; lote parado"))
        return
    conn.commit()
    lote.conta("gravados")
    metrics.cadastro_volta_total.labels(env=lote.slug, resultado="excluido").inc()
    if not lote.parar:
        _enviar(lote, cid, versao, corpo, 0)


def _na_janela_do_criar(lido: dict, criar: dict) -> bool:
    """`DATA_CAD` do produto lido cai na janela em que o `criar` do Flow o cadastrou. Fora dela
    o código foi reaproveitado (o Fire dá MAX+1): é outro produto, que não se apaga."""
    janela = cadastro_recuperacao.janela_da_reserva(criar)
    data_cad = cadastro_recuperacao._data(lido.get("DATA_CAD"))
    return janela is not None and data_cad is not None and janela[0] <= data_cad <= janela[1]


def _id_valido(valor: object) -> bool:
    """SEQ que cabe na frase ao Flow (mesmo formato do contrato: 1 a 9 dígitos, sem zero à esquerda)."""
    return re.fullmatch(r"[1-9][0-9]{0,8}", str(valor)) is not None


def _e_o_produto(lido: dict, item: CadastroPendente, criar: dict) -> bool:
    """A descrição do produto lido no Fire é a que o Flow conhece (a do item, ou a que o
    `criar` gravou). Cobre o código reaproveitado por outro produto dentro da janela do
    DATA_CAD, inclusive depois de a fábrica apagar à mão e cadastrar outro no mesmo dia."""
    descricao = lido.get("DESCRICAO")
    if not isinstance(descricao, str):
        return False
    aceitos = {cv._normaliza_nome(item.nome)}
    try:
        plano = json.loads(criar.get("plano_json") or "{}")
        nome_criado = plano["campos"]["DESCRICAO"]
        if isinstance(nome_criado, str):
            aceitos.add(cv._normaliza_nome(nome_criado))
    except (TypeError, ValueError, KeyError):
        pass
    return cv._normaliza_nome(descricao) in aceitos


def _guardar_backup(lote: _Lote, item: CadastroPendente, linha: dict, kit: list[dict]) -> str | None:
    """Backup no SQLite, commitado e conferido por releitura ANTES de qualquer DELETE.
    -> None (ok) | "transitorio" | "definitivo"."""
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    try:
        backup_repo.guardar(conn, cid, versao, int(item.erpProdutoId), linha, kit)
        conn.commit()
        gravado = backup_repo.buscar(conn, cid, versao)
        if gravado is None or backup_repo.restaurar(gravado) != (linha, kit):
            raise RuntimeError("backup não confere na releitura")
    except TypeError as exc:  # coluna de tipo que o backup não sabe guardar: esperar não resolve
        _desfazer_sqlite(conn)
        logger.error(lote.log(cid, versao, f"backup: {exc}"))
        return "definitivo"
    except Exception as exc:  # noqa: BLE001 — sem backup nada é apagado
        _desfazer_sqlite(conn)
        logger.error(lote.log(cid, versao, f"backup falhou ({type(exc).__name__})"))
        return "transitorio"
    return None


def _desfazer_sqlite(conn) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


def _divergiu_do_backup(lote: _Lote, item: CadastroPendente, motivo: str) -> None:
    """Há backup de uma tentativa anterior e o produto lido agora não é o dele: o DELETE daquela
    tentativa pode ter comitado e o código ter sido reaproveitado. Não dá para afirmar ao Flow
    nem que apagou nem que não: `incerto`, uma pessoa resolve (o backup fica intacto)."""
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    if not repo.marcar_desfecho(conn, cid, versao, "incerto", alerta="excluir_divergente"):
        logger.warning(lote.log(cid, versao, "incerto não aplicado no livro"))
    conn.commit()
    lote.conta("incertos")
    logger.error(lote.log(cid, versao, f"excluir {item.erpProdutoId}: {motivo} em relação ao "
                                       "backup da tentativa anterior; incerto, nem apaga nem "
                                       "responde (backup intacto em cadastro_exclusao_backup)"))


def _guardas_e_delete(lote: _Lote, fire, item: CadastroPendente, criar: dict) -> str:  # noqa: C901
    """Guardas na ordem: origem, janela do DATA_CAD, identidade, kit, uso no ERP, backup; só
    então o DELETE. -> "apagado" | "ja_excluido" | "barrado" (o erro ou o incerto já foi dado).
    Levanta o que o ERP ou o livro levantarem; quem chama classifica."""
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    seq = int(item.erpProdutoId)
    feito = backup_repo.buscar(conn, cid, versao)
    if feito is not None and feito["aplicado_em"] is not None:
        return "ja_excluido"  # o DELETE comitou numa passagem anterior e o livro não soube
    linha = cadastro_fire.ler_linha_completa(fire, seq)
    if linha is None:
        return "ja_excluido"
    if not _na_janela_do_criar(linha, criar):
        _erro(lote, cid, versao, "definitivo", "produto_nao_criado_pelo_flow",
              "DATA_CAD fora da janela do criar do Flow: o código foi reaproveitado",
              rollback_comprovado=True)
        return "barrado"
    anterior = backup_repo.restaurar(feito) if feito is not None else None
    if not _e_o_produto(linha, item, criar) or (anterior is not None and anterior[0] != linha):
        if anterior is not None:
            _divergiu_do_backup(lote, item, "o produto lido mudou")
        else:
            _erro(lote, cid, versao, "definitivo", "produto_mudou",
                  "o produto lido no ERP não é o que o Flow criou", rollback_comprovado=True)
        return "barrado"
    pais = cadastro_fire.pais_do_componente(fire, seq)
    if pais:
        validos = [str(p) for p in pais[:5] if _id_valido(p)]
        _erro(lote, cid, versao, "transitorio", "ligado_a_kit",
              f"componente de {len(pais)} kit(s) no ERP", rollback_comprovado=True,
              mensagem=mensagem_para_flow("ligado_a_kit", ", ".join(validos) or None))
        return "barrado"
    try:
        if lote.alvos_de_uso is None:
            lote.alvos_de_uso = produto_referencias.alvos_do_fire(fire)
    except produto_referencias.CatalogoIncompletoError as exc:
        logger.error(lote.log(cid, versao, f"guarda de uso impossível: {exc}"))
        _erro(lote, cid, versao, "transitorio", "consulta_erp_falhou", str(exc),
              rollback_comprovado=True)
        return "barrado"
    uso = produto_referencias.primeiro_uso(fire, seq, lote.alvos_de_uso)
    if uso is not None:
        _erro(lote, cid, versao, "definitivo", "em_uso_no_erp", f"em uso em {uso}",
              rollback_comprovado=True, mensagem=mensagem_para_flow("em_uso_no_erp", uso))
        return "barrado"
    kit = cadastro_fire.ler_kit_completo(fire, seq)
    if anterior is not None and anterior[1] != kit:
        _divergiu_do_backup(lote, item, "a composição mudou")
        return "barrado"
    falha = _guardar_backup(lote, item, linha, kit)
    if falha is not None:
        _erro(lote, cid, versao, falha, "backup_falhou", "backup não gravado",
              rollback_comprovado=True)
        return "barrado"
    cadastro_fire.excluir(fire, seq=seq, linhas_kit=len(kit))
    # O nome apagado sai do cache do lote: um `criar` com ele mais adiante não é `nome_existe`.
    nome = cv._normaliza_nome(str(linha.get("DESCRICAO") or ""))
    restantes = [x for x in lote.nomes.get(nome, []) if x != seq]
    if restantes:
        lote.nomes[nome] = restantes
    else:
        lote.nomes.pop(nome, None)
    return "apagado"


def _excluir_item(lote: _Lote, fire, item: CadastroPendente) -> None:
    """Reserva o item, roda as guardas e o DELETE e responde ao Flow. Tudo que só lê o ERP corre
    antes de qualquer escrita; falha de leitura é transitória, e falha do livro (SQLite) também."""
    cid, versao, conn = str(item.cadastroId), item.versao, lote.env_conn
    if not _reservar(lote, cid, versao, produto_id=str(item.produtoId), acao=item.acao,
                     tipo=item.tipo, plano=_plano(item, {}, None)):
        return
    criar = repo.criar_que_gravou_seq(conn, item.erpProdutoId, str(item.produtoId))
    if criar is None:  # a triagem já conferiu; o livro mudou desde então
        _erro(lote, cid, versao, "definitivo", "produto_nao_criado_pelo_flow",
              "o livro não tem um criar deste produto com esse código", rollback_comprovado=True)
        return
    try:
        resultado = _guardas_e_delete(lote, fire, item, criar)
    except sqlite3.Error as exc:  # o livro falhou antes de qualquer escrita no ERP
        _desfazer_sqlite(conn)
        logger.error(lote.log(cid, versao, f"livro falhou ({type(exc).__name__}); nada apagado"))
        _erro(lote, cid, versao, "transitorio", "consulta_erp_falhou",
              f"livro: {type(exc).__name__}", rollback_comprovado=True)
        return
    except Exception as exc:  # noqa: BLE001 — classificado em _falha_do_erp; nunca str(exc) ao Flow
        _falha_do_erp(lote, fire, item, exc)
        return
    if resultado == "barrado":
        return
    if resultado == "apagado":
        try:
            backup_repo.marcar_aplicado(conn, cid, versao)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — o Fire já comitou; a reentrega responde pelo livro
            logger.error(lote.log(cid, versao, f"excluído no ERP mas o backup não foi marcado "
                                               f"({type(exc).__name__})"))
    _responder_gravado(lote, item, item.erpProdutoId)


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
    """Gauge das linhas com resultado (gravado ou erro) que o Flow ainda não aceitou.
    Nunca levanta."""
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
    `0` quando o modo consome as três ações (portão de inativar ligado): zero medido, sem
    GET. `None` quando o Flow não respondeu; o ciclo segue."""
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
    # Antes do GET que reserva: é só uma contagem e não pode comer a janela da reserva.
    aguardando = _aguardando_modo(slug, m, client)
    cv.publicar_aguardando_modo(slug, aguardando)
    try:
        resp = client.list_cadastros_pendentes(limite=cv.LIMITE_GRAVACAO, acoes=cv._acoes(slug, m))
    except ValueError as exc:  # JSON ou envelope inválido
        logger.error(f"flowpcp.cadastro_volta env={slug}: resposta da fila inválida: "
                     f"{type(exc).__name__}")
        return {"modo": m, "erro_transitorio": "resposta_invalida", "aguardando_modo": aguardando}
    except Exception as exc:  # noqa: BLE001 — Flow fora não derruba o ciclo
        logger.error(f"flowpcp.cadastro_volta env={slug}: leitura da fila falhou: "
                     f"{type(exc).__name__}")
        return {"modo": m, "erro_transitorio": f"fila do Flow: {type(exc).__name__}",
                "aguardando_modo": aguardando}
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
        "aguardando_modo": aguardando,
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
