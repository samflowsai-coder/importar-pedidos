"""Corpo do `POST /cadastros/{cadastroId}/gravado` e leitura da resposta do Flow
(contrato P4a, §5). Puro: sem rede, sem banco.

A decisão é pelo `status` da resposta, nunca pela `acao` do item (regra de ouro 5).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

MAX_MENSAGEM = 500
_ERP_ID = re.compile(r"^[1-9][0-9]{0,8}$")
_RESERVADOS = frozenset({"conflito_codigo", "versao_superada", "cancelado_no_flow", "fora_do_contrato"})


class Desfecho(StrEnum):
    CONFIRMADO = "confirmado"  # gravado aceito: fecha no livro
    ERRO_REGISTRADO = "erro_registrado"  # erro definitivo aceito: fecha no livro
    TRANSITORIO_REGISTRADO = "transitorio_registrado"  # reserva liberada: item volta no GET
    ORFAO = "orfao"  # produto existe no ERP sem vínculo no Flow: avisar a operação
    SUPERADO = "superado"  # erro de versão já superada: nada a fazer
    REPETIR = "repetir"  # 429, 5xx, rede: mesmo POST depois, com espera crescente
    BUG_CORPO = "bug_corpo"  # 400/404/409 versao_divergente: alerta, mantém e repete com espera longa
    CREDENCIAL = "credencial"  # 401: para os POSTs deste ciclo


def _instante(gravado_em: datetime) -> str:
    if gravado_em.tzinfo is None:
        raise ValueError("gravadoEm sem fuso")
    utc = gravado_em.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def corpo_gravado(versao: int, erp_produto_id: str, gravado_em: datetime) -> dict:
    if not _ERP_ID.fullmatch(erp_produto_id or ""):
        raise ValueError("erpProdutoId fora de ^[1-9][0-9]{0,8}$")
    return {
        "versao": versao,
        "resultado": "gravado",
        "erpProdutoId": erp_produto_id,
        "gravadoEm": _instante(gravado_em),
    }


def corpo_erro(
    versao: int, tipo: Literal["transitorio", "definitivo"], codigo: str, mensagem: str
) -> dict:
    if codigo in _RESERVADOS or not 1 <= len(codigo) <= 64:
        raise ValueError("erro.codigo reservado ou fora de 1..64")
    texto = (mensagem or "").strip()
    if not texto:
        raise ValueError("erro.mensagem vazia")
    return {
        "versao": versao,
        "resultado": "erro",
        "erro": {"tipo": tipo, "codigo": codigo, "mensagem": texto[:MAX_MENSAGEM]},
    }


def mensagem_nome_existe(seq: int | str) -> str:
    # O Flow mostra o PRIMEIRO número de 1 a 9 dígitos da mensagem: nenhum antes do código.
    return f"Já existe produto com esse nome no ERP (código {seq})"


_MENSAGENS_ERRO: dict[str, str] = {
    "contrato_invalido": "O cadastro chegou incompleto ou com um campo em formato inesperado. Confira os dados e aprove de novo.",
    "campo_excede_fire": "Um texto do cadastro é maior do que o ERP aceita. Encurte e aprove de novo.",
    "caractere_invalido": "O cadastro tem um caractere que o ERP não grava (por exemplo, emoji). Troque e aprove de novo.",
    "componente_ausente": "Um item da composição não existe no ERP.",
    "componente_bloqueado": "Um item da composição está inativo no ERP.",
    "marca_ausente_erp": "A marca do cadastro não existe no ERP. Cadastre a marca no ERP e aprove de novo.",
    "seq_fora_do_escopo": "O produto não está entre os produtos que o Portal pode alterar no ERP.",
    "seq_inexistente": "O produto não existe mais no ERP.",
    "tipo_nao_suportado": "Este tipo de cadastro ainda não é gravado no ERP pelo Portal.",
    "erp_recusou": "O ERP recusou a gravação do cadastro.",
}
_MENSAGEM_DESCONHECIDA = "O ERP recusou o cadastro."


def mensagem_para_flow(codigo: str, detalhe: str | None = None) -> str:
    """Frase que a área de cadastro lê no Flow para um código de erro do Portal.

    `detalhe` só importa para `ja_criado_no_erp` (código do produto já existente no ERP).
    """
    if codigo == "ja_criado_no_erp":
        if detalhe:
            return f"Este produto já foi criado no ERP (código {detalhe}) e não foi vinculado. Fale com a área de cadastro."
        return "Este produto já foi criado no ERP e não foi vinculado. Fale com a área de cadastro."
    return _MENSAGENS_ERRO.get(codigo, _MENSAGEM_DESCONHECIDA)


_OK_GRAVADO = {"gravado", "ja_registrado", "registrado_versao_superada"}


def ler_resposta(
    resultado: Literal["gravado", "erro"], status_http: int | None, corpo: dict | None
) -> Desfecho:
    corpo = corpo if isinstance(corpo, dict) else {}
    if status_http == 200:
        status = corpo.get("status")
        if status == "transitorio_registrado":
            return Desfecho.BUG_CORPO if resultado == "gravado" else Desfecho.TRANSITORIO_REGISTRADO
        if status == "erro_registrado":
            return Desfecho.BUG_CORPO if resultado == "gravado" else Desfecho.ERRO_REGISTRADO
        if status in _OK_GRAVADO:
            return Desfecho.CONFIRMADO if resultado == "gravado" else Desfecho.ERRO_REGISTRADO
        return Desfecho.REPETIR
    if status_http == 409 and corpo.get("error") in ("versao_superada", "conflito_codigo"):
        return Desfecho.ORFAO if resultado == "gravado" else Desfecho.SUPERADO
    if status_http in (400, 404, 409):
        return Desfecho.BUG_CORPO
    if status_http == 401:
        return Desfecho.CREDENCIAL
    return Desfecho.REPETIR
