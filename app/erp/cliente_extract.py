from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.erp.cnpj import cnpj_digits
from app.erp.queries import LIST_CLIENTES_ATIVOS


@dataclass(frozen=True)
class ClienteFireDTO:
    fire_cliente_id: str  # str(CADASTRO.CODIGO) — PK durável
    cnpj: str  # dígitos-only, 14 — chave de match no Flow
    nome: str  # RAZAO_SOCIAL
    grupo_codigo: str | None  # str(CODGRUPO) — a marca; None se a coluna não existir
    ativo: bool  # sempre True nesta fase (janela ativa)


@dataclass(frozen=True)
class ExtracaoClientesResult:
    clientes: list[ClienteFireDTO]
    descartados_cpf: int
    descartados_invalidos: int
    colisoes_dedup: int


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def extract_clientes_ativos(fire_conn, *, desde_data: date) -> ExtracaoClientesResult:
    """Lê os clientes ativos (pedido na janela) do Fire. Read-only.

    Regras (spec I2/I6): normaliza CPF_CNPJ para dígitos; 14 = CNPJ (mantém),
    11 = CPF (descarta), resto = inválido (descarta). Dedup por CNPJ mantendo o
    maior CODIGO via comparação explícita (não depende de ORDER BY da query).
    """
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_CLIENTES_ATIVOS, (desde_data,))
        rows = cur.fetchall()
    finally:
        cur.close()

    by_cnpj: dict[str, tuple[int, str, object]] = {}
    descartados_cpf = 0
    descartados_invalidos = 0
    colisoes_dedup = 0

    for codigo, razao, cpf_cnpj, codgrupo in rows:
        digits = cnpj_digits(cpf_cnpj)
        if len(digits) == 14:
            if digits in by_cnpj:
                colisoes_dedup += 1
                if codigo > by_cnpj[digits][0]:
                    by_cnpj[digits] = (codigo, _clean(razao), codgrupo)
            else:
                by_cnpj[digits] = (codigo, _clean(razao), codgrupo)
        elif len(digits) == 11:
            descartados_cpf += 1
        else:
            descartados_invalidos += 1

    clientes = [
        ClienteFireDTO(
            fire_cliente_id=str(codigo),
            cnpj=digits,
            nome=razao,
            grupo_codigo=(str(codgrupo) if codgrupo is not None else None),
            ativo=True,
        )
        for digits, (codigo, razao, codgrupo) in by_cnpj.items()
    ]
    return ExtracaoClientesResult(
        clientes=clientes,
        descartados_cpf=descartados_cpf,
        descartados_invalidos=descartados_invalidos,
        colisoes_dedup=colisoes_dedup,
    )
