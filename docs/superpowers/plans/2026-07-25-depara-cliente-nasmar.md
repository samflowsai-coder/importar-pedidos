# De-para de cliente intercompany (Nasmar → cliente real) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Quando o cliente do pedido é a Nasmar (intercompany), o Portal resolve o cliente REAL no Firebird da Nasmar (`.4`) e sobe esse CNPJ pro Flow — sem tocar em produto, XLS ou Fire.

**Architecture:** Um leitor ERP puro (`app/erp/depara_cliente.py`) traduz `PEDIDO_CLIENTE` → CNPJ real lendo o Firebird do ambiente da revenda. Uma camada de política (`app/integrations/flowpcp/intercompany.py`) decide **se** o de-para se aplica (config por ambiente + CNPJ do cliente) e nunca levanta. O resultado entra **só** no payload do `/recebimento` do Flow, como parâmetro opcional que atravessa hook → exporter → mapper. O `Order` nunca é mutado.

**Tech Stack:** Python 3.11+, pydantic v2, FastAPI, SQLite (`app_shared.db`), Firebird via `fdb`, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-07-25-depara-cliente-nasmar-design.md`](../specs/2026-07-25-depara-cliente-nasmar-design.md)

## Global Constraints

- Python 3.11+ (`X | None`, `match` liberados). Rodar tudo com `.venv/bin/python -m pytest`.
- `ruff check app/ tests/` e `ruff format app/ tests/` limpos antes de cada commit. `line-length = 100`.
- **Nenhuma dependência nova.**
- **TDD obrigatório:** teste falhando primeiro, depois implementação mínima.
- **Nunca mutar `Order`.** A troca de cliente existe só no payload do Flow. XLS e Fire continuam com a Nasmar (é o correto fiscalmente).
- **Best-effort:** nada nesse caminho pode levantar exceção pro chamador. Falha = fallback pra Nasmar + log.
- **Sem chute:** mais de um CNPJ distinto entre os hits = fallback, nunca escolher.
- **`TRIM` simétrico** nas comparações Firebird (`TRIM(V.PEDIDO_CLIENTE) = ?` com o bind já `.strip()`ado) — padrão fixado no commit `d505f8c`.
- **Sem hardcode** de CNPJ, IP, path ou slug: tudo vem da config do ambiente.
- Não dá pra abrir Firebird de verdade do Mac (o `fdb` quebra com a fbclient FB5) — **todos os testes usam fake de conexão**.
- Docs incrementais: só a seção afetada de `docs/ai/modules/<dominio>.md`.
- Assinatura nova sempre com **parâmetro opcional com default** (`resolucao=None`), pra não conflitar com o PR #40 aberto que mexe nos mesmos arquivos.

---

## File Structure

**Criar:**
- `app/erp/depara_cliente.py` — leitor ERP: chave → cliente real do `.4`. Não conhece `Order` nem Flow.
- `app/integrations/flowpcp/intercompany.py` — política: quando aplicar, e nada mais. Não faz SQL.
- `tests/test_depara_cliente.py`
- `tests/test_flowpcp_intercompany.py`
- `tests/test_intercompany_config.py`

**Modificar:**
- `app/erp/queries.py` — query nova `FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE`
- `app/persistence/schema_shared.py` — 2 colunas novas em `environments`
- `app/persistence/environments_repo.py` — `_PUBLIC_FIELDS` + `set_intercompany_config()`
- `app/web/routes_environments.py` — `PUT /{env_id}/intercompany`
- `app/web/static/admin-ambiente-edit.html` — bloco de form
- `app/integrations/flowpcp/schema.py` — `FaturadoPor` + campo em `RecebimentoRequest`
- `app/integrations/flowpcp/mapper.py` — `resolucao` opcional
- `app/integrations/flowpcp/exporter.py` — repassa `resolucao`
- `app/integrations/flowpcp/hook.py` — resolve + audita
- `app/web/server.py` — `rehydrate_preview` expõe `depara_cliente`
- `app/web/static/index.html` — selo no preview
- `docs/ai/modules/erp.md`, `docs/ai/modules/environments.md`

---

### Task 1: Leitor ERP do de-para (`depara_cliente`)

**Files:**
- Create: `app/erp/depara_cliente.py`
- Modify: `app/erp/queries.py` (append no fim do arquivo)
- Test: `tests/test_depara_cliente.py`

**Interfaces:**
- Consumes: `app.erp.connection.FirebirdConnection.connect_with_config(cfg)`, `app.persistence.environments_repo.get_by_slug/to_fb_config`, `app.erp.cnpj.cnpj_digits`
- Produces: `ResolucaoCliente` (frozen dataclass: `resolvido: bool`, `cnpj: str | None`, `nome: str | None`, `motivo: str`, `pedidos_no_4: list[dict]`) e `resolver_cliente_real(chave: str | None, *, revenda_slug: str) -> ResolucaoCliente`. `motivo` ∈ `{"ok","sem_chave","nao_encontrado","ambiguo","config_invalida","erro_conexao"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_depara_cliente.py
from __future__ import annotations

import pytest

import app.erp.depara_cliente as dc

# (V.CODIGO, V.STATUS, V.CODNF, C.CODIGO, C.NOME, C.RAZAO_SOCIAL, C.CPF_CNPJ)
_LINHA_AF066 = (301, "FATURADO", 9001, 55, "AUTHENTIC FEET", "AUTHENTIC FEET LTDA", "10.772.208/0001-82")
_LINHA_AF066_B = (302, "FATURADO", 9002, 55, "AUTHENTIC FEET", "AUTHENTIC FEET LTDA", "10.772.208/0001-82")
_LINHA_OUTRO = (303, "PEDIDO", None, 77, "DAKOTA NORDESTE", "DAKOTA NORDESTE S/A", "00.465.813/0004-08")


class _FakeCursor:
    def __init__(self, rows, capturado):
        self._rows = rows
        self._capturado = capturado

    def execute(self, sql, params):
        self._capturado.append((sql, params))
        return self

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows, capturado):
        self._rows = rows
        self._capturado = capturado

    def execute(self, sql, params):
        return _FakeCursor(self._rows, self._capturado).execute(sql, params)


@pytest.fixture
def fake_fire(monkeypatch):
    """Aponta o resolver pra um Firebird falso. Devolve a lista de (sql, params)."""
    capturado: list = []

    def _instalar(rows, *, boom: Exception | None = None):
        import contextlib

        class _FakeFirebird:
            @contextlib.contextmanager
            def connect_with_config(self, cfg):
                if boom is not None:
                    raise boom
                yield _FakeConn(rows, capturado)

        monkeypatch.setattr(dc, "FirebirdConnection", _FakeFirebird)
        monkeypatch.setattr(
            dc.environments_repo, "get_by_slug", lambda slug: {"id": "env-4", "slug": slug}
        )
        monkeypatch.setattr(
            dc.environments_repo, "to_fb_config", lambda env: {"path": "x.fdb", "host": "h"}
        )
        return capturado

    return _instalar


def test_match_unico_resolve(fake_fire):
    fake_fire([_LINHA_AF066])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert r.motivo == "ok"
    assert r.cnpj == "10772208000182"          # só dígitos
    assert r.nome == "AUTHENTIC FEET LTDA"     # RAZAO_SOCIAL tem precedência
    assert r.pedidos_no_4 == [{"codigo": 301, "status": "FATURADO", "codnf": 9001}]


def test_varias_linhas_mesmo_cnpj_resolve(fake_fire):
    # Caso real: AF086 tem 3 linhas no .4, todas do mesmo cliente.
    fake_fire([_LINHA_AF066, _LINHA_AF066_B])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert r.cnpj == "10772208000182"
    assert len(r.pedidos_no_4) == 2


def test_cnpjs_diferentes_e_ambiguo(fake_fire):
    fake_fire([_LINHA_AF066, _LINHA_OUTRO])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "ambiguo"
    assert r.cnpj is None


def test_zero_hits_e_nao_encontrado(fake_fire):
    fake_fire([])
    r = dc.resolver_cliente_real("PULMÃO", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "nao_encontrado"


@pytest.mark.parametrize("chave", [None, "", "   "])
def test_chave_vazia_nao_consulta_o_banco(fake_fire, chave):
    capturado = fake_fire([_LINHA_AF066])
    r = dc.resolver_cliente_real(chave, revenda_slug="nasmar")
    assert r.motivo == "sem_chave"
    assert capturado == []  # não abriu conexão à toa


def test_chave_vai_trimada_pro_bind(fake_fire):
    capturado = fake_fire([_LINHA_AF066])
    dc.resolver_cliente_real("  AF066  ", revenda_slug="nasmar")
    _sql, params = capturado[0]
    assert params == ("AF066",)


def test_nome_cai_pra_nome_quando_razao_social_vazia(fake_fire):
    fake_fire([(301, "PEDIDO", None, 55, "AUTHENTIC FEET", "   ", "10.772.208/0001-82")])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.nome == "AUTHENTIC FEET"


def test_erro_de_conexao_nao_levanta(fake_fire):
    fake_fire([], boom=RuntimeError("firebird fora do ar"))
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "erro_conexao"


def test_ambiente_inexistente_e_config_invalida(monkeypatch):
    monkeypatch.setattr(dc.environments_repo, "get_by_slug", lambda slug: None)
    r = dc.resolver_cliente_real("AF066", revenda_slug="fantasma")
    assert r.resolvido is False
    assert r.motivo == "config_invalida"


def test_resolucao_ok_e_cacheada(fake_fire):
    capturado = fake_fire([_LINHA_AF066])
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert len(capturado) == 1  # segunda chamada saiu do cache


def test_nao_encontrado_nao_e_cacheado(fake_fire):
    # O pedido pode ser criado no .4 depois — cachear negativo envenenaria
    # o processo (o servidor web fica de pé por dias).
    capturado = fake_fire([])
    dc.resolver_cliente_real("AF999", revenda_slug="nasmar")
    dc.resolver_cliente_real("AF999", revenda_slug="nasmar")
    assert len(capturado) == 2
```

Adicionar no topo do arquivo de teste, logo após os imports, um autouse que limpa o cache entre testes:

```python
@pytest.fixture(autouse=True)
def _limpa_cache():
    dc.limpar_cache()
    yield
    dc.limpar_cache()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_depara_cliente.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.erp.depara_cliente'`

- [ ] **Step 3: Add the query**

Append no fim de `app/erp/queries.py`:

```python
# ── De-para de cliente intercompany (Nasmar → cliente real) ───────────────────
# Lido no Firebird da REVENDA (.4). A chave é o PEDIDO_CLIENTE (número do pedido
# de compra do cliente final), o mesmo valor que o .7 guarda no pedido faturado
# contra a Nasmar. TRIM simétrico: o bind chega .strip()ado do chamador.
# Verificado na Fire viva 2026-07-25: 939 pedidos Nasmar no .7, 0 ambiguidade.
FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE = """
    SELECT V.CODIGO, V.STATUS, V.CODNF,
           C.CODIGO, TRIM(C.NOME), TRIM(C.RAZAO_SOCIAL), TRIM(C.CPF_CNPJ)
    FROM CAB_VENDAS V
    JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
    WHERE TRIM(V.PEDIDO_CLIENTE) = ?
"""
```

- [ ] **Step 4: Write the module**

```python
# app/erp/depara_cliente.py
"""De-para de cliente intercompany: PEDIDO_CLIENTE → cliente real da revenda.

Alguns pedidos da produção (.7 Americanense) saem no nome da Nasmar, que é
revenda: ela fatura, mas quem recebe é o cliente final (Studio Z, Beira Rio,
Dakota). O número do pedido de compra do cliente final (PEDIDO_CLIENTE) é o
mesmo nos dois bancos — então ele resolve quem é o cliente de verdade.

Este módulo só LÊ o Firebird da revenda e devolve o cliente. Não conhece
`Order`, não conhece Flow e não decide quando deve ser usado (isso é
`app/integrations/flowpcp/intercompany.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.erp.cnpj import cnpj_digits
from app.erp.connection import FirebirdConnection
from app.erp.queries import FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE
from app.persistence import environments_repo
from app.utils.logger import logger


@dataclass(frozen=True)
class ResolucaoCliente:
    """Resultado do de-para. `resolvido=False` ⇒ o chamador mantém a revenda."""

    resolvido: bool
    cnpj: str | None = None
    nome: str | None = None
    motivo: str = "sem_chave"
    # Radar da demanda fantasma: STATUS/CODNF dos pedidos casados no .4.
    pedidos_no_4: list[dict] = field(default_factory=list)


# Cache de processo. Só guarda resolução POSITIVA: o vínculo chave→cliente é
# fato histórico. Negativo nunca entra — o pedido pode ser criado na revenda
# depois, e o servidor web fica de pé por dias.
_CACHE: dict[tuple[str, str], ResolucaoCliente] = {}


def limpar_cache() -> None:
    """Zera o cache de processo (usado nos testes e em reconfiguração)."""
    _CACHE.clear()


def resolver_cliente_real(chave: str | None, *, revenda_slug: str) -> ResolucaoCliente:
    """Traduz a chave (PEDIDO_CLIENTE) no cliente real cadastrado na revenda.

    Nunca levanta: qualquer falha vira `resolvido=False` com o motivo.
    """
    chave_limpa = (chave or "").strip()
    if not chave_limpa:
        return ResolucaoCliente(False, motivo="sem_chave")

    cache_key = (revenda_slug, chave_limpa)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    env = environments_repo.get_by_slug(revenda_slug)
    if env is None:
        logger.warning(f"depara_cliente: ambiente de revenda '{revenda_slug}' não existe")
        return ResolucaoCliente(False, motivo="config_invalida")

    try:
        cfg = environments_repo.to_fb_config(env)
        with FirebirdConnection().connect_with_config(cfg) as conn:
            rows = conn.execute(FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE, (chave_limpa,)).fetchall()
    except Exception as exc:  # noqa: BLE001 — best-effort: fallback pra revenda
        logger.warning(f"depara_cliente: leitura da revenda falhou (chave={chave_limpa!r}): {exc}")
        return ResolucaoCliente(False, motivo="erro_conexao")

    resultado = _decidir(rows)
    if resultado.resolvido:
        _CACHE[cache_key] = resultado
    return resultado


def _decidir(rows: list) -> ResolucaoCliente:
    """Regra pura: só resolve com UM CNPJ distinto entre os hits.

    Vários pedidos podem dividir o mesmo PEDIDO_CLIENTE na revenda (2 a 4 é
    comum) — isso não é ambiguidade enquanto apontarem pro mesmo CNPJ.
    """
    if not rows:
        return ResolucaoCliente(False, motivo="nao_encontrado")

    pedidos = [{"codigo": r[0], "status": r[1], "codnf": r[2]} for r in rows]
    cnpjs = {cnpj_digits(r[6]) for r in rows}
    if len(cnpjs) > 1:
        logger.warning(f"depara_cliente: ambíguo, {len(cnpjs)} CNPJs distintos — mantendo revenda")
        return ResolucaoCliente(False, motivo="ambiguo", pedidos_no_4=pedidos)

    cnpj = next(iter(cnpjs))
    if not cnpj:
        return ResolucaoCliente(False, motivo="ambiguo", pedidos_no_4=pedidos)

    primeira = rows[0]
    nome = (primeira[5] or "").strip() or (primeira[4] or "").strip() or None
    return ResolucaoCliente(True, cnpj=cnpj, nome=nome, motivo="ok", pedidos_no_4=pedidos)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_depara_cliente.py -v`
Expected: PASS (12 testes)

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/erp/depara_cliente.py app/erp/queries.py tests/test_depara_cliente.py
ruff format app/erp/depara_cliente.py app/erp/queries.py tests/test_depara_cliente.py
git add app/erp/depara_cliente.py app/erp/queries.py tests/test_depara_cliente.py
git commit -m "feat(erp): de-para de cliente intercompany le o Firebird da revenda"
```

---

### Task 2: Config por ambiente (migração + repo)

**Files:**
- Modify: `app/persistence/schema_shared.py` (`COLUMN_MIGRATIONS`, no fim da tupla)
- Modify: `app/persistence/environments_repo.py` (`_PUBLIC_FIELDS` linha ~28-37; função nova no fim)
- Test: `tests/test_intercompany_config.py`

**Interfaces:**
- Consumes: `environments_repo.create/get/list_active`, `router.shared_connect()`
- Produces: colunas `intercompany_cnpj` e `intercompany_env_slug` em `environments`, visíveis no `public_view`; `environments_repo.set_intercompany_config(env_id, *, cnpj: str | None, revenda_slug: str | None) -> dict | None`

- [ ] **Step 1: Write the failing test**

O fixture `fresh_shared` é cópia literal do que já existe no topo de `tests/test_environments_repo.py` — mesmo padrão, não inventar bootstrap novo.

```python
# tests/test_intercompany_config.py
"""Config do de-para de cliente intercompany (colunas + repo + rota)."""

from __future__ import annotations

import pytest

from app.persistence import environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


@pytest.fixture
def env(fresh_shared):
    return environments_repo.create(
        slug="mm", name="MM", watch_dir="/tmp/in", output_dir="/tmp/out", fb_path="/tmp/x.fdb"
    )


def test_default_e_desligado(env):
    assert env["intercompany_cnpj"] is None
    assert env["intercompany_env_slug"] is None


def test_grava_e_le_config(env):
    atualizado = environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    assert atualizado["intercompany_cnpj"] == "34513679000134"  # normalizado p/ dígitos
    assert atualizado["intercompany_env_slug"] == "nasmar"


def test_limpar_desliga(env):
    environments_repo.set_intercompany_config(
        env["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    atualizado = environments_repo.set_intercompany_config(env["id"], cnpj="", revenda_slug="")
    assert atualizado["intercompany_cnpj"] is None
    assert atualizado["intercompany_env_slug"] is None


def test_persiste_no_get(env):
    environments_repo.set_intercompany_config(
        env["id"], cnpj="34513679000134", revenda_slug="nasmar"
    )
    lido = environments_repo.get(env["id"])
    assert lido["intercompany_cnpj"] == "34513679000134"
    assert lido["intercompany_env_slug"] == "nasmar"
```

> `get()` já devolve só `_PUBLIC_FIELDS` (via `_row_to_dict`) — por isso `test_persiste_no_get` só passa se as colunas novas tiverem sido adicionadas à tupla. É esse o teste que trava o passo 4.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_intercompany_config.py -v`
Expected: FAIL — `AttributeError: module 'app.persistence.environments_repo' has no attribute 'set_intercompany_config'`

- [ ] **Step 3: Add the migrations**

Em `app/persistence/schema_shared.py`, no fim da tupla `COLUMN_MIGRATIONS`:

```python
    # De-para de cliente intercompany: CNPJ que dispara (a revenda) + slug do
    # ambiente cujo Firebird tem o vínculo. Qualquer um vazio = desligado.
    ("environments", "intercompany_cnpj",
     "ALTER TABLE environments ADD COLUMN intercompany_cnpj TEXT"),
    ("environments", "intercompany_env_slug",
     "ALTER TABLE environments ADD COLUMN intercompany_env_slug TEXT"),
```

- [ ] **Step 4: Expose in the repo**

Em `app/persistence/environments_repo.py`, adicionar ao fim da tupla `_PUBLIC_FIELDS`:

```python
    # De-para de cliente intercompany (não-secreto).
    "intercompany_cnpj", "intercompany_env_slug",
```

E a função nova (colocar logo depois de `set_flowpcp_config`):

```python
def set_intercompany_config(
    env_id: str, *, cnpj: str | None, revenda_slug: str | None
) -> dict[str, Any] | None:
    """Config do de-para de cliente intercompany.

    `cnpj` é o CNPJ que DISPARA o de-para (a revenda que aparece como cliente,
    ex: Nasmar) e é gravado só com dígitos. `revenda_slug` é o ambiente cujo
    Firebird tem o vínculo. Qualquer um vazio desliga a feature.
    """
    from app.erp.cnpj import cnpj_digits

    fields = {
        "intercompany_cnpj": cnpj_digits(cnpj) or None,
        "intercompany_env_slug": (revenda_slug or "").strip() or None,
        "updated_at": _now(),
    }
    sets = ", ".join(f"{k} = ?" for k in fields)
    with router.shared_connect() as conn:
        conn.execute(
            f"UPDATE environments SET {sets} WHERE id = ?", [*fields.values(), env_id]
        )
    return get(env_id)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_intercompany_config.py -v`
Expected: PASS

Run: `.venv/bin/python -m pytest tests/test_environments_repo.py tests/test_web_environments.py -v` (rodar os que existirem)
Expected: PASS — nenhuma regressão no schema compartilhado

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/persistence/ tests/test_intercompany_config.py
ruff format app/persistence/ tests/test_intercompany_config.py
git add app/persistence/schema_shared.py app/persistence/environments_repo.py tests/test_intercompany_config.py
git commit -m "feat(persistence): config de de-para intercompany por ambiente"
```

---

### Task 3: Rota admin + form

**Files:**
- Modify: `app/web/routes_environments.py` (modelo novo perto de `FlowPCPConfigRequest` ~linha 56; rota nova depois de `set_environment_flowpcp` ~linha 107-113)
- Modify: `app/web/static/admin-ambiente-edit.html` (bloco de form depois do bloco FlowPCP; leitura no preenchimento ~linha 430; envio ~linha 503)
- Test: `tests/test_intercompany_config.py` (append)

**Interfaces:**
- Consumes: `environments_repo.set_intercompany_config` (Task 2), `require_admin`
- Produces: `PUT /api/admin/environments/{env_id}/intercompany` com body `{"cnpj": str|null, "revenda_slug": str|null}`

- [ ] **Step 1: Write the failing test**

Append em `tests/test_intercompany_config.py`. O `setup` + `_client()` são cópia literal do topo de `tests/test_admin_environments_routes.py` (esse arquivo NÃO testa auth — `require_admin` está desligado no TestClient; não escreva teste de 401/403 aqui, ele falharia):

```python
from pathlib import Path

from fastapi.testclient import TestClient

from app.persistence import db


@pytest.fixture
def setup_rotas(tmp_path: Path):
    import os

    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _client():
    from app.web.server import app

    return TestClient(app)


def test_rota_grava_intercompany(setup_rotas):
    criado = _client().post(
        "/api/admin/environments",
        json={
            "slug": "mm",
            "name": "MM",
            "watch_dir": str(setup_rotas / "in"),
            "output_dir": str(setup_rotas / "out"),
            "fb_path": str(setup_rotas / "x.fdb"),
        },
    ).json()
    r = _client().put(
        f"/api/admin/environments/{criado['id']}/intercompany",
        json={"cnpj": "34.513.679/0001-34", "revenda_slug": "nasmar"},
    )
    assert r.status_code == 200
    assert r.json()["intercompany_cnpj"] == "34513679000134"
    assert r.json()["intercompany_env_slug"] == "nasmar"


def test_rota_404_em_ambiente_inexistente(setup_rotas):
    r = _client().put(
        "/api/admin/environments/nao-existe/intercompany",
        json={"cnpj": "34513679000134", "revenda_slug": "nasmar"},
    )
    assert r.status_code == 404
```

> Conferir no `tests/test_admin_environments_routes.py` o payload exato aceito pelo `POST /api/admin/environments` (o teste `test_create_returns_public_view` mostra) e usar os mesmos campos.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_intercompany_config.py -v -k rota`
Expected: FAIL com 404 (rota não existe)

- [ ] **Step 3: Add the route**

Em `app/web/routes_environments.py`, depois de `FlowPCPConfigRequest`:

```python
class IntercompanyConfigRequest(BaseModel):
    """De-para de cliente intercompany. Vazio em qualquer campo = desligado."""

    cnpj: str | None = None
    revenda_slug: str | None = None
```

E a rota, depois de `set_environment_flowpcp`:

```python
@router.put("/{env_id}/intercompany")
def set_environment_intercompany(
    env_id: str, payload: IntercompanyConfigRequest, _=Depends(require_admin)
):
    if environments_repo.get(env_id) is None:
        raise HTTPException(status_code=404, detail="Ambiente não encontrado")
    return environments_repo.set_intercompany_config(env_id, **payload.model_dump())
```

> Conferir no arquivo como `set_environment_flowpcp` trata "ambiente não encontrado" (linhas ~107-113) e seguir exatamente o mesmo padrão, inclusive o `public_view` na resposta se for o caso.

- [ ] **Step 4: Add the form block**

Em `app/web/static/admin-ambiente-edit.html`, depois do bloco de checkboxes do FlowPCP (~linha 325) e antes de `<div class="actions">`:

```html
      <div style="margin-top:1.25rem;border-top:1px solid var(--border,#2a2a2a);padding-top:1rem">
        <div style="font-size:.85em;color:var(--text-muted,#9aa);margin-bottom:.6rem;line-height:1.5">
          <strong>Cliente intercompany.</strong> Quando o pedido chega no nome da revenda (ela
          fatura, mas quem produz é esta empresa), o Portal busca o cliente real no Firebird da
          revenda pelo número do pedido de compra e manda esse CNPJ pro Flow. O XLS e o Fire
          continuam com a revenda. Deixe em branco para desligar.
        </div>
        <div class="row">
          <label>
            <span>CNPJ da revenda</span>
            <input name="intercompany_cnpj" placeholder="34.513.679/0001-34">
          </label>
          <label>
            <span>Ambiente da revenda (slug)</span>
            <input name="intercompany_env_slug" placeholder="nasmar">
          </label>
        </div>
        <div class="actions" style="margin-top:.75rem">
          <button type="button" id="btn-save-intercompany" class="btn btn-secondary">Salvar de-para de cliente</button>
          <span id="intercompany-status" class="flowpcp-status"></span>
        </div>
      </div>
```

No preenchimento (perto da linha 430, junto dos outros `fv(...)`):

```javascript
        fv('intercompany_cnpj').value = env.intercompany_cnpj || '';
        fv('intercompany_env_slug').value = env.intercompany_env_slug || '';
```

E o handler do botão (seguir o padrão do `btn-save-flowpcp` que já existe no arquivo — mesmo tratamento de erro e de status):

```javascript
  document.getElementById('btn-save-intercompany').addEventListener('click', async () => {
    const status = document.getElementById('intercompany-status');
    status.textContent = 'Salvando…';
    try {
      const r = await fetch(`/api/admin/environments/${envId}/intercompany`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          cnpj: fv('intercompany_cnpj').value.trim(),
          revenda_slug: fv('intercompany_env_slug').value.trim(),
        }),
      });
      if (!r.ok) throw new Error(await r.text());
      status.textContent = 'Salvo.';
    } catch (e) {
      status.textContent = 'Erro ao salvar: ' + e.message;
    }
  });
```

> Ler como `btn-save-flowpcp` monta a URL e recupera o `envId` nesse arquivo e usar a MESMA convenção — não inventar variável nova.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_intercompany_config.py -v`
Expected: PASS

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/web/routes_environments.py tests/test_intercompany_config.py
ruff format app/web/routes_environments.py tests/test_intercompany_config.py
git add app/web/routes_environments.py app/web/static/admin-ambiente-edit.html tests/test_intercompany_config.py
git commit -m "feat(web): tela de config do de-para de cliente intercompany"
```

---

### Task 4: Payload do Flow (`faturadoPor` + cliente real)

**Files:**
- Modify: `app/integrations/flowpcp/schema.py` (classe nova + campo em `RecebimentoRequest`)
- Modify: `app/integrations/flowpcp/mapper.py` (parâmetro `resolucao`)
- Test: `tests/test_flowpcp_mapper_intercompany.py` (criar)

**Interfaces:**
- Consumes: `ResolucaoCliente` (Task 1)
- Produces: `build_recebimento_payload(*, import_id, order, tenant_id, resolucao: ResolucaoCliente | None = None)`; `RecebimentoRequest.faturadoPor: FaturadoPor | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_mapper_intercompany.py
from __future__ import annotations

from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order, OrderHeader, OrderItem

_NASMAR = "34.513.679/0001-34"


def _order() -> Order:
    return Order(
        header=OrderHeader(
            order_number="AF066",
            customer_name="Nasmar Comercio De Roupas Ltda",
            customer_cnpj=_NASMAR,
        ),
        items=[OrderItem(description="MEIA STZ", quantity=12, product_code="123", ean="789")],
        source_file="pedido.pdf",
    )


def test_sem_resolucao_mantem_payload_de_hoje():
    req = build_recebimento_payload(import_id="imp1", order=_order(), tenant_id="t1")
    assert req.cliente.cnpj == "34513679000134"
    assert req.cliente.nome == "Nasmar Comercio De Roupas Ltda"
    assert req.faturadoPor is None


def test_resolucao_troca_o_cliente_e_guarda_o_faturador():
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    req = build_recebimento_payload(
        import_id="imp1", order=_order(), tenant_id="t1", resolucao=res
    )
    assert req.cliente.cnpj == "10772208000182"
    assert req.cliente.nome == "AUTHENTIC FEET LTDA"
    assert req.faturadoPor is not None
    assert req.faturadoPor.cnpj == "34513679000134"
    assert req.faturadoPor.nome == "Nasmar Comercio De Roupas Ltda"


def test_resolucao_nao_resolvida_nao_troca_nada():
    res = ResolucaoCliente(False, motivo="nao_encontrado")
    req = build_recebimento_payload(
        import_id="imp1", order=_order(), tenant_id="t1", resolucao=res
    )
    assert req.cliente.cnpj == "34513679000134"
    assert req.faturadoPor is None


def test_itens_e_fornecedor_ficam_intactos():
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    req = build_recebimento_payload(
        import_id="imp1", order=_order(), tenant_id="t1", resolucao=res
    )
    assert req.fornecedor == "Nasmar Comercio De Roupas Ltda"
    assert req.itens[0].produtoCodigo == "123"
    assert req.itens[0].produtoEan == "789"
    assert req.itens[0].quantidade == 12


def test_order_nao_e_mutado():
    order = _order()
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    build_recebimento_payload(import_id="imp1", order=order, tenant_id="t1", resolucao=res)
    assert order.header.customer_cnpj == _NASMAR
    assert order.header.customer_name == "Nasmar Comercio De Roupas Ltda"


def test_wire_usa_camelcase():
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET LTDA", motivo="ok")
    req = build_recebimento_payload(
        import_id="imp1", order=_order(), tenant_id="t1", resolucao=res
    )
    wire = req.model_dump(by_alias=True)
    assert wire["faturadoPor"] == {"nome": "Nasmar Comercio De Roupas Ltda", "cnpj": "34513679000134"}
    assert wire["schema"] == "pedido.recebimento.v1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_flowpcp_mapper_intercompany.py -v`
Expected: FAIL — `TypeError: build_recebimento_payload() got an unexpected keyword argument 'resolucao'`

- [ ] **Step 3: Add the schema**

Em `app/integrations/flowpcp/schema.py`, antes de `RecebimentoRequest`:

```python
class FaturadoPor(BaseModel):
    """Quem FATURA quando o cliente do payload é o cliente real (intercompany).

    O contrato do Flow ainda não persiste este campo (zod descarta chave
    desconhecida em silêncio). Vai no wire desde já; quando o pcp-app aceitar,
    não precisa mexer no Importador.
    """

    nome: str
    cnpj: str | None = None
```

E o campo em `RecebimentoRequest`, depois de `cliente`:

```python
    faturadoPor: FaturadoPor | None = None  # noqa: N815 — wire é camelCase
```

- [ ] **Step 4: Wire the mapper**

Em `app/integrations/flowpcp/mapper.py`, adicionar `FaturadoPor` ao import de `schema` e `from app.erp.depara_cliente import ResolucaoCliente`, e substituir a função inteira por:

```python
def build_recebimento_payload(
    *,
    import_id: str,
    order: Order,
    tenant_id: str,
    resolucao: ResolucaoCliente | None = None,
) -> RecebimentoRequest:
    h = order.header
    itens = [
        ItemRecebimento(
            produtoCodigo=it.product_code or None,
            produtoEan=it.ean or None,
            descricao=it.description,
            quantidade=float(it.quantity),
            precoUnitario=float(it.unit_price) if it.unit_price is not None else None,
        )
        for it in order.items
    ]
    primeiro_prazo = _to_iso(order.items[0].delivery_date) if order.items else None

    # Intercompany: o Flow recebe o cliente REAL — é o CNPJ que resolve cliente
    # e marca do lado de lá — e quem fatura vai em faturadoPor. O `Order` não é
    # tocado: XLS e Fire continuam com a revenda, que é o certo fiscalmente.
    nome_cliente = h.customer_name or "(sem cliente)"
    cnpj_cliente = cnpj_digits(h.customer_cnpj) or None
    faturado_por = None
    if resolucao is not None and resolucao.resolvido:
        faturado_por = FaturadoPor(nome=nome_cliente, cnpj=cnpj_cliente)
        nome_cliente = resolucao.nome or nome_cliente
        cnpj_cliente = resolucao.cnpj or cnpj_cliente

    return RecebimentoRequest(
        externalId=import_id,
        fornecedor=h.customer_name or "(sem fornecedor)",
        pedidoNumero=h.order_number or import_id,
        emitidoEm=_to_iso(h.issue_date) or datetime.now(UTC).strftime("%Y-%m-%dT00:00:00.000Z"),
        prazoSolicitado=primeiro_prazo,
        cliente=ClienteRecebimento(nome=nome_cliente, cnpj=cnpj_cliente),
        faturadoPor=faturado_por,
        itens=itens,
        origem=OrigemRecebimento(
            importadorVersao=_IMPORTADOR_VERSAO,
            arquivoOriginal=order.source_file or "",
            parserUsado="importador",
            confiancaParser="alta",
        ),
    )
```

`fornecedor` continua sendo o cliente do arquivo (a revenda) — de propósito: fornecedor não é o campo de cliente.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_flowpcp_mapper_intercompany.py tests/test_flowpcp_mapper_cnpj.py tests/test_flowpcp_mapper_prazo.py tests/test_flowpcp_schema.py -v`
Expected: PASS — inclusive os dois de mapper que já existiam (nenhuma regressão)

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/integrations/flowpcp/ tests/test_flowpcp_mapper_intercompany.py
ruff format app/integrations/flowpcp/ tests/test_flowpcp_mapper_intercompany.py
git add app/integrations/flowpcp/schema.py app/integrations/flowpcp/mapper.py tests/test_flowpcp_mapper_intercompany.py
git commit -m "feat(flowpcp): payload leva cliente real e faturadoPor no de-para intercompany"
```

---

### Task 5: Política + wiring no push

**Files:**
- Create: `app/integrations/flowpcp/intercompany.py`
- Modify: `app/integrations/flowpcp/exporter.py` (repassar `resolucao`)
- Modify: `app/integrations/flowpcp/hook.py` (resolver + auditar)
- Test: `tests/test_flowpcp_intercompany.py`

**Interfaces:**
- Consumes: `resolver_cliente_real` (Task 1), colunas `intercompany_*` (Task 2), `build_recebimento_payload(..., resolucao=)` (Task 4), `repo.append_audit`
- Produces: `resolucao_para(order: Order, *, slug: str) -> ResolucaoCliente | None` — `None` significa "não se aplica" (feature desligada ou o cliente não é a revenda)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_intercompany.py
from __future__ import annotations

from unittest.mock import MagicMock

import app.integrations.flowpcp.intercompany as ic
from app.erp.depara_cliente import ResolucaoCliente
from app.models.order import Order, OrderHeader, OrderItem

_NASMAR = "34513679000134"


def _order(cnpj: str | None = "34.513.679/0001-34", numero: str | None = "AF066") -> Order:
    return Order(
        header=OrderHeader(order_number=numero, customer_name="Nasmar", customer_cnpj=cnpj),
        items=[OrderItem(description="MEIA", quantity=1)],
    )


def _env(**over):
    base = {"id": "e1", "slug": "mm", "intercompany_cnpj": _NASMAR, "intercompany_env_slug": "nasmar"}
    base.update(over)
    return base


def test_nao_se_aplica_quando_config_vazia(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env(intercompany_cnpj=None))
    chamou = MagicMock()
    monkeypatch.setattr(ic, "resolver_cliente_real", chamou)
    assert ic.resolucao_para(_order(), slug="mm") is None
    chamou.assert_not_called()


def test_nao_se_aplica_quando_cliente_nao_e_a_revenda(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())
    chamou = MagicMock()
    monkeypatch.setattr(ic, "resolver_cliente_real", chamou)
    assert ic.resolucao_para(_order(cnpj="06.347.409/0296-51"), slug="mm") is None
    chamou.assert_not_called()


def test_cnpj_casa_mesmo_formatado_diferente(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())
    esperado = ResolucaoCliente(True, cnpj="10772208000182", nome="AF", motivo="ok")
    monkeypatch.setattr(ic, "resolver_cliente_real", lambda chave, *, revenda_slug: esperado)
    assert ic.resolucao_para(_order(cnpj="34.513.679/0001-34"), slug="mm") is esperado


def test_usa_order_number_como_chave(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())
    visto = {}

    def _fake(chave, *, revenda_slug):
        visto["chave"] = chave
        visto["slug"] = revenda_slug
        return ResolucaoCliente(False, motivo="nao_encontrado")

    monkeypatch.setattr(ic, "resolver_cliente_real", _fake)
    ic.resolucao_para(_order(numero="AF066"), slug="mm")
    assert visto == {"chave": "AF066", "slug": "nasmar"}


def test_ambiente_inexistente_nao_levanta(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: None)
    assert ic.resolucao_para(_order(), slug="fantasma") is None


def test_erro_no_resolver_nao_levanta(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())

    def _boom(chave, *, revenda_slug):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ic, "resolver_cliente_real", _boom)
    r = ic.resolucao_para(_order(), slug="mm")
    assert r is not None and r.resolvido is False and r.motivo == "erro_conexao"
```

E, no mesmo arquivo, o wiring do hook:

```python
import app.integrations.flowpcp.hook as hook
from app.integrations.flowpcp.config import FlowPCPConfig

_CFG = FlowPCPConfig(enabled=True, base_url="https://flow.test", service_token="t", tenant_id="uuid")


def test_hook_repassa_resolucao_e_audita(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AF", motivo="ok",
                           pedidos_no_4=[{"codigo": 1, "status": "FATURADO", "codnf": 9}])
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: res)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append((i, e, d)))

    fake_exporter = MagicMock()
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: fake_exporter)
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")

    envio = fake_exporter.export if fake_exporter.export.called else fake_exporter.enqueue
    assert envio.call_args.kwargs["resolucao"] is res
    assert auditado[0][1] == "depara_cliente"
    assert auditado[0][2]["motivo"] == "ok"
    assert auditado[0][2]["cnpj_real"] == "10772208000182"
    assert auditado[0][2]["pedidos_no_4"] == [{"codigo": 1, "status": "FATURADO", "codnf": 9}]


def test_hook_nao_audita_quando_nao_se_aplica(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: None)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append(e))
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")
    assert auditado == []


def test_hook_nao_derruba_o_push_se_o_audit_falhar(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    monkeypatch.setattr(
        hook, "resolucao_para", lambda order, *, slug: ResolucaoCliente(False, motivo="ambiguo")
    )

    def _boom(*a, **k):
        raise RuntimeError("audit fora")

    monkeypatch.setattr(hook.repo, "append_audit", _boom)
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")  # não pode levantar
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_flowpcp_intercompany.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.integrations.flowpcp.intercompany'`

- [ ] **Step 3: Write the policy module**

```python
# app/integrations/flowpcp/intercompany.py
"""Política do de-para de cliente intercompany.

Decide SE o de-para se aplica a um pedido; a leitura do Firebird da revenda
é do `app/erp/depara_cliente.py`. Nunca levanta: o push é best-effort e um
pedido com a revenda no lugar do cliente é melhor que push derrubado.
"""

from __future__ import annotations

from app.erp.cnpj import cnpj_digits
from app.erp.depara_cliente import ResolucaoCliente, resolver_cliente_real
from app.models.order import Order
from app.persistence import environments_repo
from app.utils.logger import logger


def resolucao_para(order: Order, *, slug: str) -> ResolucaoCliente | None:
    """Resolve o cliente real quando o pedido está no nome da revenda.

    Devolve `None` quando o de-para NÃO se aplica: ambiente sem config, ou
    cliente do pedido diferente do CNPJ intercompany. Nesse caso o chamador
    segue com o payload de sempre e não audita nada.
    """
    env = environments_repo.get_by_slug(slug)
    if env is None:
        return None

    alvo = cnpj_digits(env.get("intercompany_cnpj"))
    revenda_slug = (env.get("intercompany_env_slug") or "").strip()
    if not alvo or not revenda_slug:
        return None

    if cnpj_digits(order.header.customer_cnpj) != alvo:
        return None

    try:
        return resolver_cliente_real(order.header.order_number, revenda_slug=revenda_slug)
    except Exception as exc:  # noqa: BLE001 — o resolver já engole tudo; cinto e suspensório
        logger.warning(f"intercompany: resolver falhou (import slug={slug}): {exc}")
        return ResolucaoCliente(False, motivo="erro_conexao")
```

- [ ] **Step 4: Thread it through exporter + hook**

Em `app/integrations/flowpcp/exporter.py`, adicionar o parâmetro opcional ao método de envio (na `main` o método chama-se `export`; se o arquivo já tiver `enqueue`, é o mesmo tratamento) e repassar pro mapper:

```python
    def export(
        self, order: Order, *, import_id: str, resolucao: ResolucaoCliente | None = None
    ) -> bool:
        req = build_recebimento_payload(
            import_id=import_id, order=order, tenant_id=self._tenant_id, resolucao=resolucao
        )
```

Import novo: `from app.erp.depara_cliente import ResolucaoCliente`.

Em `app/integrations/flowpcp/hook.py`:

```python
from app.integrations.flowpcp.intercompany import resolucao_para
from app.persistence import repo
```

e dentro de `push_new_order`, depois do `cfg`:

```python
    resolucao = resolucao_para(order, slug=slug)
    if resolucao is not None:
        try:
            repo.append_audit(
                import_id,
                "depara_cliente",
                {
                    "chave": order.header.order_number,
                    "motivo": resolucao.motivo,
                    "resolvido": resolucao.resolvido,
                    "cnpj_real": resolucao.cnpj,
                    "nome_real": resolucao.nome,
                    # Radar da demanda fantasma — o pedido casado na revenda pode
                    # já estar FATURADO. Só observa; não bloqueia.
                    "pedidos_no_4": resolucao.pedidos_no_4,
                },
            )
        except Exception as exc:  # noqa: BLE001 — auditar não pode derrubar o push
            logger.warning(f"intercompany: audit falhou (import={import_id}): {exc}")
```

e passar `resolucao=resolucao` na chamada do exporter.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_flowpcp_intercompany.py tests/test_flowpcp_hook.py tests/test_flowpcp_exporter.py -v`
Expected: PASS — inclusive os testes antigos de hook/exporter

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/integrations/flowpcp/ tests/test_flowpcp_intercompany.py
ruff format app/integrations/flowpcp/ tests/test_flowpcp_intercompany.py
git add app/integrations/flowpcp/intercompany.py app/integrations/flowpcp/exporter.py app/integrations/flowpcp/hook.py tests/test_flowpcp_intercompany.py
git commit -m "feat(flowpcp): push resolve cliente intercompany antes de enviar ao Flow"
```

---

### Task 6: Selo no preview

**Files:**
- Modify: `app/web/server.py` (`rehydrate_preview`, ~linha 2402-2455)
- Modify: `app/web/static/index.html` (`renderPreview`, ~linha 1665-1680)
- Test: `tests/test_web_preview_intercompany.py` (criar)

**Interfaces:**
- Consumes: `resolucao_para` (Task 5)
- Produces: chave `depara_cliente` no payload de `GET /api/imported/{import_id}/preview`: `null` quando não se aplica, senão `{"resolvido": bool, "cnpj": str|null, "nome": str|null, "motivo": str}`

- [ ] **Step 1: Write the failing test**

O ponto delicado: **sem o cookie `portal_env` o handler não tem ambiente** (`_request_environment` devolve `None`) e o enriquecimento é pulado. Por isso o teste cria o ambiente e seta o cookie — padrão idêntico ao de `tests/test_web_server.py:312-330`.

```python
# tests/test_web_preview_intercompany.py
"""Selo do de-para de cliente no preview reidratado."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.erp.depara_cliente import ResolucaoCliente
from app.persistence import db, environments_repo, repo


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def cliente_com_pedido(tmp_path, monkeypatch):
    """TestClient com ambiente ativo + um import com snapshot. Devolve (client, import_id)."""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    from app.web.server import app

    env = environments_repo.create(
        slug="mm", name="MM", watch_dir=str(tmp_path), output_dir=str(tmp_path), fb_path=""
    )
    import_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": import_id,
            "environment_id": env["id"],
            "source_filename": "pedido.pdf",
            "imported_at": datetime.now(UTC).isoformat(),
            "order_number": "AF066",
            "customer_name": "Nasmar Comercio De Roupas Ltda",
            "customer_cnpj": "34513679000134",
            "portal_status": "parsed",
            "snapshot": {
                "header": {
                    "order_number": "AF066",
                    "customer_name": "Nasmar Comercio De Roupas Ltda",
                    "customer_cnpj": "34513679000134",
                },
                "items": [{"description": "MEIA STZ", "quantity": 12}],
            },
        }
    )
    client = TestClient(app)
    client.cookies.set("portal_env", env["id"])
    yield client, import_id
    client.cookies.clear()


def test_preview_sem_intercompany_traz_none(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: None)
    r = client.get(f"/api/imported/{import_id}/preview")
    assert r.status_code == 200, r.text
    assert r.json()["depara_cliente"] is None


def test_preview_mostra_cliente_real(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AUTHENTIC FEET", motivo="ok")
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: res)
    r = client.get(f"/api/imported/{import_id}/preview")
    assert r.json()["depara_cliente"] == {
        "resolvido": True,
        "cnpj": "10772208000182",
        "nome": "AUTHENTIC FEET",
        "motivo": "ok",
    }


def test_preview_marca_nao_resolvido(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido
    res = ResolucaoCliente(False, motivo="nao_encontrado")
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: res)
    dp = client.get(f"/api/imported/{import_id}/preview").json()["depara_cliente"]
    assert dp == {"resolvido": False, "cnpj": None, "nome": None, "motivo": "nao_encontrado"}


def test_preview_nao_quebra_se_o_resolver_explodir(cliente_com_pedido, monkeypatch):
    import app.web.server as server

    client, import_id = cliente_com_pedido

    def _boom(order, *, slug):
        raise RuntimeError("firebird fora")

    monkeypatch.setattr(server, "resolucao_para", _boom)
    r = client.get(f"/api/imported/{import_id}/preview")
    assert r.status_code == 200
    assert r.json()["depara_cliente"] is None
```

> Se o `insert_import` reclamar de campo faltando, olhar a chamada em `tests/test_metrics.py:68` e completar com os mesmos campos — é o exemplo mais enxuto do repositório.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_web_preview_intercompany.py -v`
Expected: FAIL — `KeyError: 'depara_cliente'`

- [ ] **Step 3: Expose it in the endpoint**

Em `app/web/server.py`, no import de topo junto dos outros de flowpcp (perto da linha 78):

```python
from app.integrations.flowpcp.intercompany import resolucao_para  # noqa: E402
```

Adicionar `request: Request` à assinatura de `rehydrate_preview` e, antes do `return JSONResponse(payload)`:

```python
    # De-para de cliente intercompany: mostra no preview o cliente que o Flow
    # vai receber. Leitura barata (só dispara quando o cliente é a revenda) e
    # best-effort — preview nunca pode quebrar por causa do Firebird da revenda.
    payload["depara_cliente"] = None
    slug = (_request_environment(request) or {}).get("slug")
    if slug:
        try:
            res = resolucao_para(order, slug=slug)
        except Exception:  # noqa: BLE001
            res = None
        if res is not None:
            payload["depara_cliente"] = {
                "resolvido": res.resolvido,
                "cnpj": res.cnpj,
                "nome": res.nome,
                "motivo": res.motivo,
            }
```

> Conferir como as outras rotas do arquivo pegam o ambiente (`_request_environment(request)`) e seguir igual.

- [ ] **Step 4: Add the badge**

Em `app/web/static/index.html`, dentro de `renderPreview`, logo depois de preencher `pvCustomerCnpj`:

```javascript
  // Selo do de-para de cliente intercompany (o Flow recebe o cliente real).
  const dp = data.depara_cliente;
  const dpEl = document.getElementById('pvDeparaCliente');
  if (!dp) {
    dpEl.classList.add('hidden');
    dpEl.textContent = '';
  } else if (dp.resolvido) {
    dpEl.classList.remove('hidden');
    dpEl.className = 'badge badge-ok';
    dpEl.textContent = `Flow recebe: ${dp.nome}`;
    dpEl.title = `Cliente real resolvido no banco da revenda (CNPJ ${dp.cnpj}). O XLS e o Fire seguem com a revenda.`;
  } else {
    dpEl.classList.remove('hidden');
    dpEl.className = 'badge badge-warn';
    dpEl.textContent = 'Cliente real não resolvido — sobe como revenda';
    dpEl.title = `Motivo: ${dp.motivo}`;
  }
```

E o elemento, ao lado de `pvCustomerName` no HTML do preview:

```html
<span id="pvDeparaCliente" class="badge hidden"></span>
```

> As classes `badge-ok` / `badge-warn` podem não existir. Ler o bloco `.badge` (~linha 36 do arquivo) e usar as variantes que já existem lá; se não houver variante de aviso, criar as duas seguindo os tokens de cor do arquivo (`var(--success)` / `var(--warning)`), sem inventar paleta nova.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_web_preview_intercompany.py tests/test_web_server.py tests/test_preview_cache.py -v`
Expected: PASS

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/web/server.py tests/test_web_preview_intercompany.py
ruff format app/web/server.py tests/test_web_preview_intercompany.py
git add app/web/server.py app/web/static/index.html tests/test_web_preview_intercompany.py
git commit -m "feat(web): selo do de-para de cliente no preview"
```

---

### Task 7: Docs + suíte completa

**Files:**
- Modify: `docs/ai/modules/erp.md` (seção nova)
- Modify: `docs/ai/modules/environments.md` (seção nova)
- Modify: `docs/ai/00-index.md` (uma linha no mapa de tarefas)

**Interfaces:**
- Consumes: tudo das Tasks 1-6
- Produces: nada de código

- [ ] **Step 1: Update `docs/ai/modules/erp.md`**

Adicionar seção no fim:

```markdown
## De-para de cliente intercompany (Nasmar → cliente real)

`app/erp/depara_cliente.py` — `resolver_cliente_real(chave, *, revenda_slug)`.

Pedido no nome da revenda (ela fatura, a produção é nossa) sobe pro Flow com o
cliente REAL. A chave é o `PEDIDO_CLIENTE` (= `order.header.order_number`),
buscada na `CAB_VENDAS` do Firebird do ambiente da revenda; o `CADASTRO` de lá
dá o CNPJ.

- Resolve só com **um CNPJ distinto** entre os hits. Várias linhas com o mesmo
  CNPJ é normal e resolve; CNPJs diferentes = `ambiguo` → mantém a revenda.
- `motivo` ∈ `ok | sem_chave | nao_encontrado | ambiguo | config_invalida | erro_conexao`.
- Cache de processo só para resolução positiva (`limpar_cache()` nos testes).
- **Produto nunca vem da revenda** — só o cliente. O `.7` segue sendo a fonte
  de produto/preço.
- Nunca levanta. Config em `environments.intercompany_cnpj` + `intercompany_env_slug`.

Testes: `tests/test_depara_cliente.py`.
```

- [ ] **Step 2: Update `docs/ai/modules/environments.md`**

Adicionar na seção de padrões:

```markdown
### De-para de cliente intercompany

`intercompany_cnpj` (CNPJ que dispara) + `intercompany_env_slug` (ambiente cujo
Firebird tem o vínculo). Qualquer um vazio = desligado. O ambiente da produção
lê o Firebird do ambiente da revenda pela config **já cifrada** dela — não
existe credencial nova nem host no código. Configurável em `/admin/ambientes`
(`PUT /api/admin/environments/{id}/intercompany`).
```

- [ ] **Step 3: Update `docs/ai/00-index.md`**

Adicionar linha na tabela "tarefa → módulo":

```markdown
| De-para de cliente intercompany (pedido no nome da revenda) | `erp` + `environments` | `modules/erp.md`, `modules/environments.md` |
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS, zero falhas. Anotar o total de testes.

Run: `ruff check app/ tests/ && ruff format --check app/ tests/`
Expected: limpo

- [ ] **Step 5: Commit**

```bash
git add docs/ai/
git commit -m "docs(ai): de-para de cliente intercompany (erp + environments)"
```

---

## Verificação manual (depois do merge, com VPN)

Não dá pra fazer local: o `fdb` não abre Firebird do Mac. No servidor do cliente:

1. `/admin/ambientes` → MM → CNPJ da revenda `34.513.679/0001-34`, ambiente `nasmar` → Salvar.
2. Abrir o preview de um pedido cujo cliente é a Nasmar (ex.: `order_number` `AF066`) → selo
   verde "Flow recebe: AUTHENTIC FEET".
3. Exportar XLSX → conferir que o XLS **continua com a Nasmar**.
4. No Flow, o pedido aparece sob o cliente real.
5. `AF112` (não casa no `.4`) → selo de aviso e sobe como Nasmar.
