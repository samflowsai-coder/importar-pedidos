# Reconciliação de pedidos com o Fire — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Descobrir quais pedidos em `parsed` já existem no Fire (cadastrados à mão pela operação), marcá-los com um estado próprio e tirá-los da visão padrão da lista.

**Architecture:** Uma camada de leitura pura do Firebird (`app/erp/fire_reconcile.py`) que nunca levanta e consulta em lote; um runner (`app/reconcile/runner.py`) que orquestra por ambiente; três gatilhos (periódico no processo web, botão, entrada do operador) que chamam o mesmo runner; gravação idempotente por compare-and-set no repo.

**Tech Stack:** Python 3.11, FastAPI, SQLite (`app_state_<slug>.db`), Firebird via `firebird-driver`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-reconciliacao-pedidos-fire-design.md` (revisão 2)

## Global Constraints

- **Reconciliação é leitura.** Nenhum caminho deste plano escreve, altera ou cancela no Firebird. Só `SELECT`.
- **Chave sempre dupla:** número do pedido **E** identidade do cliente. Nunca casar por número sozinho.
- **Nunca levantar a partir do Firebird.** Falha de conexão vira resultado vazio + log; o chamador segue.
- **Cool-down armado só em volta de `connect_with_config`** — nunca em volta do bloco inteiro (é o §1.4 do BACKLOG; não repetir o erro).
- **Só toca pedido em `portal_status = 'parsed'`.** `sent_to_fire`, `cancelled` e `error` são intocáveis.
- **`found_in_fire` NÃO dispara `push_new_order` do FlowPCP.** Status quo preservado.
- Todo teste roda contra Firebird falso. Nenhum teste abre conexão real.
- `ruff check .` limpo e suíte completa verde antes de cada commit.

---

### Task 1: Estado, evento e transições

**Files:**
- Modify: `app/state/machine.py`
- Test: `tests/test_state_machine.py`

**Interfaces:**
- Consumes: nada.
- Produces: `PortalStatus.FOUND_IN_FIRE` (valor `"found_in_fire"`), `LifecycleEvent.FOUND_IN_FIRE` (valor `"found_in_fire"`). Todas as tarefas seguintes usam estes dois nomes.

- [ ] **Step 1: Escrever os testes que falham**

Em `tests/test_state_machine.py`:

```python
def test_parsed_para_found_in_fire():
    from app.state.machine import (
        LifecycleEvent,
        PortalStatus,
        ProductionStatus,
        transition,
    )

    portal, producao = transition(
        PortalStatus.PARSED, ProductionStatus.NONE, LifecycleEvent.FOUND_IN_FIRE
    )
    assert portal == PortalStatus.FOUND_IN_FIRE
    assert producao == ProductionStatus.NONE


def test_found_in_fire_aceita_evento_de_status_do_fire():
    """Sem isto, o poll_fire estoura ao ver mudança de status num reconciliado."""
    from app.state.machine import (
        LifecycleEvent,
        PortalStatus,
        ProductionStatus,
        transition,
    )

    portal, _ = transition(
        PortalStatus.FOUND_IN_FIRE,
        ProductionStatus.NONE,
        LifecycleEvent.FIRE_STATUS_CHANGED,
    )
    assert portal == PortalStatus.FOUND_IN_FIRE


def test_found_in_fire_aceita_enfileiramento_no_gestor():
    """_enqueue_gestor grava no outbox ANTES da transição: sem esta linha o
    outbox fica órfão e o except genérico engole o erro."""
    from app.state.machine import (
        LifecycleEvent,
        PortalStatus,
        ProductionStatus,
        transition,
    )

    portal, producao = transition(
        PortalStatus.FOUND_IN_FIRE,
        ProductionStatus.NONE,
        LifecycleEvent.POST_TO_GESTOR_REQUESTED,
    )
    assert portal == PortalStatus.FOUND_IN_FIRE
    assert producao == ProductionStatus.REQUESTED


def test_sent_to_fire_nao_regride_para_found_in_fire():
    from app.state.machine import (
        InvalidTransitionError,
        LifecycleEvent,
        PortalStatus,
        ProductionStatus,
        transition,
    )
    import pytest

    with pytest.raises(InvalidTransitionError):
        transition(
            PortalStatus.SENT_TO_FIRE,
            ProductionStatus.NONE,
            LifecycleEvent.FOUND_IN_FIRE,
        )
```

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/pytest tests/test_state_machine.py -k found_in_fire -v`
Expected: FAIL com `AttributeError: FOUND_IN_FIRE`.

- [ ] **Step 3: Implementar**

Em `app/state/machine.py`, adicionar ao enum `PortalStatus` (logo após `SENT_TO_FIRE`):

```python
    FOUND_IN_FIRE = "found_in_fire"  # existe no Fire; o portal NÃO foi quem inseriu
```

Adicionar ao enum `LifecycleEvent`, junto dos eventos de fase 5:

```python
    FOUND_IN_FIRE = "found_in_fire"
```

Adicionar a `PORTAL_TRANSITIONS`:

```python
    # Reconciliação — pedido cadastrado à mão no Fire, observado pelo portal
    (PortalStatus.PARSED, LifecycleEvent.FOUND_IN_FIRE): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.FIRE_STATUS_CHANGED): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.POST_TO_GESTOR_REQUESTED): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.POST_TO_GESTOR_SENT): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.POST_TO_GESTOR_FAILED): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.PRODUCTION_UPDATE): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.PRODUCTION_COMPLETED): PortalStatus.FOUND_IN_FIRE,
    (PortalStatus.FOUND_IN_FIRE, LifecycleEvent.PRODUCTION_CANCELLED): PortalStatus.FOUND_IN_FIRE,
```

Adicionar a `PRODUCTION_TRANSITIONS` a linha do evento novo (espelhando a de `SEND_TO_FIRE_SUCCEEDED`):

```python
    (ProductionStatus.NONE, LifecycleEvent.FOUND_IN_FIRE): ProductionStatus.NONE,
```

**Atenção:** `transition()` exige o par nas DUAS tabelas. Antes de commitar, leia as linhas de `PRODUCTION_TRANSITIONS` que já existem para `FIRE_STATUS_CHANGED`, `POST_TO_GESTOR_*` e `PRODUCTION_*` e confirme que cobrem os estados de produção alcançáveis; se alguma faltar para o par novo, adicione espelhando a linha equivalente de `SENT_TO_FIRE`.

- [ ] **Step 4: Rodar até passar**

Run: `.venv/bin/pytest tests/test_state_machine.py -v`
Expected: PASS, incluindo os testes que já existiam.

- [ ] **Step 5: Commit**

```bash
git add app/state/machine.py tests/test_state_machine.py
git commit -m "feat(state): estado found_in_fire e suas transições"
```

---

### Task 2: Normalização do número e query do Fire

**Files:**
- Create: `app/erp/numero_pedido.py`
- Modify: `app/erp/queries.py`
- Test: `tests/test_numero_pedido.py`

**Interfaces:**
- Consumes: nada.
- Produces:
  - `variantes(numero: str) -> list[str]` — variantes de comparação, sem duplicatas, na ordem: exato, sem sufixo `-NNNN`, sem zeros à esquerda.
  - `FIND_ORDERS_BY_PEDIDO_CLIENTE(n: int) -> str` — SQL com `n` placeholders no `IN`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_numero_pedido.py`:

```python
"""O Fire guarda o número com ruído de digitação manual.

Caso real medido: Sam's Club vive como `06654993-0000` no portal e `06654993`
no Fire. Sem as variantes, todo pedido Sam's é falso negativo silencioso.
"""

import pytest

from app.erp.numero_pedido import variantes


def test_exato_vem_primeiro():
    assert variantes("6702645869")[0] == "6702645869"


def test_sufixo_de_quatro_digitos_vira_variante():
    """Caso Sam's: portal 06654993-0000, Fire 06654993."""
    assert "06654993" in variantes("06654993-0000")


def test_zeros_a_esquerda_viram_variante():
    assert "29852483" in variantes("0029852483")


def test_sem_duplicata_quando_variantes_coincidem():
    assert variantes("6702645869") == ["6702645869"]


def test_espaco_em_volta_e_ignorado():
    assert variantes("  K01  ")[0] == "K01"


@pytest.mark.parametrize("entrada", ["", "   ", None])
def test_entrada_vazia_devolve_lista_vazia(entrada):
    assert variantes(entrada) == []


def test_sufixo_que_nao_e_de_quatro_digitos_nao_e_cortado():
    """`AF-198` não é sufixo de loja; cortar viraria match errado."""
    assert variantes("AF-198") == ["AF-198"]
```

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/pytest tests/test_numero_pedido.py -v`
Expected: FAIL com `ModuleNotFoundError: app.erp.numero_pedido`.

- [ ] **Step 3: Implementar o helper**

Criar `app/erp/numero_pedido.py`:

```python
"""Variantes de comparação do número do pedido.

O `PEDIDO_CLIENTE` do Fire é digitado à mão e não bate byte a byte com o que o
parser extrai. Medido em dado real: Sam's Club guarda `06654993-0000` no portal
e `06654993` no Fire; Centauro guarda `29852483` nos dois lados. Comparar só o
exato produz falso negativo silencioso — o pedido está lá e o portal diz que
não.

As variantes NUNCA substituem a segunda perna da chave (identidade do cliente).
Elas só ampliam o que conta como "mesmo número".
"""

from __future__ import annotations

import re

# Sufixo de loja/filial: hífen seguido de exatamente 4 dígitos no fim.
# `AF-198` não casa (3 dígitos) — cortar ali viraria match errado.
_SUFIXO_LOJA = re.compile(r"-\d{4}$")


def variantes(numero: str | None) -> list[str]:
    """Formas de comparação do número, sem duplicatas, mais específica primeiro."""
    base = (numero or "").strip()
    if not base:
        return []

    saida = [base]

    sem_sufixo = _SUFIXO_LOJA.sub("", base)
    if sem_sufixo and sem_sufixo not in saida:
        saida.append(sem_sufixo)

    for candidato in list(saida):
        sem_zeros = candidato.lstrip("0")
        if sem_zeros and sem_zeros not in saida:
            saida.append(sem_zeros)

    return saida
```

- [ ] **Step 4: Rodar até passar**

Run: `.venv/bin/pytest tests/test_numero_pedido.py -v`
Expected: PASS (7 testes).

- [ ] **Step 5: Adicionar a query**

Em `app/erp/queries.py`, logo depois de `FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE`:

```python
def FIND_ORDERS_BY_PEDIDO_CLIENTE(n: int) -> str:
    """Pedidos do Fire por lista de números, com cliente e data.

    Generaliza a `FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE` para receber N números
    de uma vez — 308 pedidos viram 2 idas ao banco em vez de 308.

    As colunas duplicadas vêm ALIASADAS: a query original devolve `V.CODIGO` e
    `C.CODIGO` sem alias, e quem lê por posição erra na leitura.

    `DATA_PEDIDO` sai junto porque a guarda temporal precisa dela: número curto
    e sequencial (K01, MF048, AF198) se repete entre anos no MESMO cliente, e a
    chave dupla não fecha esse caso sozinha.
    """
    marcadores = ", ".join("?" for _ in range(n))
    return f"""
    SELECT TRIM(V.PEDIDO_CLIENTE), V.CODIGO, V.STATUS, V.DATA_PEDIDO,
           C.CODIGO, TRIM(C.CPF_CNPJ)
    FROM CAB_VENDAS V
    JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
    WHERE TRIM(V.PEDIDO_CLIENTE) IN ({marcadores})
    """
```

- [ ] **Step 6: Rodar lint e suíte**

Run: `.venv/bin/ruff check app/ tests/ && .venv/bin/pytest tests/ -q`
Expected: ruff limpo, suíte verde.

- [ ] **Step 7: Commit**

```bash
git add app/erp/numero_pedido.py app/erp/queries.py tests/test_numero_pedido.py
git commit -m "feat(erp): variantes do número do pedido + query em lote do Fire"
```

---

### Task 3: Leitura do Fire (`fire_reconcile`)

**Files:**
- Create: `app/erp/fire_reconcile.py`
- Test: `tests/test_fire_reconcile.py`

**Interfaces:**
- Consumes: `variantes` (Task 2), `FIND_ORDERS_BY_PEDIDO_CLIENTE` (Task 2), `cnpj_digits` de `app/erp/cnpj.py`, `FirebirdConnection` de `app/erp/connection.py`, `environments_repo.get_by_slug` / `to_fb_config`.
- Produces:

```python
@dataclass(frozen=True)
class Candidato:
    import_id: str
    numero: str
    cliente_codigo: int | None      # de imports.cliente_override_codigo
    cnpj_header: str | None         # de imports.customer_cnpj
    cnpjs_entrega: tuple[str, ...]  # delivery_cnpj distintos do snapshot
    data_pedido: str | None         # ISO, para a guarda temporal

@dataclass(frozen=True)
class Achado:
    import_id: str
    fire_codigo: int
    fire_status: str
    caminho: int          # 1 = override, 2 = CNPJ header, 3 = lojas
    lojas_casadas: int

def buscar_no_fire(candidatos: list[Candidato], *, env_slug: str) -> dict[str, Achado]
def limpar_cache() -> None
```

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_fire_reconcile.py`. O fake espelha a forma REAL do `fdb`: `cursor()`, `execute()`, `fetchall()` devolvendo **tuplas** (sem acesso por nome).

```python
"""Reconciliação: achar no Fire o pedido que a operação cadastrou à mão.

Regra que atravessa o arquivo: a chave é SEMPRE dupla — número do pedido E
identidade do cliente. Casar por número sozinho tira pedido da fila de trabalho
sem ele estar no ERP, que é o pior desfecho possível desta feature.
"""

from __future__ import annotations

import pytest

from app.erp import fire_reconcile
from app.erp.fire_reconcile import Candidato, buscar_no_fire


class _FakeCursor:
    def __init__(self, linhas):
        self._linhas = linhas
        self.executados = []

    def execute(self, sql, params=None):
        self.executados.append((sql, list(params or [])))

    def fetchall(self):
        return self._linhas

    def close(self):
        pass


class _FakeConn:
    def __init__(self, linhas):
        self._cursor = _FakeCursor(linhas)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _limpa():
    fire_reconcile.limpar_cache()
    yield
    fire_reconcile.limpar_cache()


def _plugar(monkeypatch, linhas, *, erro=None):
    """Substitui a conexão e o lookup de ambiente por fakes."""
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    def _connect(self, cfg):
        if erro:
            raise erro
        return _FakeConn(linhas)

    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection, "connect_with_config", _connect
    )


# (PEDIDO_CLIENTE, V.CODIGO, STATUS, DATA_PEDIDO, C.CODIGO, CPF_CNPJ)
def _linha(numero, codigo, cnpj, *, status="PEDIDO", data="2026-08-01", cliente=77):
    return (numero, codigo, status, data, cliente, cnpj)


def test_caminho_2_casa_por_cnpj_do_header(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "12.345.678/0001-99")])
    cand = Candidato(
        import_id="i1",
        numero="6702645869",
        cliente_codigo=None,
        cnpj_header="12.345.678/0001-99",
        cnpjs_entrega=(),
        data_pedido="2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].fire_codigo == 900
    assert achados["i1"].caminho == 2


def test_cnpj_divergente_nao_casa(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "99.999.999/0001-11")])
    cand = Candidato("i1", "6702645869", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_1_override_ganha_e_dispensa_cnpj(monkeypatch):
    _plugar(monkeypatch, [_linha("K01", 901, "", cliente=4242)])
    cand = Candidato("i1", "K01", 4242, None, (), "2026-08-01")
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].caminho == 1


def test_caminho_3_marca_so_quando_todas_as_lojas_casam(monkeypatch):
    """Riachuelo: 3 lojas no pedido, 2 no Fire => NÃO marca."""
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22", "33.333.333/0003-33"),
        "2026-08-01",
    )
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_3_marca_quando_todas_as_lojas_casam(monkeypatch):
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22"),
        "2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].caminho == 3
    assert achados["i1"].lojas_casadas == 2
    assert achados["i1"].fire_codigo == 900  # menor CODIGO


def test_variante_sem_sufixo_casa_caso_sams(monkeypatch):
    _plugar(monkeypatch, [_linha("06654993", 902, "12.345.678/0001-99")])
    cand = Candidato("i1", "06654993-0000", None, "12.345.678/0001-99", (), "2026-08-01")
    assert "i1" in buscar_no_fire([cand], env_slug="mm")


def test_guarda_temporal_barra_numero_reusado(monkeypatch):
    """K01 do ano passado, mesmo cliente. Chave dupla não fecha; a data fecha."""
    _plugar(monkeypatch, [_linha("K01", 903, "12.345.678/0001-99", data="2024-01-10")])
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_firebird_fora_devolve_vazio_sem_levantar(monkeypatch):
    _plugar(monkeypatch, [], erro=RuntimeError("host inalcançável"))
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_cool_down_evita_segunda_tentativa(monkeypatch):
    tentativas = []

    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    def _connect(self, cfg):
        tentativas.append(1)
        raise RuntimeError("fora")

    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection, "connect_with_config", _connect
    )

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    buscar_no_fire([cand], env_slug="mm")
    buscar_no_fire([cand], env_slug="mm")
    assert len(tentativas) == 1


def test_lote_acima_de_200_quebra_em_blocos(monkeypatch):
    _plugar(monkeypatch, [])
    cands = [
        Candidato(f"i{i}", f"P{i}", None, "12.345.678/0001-99", (), "2026-08-01")
        for i in range(250)
    ]
    buscar_no_fire(cands, env_slug="mm")
    # 250 números viram 2 execuções, não 250
    assert len(fire_reconcile._ultimo_conn.cursor().executados) == 2
```

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/pytest tests/test_fire_reconcile.py -v`
Expected: FAIL com `ModuleNotFoundError: app.erp.fire_reconcile`.

- [ ] **Step 3: Implementar**

Criar `app/erp/fire_reconcile.py`. Espelhe a forma de `app/erp/depara_cliente.py` (leia esse arquivo antes: mesma disciplina de cool-down, log e "nunca levanta"). Requisitos que os testes fixam:

1. Monta o conjunto de números a consultar com `variantes()` de cada candidato; deduplica; consulta em blocos de **200** com `FIND_ORDERS_BY_PEDIDO_CLIENTE(len(bloco))`.
2. Indexa as linhas devolvidas por número (a coluna 0, já `TRIM`ada).
3. Para cada candidato, decide na ordem **1 → 2 → 3**:
   - **caminho 1**, se `cliente_codigo` não é `None`: casa linha cuja coluna 4 (`C.CODIGO`) seja igual;
   - **caminho 2**, se `cnpj_header`: casa linha cuja coluna 5 tenha `cnpj_digits` igual ao do header;
   - **caminho 3**, se `cnpjs_entrega`: agrupa as linhas por `cnpj_digits`; só devolve `Achado` quando **todo** CNPJ de entrega tem ao menos uma linha. `lojas_casadas` = quantos CNPJs distintos casaram.
4. **Guarda temporal:** descarta linha cuja `DATA_PEDIDO` seja anterior a `data_pedido do candidato − 90 dias`. Candidato sem `data_pedido` não aplica a guarda. Constante `_JANELA_DIAS = 90` no topo, com comentário explicando o caso K01/MF048.
5. `fire_codigo` = **menor** `V.CODIGO` entre as linhas que casaram.
6. Erro em `connect_with_config` arma o cool-down (`_COOLDOWN_S = 45.0`, clock injetável `_clock = time.monotonic`) **e só ali**; erro depois da conexão loga e devolve vazio **sem** armar. Conexão bem-sucedida limpa o cool-down do slug.
7. `limpar_cache()` zera o cool-down (os testes dependem disso).
8. Exponha `_ultimo_conn` (última conexão aberta) apenas para o teste de lote — comente que é gancho de teste.

- [ ] **Step 4: Rodar até passar**

Run: `.venv/bin/pytest tests/test_fire_reconcile.py -v`
Expected: PASS (10 testes).

- [ ] **Step 5: Lint e suíte**

Run: `.venv/bin/ruff check app/ tests/ && .venv/bin/pytest tests/ -q`

- [ ] **Step 6: Commit**

```bash
git add app/erp/fire_reconcile.py tests/test_fire_reconcile.py
git commit -m "feat(erp): leitura do Fire para reconciliação, com chave dupla nos 3 caminhos"
```

---

### Task 4: Persistência — candidatos, gravação idempotente e filtros

**Files:**
- Modify: `app/persistence/repo.py`
- Test: `tests/test_reconcile_repo.py`

**Interfaces:**
- Consumes: `Candidato` (Task 3), `PortalStatus.FOUND_IN_FIRE` / `LifecycleEvent.FOUND_IN_FIRE` (Task 1).
- Produces:
  - `list_parsed_for_reconcile(limit: int = 500) -> list[Candidato]`
  - `mark_found_in_fire(import_id: str, *, fire_codigo: int, fire_status: str, caminho: int, lojas_casadas: int, at: str) -> bool` — `True` se ganhou a corrida.
  - `_build_where` passa a aceitar `portal_status` como `str | list[str]`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_reconcile_repo.py`:

```python
"""Candidatos e gravação da reconciliação.

O ponto sensível é a idempotência: web e worker são processos distintos e
`transition()` lê o estado FORA da transação de escrita. Sem compare-and-set,
dois gatilhos simultâneos gravam o evento duas vezes no log canônico.
"""

from __future__ import annotations

from app.persistence import repo


def test_candidato_com_cnpj_de_header_e_elegivel(env_db_com_import):
    cands = repo.list_parsed_for_reconcile()
    ids = {c.import_id for c in cands}
    assert "com-header" in ids


def test_candidato_riachuelo_sem_header_e_elegivel_pelos_cnpjs_de_entrega(
    env_db_com_import,
):
    """Este é o caso dos 308: sem CNPJ no header, com CNPJ por loja nos itens."""
    cand = next(c for c in repo.list_parsed_for_reconcile() if c.import_id == "riachuelo")
    assert cand.cnpj_header is None
    assert len(cand.cnpjs_entrega) == 3


def test_pedido_sem_nenhuma_identidade_nao_e_candidato(env_db_com_import):
    ids = {c.import_id for c in repo.list_parsed_for_reconcile()}
    assert "sem-identidade" not in ids


def test_pedido_ja_no_fire_nao_e_candidato(env_db_com_import):
    ids = {c.import_id for c in repo.list_parsed_for_reconcile()}
    assert "ja-no-fire" not in ids


def test_marca_e_grava_as_quatro_colunas(env_db_com_import):
    ok = repo.mark_found_in_fire(
        "com-header",
        fire_codigo=900,
        fire_status="PEDIDO",
        caminho=2,
        lojas_casadas=0,
        at="2026-08-24T12:00:00Z",
    )
    assert ok is True
    row = repo.get_import("com-header")
    assert row["portal_status"] == "found_in_fire"
    assert row["fire_codigo"] == 900
    assert row["fire_status_last_seen"] == "PEDIDO"


def test_segunda_marcacao_perde_a_corrida_e_nao_duplica_evento(env_db_com_import):
    primeira = repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:00Z",
    )
    segunda = repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:05Z",
    )
    assert primeira is True
    assert segunda is False
    from app.state import events as ev

    eventos = [e for e in ev.list_events("com-header") if e["event"] == "found_in_fire"]
    assert len(eventos) == 1


def test_filtro_aceita_lista_de_status(env_db_com_import):
    repo.mark_found_in_fire(
        "com-header", fire_codigo=900, fire_status="PEDIDO",
        caminho=2, lojas_casadas=0, at="2026-08-24T12:00:00Z",
    )
    linhas = repo.list_imports(portal_status=["sent_to_fire", "found_in_fire"])
    ids = {r["id"] for r in linhas}
    assert {"com-header", "ja-no-fire"} <= ids
```

A fixture `env_db_com_import` cria um SQLite de ambiente com quatro imports: `com-header` (parsed, `customer_cnpj` preenchido), `riachuelo` (parsed, sem `customer_cnpj`, snapshot com 3 itens de `delivery_cnpj` distintos), `sem-identidade` (parsed, sem CNPJ em lugar nenhum) e `ja-no-fire` (`sent_to_fire`). Siga o padrão de fixture de `tests/test_persistence_repo.py` — leia esse arquivo antes de escrever a sua.

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/pytest tests/test_reconcile_repo.py -v`
Expected: FAIL com `AttributeError: list_parsed_for_reconcile`.

- [ ] **Step 3: Implementar**

Em `app/persistence/repo.py`:

`list_parsed_for_reconcile(limit=500)` — seleciona `portal_status='parsed'`, mais antigos primeiro, e monta `Candidato` por linha: `cliente_codigo` de `cliente_override_codigo`; `cnpj_header` de `customer_cnpj`; `cnpjs_entrega` dos `delivery_cnpj` distintos do `snapshot_json` (`json.loads`, itens sem `delivery_cnpj` ignorados); `data_pedido` de `imported_at`. Descarta candidato sem nenhuma das três identidades.

`mark_found_in_fire(...) -> bool` — compare-and-set numa transação:

```python
cur = conn.execute(
    """
    UPDATE imports
       SET portal_status = 'found_in_fire',
           fire_codigo = ?,
           fire_status_last_seen = ?,
           fire_status_polled_at = ?
     WHERE id = ? AND portal_status = 'parsed'
    """,
    (fire_codigo, fire_status, at, import_id),
)
if cur.rowcount != 1:
    return False   # outro gatilho ganhou; sem evento duplicado
```

Só depois do `rowcount == 1` grava o evento de ciclo de vida (`LifecycleEvent.FOUND_IN_FIRE`, origem `EventSource.FIRE`, payload com `fire_codigo`, `fire_status`, `caminho`, `lojas_casadas`). **Não** usar `transition()` cru: ele lê o estado fora da transação.

`_build_where` — quando `portal_status` for lista, gerar `portal_status IN (?, ...)`; string segue com igualdade. `list_imports` e `count_imports` passam o tipo adiante sem outra mudança.

`list_pending_for_fire_poll` — trocar `WHERE portal_status = 'sent_to_fire'` por `WHERE portal_status IN ('sent_to_fire', 'found_in_fire')`, e trocar a janela `imported_at >= datetime('now', '-N days')` por `COALESCE(fire_status_polled_at, imported_at) >= datetime('now', '-' || ? || ' days')` — senão pedido reconciliado meses depois da importação nunca entra no poll.

- [ ] **Step 4: Rodar até passar**

Run: `.venv/bin/pytest tests/test_reconcile_repo.py tests/test_persistence_repo.py tests/test_worker_poll_fire.py -v`
Expected: PASS. Os testes que já existiam continuam verdes.

- [ ] **Step 5: Lint e suíte**

Run: `.venv/bin/ruff check app/ tests/ && .venv/bin/pytest tests/ -q`

- [ ] **Step 6: Commit**

```bash
git add app/persistence/repo.py tests/test_reconcile_repo.py
git commit -m "feat(persistence): candidatos, marcação por compare-and-set e filtro multi-status"
```

---

### Task 5: Runner e os três gatilhos

**Files:**
- Create: `app/reconcile/__init__.py`, `app/reconcile/runner.py`
- Modify: `app/web/server.py`, `app/web/routes_env_select.py`, `app/worker/scheduler.py`
- Test: `tests/test_reconcile_runner.py`, `tests/test_web_reconciliar_fire.py`

**Interfaces:**
- Consumes: `buscar_no_fire` (Task 3), `list_parsed_for_reconcile` / `mark_found_in_fire` (Task 4).
- Produces:
  - `reconciliar(env_slug: str, *, respeitar_trava: bool = True) -> Resultado`
  - `@dataclass Resultado: verificados: int; casaram: int; erro_conexao: bool`

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_reconcile_runner.py`:

```python
def test_ambiente_com_firebird_fora_nao_impede_os_outros(monkeypatch, dois_ambientes):
    """Um Firebird inalcançável não pode cancelar a varredura dos demais."""
    from app.reconcile import runner

    chamados = []

    def _busca(cands, *, env_slug):
        chamados.append(env_slug)
        if env_slug == "quebrado":
            raise RuntimeError("nunca deveria vazar até aqui")
        return {}

    monkeypatch.setattr(runner, "buscar_no_fire", _busca)
    runner.reconciliar("quebrado")
    r = runner.reconciliar("mm")
    assert "mm" in chamados
    assert r.erro_conexao is False


def test_dois_gatilhos_concorrentes_geram_um_evento(env_db_com_import, monkeypatch):
    """O CAS da Task 4 é quem fecha isso; aqui provamos ponta a ponta."""
    from app.reconcile import runner
    from app.erp.fire_reconcile import Achado
    from app.state import events as ev

    monkeypatch.setattr(
        runner,
        "buscar_no_fire",
        lambda cands, *, env_slug: {
            "com-header": Achado("com-header", 900, "PEDIDO", 2, 0)
        },
    )
    runner.reconciliar("mm", respeitar_trava=False)
    runner.reconciliar("mm", respeitar_trava=False)

    eventos = [
        e for e in ev.list_events("com-header") if e["event"] == "found_in_fire"
    ]
    assert len(eventos) == 1


def test_trava_barra_a_segunda_execucao(env_db_com_import, monkeypatch):
    from app.reconcile import runner

    execucoes = []
    monkeypatch.setattr(
        runner,
        "buscar_no_fire",
        lambda cands, *, env_slug: execucoes.append(env_slug) or {},
    )
    runner.reconciliar("mm")
    runner.reconciliar("mm")
    assert len(execucoes) == 1


def test_botao_ignora_a_trava(env_db_com_import, monkeypatch):
    from app.reconcile import runner

    execucoes = []
    monkeypatch.setattr(
        runner,
        "buscar_no_fire",
        lambda cands, *, env_slug: execucoes.append(env_slug) or {},
    )
    runner.reconciliar("mm")
    runner.reconciliar("mm", respeitar_trava=False)
    assert len(execucoes) == 2
```

Criar `tests/test_web_reconciliar_fire.py`:

```python
def test_rota_exige_autenticacao(client_sem_auth):
    r = client_sem_auth.post("/api/imported/reconciliar-fire")
    assert r.status_code in (401, 403)


def test_rota_devolve_o_resultado(client, monkeypatch):
    from app.reconcile.runner import Resultado
    from app.web import server

    monkeypatch.setattr(
        server, "reconciliar", lambda slug, **kw: Resultado(12, 5, False)
    )
    body = client.post("/api/imported/reconciliar-fire").json()
    assert body == {"verificados": 12, "casaram": 5, "erro_conexao": False}


def test_firebird_fora_nao_vira_zero_silencioso(client, monkeypatch):
    """Sem isto a Grazi vê '0 encontrados' e conclui que quebrou."""
    from app.reconcile.runner import Resultado
    from app.web import server

    monkeypatch.setattr(
        server, "reconciliar", lambda slug, **kw: Resultado(12, 0, True)
    )
    body = client.post("/api/imported/reconciliar-fire").json()
    assert body["erro_conexao"] is True
```

As fixtures `dois_ambientes`, `env_db_com_import`, `client` e `client_sem_auth` seguem os padrões de `tests/test_persistence_repo.py` e `tests/test_web_server.py` — leia os dois antes de escrever as suas.

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/pytest tests/test_reconcile_runner.py tests/test_web_reconciliar_fire.py -v`

- [ ] **Step 3: Implementar o runner**

`app/reconcile/runner.py` — `reconciliar(env_slug, respeitar_trava=True)`: consulta a trava (dict de módulo `_ULTIMA_EXECUCAO: dict[str, float]`, `_TRAVA_S = 600`, clock injetável); lista candidatos; chama `buscar_no_fire`; aplica `mark_found_in_fire` por achado; devolve `Resultado`. `erro_conexao` vem de `buscar_no_fire` ter devolvido vazio **por falha de conexão** — exponha isso no retorno dela (tupla ou atributo), não infira de "veio vazio".

- [ ] **Step 4: Ligar os três gatilhos**

**Periódico no processo web** — em `app/web/server.py`, no evento de startup do FastAPI, uma thread daemon que dorme até a próxima das 07h/12h/18h locais e chama `reconciliar` para cada ambiente ativo. **Não** registre isso só no scheduler do worker: `scripts/setup-service.ps1` sobe apenas `ui.py` no Windows do cliente — o worker nunca roda lá, e um job só no scheduler nunca dispararia em produção.

**Botão** — `POST /api/imported/reconciliar-fire` em `server.py`, exigindo `require_user`, chamando `reconciliar(slug, respeitar_trava=False)` e devolvendo o `Resultado` como JSON.

**Entrada do operador** — em `app/web/routes_env_select.py`, após setar o cookie, dispara `reconciliar(slug)` em background (`BackgroundTasks` do FastAPI). A resposta **não** espera. A função de background precisa **ativar `active_env` explicitamente** com o slug recém-selecionado (`app/persistence/context.py`); o contexto do request ainda aponta pro ambiente anterior.

**Worker** — registre o mesmo runner em `app/worker/scheduler.py` com `CronTrigger` nas mesmas horas, para deploys docker onde o worker existe. Os dois caminhos chamam a mesma função; a trava evita trabalho dobrado.

- [ ] **Step 5: Rodar até passar**

Run: `.venv/bin/pytest tests/test_reconcile_runner.py tests/test_web_reconciliar_fire.py -v`

- [ ] **Step 6: Lint e suíte**

Run: `.venv/bin/ruff check app/ tests/ && .venv/bin/pytest tests/ -q`

- [ ] **Step 7: Commit**

```bash
git add app/reconcile/ app/web/server.py app/web/routes_env_select.py app/worker/scheduler.py tests/test_reconcile_runner.py tests/test_web_reconciliar_fire.py
git commit -m "feat(reconcile): runner e os três gatilhos (periódico no web, botão, entrada)"
```

---

### Task 6: Correção do §1.3 no `poll_fire` com fake realista

**Files:**
- Modify: `app/worker/jobs/poll_fire.py:67`
- Test: `tests/test_worker_poll_fire.py`

**Interfaces:**
- Consumes: `list_pending_for_fire_poll` alterada na Task 4.
- Produces: nada novo.

- [ ] **Step 1: Escrever o teste que falha**

O fake atual do arquivo dá `ctx.execute` de `MagicMock` e **jamais pegaria o bug**. Escreva um fake com a forma real do `fdb`: só `cursor()`, e linhas como **tuplas** (sem `__getitem__` por nome).

```python
class _FakeCursorFdb:
    """Forma real do fdb: sem execute na conexão, linhas como tupla."""

    def __init__(self, linhas):
        self._linhas = linhas

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return self._linhas[0] if self._linhas else None

    def close(self):
        pass


class _FakeConnFdb:
    def __init__(self, linhas):
        self._cursor = _FakeCursorFdb(linhas)

    def cursor(self):
        return self._cursor

    # NÃO existe .execute — é exatamente esse o bug

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_poll_usa_cursor_e_le_status_por_posicao(monkeypatch, ...):
    """Com fire_codigo preenchido o job alcança a linha 67. Antes da correção
    isto estoura com AttributeError: 'execute'."""
```

- [ ] **Step 2: Rodar e confirmar que falha**

Run: `.venv/bin/pytest tests/test_worker_poll_fire.py -k cursor -v`
Expected: FAIL com `AttributeError` em `conn.execute`.

- [ ] **Step 3: Corrigir**

Em `app/worker/jobs/poll_fire.py:67`, trocar `conn.execute(...).fetchone()` por cursor explícito, com `close()` em `finally`, e ler `STATUS` por **posição** (`row[0]`), não por nome. Siga o padrão de `app/erp/depara_cliente.py`.

- [ ] **Step 4: Rodar até passar**

Run: `.venv/bin/pytest tests/test_worker_poll_fire.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/worker/jobs/poll_fire.py tests/test_worker_poll_fire.py
git commit -m "fix(worker): poll_fire usava conn.execute, que não existe em fdb.Connection"
```

---

### Task 7: Guardas das rotas que passam a ver o estado novo

**Files:**
- Modify: `app/web/server.py` (cancel ~2209, export-xlsx ~1836)
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: `PortalStatus.FOUND_IN_FIRE` (Task 1).
- Produces: nada novo.

- [ ] **Step 1: Escrever os testes que falham**

Cancelar um `found_in_fire` devolve 409 **e não grava audit** — hoje `append_audit("cancelled")` roda **antes** da transição, então o log registra um cancelamento que não aconteceu. Exportar XLSX de um `found_in_fire` é recusado: o pedido já está no ERP e reexportar convida duplicata.

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `.venv/bin/pytest tests/test_web_server.py -k "found_in_fire" -v`

- [ ] **Step 3: Implementar**

No `cancel`: incluir `found_in_fire` no guard ao lado de `sent_to_fire`, e **mover o `append_audit` para depois da transição bem-sucedida**. No `export-xlsx`: recusar com 409 e mensagem explicando que o pedido já consta no Fire.

- [ ] **Step 4: Rodar até passar**

Run: `.venv/bin/pytest tests/test_web_server.py -v`

- [ ] **Step 5: Commit**

```bash
git add app/web/server.py tests/test_web_server.py
git commit -m "fix(web): guards de cancel e export-xlsx para pedido reconciliado"
```

---

### Task 8: Lista — chip, badge e botão

**Files:**
- Modify: `app/web/static/index.html` (chip ~718, labels ~1234-1250)
- Test: manual + `tests/test_web_reconciliar_fire.py` (contrato do filtro, já coberto)

**Interfaces:**
- Consumes: filtro multi-status (Task 4), rota do botão (Task 5).
- Produces: nada.

- [ ] **Step 1: Ajustar o chip existente**

Já existe chip "No Fire" mapeado a `sent_to_fire` (`index.html:718`). Passa a filtrar **os dois** estados (`sent_to_fire` e `found_in_fire`), com badge distinguindo a origem: "Enviado pelo portal" vs "Cadastrado no Fire".

- [ ] **Step 2: Trocar o filtro padrão da tela**

O padrão passa de "Tudo" para **"Em revisão"** (`parsed`) — é o trabalho pendente. É esta mudança que faz a lista deixar de ser arquivo morto.

- [ ] **Step 3: Adicionar o estado nas funções de rótulo**

Em `portalStatusLabel`, `portalStatusColor` e `portalStatusBg` (`index.html:1234-1250`), acrescentar `found_in_fire` → rótulo "Cadastrado no Fire", cor e fundo de sucesso, distintos do `sent_to_fire`.

- [ ] **Step 4: Adicionar o botão**

"Verificar no Fire" ao lado de "Atualizar". Chama `POST /api/imported/reconciliar-fire` e mostra o resultado de forma que **distinga 0-casaram de Firebird fora** — com `erro_conexao: true`, a mensagem diz que não deu pra consultar, nunca "0 encontrados".

- [ ] **Step 5: Suíte e commit**

```bash
.venv/bin/pytest tests/ -q
git add app/web/static/index.html
git commit -m "feat(web): chip No Fire com origem, padrão Em revisão e botão Verificar no Fire"
```

---

### Task 9: Documentação dos módulos afetados

**Files:**
- Modify: `docs/ai/modules/erp.md`, `docs/ai/modules/state.md`, `docs/ai/modules/worker.md`, `docs/ai/00-index.md`, `docs/BACKLOG.md`

- [ ] **Step 1: Atualizar só as seções afetadas**

`modules/erp.md`: seção nova sobre `fire_reconcile` — os 3 caminhos de chave, a regra "todas as lojas", a guarda temporal de 90 dias e as variantes do número (com o caso Sam's `06654993-0000`).

`modules/state.md`: `found_in_fire`, o que distingue de `sent_to_fire`, e por que as transições de gestor/produção precisam existir (outbox órfão).

`modules/worker.md`: o gatilho periódico vive no **processo web**, porque o worker não sobe no Windows do cliente.

`00-index.md`: linha de roteamento para o domínio `reconcile`.

`docs/BACKLOG.md`: remover o §1.3 (corrigido na Task 6).

- [ ] **Step 2: Commit**

```bash
git add docs/
git commit -m "docs(ai): reconciliação com o Fire nos módulos afetados"
```
