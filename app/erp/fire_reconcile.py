"""Reconciliação: achar no Fire o pedido que a operação cadastrou à mão.

O portal tem pedidos eternamente "em revisão" porque a operadora exporta XLS
e cadastra à mão no Firebird do Fire Sistemas, e o portal nunca fica sabendo.
Este módulo LÊ o Fire e devolve, para cada pedido pendente do portal, se ele
já foi cadastrado lá — para tirar da fila de revisão só o que está confirmado.

Regra que atravessa o arquivo: a chave é SEMPRE dupla — número do pedido E
identidade do cliente (override de código, CNPJ do header, ou CNPJ de TODAS
as lojas de entrega). Casar por número sozinho tira pedido da fila de trabalho
sem ele estar no ERP, que é o pior desfecho possível desta feature — pior que
não achar nada. Nenhum dos três caminhos abaixo (`_decidir_candidato`) resolve
sem uma dessas âncoras de cliente; um candidato sem nenhuma delas nunca casa.

Este módulo só LÊ o Firebird. Não conhece `Order`, não escreve nada, nunca
levanta — qualquer falha vira dicionário vazio + log.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.erp.cnpj import cnpj_digits
from app.erp.connection import FirebirdConnection
from app.erp.numero_pedido import variantes
from app.erp.queries import FIND_ORDERS_BY_PEDIDO_CLIENTE
from app.persistence import environments_repo
from app.utils.logger import logger

# Quantos números por ida ao banco. Firebird aceita uma IN-clause grande, mas
# blocos limitados mantêm a query previsível e o SQL montado curto — 308
# pedidos pendentes viram ~2 idas ao banco, não 308.
_BLOCO = 200

# Guarda temporal: descarta match cujo DATA_PEDIDO no Fire é mais antigo que
# "data do candidato − 90 dias". Números curtos e sequenciais (K01, MF048,
# AF198) se repetem entre anos no MESMO cliente — sem isto, um K01 de 2024 do
# mesmo CNPJ fecharia a chave dupla e marcaria como reconciliado um pedido de
# 2026 que na verdade nunca foi cadastrado.
_JANELA_DIAS = 90

# Cool-down após falha em `connect_with_config`, em segundos. Mesma razão do
# `depara_cliente`: `fdb` não expõe timeout, host inalcançável trava a conexão
# por dezenas de segundos. Arma SÓ em volta da conexão — erro depois dela (SQL
# malformado, charset, linha ruim) loga e devolve vazio sem armar, porque um
# erro de dado não pode suprimir a reconciliação do ambiente inteiro por 45s
# (foi exatamente esse desvio que virou item de backlog no `depara_cliente`).
_COOLDOWN_S = 45.0

# Clock injetável — testes controlam o tempo sem sleep de parede real.
_clock = time.monotonic

# Cool-down por env_slug: timestamp (`_clock()`) da última falha de conexão.
_FALHA_RECENTE: dict[str, float] = {}


@dataclass(frozen=True)
class Candidato:
    """Um pedido pendente do portal, com as âncoras de cliente disponíveis."""

    import_id: str
    numero: str
    cliente_codigo: int | None  # de imports.cliente_override_codigo
    cnpj_header: str | None  # de imports.customer_cnpj
    cnpjs_entrega: tuple[str, ...]  # delivery_cnpj distintos do snapshot
    data_pedido: str | None  # ISO, para a guarda temporal


@dataclass(frozen=True)
class Achado:
    """Um pedido do portal que foi encontrado no Fire, e por qual caminho."""

    import_id: str
    fire_codigo: int
    fire_status: str
    caminho: int  # 1 = override, 2 = CNPJ header, 3 = lojas
    # Só o caminho 3 avalia lojas de fato — quantos CNPJs de entrega distintos
    # casaram. Caminhos 1/2 usam `0`, não `1`: a checagem ali foi por cliente
    # inteiro, não por loja, e `1` mentiria "bati 1 loja" pra quem ler o campo.
    lojas_casadas: int


def limpar_cache() -> None:
    """Zera o cool-down de falha (testes/reconfiguração)."""
    _FALHA_RECENTE.clear()


def buscar_no_fire(candidatos: list[Candidato], *, env_slug: str) -> dict[str, Achado]:
    """Para cada candidato, tenta achar o pedido correspondente no Fire.

    Devolve só os `import_id` que casaram — chave dupla fechada (número E
    identidade de cliente) e dentro da janela temporal. Nunca levanta.
    """
    if not candidatos:
        return {}

    numeros = _todos_numeros(candidatos)
    if not numeros:
        return {}

    falha_em = _FALHA_RECENTE.get(env_slug)
    if falha_em is not None and (_clock() - falha_em) < _COOLDOWN_S:
        # Cool-down ativo: nem tenta a rede.
        return {}

    try:
        env = environments_repo.get_by_slug(env_slug)
    except Exception as exc:  # noqa: BLE001 — nunca levanta a partir do Fire
        logger.warning(f"fire_reconcile: lookup do ambiente '{env_slug}' falhou: {exc}")
        return {}

    if env is None:
        logger.warning(f"fire_reconcile: ambiente '{env_slug}' não existe")
        return {}

    try:
        cfg = environments_repo.to_fb_config(env)
    except Exception as exc:  # noqa: BLE001 — nunca levanta a partir do Fire
        # Não é falha de rede (lê SQLite local + decripta senha) — não arma
        # cool-down, só loga e desiste desta chamada.
        logger.warning(f"fire_reconcile: config do ambiente '{env_slug}' inválida: {exc}")
        return {}

    # Conexão isolada num try próprio: só ela arma o cool-down.
    try:
        mgr = FirebirdConnection().connect_with_config(cfg)
        conn = mgr.__enter__()
    except Exception as exc:  # noqa: BLE001 — nunca levanta a partir do Fire
        logger.warning(f"fire_reconcile: conexão ao Fire ('{env_slug}') falhou: {exc}")
        _FALHA_RECENTE[env_slug] = _clock()
        return {}

    # Conectou — Firebird está de pé. Limpa cool-down anterior desse slug.
    _FALHA_RECENTE.pop(env_slug, None)

    # Leitura num try separado: erro aqui (SQL malformado, charset, linha
    # ruim) loga e devolve vazio, mas NÃO arma o cool-down.
    try:
        linhas = _consultar_em_blocos(conn, numeros)
    except Exception as exc:  # noqa: BLE001 — nunca levanta a partir do Fire
        logger.warning(f"fire_reconcile: leitura do Fire ('{env_slug}') falhou: {exc}")
        with contextlib.suppress(Exception):
            mgr.__exit__(type(exc), exc, exc.__traceback__)
        return {}

    with contextlib.suppress(Exception):
        mgr.__exit__(None, None, None)

    # Dado cru do Fire a partir daqui (TRIM que falha em não-string, CODIGO
    # nulo que quebra o min() do desempate) — try próprio, sem armar cool-down
    # (não é falha de conexão, é dado ruim numa linha).
    try:
        indice = _indexar_por_numero(linhas)

        achados: dict[str, Achado] = {}
        for candidato in candidatos:
            linhas_candidato = _linhas_do_candidato(candidato, indice)
            achado = _decidir_candidato(candidato, linhas_candidato)
            if achado is not None:
                achados[candidato.import_id] = achado
        return achados
    except Exception as exc:  # noqa: BLE001 — nunca levanta a partir do Fire
        logger.warning(f"fire_reconcile: dado do Fire ('{env_slug}') inesperado: {exc}")
        return {}


def _todos_numeros(candidatos: list[Candidato]) -> list[str]:
    """Une as variantes de número de todos os candidatos, sem duplicatas."""
    vistos: set[str] = set()
    numeros: list[str] = []
    for candidato in candidatos:
        for variante in variantes(candidato.numero):
            if variante not in vistos:
                vistos.add(variante)
                numeros.append(variante)
    return numeros


def _consultar_em_blocos(conn, numeros: list[str]) -> list[tuple]:
    """Consulta `numeros` em blocos de `_BLOCO`, uma query por bloco."""
    linhas: list[tuple] = []
    for inicio in range(0, len(numeros), _BLOCO):
        bloco = numeros[inicio : inicio + _BLOCO]
        cur = conn.cursor()
        try:
            cur.execute(FIND_ORDERS_BY_PEDIDO_CLIENTE(len(bloco)), bloco)
            linhas.extend(cur.fetchall())
        finally:
            cur.close()
    return linhas


def _indexar_por_numero(linhas: list[tuple]) -> dict[str, list[tuple]]:
    """Agrupa as linhas devolvidas pelo Fire pela coluna 0 (PEDIDO_CLIENTE, já TRIMada)."""
    indice: dict[str, list[tuple]] = {}
    for linha in linhas:
        numero = (linha[0] or "").strip()
        indice.setdefault(numero, []).append(linha)
    return indice


def _linhas_do_candidato(candidato: Candidato, indice: dict[str, list[tuple]]) -> list[tuple]:
    """Todas as linhas do Fire cujo número bate com alguma variante do candidato."""
    linhas: list[tuple] = []
    for variante in variantes(candidato.numero):
        linhas.extend(indice.get(variante, []))
    return linhas


def _parse_data(valor) -> date | None:
    """Normaliza `date`/`datetime`/string ISO devolvidos pelo driver. `None` em qualquer falha."""
    if valor is None:
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto:
            return None
        try:
            return date.fromisoformat(texto[:10])
        except ValueError:
            return None
    return None


def _dentro_da_janela(data_linha, data_candidato: str | None) -> bool:
    """Guarda temporal: `False` derruba a linha do conjunto de candidatas a match.

    Candidato SEM `data_pedido` (`None`/vazio) não aplica a guarda — é a regra
    da spec, guarda passa tudo. Candidato COM `data_pedido` preenchido mas
    ilegível é outro caso: não dá pra confiar, e a incerteza tem que descartar
    a linha, não deixar passar — se o desarmasse igual ao "sem data", uma
    mudança de chamador que passasse lixo na data desligaria a guarda inteira
    em silêncio. Linha do Fire ilegível segue a mesma regra (descarta).
    """
    if not data_candidato:
        return True

    referencia = _parse_data(data_candidato)
    if referencia is None:
        logger.warning(f"fire_reconcile: data_pedido do candidato ilegível: {data_candidato!r}")
        return False

    data_linha_parsed = _parse_data(data_linha)
    if data_linha_parsed is None:
        return False

    limite = referencia - timedelta(days=_JANELA_DIAS)
    return data_linha_parsed >= limite


def _decidir_candidato(candidato: Candidato, linhas: list[tuple]) -> Achado | None:
    """Decide se e como o candidato casa, na ordem 1 (override) → 2 (CNPJ header) → 3 (lojas).

    Cada caminho é tentado no máximo uma vez: qual campo do candidato está
    preenchido decide qual caminho se aplica, e só esse é tentado. Não há
    fallback entre caminhos — um candidato com `cliente_codigo` preenchido que
    não casa por ele NÃO tenta CNPJ/lojas depois. Nenhum dos três caminhos
    aceita casar só pelo número: cada `if` abaixo exige uma âncora de cliente
    não-vazia antes de olhar `linhas`, e o `return None` final cobre o
    candidato que não tem NENHUMA âncora — esse nunca casa.
    """
    linhas_na_janela = [
        linha for linha in linhas if _dentro_da_janela(linha[3], candidato.data_pedido)
    ]
    if not linhas_na_janela:
        return None

    if candidato.cliente_codigo is not None:
        casadas = [linha for linha in linhas_na_janela if linha[4] == candidato.cliente_codigo]
        if not casadas:
            return None
        return _montar_achado(candidato, casadas, caminho=1, lojas_casadas=0)

    if candidato.cnpj_header:
        alvo = cnpj_digits(candidato.cnpj_header)
        casadas = [
            linha for linha in linhas_na_janela if alvo and cnpj_digits(linha[5]) == alvo
        ]
        if not casadas:
            return None
        return _montar_achado(candidato, casadas, caminho=2, lojas_casadas=0)

    if candidato.cnpjs_entrega:
        grupos: dict[str, list[tuple]] = {}
        for linha in linhas_na_janela:
            grupos.setdefault(cnpj_digits(linha[5]), []).append(linha)

        alvo_cnpjs = {cnpj_digits(c) for c in candidato.cnpjs_entrega}
        # "" bloqueia, não descarta: um `delivery_cnpj` sem dígito ("A
        # COMBINAR", "N/A", texto livre do LLM/parser) normaliza pra "" — e
        # CADASTRO.CPF_CNPJ NULL/branco no Fire normaliza pro MESMO "". Sem
        # este guard os dois vazios se encontram e o match fecha sem nenhuma
        # âncora de cliente real. Uma loja não-verificável impede provar
        # "todas as lojas casaram" — o pedido tem que continuar `parsed`, não
        # sair da fila com uma contagem de lojas inflada.
        if not alvo_cnpjs or "" in alvo_cnpjs or any(not grupos.get(cnpj) for cnpj in alvo_cnpjs):
            # Só marca quando TODA loja de entrega tem pelo menos uma linha —
            # match parcial (2 de 3 lojas, caso Riachuelo) não é reconciliação.
            return None

        casadas = [linha for cnpj in alvo_cnpjs for linha in grupos[cnpj]]
        return _montar_achado(candidato, casadas, caminho=3, lojas_casadas=len(alvo_cnpjs))

    return None


def _montar_achado(
    candidato: Candidato, linhas: list[tuple], *, caminho: int, lojas_casadas: int
) -> Achado:
    """`fire_codigo` = menor V.CODIGO (coluna 1) entre as linhas casadas."""
    escolhida = min(linhas, key=lambda linha: linha[1])
    return Achado(
        import_id=candidato.import_id,
        fire_codigo=escolhida[1],
        fire_status=(escolhida[2] or "").strip(),
        caminho=caminho,
        lojas_casadas=lojas_casadas,
    )
