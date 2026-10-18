"""Schemas do contrato precos v1 (Portal ↔ Flow), porta do zod do pcp-app
(`src/lib/services/portal-pedidos/contract-preco.ts`), com os `superRefine`.

Aqui: a página da rota (a) (`tabelaPaginaV1`), o envio da rota (b)
(`aprovacaoEnvioV1`, pedido e retirada) e a resposta dele (`aprovacaoRespostaV1`),
a decisão da rota (c) (`decisaoPrecoV1`, `decisoesPaginaV1`) e a confirmação
(`confirmacaoDecisoesV1`, `confirmacaoRespostaV1`). O Portal valida o corpo do
envio ANTES de gravar a revisão: corpo inválido é bloqueio local, nunca vai ao
Flow. Campo desconhecido é ignorado (o zod `object()` descarta).

Fidelidade ao zod que importa aqui:
- inteiros são estritos (`True` não é 1; `1.0` não é inteiro de JSON do Portal);
- `z.string().datetime()` aceita só `Z` (sem `±hh:mm`);
- `z.string().uuid()` do zod 4 exige versão 1-8 e variante 8/9/a/b;
- `.max(n)` de texto conta unidades UTF-16 (como o JS), exceto `motivoRecusa`
  e `observacao`, que contam code points (`textoAte` do contrato).
"""

from __future__ import annotations

import math
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    model_validator,
)

TETO_CENTAVOS = 10_000_000_000
INT4_MAX = 2_147_483_647
_TETO_QUANTIDADE = Decimal("999999999999.99")

_CNPJ = re.compile(r"[0-9A-Z]{12}[0-9]{2}")
_RAIZ = re.compile(r"[0-9A-Z]{8}")
_DATA_CIVIL = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
    r"|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff"
)
_DATETIME_Z = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T(?:[01][0-9]|2[0-3]):[0-5][0-9]"
    r"(?::[0-5][0-9](?:\.[0-9]+)?)?Z"
)


def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _texto(minimo: int, maximo: int, *, trim: bool = False, code_points: bool = False):
    def _v(s: str) -> str:
        if trim:
            s = s.strip()
        n = len(s) if code_points else _utf16(s)
        if n < minimo or n > maximo:
            raise ValueError(f"texto com {minimo}..{maximo} caracteres")
        return s

    return AfterValidator(_v)


def _cnpj(s: str) -> str:
    if not _CNPJ.fullmatch(s):
        raise ValueError("CNPJ com 14 caracteres (12 de [0-9A-Z] + 2 dígitos), sem máscara")
    return s


def _raiz(s: str) -> str:
    if not _RAIZ.fullmatch(s):
        raise ValueError("raiz de CNPJ com 8 caracteres [0-9A-Z]")
    return s


def _data_civil(s: str) -> str:
    if not _DATA_CIVIL.fullmatch(s):
        raise ValueError("data civil YYYY-MM-DD")
    try:
        date.fromisoformat(s)
    except ValueError as exc:
        raise ValueError("data inexistente") from exc
    return s


def _uuid(s: str) -> str:
    if not _UUID.fullmatch(s):
        raise ValueError("uuid inválido")
    return s


def _datetime_z(s: str) -> str:
    m = _DATETIME_Z.fullmatch(s)
    if m is None:
        raise ValueError("instante ISO 8601 em UTC (Z)")
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        raise ValueError("data inexistente") from exc
    return s


def _entre(minimo: int, maximo: int):
    def _v(n: int) -> int:
        if n < minimo or n > maximo:
            raise ValueError(f"inteiro em {minimo}..{maximo}")
        return n

    return AfterValidator(_v)


def _quantidade(v: Any) -> float | int:
    """`z.number().positive().multipleOf(0.01).max(999_999_999_999.99)`."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError("quantidade numérica")
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError("quantidade finita")
    try:
        d = Decimal(str(v))
    except InvalidOperation as exc:
        raise ValueError("quantidade numérica") from exc
    if d <= 0 or d > _TETO_QUANTIDADE:
        raise ValueError("quantidade > 0 e até 999.999.999.999,99")
    if d != d.quantize(Decimal("0.01")):
        raise ValueError("quantidade com no máximo 2 casas")
    return v


Cnpj = Annotated[StrictStr, AfterValidator(_cnpj)]
Raiz = Annotated[StrictStr, AfterValidator(_raiz)]
DataCivil = Annotated[StrictStr, AfterValidator(_data_civil)]
Uuid = Annotated[StrictStr, AfterValidator(_uuid)]
InstanteZ = Annotated[StrictStr, AfterValidator(_datetime_z)]
Centavos = Annotated[StrictInt, _entre(1, TETO_CENTAVOS)]
Revisao = Annotated[StrictInt, _entre(1, INT4_MAX)]
ErpProdutoId = Annotated[StrictStr, _texto(1, 128, trim=True)]
Cursor = Annotated[StrictStr, _texto(1, 256)]
Quantidade = Annotated[Any, BeforeValidator(_quantidade)]
MotivoFila = Literal[
    "rede_travada", "pedido_sem_preco", "sem_preco_tabela", "preco_acima", "preco_abaixo"
]


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ─── (a) GET /api/portal-pedidos/precos/tabela ─────────────────────────────


class TabelaRedeV1(_Base):
    tipo: Literal["rede"]
    redeId: Uuid  # noqa: N815 — nome do contrato
    nome: Annotated[StrictStr, _texto(1, 200)]
    ativa: StrictBool
    travada: StrictBool
    raizes: Annotated[list[Raiz], Field(max_length=50)]
    cnpjs: Annotated[list[Cnpj], Field(max_length=500)]


class TabelaPrecoV1(_Base):
    tipo: Literal["preco"]
    precoId: Uuid  # noqa: N815
    redeId: Uuid | None  # noqa: N815 — None = preço base do tenant
    empresaFaturadoraCnpj: Cnpj  # noqa: N815
    erpProdutoId: ErpProdutoId  # noqa: N815
    unidadeErp: Annotated[StrictStr, _texto(1, 16, trim=True)] | None  # noqa: N815
    precoCentavos: Centavos  # noqa: N815
    vigenciaInicio: DataCivil  # noqa: N815
    registradoEm: InstanteZ  # noqa: N815
    cancelado: StrictBool


class TabelaPaginaV1(_Base):
    schema_: Literal["precos.tabela.v1"] = Field(alias="schema")
    capacidades: list[StrictStr]
    geradoEm: InstanteZ  # noqa: N815
    empresaFaturadoraCnpj: Cnpj | None  # noqa: N815
    reiniciar: StrictBool
    mudancas: list[Annotated[TabelaRedeV1 | TabelaPrecoV1, Field(discriminator="tipo")]]
    cursor: Cursor
    temMais: StrictBool  # noqa: N815


# ─── (b) POST /api/portal-pedidos/precos/aprovacoes ────────────────────────


class AprovacaoItemV1(_Base):
    itemChave: Annotated[StrictStr, _texto(1, 128)]  # noqa: N815 — nome do contrato
    linha: Annotated[StrictInt, _entre(0, INT4_MAX)]
    erpProdutoId: ErpProdutoId  # noqa: N815
    codigo: Annotated[StrictStr, _texto(1, 128, trim=True)]
    descricao: Annotated[StrictStr, _texto(1, 512)]
    unidadeErp: Annotated[StrictStr, _texto(1, 16, trim=True)]  # noqa: N815
    quantidade: Quantidade
    precoPedidoCentavos: Centavos | None  # noqa: N815
    precoTabelaCentavos: Centavos | None  # noqa: N815
    precoTabelaId: Uuid | None  # noqa: N815
    situacao: Literal["ok", "na_fila"]
    motivo: MotivoFila | None


class ClienteV1(_Base):
    nome: Annotated[StrictStr, _texto(1, 255)]
    cnpj: Cnpj


class AprovacaoPedidoV1(_Base):
    schema_: Literal["precos.aprovacao.v1"] = Field(alias="schema")
    tipo: Literal["pedido"]
    externalId: Annotated[StrictStr, _texto(1, 128)]  # noqa: N815
    revisao: Revisao
    conteudoHash: Annotated[StrictStr, _texto(16, 128)]  # noqa: N815
    empresaFaturadoraCnpj: Cnpj  # noqa: N815
    pedidoNumero: Annotated[StrictStr, _texto(1, 64)]  # noqa: N815
    emitidoEm: InstanteZ | None  # noqa: N815
    recebidoEm: InstanteZ  # noqa: N815
    cliente: ClienteV1
    redeId: Uuid  # noqa: N815
    tabelaCursor: Cursor  # noqa: N815
    itens: Annotated[list[AprovacaoItemV1], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def _regras_dos_itens(self) -> AprovacaoPedidoV1:
        """Os `superRefine` do `aprovacaoEnvioV1` para `tipo: "pedido"`."""
        erros: list[str] = []
        chaves: set[str] = set()
        na_fila = 0
        for i, it in enumerate(self.itens):
            if it.itemChave in chaves:
                erros.append(f"itens[{i}].itemChave repetido")
            chaves.add(it.itemChave)
            if (it.precoTabelaCentavos is None) != (it.precoTabelaId is None):
                erros.append(f"itens[{i}].precoTabelaId acompanha precoTabelaCentavos")
            if it.situacao == "ok":
                if it.motivo is not None:
                    erros.append(f"itens[{i}].motivo: item ok não tem motivo")
                if (
                    it.precoPedidoCentavos is None
                    or it.precoPedidoCentavos != it.precoTabelaCentavos
                ):
                    erros.append(
                        f"itens[{i}].situacao: item ok exige preço do pedido igual ao da tabela"
                    )
                continue
            na_fila += 1
            ped, tab = it.precoPedidoCentavos, it.precoTabelaCentavos
            coerente = (
                it.motivo == "rede_travada"
                or (it.motivo == "pedido_sem_preco" and ped is None)
                or (it.motivo == "sem_preco_tabela" and ped is not None and tab is None)
                or (
                    it.motivo == "preco_acima" and ped is not None and tab is not None and ped > tab
                )
                or (
                    it.motivo == "preco_abaixo"
                    and ped is not None
                    and tab is not None
                    and ped < tab
                )
            )
            if not coerente:
                erros.append(f"itens[{i}].motivo incoerente com os preços do item")
        if na_fila == 0:
            erros.append("itens: pedido sem item na fila não vai para aprovação")
        if erros:
            raise ValueError("; ".join(erros))
        return self


class AprovacaoRetiradaV1(_Base):
    schema_: Literal["precos.aprovacao.v1"] = Field(alias="schema")
    tipo: Literal["retirada"]
    externalId: Annotated[StrictStr, _texto(1, 128)]  # noqa: N815
    empresaFaturadoraCnpj: Cnpj  # noqa: N815
    revisao: Revisao
    motivo: Annotated[StrictStr, _texto(1, 500, trim=True)]


_ENVIO = TypeAdapter(
    Annotated[AprovacaoPedidoV1 | AprovacaoRetiradaV1, Field(discriminator="tipo")]
)


def validar_envio(corpo: dict[str, Any]) -> AprovacaoPedidoV1 | AprovacaoRetiradaV1:
    """`aprovacaoEnvioV1`. Corpo fora do contrato → `ValueError`
    (`pydantic.ValidationError` é subclasse)."""
    return _ENVIO.validate_python(corpo)


class AprovacaoItemRespostaV1(_Base):
    itemChave: StrictStr  # noqa: N815
    situacao: Literal[
        "conforme_tabela", "aprovado_automatico", "pendente", "decidido", "invalidado"
    ]


class AprovacaoRespostaV1(_Base):
    """201/200 da rota (b). Informativa: a fonte das decisões é a rota (c)."""

    aprovacaoId: Uuid  # noqa: N815 — UUID nulo na retirada de externalId desconhecido
    externalId: StrictStr  # noqa: N815
    empresaFaturadoraCnpj: Cnpj  # noqa: N815
    revisao: Revisao
    situacao: Literal["pendente", "decidida", "retirada"]
    jaRecebido: StrictBool  # noqa: N815
    itens: list[AprovacaoItemRespostaV1]


# ─── (c) GET /api/portal-pedidos/precos/decisoes ───────────────────────────

ResultadoDecisao = Literal[
    "conforme_tabela", "aprovado_automatico", "acordo_comercial", "virar_regra", "recusado"
]


class DecisaoPrecoV1(_Base):
    decisaoId: Uuid  # noqa: N815
    externalId: StrictStr  # noqa: N815
    empresaFaturadoraCnpj: Cnpj  # noqa: N815
    revisao: Annotated[StrictInt, _entre(1, 2**63 - 1)]
    itemChave: StrictStr  # noqa: N815
    erpProdutoId: ErpProdutoId  # noqa: N815
    resultado: ResultadoDecisao
    precoAprovadoCentavos: Centavos | None  # noqa: N815
    precoTabelaNoMomentoCentavos: Centavos | None  # noqa: N815
    precoIdCriado: Uuid | None  # noqa: N815
    motivoRecusa: Annotated[StrictStr, _texto(0, 500, code_points=True)] | None  # noqa: N815
    observacao: Annotated[StrictStr, _texto(0, 1000, code_points=True)] | None
    decisor: Literal["automatico", "aprovador"]
    decididoEm: InstanteZ  # noqa: N815

    @model_validator(mode="after")
    def _coerencia(self) -> DecisaoPrecoV1:
        erros: list[str] = []
        recusado = self.resultado == "recusado"
        if recusado != (self.precoAprovadoCentavos is None):
            erros.append("precoAprovadoCentavos: preço aprovado é null só em recusado")
        if recusado != (self.motivoRecusa is not None):
            erros.append("motivoRecusa: motivo de recusa só (e sempre) em recusado")
        if (self.resultado == "virar_regra") != (self.precoIdCriado is not None):
            erros.append("precoIdCriado: só (e sempre) em virar_regra")
        automatico = self.resultado in ("conforme_tabela", "aprovado_automatico")
        if automatico != (self.decisor == "automatico"):
            erros.append("decisor incoerente com o resultado")
        if erros:
            raise ValueError("; ".join(erros))
        return self


class DecisoesPaginaV1(_Base):
    schema_: Literal["precos.decisoes.v1"] = Field(alias="schema")
    empresaFaturadoraCnpj: Cnpj | None  # noqa: N815
    decisoes: list[DecisaoPrecoV1]
    cursor: Cursor | None
    temMais: StrictBool  # noqa: N815


# ─── (c) POST /api/portal-pedidos/precos/decisoes/confirmar ────────────────


class ConfirmacaoV1(_Base):
    decisaoId: Uuid  # noqa: N815
    resultado: Literal["aplicada", "descartada"]


class ConfirmacaoDecisoesV1(_Base):
    schema_: Literal["precos.decisoes.confirmacao.v1"] = Field(alias="schema")
    empresaFaturadoraCnpj: Cnpj | None = None  # noqa: N815 — o Portal manda sempre
    confirmacoes: Annotated[list[ConfirmacaoV1], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def _sem_repeticao(self) -> ConfirmacaoDecisoesV1:
        """uuid não tem caixa: o mesmo id em maiúsculas e minúsculas é repetido."""
        ids: set[str] = set()
        for i, c in enumerate(self.confirmacoes):
            chave = c.decisaoId.lower()
            if chave in ids:
                raise ValueError(f"confirmacoes[{i}].decisaoId repetido")
            ids.add(chave)
        return self


class ConfirmacaoResultadoV1(_Base):
    decisaoId: Uuid  # noqa: N815
    situacao: Literal["confirmada", "ja_confirmada", "conflito", "invalidada", "nao_encontrada"]


class ConfirmacaoRespostaV1(_Base):
    resultados: list[ConfirmacaoResultadoV1]
