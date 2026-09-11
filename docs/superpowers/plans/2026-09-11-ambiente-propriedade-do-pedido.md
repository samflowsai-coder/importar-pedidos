# O ambiente é propriedade do pedido — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Com `roteamento_modo = 'ligado'` e nenhuma empresa selecionada, agir num pedido da
caixa somada passa a funcionar — o Portal descobre de qual empresa o pedido é e opera nela.

**Architecture:** Um resolver (`import_id → empresa ativa`) exposto como dependency `async`
do FastAPI que envolve o handler em `active_env`. Gated em `ligado`: nos outros modos a
dependency devolve `None` na hora e o cookie segue sendo a única fonte. O lote agrupa por
empresa em vez de assumir uma só. O fluxo de pasta declara `env_slug`, porque nome de
arquivo não é único entre empresas.

**Tech Stack:** Python 3.11+, FastAPI, SQLite (um `app_state_<slug>.db` por empresa),
pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-09-11-ambiente-propriedade-do-pedido-design.md`](../specs/2026-09-11-ambiente-propriedade-do-pedido-design.md)

---

## Global Constraints

- Python 3.11+ — `X | Y` e `match` liberados.
- **`environment_id` em `imports` é bind imutável.** Nada aqui escreve nele; só o lê de volta.
- **A dependency tem que ser `async def`.** Medido neste repo em 2026-09-11: dependency
  síncrona com `yield` que entra num contextvar levanta
  `ValueError: Token was created in a different Context`, porque o FastAPI a roda via
  `contextmanager_in_threadpool` e o `__enter__`/`__exit__` caem em contextos diferentes.
  A variante `async def` propaga para handler sync **e** async.
- **`desligado` e `observando` não podem mudar em nada.** É o teste que protege a adoção.
- **Nenhum caminho devolve ambiente default.** Sem resposta é 404 ou 400, nunca um palpite.
- **O modo vem de `roteamento_repo`**, com as constantes `DESLIGADO` / `OBSERVANDO` /
  `LIGADO` (`app/persistence/roteamento_repo.py:20`). Nunca compare com string literal.
- **Lint:** `ruff check app/ tests/` tem que passar. **NÃO rode `ruff format` em arquivo que
  já existe** — 115 dos ~250 arquivos do repo não estão format-clean e reformatar viola o
  guard de diff mínimo. `ruff format` só em arquivo que a task CRIA.
- Python do projeto: `.venv/bin/python` e `.venv/bin/pytest`.
- **Baseline: 1310 testes passando** no merge-base desta branch (medido 2026-09-11). Nenhum
  pode passar a falhar.
- **Nenhum teste toca Firebird nem socket** — duas fixtures `autouse` em `tests/conftest.py`
  bloqueiam. Não use o marcador `firebird_stub_proprio`.
- Diff mínimo: sem reformatação oportunista, sem rename não pedido.

## Leia o código antes de editar

`app/web/server.py` tem ~3100 linhas. **Os números de linha deste plano foram conferidos em
2026-09-11 mas são orientação, não verdade** — localize pelo nome da função. Onde este plano
e o código divergirem, o código manda: reporte a divergência em vez de forçar o plano.

Fatos do código conferidos ao escrever este plano (use-os, não os re-derive):

| fato | onde |
|---|---|
| `EnvironmentMiddleware` ativa o `active_env` do cookie e anexa `request.state.environment`; sem cookie o handler roda sem contexto | `app/web/middleware/environment.py` |
| o 412 nasce do `@app.exception_handler(NoActiveEnvironmentError)` | `app/web/server.py:117` |
| `_request_environment(request)` = `getattr(request.state, "environment", None)` | `app/web/server.py:182` |
| `_get_cfg_for_request(request)` devolve o cfg legado com `watch_dir`/`output_dir` do env do cookie — **sem cookie, cai no legado (`INPUT_DIR`/`OUTPUT_DIR`), não dá 412** | `app/web/server.py:186` |
| resposta do lote: `{"total", "ok", "failed", "results": [...]}`; item ok = `{"id", "ok": True, "output_files"}`; item falho = `{"id", "ok": False, "reason", "detail"}` | `app/web/server.py:2412` |
| `/api/pending` responde `{"files": [...], "watchDir": str, "exists": bool}` | `app/web/server.py:1197` |
| fixture canônica de duas empresas + TestClient | `tests/test_routing_wiring.py:27`, `tests/test_imports_cross_env.py:10` |

---

## File Structure

**Criados:**

| Arquivo | Responsabilidade |
|---|---|
| `app/web/dependencies/pedido.py` | O resolver e a dependency `env_do_pedido` |
| `tests/test_env_do_pedido.py` | Resolver, gate e a garantia do `async def` |
| `tests/test_rotas_por_pedido_cross_env.py` | As 10 rotas + o lote agrupado |
| `tests/test_pasta_cross_env.py` | O fluxo de pasta somado |

**Modificados:**

| Arquivo | O quê |
|---|---|
| `app/web/server.py` | `Depends(env_do_pedido)` nas 10 rotas + `require_user` nas 2 que não têm; lote agrupado; 4 rotas de pasta com `env_slug` |
| `app/web/static/index.html` | Selo de empresa na aba de pendentes; `env_slug` nas chamadas; resultado do lote por empresa |
| `docs/ai/modules/web.md` | A dependency nova e o contrato de `env_slug` |

---

# Task 1: O resolver e a dependency

**Files:**
- Create: `app/web/dependencies/pedido.py`, `tests/test_env_do_pedido.py`
- Test: `tests/test_env_do_pedido.py`

**Interfaces:**
- Consumes: `environments_repo.list_active()`, `environments_repo.soft_delete(env_id)`,
  `router.env_connect(slug)`, `env_context.active_env(env_id, slug)`,
  `roteamento_repo.modo()` / `roteamento_repo.LIGADO`
- Produces: `env_do_import_id(import_id: str) -> dict | None` e a dependency
  `async def env_do_pedido(import_id: str)` que cede `dict | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_env_do_pedido.py
"""De qual empresa é este pedido? — o resolver e o gate.

Espelha a fixture de `tests/test_imports_cross_env.py`: duas empresas reais
em `tmp_path`, SQLite puro, zero Firebird.
"""

from __future__ import annotations

import pytest

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router


@pytest.fixture
def duas_empresas(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("mm", "MM Americanense"), ("nasmar", "Nasmar")):
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    yield tmp_path


def _grava(slug: str, ident: str) -> None:
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": ident,
                "source_filename": f"{ident}.pdf",
                "imported_at": "2026-09-11T10:00:00",
                "order_number": ident,
                "customer_name": "DAJU",
                "status": "success",
                "portal_status": "parsed",
            }
        )


def test_resolver_acha_na_empresa_certa(duas_empresas):
    from app.web.dependencies.pedido import env_do_import_id

    _grava("nasmar", "N1")
    achado = env_do_import_id("N1")
    assert achado is not None
    assert achado["slug"] == "nasmar"


def test_resolver_devolve_none_para_id_inexistente(duas_empresas):
    from app.web.dependencies.pedido import env_do_import_id

    assert env_do_import_id("nao-existe") is None


def test_resolver_ignora_empresa_desativada(duas_empresas):
    """Desativar uma empresa para a atividade nela, nao so a esconde da UI."""
    from app.web.dependencies.pedido import env_do_import_id

    _grava("nasmar", "N1")
    environments_repo.soft_delete(environments_repo.get_by_slug("nasmar")["id"])
    assert env_do_import_id("N1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_env_do_pedido.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.web.dependencies.pedido'`

- [ ] **Step 3: Write the resolver**

```python
# app/web/dependencies/pedido.py
"""De qual empresa é este pedido?

O Portal roda N empresas em paralelo, cada uma com seu `app_state_<slug>.db`.
`imports.environment_id` é bind imutável — a empresa já é propriedade do
pedido. Este módulo é o que finalmente a lê de volta, para que agir num
pedido não dependa de ter uma empresa selecionada na sessão.

Só vale com `roteamento_modo = 'ligado'`. Nos outros modos a dependency sai
na hora e o cookie `portal_env` segue sendo a única fonte, exatamente como
antes — é o que permite deployar isto sem mudar nada para a operação.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.persistence import context as env_context
from app.persistence import environments_repo, roteamento_repo, router


def env_do_import_id(import_id: str) -> dict[str, Any] | None:
    """Empresa ATIVA que contém este pedido, ou `None`.

    `imports.id` é PRIMARY KEY em cada banco de empresa, então é acerto de
    índice por empresa.

    Usa `list_active()` de propósito: pedido de empresa desativada fica
    inalcançável. É coerente com `repo.list_imports_all_envs`, que também só
    soma ativas — o pedido nem aparece na caixa de entrada — e com o
    middleware, que já recusa cookie apontando para empresa inativa.
    """
    if not import_id:
        return None
    for env in environments_repo.list_active():
        with router.env_connect(env["slug"]) as conn:
            achou = conn.execute(
                "SELECT 1 FROM imports WHERE id = ? LIMIT 1", (import_id,)
            ).fetchone()
        if achou:
            return env
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_env_do_pedido.py -v`
Expected: PASS (3 testes)

- [ ] **Step 5: Write the failing test for the gate**

```python
# append to tests/test_env_do_pedido.py
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


def _app_de_teste():
    """App mínimo que expõe o que a dependency ativou no contextvar."""
    from app.web.dependencies.pedido import env_do_pedido

    app = FastAPI()

    @app.get("/x/{import_id}")
    def rota(import_id: str, env=Depends(env_do_pedido)):
        ativo = env_context.current()
        return {
            "env_da_dependency": env["slug"] if env else None,
            "contextvar": ativo.slug if ativo else None,
        }

    return TestClient(app)


def test_ligado_resolve_e_ativa_o_ambiente_do_pedido(duas_empresas):
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = _app_de_teste().get("/x/N1")
    assert r.status_code == 200
    assert r.json() == {"env_da_dependency": "nasmar", "contextvar": "nasmar"}


@pytest.mark.parametrize(
    "modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO]
)
def test_fora_de_ligado_a_dependency_e_inerte(duas_empresas, modo, monkeypatch):
    """Nao resolve, nao ativa, e NEM CONSULTA BANCO."""
    from app.web.dependencies import pedido as mod

    _grava("nasmar", "N1")
    roteamento_repo.set_modo(modo, por="teste")

    chamou: list[str] = []
    monkeypatch.setattr(mod, "env_do_import_id", lambda i: chamou.append(i) or None)
    r = _app_de_teste().get("/x/N1")
    assert r.json() == {"env_da_dependency": None, "contextvar": None}
    assert chamou == []


def test_ligado_com_id_inexistente_da_404(duas_empresas):
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    assert _app_de_teste().get("/x/nao-existe").status_code == 404


def test_a_dependency_e_async(duas_empresas):
    """Dependency sincrona com yield + contextvar estoura no FastAPI com
    'Token was created in a different Context'. Este teste falha se alguem
    trocar o `async def` por `def`."""
    import inspect

    from app.web.dependencies.pedido import env_do_pedido

    assert inspect.isasyncgenfunction(env_do_pedido), (
        "env_do_pedido tem que ser `async def` com yield — a versao sincrona "
        "roda em threadpool e o reset do contextvar estoura"
    )
```

> `env_context.current()` devolve um `ActiveEnv` (dataclass), não um dict — por isso
> `ativo.slug` e não `ativo["slug"]`. Confira em `app/persistence/context.py:38`.

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_env_do_pedido.py -v`
Expected: FAIL — `ImportError: cannot import name 'env_do_pedido'`

- [ ] **Step 7: Write the gate**

```python
# append to app/web/dependencies/pedido.py


async def env_do_pedido(import_id: str):
    """Ativa a empresa dona deste pedido, quando o roteamento está ligado.

    **Tem que ser `async def`.** Medido em 2026-09-11: a versão síncrona com
    `yield` que entra num contextvar levanta
    `ValueError: Token was created in a different Context`, porque o FastAPI
    roda dependency síncrona via `contextmanager_in_threadpool` e o
    `__enter__`/`__exit__` caem em contextos diferentes. A variante `async`
    roda no mesmo contexto do handler, sync ou async.

    Em `ligado` o PEDIDO decide e o cookie não opina — o cookie passa a
    filtrar a listagem e nada mais. Isso resolve o link direto: abrir um
    pedido da Nasmar com "MM" no filtro funciona em vez de dar 404.
    """
    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        yield None
        return
    env = env_do_import_id(import_id)
    if env is None:
        raise HTTPException(
            status_code=404, detail="Pedido não encontrado em nenhuma empresa ativa"
        )
    with env_context.active_env(env["id"], env["slug"]):
        yield env
```

- [ ] **Step 8: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_env_do_pedido.py -v`
Expected: PASS (8 testes)

- [ ] **Step 9: Suíte completa e commit**

```bash
.venv/bin/ruff check app/ tests/
.venv/bin/ruff format app/web/dependencies/pedido.py tests/test_env_do_pedido.py
.venv/bin/pytest tests/ -q
git add app/web/dependencies/pedido.py tests/test_env_do_pedido.py
git commit -m "feat(web): resolver de empresa a partir do pedido, gated em ligado"
```

Expected: 1318 passando (1310 + 8).

---

# Task 2: As 10 rotas por-pedido

**Files:**
- Modify: `app/web/server.py` — as 10 rotas `/api/imported/{import_id}...`
- Test: `tests/test_rotas_por_pedido_cross_env.py`

**Interfaces:**
- Consumes: `env_do_pedido` (Task 1)
- Produces: as 10 rotas operando na empresa do pedido quando `ligado` sem cookie

**As 10 rotas** (linha conferida em 2026-09-11; localize pelo nome da função):

| linha | rota | tem `require_user` hoje? |
|---|---|---|
| 1402 | `GET /api/imported/{import_id}` | **NÃO** |
| 1413 | `GET /api/imported/{import_id}/arquivo-original` | sim |
| 3063 | `GET /api/imported/{import_id}/preview` | **NÃO** |
| 2177 | `POST .../send-to-fire` | sim |
| 2337 | `POST .../export-xlsx` | sim |
| 2468 | `POST .../post-to-gestor` | sim |
| 2607 | `POST .../cancel` | sim |
| 2715 | `POST .../override-cliente` | sim |
| 2885 | `POST .../vincular-produto` | sim |
| 2993 | `POST .../ack-sem-preco` | sim |

**As duas sem `require_user` são parte desta task, não escopo extra.** Hoje a falta do
cookie `portal_env` é um portão acidental: sem ele o handler toca `db.connect()` e o
`NoActiveEnvironmentError` vira 412 antes de qualquer dado sair. Esta entrega remove esse
portão — em `ligado`, um chamador **anônimo** passaria a receber 200 com o pedido de
qualquer empresa. Quem abre o buraco é esta mudança, então quem o fecha é esta task.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotas_por_pedido_cross_env.py
"""Em 'ligado' sem empresa selecionada, agir num pedido funciona — na empresa dele.

O teste que protege a adocao e o de 'desligado'/'observando': 412, como hoje.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router
from app.web.server import app


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("mm", "MM Americanense"), ("nasmar", "Nasmar")):
        (tmp_path / slug / "in").mkdir(parents=True)
        (tmp_path / slug / "out").mkdir(parents=True)
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug / "in"),
            output_dir=str(tmp_path / slug / "out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    yield TestClient(app)


def _grava(slug: str, ident: str) -> None:
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": ident,
                "source_filename": f"{ident}.pdf",
                "imported_at": "2026-09-11T10:00:00",
                "order_number": ident,
                "customer_name": "DAJU",
                "status": "success",
                "portal_status": "parsed",
            }
        )


def test_ligado_sem_cookie_abre_pedido_da_outra_empresa(portal):
    """O caso que motivou a entrega: a caixa soma, e clicar na linha funciona."""
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.get("/api/imported/N1")
    assert r.status_code == 200
    assert r.json()["entry"]["order_number"] == "N1"


def test_ligado_o_pedido_decide_e_o_cookie_nao_opina(portal):
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    portal.cookies.set("portal_env", environments_repo.get_by_slug("mm")["id"])
    r = portal.get("/api/imported/N1")
    assert r.status_code == 200
    assert r.json()["entry"]["order_number"] == "N1"


def test_ligado_id_inexistente_da_404_nunca_um_default(portal):
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    assert portal.get("/api/imported/nao-existe").status_code == 404


@pytest.mark.parametrize(
    "modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO]
)
def test_fora_de_ligado_sem_cookie_continua_412(portal, modo):
    """O teste que protege a adocao: o comportamento de hoje, intacto."""
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(modo, por="teste")
    assert portal.get("/api/imported/N1").status_code == 412
```

> O nome do cookie de ambiente é `ENV_COOKIE_NAME` em `app/web/auth.py` — confira o valor
> real e use a constante se preferir. Se o shape de `GET /api/imported/{id}` não for
> `{"entry": ..., "audit": ...}`, **ajuste o teste ao código real**, não o contrário. O que
> não pode mudar é o status e a empresa em que o pedido foi lido.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_rotas_por_pedido_cross_env.py -v`
Expected: os três primeiros FALHAM (412 onde se espera 200/404); os de
`desligado`/`observando` já passam.

- [ ] **Step 3: Fechar o buraco de auth antes de abrir o portão**

Nas duas rotas sem autenticação — `get_imported` (1402) e `rehydrate_preview` (3063) —
acrescente `_user: User = Depends(require_user)`, exatamente como as outras oito já fazem.
Escreva o teste que trava isso:

```python
# append to tests/test_rotas_por_pedido_cross_env.py

@pytest.mark.parametrize(
    "rota", ["/api/imported/N1", "/api/imported/N1/preview"]
)
def test_ligado_nao_serve_pedido_a_anonimo(tmp_path, monkeypatch, rota):
    """Sem o portao acidental do cookie, so a auth separa anonimo dos dados
    das duas empresas."""
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    router.reset_init_cache()
    with router.shared_connect():
        pass
    environments_repo.create(
        slug="nasmar",
        name="Nasmar",
        watch_dir=str(tmp_path / "n"),
        output_dir=str(tmp_path / "n"),
        fb_path=str(tmp_path / "n.fdb"),
    )
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    assert TestClient(app).get(rota).status_code == 401
```

- [ ] **Step 4: Declare a dependency nas 10 rotas**

No topo do módulo, junto dos outros imports de `app.web`:

```python
from app.web.dependencies.pedido import env_do_pedido
```

Em cada uma das 10 rotas, acrescente um parâmetro. Exemplo em `get_imported`:

```python
@app.get("/api/imported/{import_id}")
def get_imported(
    import_id: str,
    _user: User = Depends(require_user),
    _env=Depends(env_do_pedido),
) -> JSONResponse:
    ...  # corpo inalterado
```

Não reindente corpo nenhum, não mude mais nada nas assinaturas. Onde a rota já tem
`request: Request` ou `_user`, o parâmetro novo entra ao lado, respeitando a ordem do
Python (parâmetros com default depois dos sem default).

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_rotas_por_pedido_cross_env.py -v`
Expected: PASS

- [ ] **Step 6: Nenhum teste existente pode ter quebrado**

Run: `.venv/bin/pytest tests/ -q`
Expected: 1310 anteriores + os novos, todos verdes. O default é `desligado`, então nada
existente deveria precisar de ajuste — **exceto** testes que hoje batem em
`/api/imported/{id}` ou `/api/imported/{id}/preview` sem autenticação. Se algum falhar com
401, é o `require_user` novo fazendo efeito: ajuste o teste para autenticar (ou setar
`TEST_AUTH_BYPASS`), **e reporte quais testes precisaram disso**. Se algum falhar por outro
motivo, PARE e reporte.

- [ ] **Step 7: Commit**

```bash
.venv/bin/ruff check app/ tests/
.venv/bin/ruff format tests/test_rotas_por_pedido_cross_env.py
git add app/web/server.py tests/test_rotas_por_pedido_cross_env.py
git commit -m "feat(web): as rotas por-pedido resolvem a empresa pelo proprio pedido"
```

---

# Task 3: O lote agrupado por empresa

**Files:**
- Modify: `app/web/server.py` — `batch_send_to_fire` (2361), `batch_export_xlsx` (2412), e
  um helper `_cfg_para_env` junto de `_get_cfg_for_request` (186)
- Test: `tests/test_rotas_por_pedido_cross_env.py` (estender)

**Interfaces:**
- Consumes: `env_do_import_id` (Task 1)
- Produces: resposta do lote com `env_slug` e `env_name` por item

**O problema:** o lote hoje resolve o ambiente **uma vez** (`request_env = getattr(request.state,
"environment", None)`) e passa o mesmo para todos os ids do loop. Isso era garantido pelo
portão do cookie e deixa de ser quando a caixa soma empresas — em `ligado`, uma seleção pode
ter pedidos das duas, e um clique escreveria em dois Firebird de produção.

**A armadilha do `cfg`:** `_get_cfg_for_request` resolve `watch_dir`/`output_dir` a partir do
env do **cookie**. Com o agrupamento, cada grupo precisa das pastas da **sua** empresa —
senão o xlsx da Nasmar é escrito na pasta da MM. Por isso o helper.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_rotas_por_pedido_cross_env.py

def test_lote_agrupa_por_empresa(portal):
    """Selecao mista nao e recusada: cada pedido e processado na SUA empresa."""
    _grava("mm", "M1")
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")

    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1", "N1"]})
    assert r.status_code == 200
    por_id = {x["id"]: x for x in r.json()["results"]}
    assert por_id["M1"]["env_slug"] == "mm"
    assert por_id["M1"]["env_name"] == "MM Americanense"
    assert por_id["N1"]["env_slug"] == "nasmar"


def test_lote_com_id_orfao_falha_so_aquele_item(portal):
    _grava("mm", "M1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")

    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1", "fantasma"]})
    assert r.status_code == 200
    corpo = r.json()
    por_id = {x["id"]: x for x in corpo["results"]}
    assert por_id["fantasma"]["ok"] is False
    assert por_id["fantasma"]["reason"] == "nao_encontrado"
    assert por_id["M1"]["env_slug"] == "mm"
    assert corpo["total"] == 2
    assert corpo["failed"] >= 1


def test_lote_fora_de_ligado_sem_cookie_continua_412(portal):
    """Sem cookie e sem 'ligado', o lote e o de hoje."""
    _grava("mm", "M1")
    roteamento_repo.set_modo(roteamento_repo.DESLIGADO, por="teste")
    r = portal.post("/api/batch/export-xlsx", json={"ids": ["M1"]})
    assert r.status_code == 412
```

> Se em `desligado` sem cookie o lote hoje **não** devolver 412 (o `_export_one_xlsx` pode
> capturar o erro e virar item falho), ajuste este último teste ao que o código realmente
> faz **antes** de mudar qualquer coisa, e registre no report qual é o comportamento de hoje.
> A regra que não se negocia: seja qual for, `desligado` continua igual depois da task.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_rotas_por_pedido_cross_env.py -v -k lote`
Expected: FAIL — `KeyError: 'env_slug'` (ou 412 nos dois primeiros)

- [ ] **Step 3: O helper de cfg por empresa**

Junto de `_get_cfg_for_request`, para que as duas formas de resolver pastas fiquem lado a
lado:

```python
def _cfg_para_env(env: dict) -> dict:
    """Cfg legado com as pastas DESTA empresa. Usado pelo lote agrupado, onde
    cada grupo escreve na pasta da sua empresa e não na do cookie."""
    out = dict(_get_cfg())
    out["watch_dir"] = env["watch_dir"]
    out["output_dir"] = env["output_dir"]
    return out


def _get_cfg_for_request(request: Request) -> dict:
    """Return legacy config, overridden by selected environment dirs when present."""
    env = _request_environment(request)
    return _get_cfg() if env is None else _cfg_para_env(env)
```

- [ ] **Step 4: Agrupar**

Nas duas rotas de lote, troque o `request_env` único pelo agrupamento. Mantenha a tolerância
a falha parcial e os contadores `ok_count`/`fail_count` que já existem:

```python
from collections import defaultdict

# ... no lugar de `request_env = getattr(request.state, "environment", None)`:
env_do_cookie = _request_environment(request)
if roteamento_repo.modo() == roteamento_repo.LIGADO and env_do_cookie is None:
    grupos: dict[str | None, list[str]] = defaultdict(list)
    for import_id in body.ids:
        achado = env_do_import_id(import_id)
        grupos[achado["slug"] if achado else None].append(import_id)
else:
    # comportamento de hoje: uma empresa só, a do cookie (None = sem cookie,
    # e aí o handler falha como sempre falhou)
    grupos = {env_do_cookie["slug"] if env_do_cookie else None: list(body.ids)}
    sem_cookie = env_do_cookie is None

for slug, ids in grupos.items():
    if slug is None and <caso ligado>:
        for import_id in ids:
            fail_count += 1
            results.append(
                {"id": import_id, "ok": False, "reason": "nao_encontrado",
                 "detail": "Pedido não encontrado em nenhuma empresa ativa"}
            )
        continue
    env = environments_repo.get_by_slug(slug) if slug else None
    cfg_do_grupo = _cfg_para_env(env) if env else _get_cfg()
    ctx = env_context.active_env(env["id"], env["slug"]) if env else nullcontext()
    with ctx:
        for import_id in ids:
            outcome = _export_one_xlsx(import_id, cfg_do_grupo, request_env=env)
            # monta o item como hoje, e acrescenta:
            #   "env_slug": env["slug"], "env_name": env["name"]   (quando env)
```

**Os dois `slug is None` são casos diferentes e não podem colapsar:**
em `ligado` significa "esse id não existe em empresa ativa" → item falho com
`nao_encontrado`; fora de `ligado` significa "não há cookie" → o handler tem que se comportar
como hoje (deixar o `NoActiveEnvironmentError` subir e virar 412). Escreva o código de forma
que a distinção seja explícita, não implícita.

`nullcontext` vem de `contextlib`. Aplique a mesma estrutura em `batch_send_to_fire`, que usa
`_send_one_to_fire` no lugar de `_export_one_xlsx` — leia a rota antes de editar, os
contadores e o shape do item são dela.

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_rotas_por_pedido_cross_env.py -v`
Expected: PASS

- [ ] **Step 6: Suíte completa e commit**

```bash
.venv/bin/ruff check app/ tests/
.venv/bin/pytest tests/ -q
git add app/web/server.py tests/test_rotas_por_pedido_cross_env.py
git commit -m "feat(web): lote agrupa por empresa em vez de assumir uma so"
```

---

# Task 4: O fluxo da pasta de entrada

**Files:**
- Modify: `app/web/server.py` — `list_pending` (1197), `import_files` (1236), `reimport`
  (1476), `preview_pending` (1683); `app/web/static/index.html`
- Test: `tests/test_pasta_cross_env.py`

**Interfaces:**
- Consumes: `environments_repo.list_active()`, `_cfg_para_env` (Task 3)
- Produces: itens de `/api/pending` com `env_slug` e `env_name`; `env_slug` aceito no corpo
  das três rotas de ação

**Por que aqui é diferente:** `import_id` é único globalmente; **nome de arquivo não é**. Dois
arquivos com o mesmo nome podem existir nas pastas das duas empresas. Então a empresa não é
derivada — é **declarada** pelo cliente, a partir do que a listagem devolveu.

**O "antes" real, conferido:** em `ligado` sem cookie, `/api/pending` **não** dá 412. Ele cai
no cfg legado (`INPUT_DIR`) e lista uma pasta que normalmente não existe — a aba aparece
vazia, com `exists: false`. As três rotas de **ação** é que dão 412, porque tocam o banco. A
spec chama isso de 412 na tabela; o comportamento real é este, e é o que a task corrige.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pasta_cross_env.py
"""A aba de arquivos esperando, somada entre empresas.

Aqui a empresa e DECLARADA, nao derivada: `import_id` e unico globalmente,
nome de arquivo nao e.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, roteamento_repo, router
from app.web.server import app


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("mm", "MM Americanense"), ("nasmar", "Nasmar")):
        (tmp_path / slug / "in").mkdir(parents=True)
        (tmp_path / slug / "out").mkdir(parents=True)
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug / "in"),
            output_dir=str(tmp_path / slug / "out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    (tmp_path / "mm" / "in" / "PEDIDO.pdf").write_bytes(b"%PDF-1.4 mm")
    (tmp_path / "nasmar" / "in" / "PEDIDO.pdf").write_bytes(b"%PDF-1.4 nasmar")
    yield TestClient(app)


def test_ligado_pending_soma_as_pastas_com_selo(portal):
    """Mesmo NOME de arquivo nas duas pastas: o selo e o que os distingue."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.get("/api/pending")
    assert r.status_code == 200
    files = r.json()["files"]
    assert len(files) == 2
    assert {f["env_slug"] for f in files} == {"mm", "nasmar"}
    assert {f["env_name"] for f in files} == {"MM Americanense", "Nasmar"}
    assert {f["name"] for f in files} == {"PEDIDO.pdf"}


@pytest.mark.parametrize(
    "modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO]
)
def test_fora_de_ligado_pending_e_o_de_hoje(portal, modo):
    roteamento_repo.set_modo(modo, por="teste")
    portal.cookies.set("portal_env", environments_repo.get_by_slug("mm")["id"])
    files = portal.get("/api/pending").json()["files"]
    assert len(files) == 1
    assert "env_slug" not in files[0]


def test_ligado_sem_env_slug_recusa_com_400(portal):
    """Nome de arquivo nao e unico entre empresas: sem o slug nao da pra saber."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post("/api/preview-pending", json={"filename": "PEDIDO.pdf"})
    assert r.status_code == 400
    assert "env_slug" in str(r.json()["detail"])


def test_ligado_env_slug_invalido_da_404(portal):
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post(
        "/api/preview-pending",
        json={"filename": "PEDIDO.pdf", "env_slug": "fantasma"},
    )
    assert r.status_code == 404
```

> **Confira o nome real do campo de arquivo** no modelo pydantic de `/api/preview-pending`
> antes de escrever o corpo do POST (pode ser `filename`, `file`, `path`). Ajuste o teste ao
> modelo real. Em `ligado` com cookie presente, o cookie continua mandando — `env_slug` só é
> obrigatório quando não há cookie.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_pasta_cross_env.py -v`
Expected: FAIL — `files` com 0 ou 1 item e sem `env_slug`

- [ ] **Step 3: Implementar `list_pending`**

Em `ligado` sem cookie, itera `environments_repo.list_active()` e concatena os arquivos de
cada `watch_dir`, acrescentando `env_slug` e `env_name` a cada item, mantendo a ordenação por
`mtime` decrescente **no conjunto somado**. Nos outros modos (ou com cookie), comportamento
de hoje, sem os campos novos.

O `watchDir` do topo da resposta deixa de ter um valor único no caso somado. Escolha: mande
`"watchDir": None` e `"exists": True` quando houver ao menos uma pasta existente, e
**documente a escolha no report** — a UI passa a usar o selo por item. Se alguma coisa na UI
depender de `watchDir` não-nulo, trate isso na Step 5.

- [ ] **Step 4: Implementar as três rotas de ação**

Cada uma ganha `env_slug: str | None = None` no modelo pydantic do corpo. Em `ligado` sem
cookie:

- ausente → `HTTPException(400, "env_slug é obrigatório ...")` nomeando o campo
- não corresponde a empresa ativa → `HTTPException(404, ...)`
- válido → resolve o cfg com `_cfg_para_env(env)` e envolve o trabalho em
  `env_context.active_env(env["id"], env["slug"])`

Nos outros modos, e em `ligado` **com** cookie, o campo é ignorado e o cookie manda.

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_pasta_cross_env.py -v`
Expected: PASS

- [ ] **Step 6: A UI**

Em `app/web/static/index.html`:

- a aba de pendentes ganha o mesmo selo de empresa que a caixa de entrada já usa — reuse a
  classe e os tokens existentes (procure por `env-badge` / `log-env-badge` no CSS do shell),
  **não invente cor nem classe nova**;
- o `env_slug` do item volta no corpo de importar, reimportar e pré-visualizar;
- o resultado do lote agrupa visualmente por empresa, usando o `env_name` da Task 3.

**Não rode `ruff format` e não reformate o HTML.** Diff mínimo. Se você mexer em algum
arquivo de `/static/css` ou `/static/js`, **bump o `?v=` dele em TODAS as páginas que o
carregam** — assets são cacheáveis e o cache-bust é o `?v=N` (ver `app/web/server.py:88`).
Uma mudança de CSS sem bump não chega no browser do cliente.

- [ ] **Step 7: Suíte completa, doc e commit**

Atualize `docs/ai/modules/web.md`: a dependency nova, o contrato de `env_slug` no fluxo de
pasta, e a mudança de shape de `/api/pending` em `ligado`.

```bash
.venv/bin/ruff check app/ tests/
.venv/bin/ruff format tests/test_pasta_cross_env.py
.venv/bin/pytest tests/ -q
git add app/web/server.py app/web/static/index.html tests/test_pasta_cross_env.py docs/ai/modules/web.md
git commit -m "feat(web): aba de pendentes soma as pastas e declara a empresa"
```

---

## Validação final antes do merge

- [ ] **Suíte verde e lint limpo**

```bash
.venv/bin/ruff check app/ tests/
.venv/bin/pytest tests/ -v
```

- [ ] **Os testes que não podem ser relaxados**

| teste | o que protege |
|---|---|
| `test_fora_de_ligado_sem_cookie_continua_412` | a adoção: enquanto a chave não virar, o Portal é o de hoje |
| `test_fora_de_ligado_a_dependency_e_inerte` | em `desligado` a dependency nem consulta banco |
| `test_ligado_nao_serve_pedido_a_anonimo` | o portão acidental do cookie vira auth de verdade |
| `test_resolver_ignora_empresa_desativada` | desativar uma empresa para a atividade nela |
| `test_ligado_id_inexistente_da_404_nunca_um_default` | nenhum caminho devolve ambiente default |
| `test_a_dependency_e_async` | a armadilha do contextvar em threadpool |
| `test_lote_agrupa_por_empresa` | um clique não escreve no Firebird errado |

- [ ] **Conferir que `desligado` é bit-a-bit o de hoje**, com espião em `env_do_import_id`:
  zero chamadas no fluxo inteiro (abrir pedido, lote, pendentes).

- [ ] **Doc incremental:** `docs/ai/modules/web.md` (Task 4) e uma linha em
  `docs/ai/00-index.md` apontando a dependency nova no domínio `web`.
