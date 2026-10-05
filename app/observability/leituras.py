"""Log de leituras — diagnóstico do que o pipeline fez com cada arquivo.

Módulo leve de propósito: sem imports de pipeline, web ou persistência, para
o pipeline poder importá-lo sem ciclo.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field

_MAX_ROTULO = 40
_MAX_LISTA = 20
_MAX_BYTES = 16 * 1024
_VERSAO: dict | None = None
_VERSAO_CHAVE: tuple | None = None  # (caminho, mtime) do applied_update.json em cache
_RE_ROTULO_NUMERO = re.compile(r"^(OBSERVA|CNPJ|TOTAL|DATA)", re.IGNORECASE)
_LINHAS_CABECALHO = 30
_MAX_CELULAS = 20
_MIN_ROTULO = 3
# Sinais que apontam leitura provavelmente errada; os demais são informativos
# (o formato do cliente os produz legitimamente: numero_gerado, sem_cliente...).
_SINAIS_FORTES = frozenset(
    {
        "parser_generico",
        "llm_usado",
        "item_parece_rotulo",
        "numero_parece_rotulo",
        "validacao_reprovada",
        "cliente_igual_ao_ambiente",
    }
)


@dataclass
class Diagnostico:
    fmt: str | None = None
    extracao: dict = field(default_factory=dict)
    parser_usado: str | None = None
    parsers_pulados: list[dict] = field(default_factory=list)
    llm: dict = field(default_factory=dict)
    parser_ms: int = 0
    validacao_ok: bool | None = None
    avisos: list[str] = field(default_factory=list)


def cabecalho_candidato(rows: list) -> dict | None:
    """Linha de `rows[:30]` com MAIS rótulos curtos de texto sem dígito nem '@' (empate: a primeira).

    A primeira linha com texto é o bloco de cadastro (RAZÃO SOCIAL, CONTATO...); o
    cabeçalho da tabela é a linha mais larga em rótulos. Rótulo = texto de 3 a 40
    caracteres. Heurística de diagnóstico, não decide nada. `linha` é o índice
    (base 0) em `rows`; `celulas` são só as células da linha escolhida (até 20).
    """
    melhor: tuple[int, list[str]] | None = None
    for idx, row in enumerate(rows[:_LINHAS_CABECALHO]):
        celulas = [
            c.strip()
            for c in row
            if isinstance(c, str)
            and c.strip()
            and _MIN_ROTULO <= len(c.strip()) <= _MAX_ROTULO
            and "@" not in c
            and not any(ch.isdigit() for ch in c)
        ]
        if len(celulas) >= 3 and (melhor is None or len(celulas) > len(melhor[1])):
            melhor = (idx, celulas)
    if melhor is None:
        return None
    return {"linha": melhor[0], "celulas": melhor[1][:_MAX_CELULAS]}


def _digitos(valor: str | None) -> str:
    return "".join(ch for ch in (valor or "") if ch.isdigit())


def _fin(x) -> bool:
    """Número finito (NaN/inf vêm de planilha corrompida e derrubariam `int(...)`)."""
    return x is not None and math.isfinite(x)


def _soma_valor(order) -> float:
    total = 0.0
    for it in order.items:
        if _fin(it.total_price):
            total += it.total_price
        elif _fin(it.quantity) and _fin(it.unit_price):
            total += it.quantity * it.unit_price
    return round(total, 2)


def sinais_suspeitos(order, diag: Diagnostico, cnpj_ambiente: str | None = None) -> list[str]:
    """Sinais de leitura suspeita (puro). Lista vazia = nada a notar.

    `cnpj_ambiente` é o CNPJ do ambiente DA LEITURA (None = sem comparação).
    """
    if order is None:
        return []
    h = order.header
    itens = order.items
    qtds = [it.quantity for it in itens if _fin(it.quantity)]
    sinais: list[str] = []

    if diag.parser_usado == "GenericParser":
        sinais.append("parser_generico")
    if (diag.llm or {}).get("tentado"):
        sinais.append("llm_usado")
    if not _digitos(h.customer_cnpj) and not any(_digitos(it.delivery_cnpj) for it in itens):
        sinais.append("sem_cnpj")
    if not (h.customer_name or "").strip():
        sinais.append("sem_cliente")
    if h.order_number_gerado:
        sinais.append("numero_gerado")
    if _RE_ROTULO_NUMERO.match((h.order_number or "").strip()):
        sinais.append("numero_parece_rotulo")
    for it in itens:
        d = (it.description or "").strip()
        if d.endswith(":") or len(d) < 4:
            sinais.append("item_parece_rotulo")
            break
    if any(q != int(q) for q in qtds):
        sinais.append("qtd_fracionada")
    if any(q > 10000 for q in qtds):
        sinais.append("qtd_max_alta")
    if itens and all(not it.unit_price and not it.total_price for it in itens):
        sinais.append("preco_ausente_em_todos")
    # Mesmo código em loja/data diferentes é normal; repetição só conta com tudo igual.
    chaves = [
        (it.product_code, it.description, it.delivery_cnpj or it.delivery_name, it.delivery_date)
        for it in itens
        if it.product_code
    ]
    if len(chaves) != len(set(chaves)):
        sinais.append("codigos_repetidos")
    if diag.validacao_ok is False:
        sinais.append("validacao_reprovada")
    cnpj = _digitos(h.customer_cnpj)
    if cnpj and cnpj_ambiente and cnpj == _digitos(cnpj_ambiente):
        sinais.append("cliente_igual_ao_ambiente")
    return sinais


def sinais_fortes(sinais: list[str]) -> bool:
    return any(s in _SINAIS_FORTES for s in sinais)


def montar_pedido(order) -> dict | None:
    """Campos do esquema `pedido` (sem `pedido_ja_importado`, que é do registrador)."""
    if order is None:
        return None
    h = order.header
    qtd = sum(it.quantity for it in order.items if _fin(it.quantity))
    return {
        "numero": h.order_number,
        "numero_gerado": h.order_number_gerado,
        "emissao": h.issue_date,
        "cliente": h.customer_name,
        "cnpj": h.customer_cnpj,
        "supplier_cnpj": h.supplier_cnpj,
        "itens": len(order.items),
        "qtd_total": int(qtd) if qtd == int(qtd) else qtd,
        "valor_total": _soma_valor(order),
        "itens_sem_preco": sum(1 for it in order.items if not it.unit_price),
        "itens_sem_codigo": sum(1 for it in order.items if not it.product_code),
    }


def resumo_fire_check(check: dict | None) -> dict | None:
    """Resumo do `check_order` só com campos do esquema (nunca copia `env`)."""
    if check is None:
        return None
    disponivel = bool(check.get("available"))
    cli = check.get("client") or {}
    sm = check.get("summary") or {}
    ps = sm.get("price_summary") or {}
    nao_encontrados: list[str] = []
    if disponivel:
        for it in check.get("items") or []:
            if it.get("match"):
                continue
            chave = it.get("product_code") or it.get("ean")
            if chave:
                nao_encontrados.append(str(chave))
            if len(nao_encontrados) >= _MAX_LISTA:
                break
    return {
        "available": disponivel,
        "reason": check.get("reason"),
        "cliente": {
            "match": cli.get("match"),
            "fire_id": cli.get("fire_id"),
            "razao": cli.get("razao_social"),
        },
        "itens": {
            "total": sm.get("items_total"),
            "match": sm.get("items_matched"),
            "missing": sm.get("items_missing"),
        },
        "nao_encontrados": nao_encontrados,
        "preco": {
            "match": ps.get("items_match"),
            "mismatch": ps.get("items_mismatch"),
            "sem_preco_fire": ps.get("items_no_price_in_fire"),
            "sem_preco_pedido": ps.get("items_no_order_price"),
        },
        "troca_no_fire": sm.get("items_troca_no_fire"),
    }


def serializar(linha: dict) -> str:
    return json.dumps(linha, default=str, ensure_ascii=False)


def _cortar_listas(obj, cortou: list[bool]):
    if isinstance(obj, list):
        if len(obj) > _MAX_LISTA:
            cortou[0] = True
            obj = obj[:_MAX_LISTA]
        return [_cortar_listas(x, cortou) for x in obj]
    if isinstance(obj, dict):
        return {k: _cortar_listas(v, cortou) for k, v in obj.items()}
    return obj


def limitar(linha: dict) -> dict:
    """Listas ≤20 e linha ≤16 KB; marca `truncado` quando perdeu algo. Não muta a entrada."""
    cortou = [False]
    out = _cortar_listas(copy.deepcopy(linha), cortou)
    if len(serializar(out).encode("utf-8")) > _MAX_BYTES:
        cortou[0] = True
        for caminho in (
            ("fire_check", "nao_encontrados"),
            ("validacao", "avisos"),
            ("extracao", "cabecalho_candidato", "celulas"),
        ):
            alvo = out
            for k in caminho[:-1]:
                alvo = alvo.get(k) if isinstance(alvo, dict) else None
            if isinstance(alvo, dict) and caminho[-1] in alvo:
                alvo[caminho[-1]] = []
    if cortou[0]:
        out["truncado"] = True
    return out


# --- escrita, versão e duplicidade (imports pesados sempre locais) --------------


def escrever(linha: dict) -> None:
    """Grava a linha no canal `leituras`. Observabilidade, nunca portão: não levanta."""
    try:
        from app.utils.logger import logger

        logger.bind(canal="leituras").info(serializar(limitar(linha)))
    except Exception as exc:  # noqa: BLE001
        try:
            from app.utils.logger import logger

            logger.warning(f"log de leituras: falha ao gravar ({type(exc).__name__}): {exc}")
        except Exception:  # noqa: BLE001
            pass


def versao_portal() -> dict:
    """`{"versao","git_commit"}` do `applied_update.json` (cache por caminho + mtime).

    O updater só grava o arquivo depois do healthcheck, então um cache eterno
    ficaria com a versão antiga/"desconhecida"; a mtime invalida.

    Lê o arquivo direto via `router.data_dir()` em vez de importar
    `app.web.routes_update._applied_info`: evita puxar FastAPI/auth para dentro
    deste módulo (e um possível ciclo com a web). Mesmo arquivo, mesmas chaves.
    """
    global _VERSAO, _VERSAO_CHAVE
    info = {"versao": "desconhecida", "git_commit": None}
    chave = None
    try:
        from app.persistence import router

        p = router.data_dir() / "applied_update.json"
        chave = (str(p), p.stat().st_mtime_ns if p.exists() else None)
        if _VERSAO is not None and _VERSAO_CHAVE == chave:
            return _VERSAO
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                info = {
                    "versao": data.get("version") or "desconhecida",
                    "git_commit": data.get("git_commit"),
                }
    except Exception:  # noqa: BLE001
        pass
    _VERSAO = info
    _VERSAO_CHAVE = chave
    return info


def sha_ja_visto(sha: str | None) -> dict | None:
    """Import do ambiente ativo com o mesmo sha256, ou None (qualquer falha → None)."""
    if not sha:
        return None
    try:
        from app.persistence import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, imported_at FROM imports WHERE file_sha256 = ? "
                "ORDER BY imported_at DESC LIMIT 1",
                (sha,),
            ).fetchone()
        return {"import_id": row[0], "quando": row[1]} if row else None
    except Exception:  # noqa: BLE001
        return None


def pedido_ja_importado(numero: str | None, cnpj: str | None, sha: str | None) -> dict | None:
    """Mesmo pedido (número+CNPJ) importado nos últimos 90 dias com OUTRO sha256."""
    if not numero or not cnpj:
        return None
    try:
        from app.persistence import db

        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, file_sha256, imported_at FROM imports "
                "WHERE order_number = ? AND customer_cnpj = ? AND file_sha256 IS NOT ? "
                "AND imported_at >= date('now','-90 day') "
                "ORDER BY imported_at DESC LIMIT 1",
                (numero, cnpj, sha),
            ).fetchone()
        return {"import_id": row[0], "sha256": row[1], "quando": row[2]} if row else None
    except Exception:  # noqa: BLE001
        return None


def _cnpj_ambiente(slug: str | None) -> str | None:
    """CNPJ do ambiente `slug` (None sem slug, sem cadastro ou em qualquer falha)."""
    if not slug:
        return None
    try:
        from app.persistence import environments_repo

        for e in environments_repo.list_active():
            if e.get("slug") == slug and e.get("cnpj"):
                return e["cnpj"]
    except Exception:  # noqa: BLE001
        pass
    return None


def _onde(tb) -> str | None:
    """`arquivo.py:linha` do último frame do traceback dentro de `app/`."""
    from pathlib import Path

    raiz = os.path.normcase(str(Path(__file__).resolve().parents[1]))
    achado = None
    while tb is not None:
        f = tb.tb_frame.f_code.co_filename
        if os.path.normcase(f).startswith(raiz):
            achado = f"{Path(f).name}:{tb.tb_lineno}"
        tb = tb.tb_next
    return achado


def _msg_excecao(exc: BaseException) -> str:
    """Mensagem curta da exceção; ValidationError do pydantic traz `input_value` (texto
    da planilha), então só `[(loc, tipo)]`."""
    if hasattr(exc, "errors"):
        try:
            return str(
                [(e.get("loc"), e.get("type")) for e in exc.errors(include_input=False)]
            )[:500]
        except Exception:  # noqa: BLE001
            return type(exc).__name__
    return str(exc)[:500]


def _ts() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _trace() -> str | None:
    try:
        from app.observability.trace import current_trace_id

        return current_trace_id()
    except Exception:  # noqa: BLE001
        return None


def _extensao(nome: str) -> str:
    return ("." + nome.rsplit(".", 1)[-1].lower()) if "." in (nome or "") else ""


def _linha_base(origem, nome, usuario, ambiente, tamanho, sha, copia) -> dict:
    return {
        "v": 1,
        "ts": _ts(),
        "portal": versao_portal(),
        "trace_id": _trace(),
        "origem": origem,
        "ambiente": ambiente,
        "usuario": usuario,
        "arquivo": {
            "nome": nome,
            "ext": _extensao(nome),
            "tamanho": tamanho,
            "sha256": sha,
            "copia": str(copia) if copia else None,
            "sha_ja_visto": None,
        },
        "etapa": None,
        "rejeicao": None,
        "extracao": None,
        "parser": None,
        "pedido": None,
        "validacao": None,
        "suspeito": {"sinais": [], "forte": False},
        "fire_check": None,
        "resultado": None,
        "excecao": None,
        "ms_total": None,
    }


def rejeitado(
    origem: str,
    nome: str,
    motivo: str,
    detalhe: str | None = None,
    tamanho: int | None = None,
    usuario: str | None = None,
    ambiente: str | None = None,
    resultado: str | None = None,
) -> None:
    """Uma linha `etapa="rejeitado"` para arquivo barrado antes do parse. Não levanta."""
    try:
        linha = _linha_base(origem, nome, usuario, ambiente, tamanho, None, None)
        linha["etapa"] = "rejeitado"
        linha["rejeicao"] = {"motivo": motivo, "detalhe": detalhe}
        linha["resultado"] = resultado
        escrever(linha)
    except Exception:  # noqa: BLE001
        pass


class registrar:  # noqa: N801 — usado como context manager: `with leituras.registrar(...)`
    """Escreve UMA linha por arquivo lido, também quando há exceção (que re-levanta)."""

    def __init__(self, origem, nome, raw=None, usuario=None, ambiente=None, copia=None):
        self.origem = origem
        self.nome = nome
        self.raw = raw
        self.usuario = usuario
        self.ambiente = ambiente
        self.copia = copia
        self.diag = Diagnostico()
        self.order = None
        self.check: dict | None = None
        self.resultado: str | None = None
        self._t0 = 0.0
        self._falha: tuple[BaseException, object] | None = None
        self._sha: str | None = None
        self._ja_visto: dict | None = None

    def __enter__(self):
        self._t0 = time.perf_counter()
        if self.raw is not None:
            # Antes do trabalho do chamador: no worker o `with` engloba o insert do
            # próprio import, e consultar no `__exit__` acharia a si mesmo.
            self._sha = hashlib.sha256(self.raw).hexdigest()
            self._ja_visto = sha_ja_visto(self._sha)
        return self

    def falha(self, exc: BaseException) -> None:
        """Registra uma exceção que o chamador vai engolir (ex.: insert do worker)."""
        self._falha = (exc, exc.__traceback__)

    def processar(self, loaded):
        from app import pipeline  # local: pipeline importa este módulo

        try:
            self.order = pipeline.process_detailed(loaded, self.diag)
        except BaseException as exc:
            # Guarda antes de re-levantar: quem chama pode engolir a exceção (o scan do
            # worker faz isso) e o `__exit__` nunca a veria.
            self._falha = (exc, exc.__traceback__)
            raise
        return self.order

    def __exit__(self, exc_type, exc, tb):
        if exc is None and self._falha is not None:
            exc, tb = self._falha
        try:
            escrever(self._montar(exc, tb))
        except Exception as e:  # noqa: BLE001
            try:
                from app.utils.logger import logger

                logger.warning(f"log de leituras: falha ao montar linha ({type(e).__name__}): {e}")
            except Exception:  # noqa: BLE001
                pass
        return False

    def _montar(self, exc, tb) -> dict:
        raw = self.raw
        sha = self._sha
        linha = _linha_base(
            self.origem,
            self.nome,
            self.usuario,
            self.ambiente,
            len(raw) if raw is not None else None,
            sha,
            self.copia,
        )
        linha["arquivo"]["sha_ja_visto"] = self._ja_visto
        d = self.diag
        order = self.order

        if d.extracao or d.fmt:
            ex = d.extracao or {}
            linha["extracao"] = {
                "fmt": d.fmt,
                "linhas": ex.get("linhas"),
                "abas": [
                    {"nome": a.get("name"), "linhas": a.get("rows")} for a in ex.get("abas") or []
                ],
                "cabecalho_candidato": ex.get("cabecalho_candidato"),
            }
        if d.parser_usado or d.parsers_pulados or d.llm:
            linha["parser"] = {
                "usado": d.parser_usado,
                "ms": d.parser_ms,
                "pulados": d.parsers_pulados,
                "llm": d.llm or None,
            }
        if order is not None:
            ped = montar_pedido(order)
            ped["pedido_ja_importado"] = pedido_ja_importado(
                order.header.order_number, order.header.customer_cnpj, sha
            )
            linha["pedido"] = ped
            sinais = sinais_suspeitos(order, d, _cnpj_ambiente(self.ambiente))
            linha["suspeito"] = {"sinais": sinais, "forte": sinais_fortes(sinais)}
        if d.validacao_ok is not None:
            linha["validacao"] = {"ok": d.validacao_ok, "avisos": list(d.avisos)}
        linha["fire_check"] = resumo_fire_check(self.check)

        linha["etapa"] = "parseado" if order is not None else "erro"
        linha["resultado"] = self.resultado
        status = getattr(exc, "status_code", None) if exc is not None else None
        if exc is not None and status is not None:
            linha["resultado"] = str(status)
        elif exc is not None:
            linha["etapa"] = "erro"
            linha["resultado"] = "erro"
            linha["excecao"] = {
                "tipo": type(exc).__name__,
                "msg": _msg_excecao(exc),
                "onde": _onde(tb),
            }
        linha["ms_total"] = int((time.perf_counter() - self._t0) * 1000)
        return linha
