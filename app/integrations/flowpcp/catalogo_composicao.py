"""Composição de kits e código do Apontaê no catálogo enviado ao Flow.

Regra-mãe (spec 2026-10-15-catalogo-composicao-portal-design.md): o Portal só
afirma o que leu. Chave `componentes` ausente = "não mexer" no Flow; `[]` = o
Fire afirma que não tem composição (o Flow remove a dele); lista = a composição
é exatamente esta. Na dúvida, omite. Kit/pack sem linha em PRODUTOS_KIT vai sem
a chave (decisão C3 do Samuel, 15/10/2026): o PCP pode ter uma composição
provisória no Flow, que o Fire substitui quando for corrigido. Puro: sem Fire,
sem rede.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.erp.catalog_extract import ProdutoFireDTO

Linha = tuple[str | None, float | None]

MAX_FILHOS = 50
MAX_QUANTIDADE = 100_000
MAX_DIGITOS_MES = 9  # cabe no integer do Flow; código de cliente (12 dígitos) não passa
_DIGITOS = re.compile(r"[0-9]+")
_SEQ = re.compile(r"[0-9]{1,10}")
# "KIT C/5", "KIT 6", "PACK C/36", "KIT3": a quantidade que o nome promete.
_QTD_NOME = re.compile(r"\b(?:KITS?|PACKS?)\s*(?:C\s*/\s*)?(\d{1,3})(?!\d)")


def envio_ligado(slug: str) -> bool:
    """`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=mm,nasmar` liga por empresa. Chave de
    migração: só ligar depois de o Flow aceitar os campos (Fatia A); antes disso
    ele recusa o lote inteiro com 400."""
    bruto = os.environ.get("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", "")
    return slug.lower() in {s.strip().lower() for s in bruto.split(",") if s.strip()}


def _var_excluir(slug: str) -> str:
    return "PORTAL_FLOWPCP_CATALOGO_EXCLUIR_" + re.sub(r"[^A-Z0-9]", "_", slug.upper())


def excluir_guardado(slug: str) -> frozenset[str]:
    """SEQs que ficam fora de todo envio do ambiente (manual e diário) até o Fire
    ser corrigido: em 15/10/2026, os pares duplicados do listrado Sam's. Valor
    inválido levanta ValueError: melhor não enviar do que enviar o duplicado."""
    nome = _var_excluir(slug)
    seqs: set[str] = set()
    for parte in os.environ.get(nome, "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        if not _SEQ.fullmatch(parte):
            raise ValueError(f"{nome}: '{parte}' não é um código do Fire (SEQ)")
        seqs.add(str(int(parte)))
    return frozenset(seqs)


def codigo_mes(tipo: str, ativo: bool, codigo_alternativo: str | None) -> str | None:
    """Código do par no Apontaê = CODPROD_ALTERN só dígitos, e só em par ativo: na
    MM, 116 kits têm código alternativo numérico que é código de cliente (0 batem
    com o Apontaê), contra 405 de 410 pares que batem (15/10/2026)."""
    if tipo != "simples" or not ativo or not codigo_alternativo:
        return None
    valor = codigo_alternativo.strip()
    if not _DIGITOS.fullmatch(valor) or len(valor) > MAX_DIGITOS_MES:
        return None
    return valor


def codigos_mes(dtos: list[ProdutoFireDTO]) -> tuple[dict[str, str | None], dict[str, list[str]]]:
    """Código por SEQ; repetido em mais de um par → None em todos (quem ganharia o
    vínculo no Flow dependeria da ordem)."""
    brutos = {d.fire_produto_id: codigo_mes(d.tipo, d.ativo, d.codigo_alternativo) for d in dtos}
    por_codigo: dict[str, list[str]] = defaultdict(list)
    for seq, codigo in brutos.items():
        if codigo:
            por_codigo[codigo].append(seq)
    repetidos = {c: sorted(s, key=int) for c, s in por_codigo.items() if len(s) > 1}
    finais = {seq: (None if c in repetidos else c) for seq, c in brutos.items()}
    return finais, repetidos


@dataclass(frozen=True)
class ComposicaoDoEnvio:
    por_seq: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    omitidos: dict[str, str] = field(default_factory=dict)
    sem_linha: tuple[str, ...] = ()
    divergencia_nome: dict[str, str] = field(default_factory=dict)
    leitura_ok: bool = True
    motivo_falha: str | None = None


def _falha(motivo: str) -> ComposicaoDoEnvio:
    return ComposicaoDoEnvio(leitura_ok=False, motivo_falha=motivo)


def _problema(seq: str, linhas: list[Linha], enviados: set[str]) -> str | None:
    if any(f is None or q is None for f, q in linhas):
        return "linha com filho ou quantidade nulos"
    filhos = [f for f, _ in linhas]
    if seq in filhos:
        return "kit dentro dele mesmo"
    if len(linhas) > MAX_FILHOS:
        return f"mais de {MAX_FILHOS} filhos ({len(linhas)})"
    repetidos = sorted({f for f in filhos if filhos.count(f) > 1}, key=int)
    if repetidos:
        return f"filho repetido: {', '.join(repetidos)}"
    fora_faixa = [f for f, q in linhas if not 0 < q <= MAX_QUANTIDADE]
    if fora_faixa:
        return f"quantidade fora de 0 < q <= {MAX_QUANTIDADE}: {', '.join(fora_faixa)}"
    ausentes = [f for f in filhos if f not in enviados]
    if ausentes:
        return f"filho fora do envio: {', '.join(ausentes)}"
    return None


def _divergencia(nome: str, linhas: list[tuple[str, float]]) -> str | None:
    """Só aviso: pega o 12 no lugar do 2 na carga da MM (OP 6× maior)."""
    achado = _QTD_NOME.search(nome.upper())
    if not achado:
        return None
    prometido, soma = int(achado.group(1)), sum(q for _, q in linhas)
    if soma == prometido:
        return None
    return f"nome diz {prometido}, composição soma {soma:g}"


def montar_composicao(
    dtos: list[ProdutoFireDTO], lida: dict[str, list[Linha]] | None
) -> ComposicaoDoEnvio:
    """`dtos` = o que vai ao Flow nesta rodada (já sem os excluídos). Erro, leitura
    vazia ou leitura sem nenhum pai que o extract conhece = falha: nenhum item leva
    a chave (base errada ou cópia vazia não pode virar "nenhum kit tem composição")."""
    if lida is None:
        return _falha("leitura de PRODUTOS_KIT falhou")
    if not lida:
        return _falha("leitura de PRODUTOS_KIT voltou vazia")
    pais = {d.fire_produto_id for d in dtos if d.tem_composicao}
    if pais and not pais & lida.keys():
        return _falha("leitura incoerente: nenhum kit do catálogo está nela")

    enviados = {d.fire_produto_id for d in dtos}
    por_seq: dict[str, list[tuple[str, float]]] = {}
    omitidos: dict[str, str] = {}
    sem_linha: list[str] = []
    divergencia: dict[str, str] = {}
    for d in dtos:
        seq = d.fire_produto_id
        linhas = lida.get(seq, [])
        if not linhas:
            if d.tipo == "simples":
                por_seq[seq] = []
            else:
                sem_linha.append(seq)  # C3: sem a chave
            continue
        motivo = _problema(seq, linhas, enviados)
        if motivo:
            omitidos[seq] = motivo
            continue
        validas = [(str(f), float(q)) for f, q in linhas]
        por_seq[seq] = validas
        aviso = _divergencia(d.nome, validas)
        if aviso:
            divergencia[seq] = aviso
    return ComposicaoDoEnvio(
        por_seq=por_seq,
        omitidos=omitidos,
        sem_linha=tuple(sorted(sem_linha, key=int)),
        divergencia_nome=divergencia,
    )


def resumo(
    comp: ComposicaoDoEnvio, codigos: dict[str, str | None], repetidos: dict[str, list[str]]
) -> dict:
    """O que o Portal mandou, para o log, a tela e o diário (`composicao_portal`)."""
    com_lista = sum(1 for v in comp.por_seq.values() if v)
    return {
        "leitura_ok": comp.leitura_ok,
        "motivo_falha": comp.motivo_falha,
        "com_lista": com_lista,
        "pares_vazios": len(comp.por_seq) - com_lista,
        "kits_sem_composicao": len(comp.sem_linha),
        "kits_sem_composicao_seqs": list(comp.sem_linha[:50]),
        "omitidos": dict(sorted(comp.omitidos.items(), key=lambda kv: int(kv[0]))),
        "divergencia_nome": dict(sorted(comp.divergencia_nome.items(), key=lambda kv: int(kv[0]))),
        "codigo_mes": sum(1 for c in codigos.values() if c),
        "codigo_mes_repetido": repetidos,
    }
