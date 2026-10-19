# Regra Comercial de Faturamento MM × Nasmar — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O roteamento sugere MM ou Nasmar pela regra do comercial (4 redes sempre MM; demais pela UF do CNPJ faturado no Fire), o operador pode trocar ou mover o pedido antes do Fire, e cada decisão fica registrada com quem decidiu.

**Architecture:** Degrau novo `regra` no topo da escada pura (`app/routing/ambiente.py`), alimentado por `Deps.regra`. A política (empresas + redes fixas) mora em `app_shared.db`, editável em `/admin/roteamento`. A UF vem de um SELECT só-leitura no `CADASTRO` das duas bases da política. O rastro por pedido vai para `roteamento_decisao` (append-only). Tudo atrás do interruptor de três estados que já existe, e a regra atrás da própria chave (`ativa`).

**Tech Stack:** Python 3.11, FastAPI, SQLite (`app_shared.db` / `app_state_<slug>.db`), firebird-driver, pytest, ruff, HTML/JS estático em `app/web/static/`.

**Spec:** `docs/superpowers/specs/2026-10-19-regra-faturamento-mm-nasmar-design.md` (aprovada pelo Samuel em 19/10). Ler as §§4, 6 e 10 antes de qualquer task.

**Domínio:** `routing` (+ `persistence`, `web`). Ler `docs/ai/modules/routing.md` antes de começar. Não ler módulos irmãos.

## Decisões deste plano além da spec (de propósito)

1. **Quatro tabelas, não três.** As chaves das redes ficam em `regra_faturamento_chave` com `PRIMARY KEY (tipo, chave)`. Assim uma mesma chave não pode pertencer a duas redes, e a ambiguidade fica impossível no banco.
2. **Semente = as 4 redes sem chave.** As raízes/CNPJs **não** são chutados no código. A regra **não liga** (`set_config(ativa=True)` recusa) enquanto existir rede fixa sem chave. Quem preenche as chaves é o Samuel no admin, a partir da tabela que a Task 0 produz.
3. **`acao` ganha dois valores** além dos quatro da spec: `escolha_sem_sugestao` (em `ligado`, quando o roteador não resolveu e o operador escolheu) e `observando` (registro no modo `observando`, quando ninguém trocou nada).
4. **Rede fixa é identificada por qualquer CNPJ do pedido** (cabeçalho ou lojas, via `cnpjs_do_pedido`). Se dois CNPJs do pedido casarem com redes **diferentes**, a regra não responde. `cnpj_da_rede` (tabela de preço) **não** é reusada: depois da §10.3 a regra só usa `customer_cnpj` para a UF.

## Global Constraints

- `roteamento_modo = 'desligado'` continua **bit-a-bit** o comportamento de hoje. Nenhuma task pode mudar o caminho `desligado`.
- `regra_faturamento_config.ativa = 0` (estado inicial) → `Deps.regra is None` → zero I/O novo.
- Nunca chutar UF. Fire mudo = "não sei", nunca "fora de SP" (spec §4).
- Leitura no Fire só por `FirebirdConnection().connect_somente_leitura(cfg)`. O SQL começa com `SELECT`.
- `registrar` do rastro **nunca levanta** para o chamador (mesmo padrão de `roteamento_repo.registrar_sombra`).
- `decidido_por` = `user.email` (o mesmo valor que `decisao_ambiente_repo.lembrar(por=...)` já usa); sem humano = `sistema:<degrau>` ou `sistema:pasta`.
- Textos para o operador em pt-BR, voz do produto: "Regra comercial: Centauro fatura sempre pela MM".
- Sem dependência nova. `ruff check app/ tests/` e a suíte completa verdes antes do commit final.
- Migration (tabelas novas em `schema_shared.py`) só é **aplicada** em produção com ok explícito do Samuel. No código e nos testes ela entra normalmente.

## Review Focus

1. **CNPJ com máscara de um lado e só dígitos do outro** (`12.345.678/0001-90` no pedido, `12345678000190` no Fire, ou o contrário) → tem que casar. Teste na Task 3.
2. **UF suja no cadastro** (`"sp "`, `"Sp"`, `""`, `NULL`) → `sp ` vale como SP; vazio/NULL = sem UF = não responde. Teste na Task 2.
3. **Empresa da política desativada entre a sugestão e o commit** → 412 explícito, nunca grava na empresa do cookie em silêncio. Teste na Task 6.
4. **Mover interrompido no meio** (pedido já nos dois bancos) → repetir o mover conclui sem duplicar e sem perder o audit. Teste na Task 8.
5. **Mesmo CNPJ cadastrado duas vezes no mesmo Fire com UFs diferentes** → não responde. Teste na Task 3.

---

### Task 0: Portão — coluna da UF e chaves das 4 redes (sem código)

**Files:**
- Create: `docs/superpowers/plans/2026-10-19-regra-faturamento-chaves.md` (nota de evidência; não é código)

- [ ] **Step 1: Pedir ao Samuel o ok de leitura na Fire de produção nesta sessão.** Sem o ok, PARAR. A proteção automática bloqueia, e contornar é proibido.
- [ ] **Step 2: Ler o schema de `CADASTRO` nas duas bases** (`.7` MM_AMERICANENSE e a base da Nasmar configurada no ambiente), só SELECT em `RDB$RELATION_FIELDS`, filtrando `UF`/`ESTADO`/`CIDADE`. Registrar o nome exato da coluna e a distribuição de valores (`SELECT <col>, COUNT(*) ... WHERE RELAC_CLIENTE='Sim' GROUP BY 1`).
Esperado: uma coluna de UF com siglas. Se não houver, PARAR e voltar ao Samuel, porque a spec depende dela.
- [ ] **Step 3: Levantar as chaves candidatas das 4 redes.** Usar `samples/` (parsers `sbf_centauro`, `kallan_xls`, `sams_club`, e Riachuelo onde aparecer), `data/app_state_*.db` (`imports.customer_cnpj`) e o `CADASTRO` (por razão social). Para cada rede: raiz(es) de 8 dígitos ou CNPJs completos, com a evidência (arquivo/linha ou query). O Sam's vai **por CNPJ completo do CD**, nunca por raiz (spec §10.8).
- [ ] **Step 4: Escrever a nota** com: nome da coluna, distribuição de UF, tabela rede → chaves → evidência. Itens sem evidência ficam marcados "não encontrado" (nunca inventados).
- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-10-19-regra-faturamento-chaves.md
git commit -m "docs(regra-faturamento): coluna UF do CADASTRO e chaves das redes fixas"
```

---

### Task 1: Política e rastro no banco compartilhado

**Files:**
- Modify: `app/persistence/schema_shared.py` (4 tabelas + índices + semente)
- Create: `app/persistence/regra_faturamento_repo.py`
- Create: `app/persistence/roteamento_decisao_repo.py`
- Test: `tests/test_regra_faturamento_repo.py`, `tests/test_roteamento_decisao_repo.py`

**Interfaces:**
- Produces:
  - Tabelas:
    - `regra_faturamento_config(id INTEGER PRIMARY KEY CHECK (id=1), env_sp TEXT NOT NULL, env_fora_sp TEXT NOT NULL, ativa INTEGER NOT NULL DEFAULT 0, alterado_por TEXT NOT NULL, alterado_em TEXT NOT NULL)`
    - `regra_faturamento_rede(id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL UNIQUE, env_slug TEXT NOT NULL, alterado_por TEXT NOT NULL, alterado_em TEXT NOT NULL)`
    - `regra_faturamento_chave(rede_id INTEGER NOT NULL REFERENCES regra_faturamento_rede(id) ON DELETE CASCADE, tipo TEXT NOT NULL CHECK (tipo IN ('raiz','cnpj')), chave TEXT NOT NULL, PRIMARY KEY (tipo, chave))`
    - `roteamento_decisao(id INTEGER PRIMARY KEY AUTOINCREMENT, import_id TEXT NOT NULL, decidido_em TEXT NOT NULL, modo TEXT NOT NULL, degrau TEXT, env_sugerido TEXT, env_escolhido TEXT NOT NULL, decidido_por TEXT NOT NULL, motivo TEXT, acao TEXT NOT NULL CHECK (acao IN ('sugestao_aceita','troca_no_preview','escolha_sem_sugestao','movido_apos_importar','automatico','observando')))` + índice `(import_id, decidido_em)`
    - Semente: `INSERT OR IGNORE INTO regra_faturamento_rede (nome, env_slug, alterado_por, alterado_em) VALUES ('Centauro','mm','semente',...), ('Riachuelo',...), ('Kallan',...), ("Sam's Club",...)`. Sem linha em `regra_faturamento_config` e sem chaves.
  - `regra_faturamento_repo`:
    - `config() -> dict | None` (`{env_sp, env_fora_sp, ativa: bool, alterado_por, alterado_em}`)
    - `set_config(*, env_sp: str, env_fora_sp: str, ativa: bool, por: str) -> None`. Levanta `ValueError` se `env_sp == env_fora_sp`, se algum slug não for ambiente ativo (`environments_repo.get_by_slug`), ou se `ativa` e existir rede sem chave (mensagem nomeia a rede).
    - `listar_redes() -> list[dict]` (`{id, nome, env_slug, chaves: [{tipo, chave}]}`, ordem por nome)
    - `salvar_rede(*, nome: str, env_slug: str, chaves: list[tuple[str, str]], por: str, rede_id: int | None = None) -> int`. Chave normalizada por `cnpj_digits`. `raiz` exige 8 dígitos e `cnpj` exige 14 (senão `ValueError`). Chave já usada por outra rede → `ValueError` nomeando a outra rede. As chaves da rede são substituídas inteiras.
    - `remover_rede(rede_id: int) -> None`
    - `politica() -> Politica | None`. `None` quando não há config ou `ativa` é falso. Monta `app.routing.regra.Politica` (Task 2, import local).
  - `roteamento_decisao_repo`:
    - `registrar(*, import_id: str, modo: str, degrau: str | None, env_sugerido: str | None, env_escolhido: str, decidido_por: str, motivo: str | None, acao: str) -> None`. Nunca levanta (loga `roteamento.decisao_falhou`).
    - `listar_por_import(import_id: str) -> list[dict]` (ordem cronológica)
    - `ultima(import_id: str) -> dict | None`

- [ ] **Step 1: Testes do repo da política**

```python
def test_semente_tem_as_4_redes_sem_chave(shared_db):
    nomes = [r["nome"] for r in regra_faturamento_repo.listar_redes()]
    assert nomes == ["Centauro", "Kallan", "Riachuelo", "Sam's Club"]
    assert all(r["chaves"] == [] and r["env_slug"] == "mm" for r in regra_faturamento_repo.listar_redes())

def test_politica_none_sem_config_e_com_ativa_falso(shared_db, envs_mm_nasmar): ...
    # sem config -> None; set_config(ativa=False) -> None

def test_nao_liga_com_rede_sem_chave(shared_db, envs_mm_nasmar):
    with pytest.raises(ValueError, match="Riachuelo"):
        regra_faturamento_repo.set_config(env_sp="mm", env_fora_sp="nasmar", ativa=True, por="a@x")

def test_chave_normalizada_e_tamanho_validado(shared_db):
    # "06.347.409" raiz -> "06347409"; raiz de 7 dígitos -> ValueError; cnpj de 13 -> ValueError

def test_chave_nao_pertence_a_duas_redes(shared_db):  # ValueError cita "Centauro"

def test_set_config_recusa_mesma_empresa_e_empresa_inativa(shared_db, envs_mm_nasmar): ...

def test_politica_monta_redes_com_raizes_e_cnpjs(shared_db, envs_mm_nasmar):
    # após cadastrar chaves nas 4 e ativar: politica().env_sp == "mm", rede Sam's tem cnpjs={...}, raizes=frozenset()
```

Fixtures `shared_db` / `envs_mm_nasmar`: seguir o padrão de `tests/test_roteamento_repo.py` (APP_DATA_DIR em `tmp_path` + `environments_repo.create`).

- [ ] **Step 2: Testes do rastro**

```python
def test_registrar_e_listar_em_ordem(shared_db): ...
def test_registrar_nunca_levanta(shared_db, monkeypatch):
    # router.shared_connect levantando -> registrar retorna None, sem exceção
def test_acao_invalida_nao_grava_e_nao_levanta(shared_db): ...
```

- [ ] **Step 3: Rodar e ver falhar:** `.venv/bin/pytest tests/test_regra_faturamento_repo.py tests/test_roteamento_decisao_repo.py -v`. Esperado: FAIL (módulo inexistente).
- [ ] **Step 4: Implementar** tabelas + semente em `schema_shared.py` (no mesmo bloco das tabelas de roteamento, com comentário de papel como as vizinhas) e os dois repos.
- [ ] **Step 5: Rodar e ver passar** (mesmo comando) + `.venv/bin/pytest tests/test_roteamento_repo.py -v` (não regrediu).
- [ ] **Step 6: Commit** `feat(roteamento): política de faturamento e rastro de decisão no banco compartilhado`

---

### Task 2: Núcleo puro da regra

**Files:**
- Create: `app/routing/regra.py`
- Test: `tests/test_routing_regra.py`

**Interfaces:**
- Consumes: `cnpjs_do_pedido(order)` e `_fmt` de `app/routing/ambiente.py` (import local para não haver ciclo, ou mover `_fmt` para `app/erp/cnpj.py` se o ciclo aparecer). Usa `cnpj_digits` de `app.erp.cnpj`.
- Produces:

```python
@dataclass(frozen=True)
class RedeFixa:
    nome: str
    env_slug: str
    raizes: frozenset[str]
    cnpjs: frozenset[str]

@dataclass(frozen=True)
class Politica:
    env_sp: str
    env_fora_sp: str
    redes: tuple[RedeFixa, ...]

@dataclass(frozen=True)
class UfLida:
    env_slug: str
    ufs: frozenset[str]      # UFs já normalizadas (strip+upper); "" = linha sem UF; vazio = não achou
    indisponivel: bool = False

@dataclass(frozen=True)
class ResultadoRegra:
    env_slug: str
    explicacao: str

def rede_fixa_do_pedido(order: Order, politica: Politica) -> RedeFixa | None: ...
def avaliar(order: Order, politica: Politica, ler_uf: Callable[[str], tuple[UfLida, ...]]) -> ResultadoRegra | None: ...
```

`rede_fixa_do_pedido`: para cada CNPJ de `cnpjs_do_pedido(order)`, casa por `cnpj` completo antes de `raiz`. Junta as redes que casaram; exatamente uma → ela; zero ou mais de uma → `None`.

`avaliar`, nesta ordem:
1. Rede fixa → `ResultadoRegra(rede.env_slug, f"Regra comercial: {rede.nome} fatura sempre por {rede.env_slug}")`. `ler_uf` **não** é chamado.
2. `cnpj = cnpj_digits(order.header.customer_cnpj)`; sem CNPJ → `None`.
3. `leituras = ler_uf(cnpj)`. Qualquer `indisponivel` → `None`. União das `ufs`: vazia → `None`; contém `""` → `None`; mais de uma → `None`.
4. `uf == "SP"` → `env_sp`, senão `env_fora_sp`. Explicação: `f"Regra comercial: CNPJ faturado {_fmt(cnpj)} em {uf} → {slug}"`.

- [ ] **Step 1: Testes**

```python
def test_rede_fixa_por_raiz_resolve_sem_ler_uf():  # ler_uf que levanta se chamado; resultado env "mm"
def test_rede_fixa_por_cnpj_completo_vence_raiz():   # Sam's com cnpjs={"X"}; outra rede com raiz de X[:8] -> Sam's
def test_rede_fixa_casa_por_cnpj_de_loja_sem_cabecalho():
def test_dois_cnpjs_em_redes_diferentes_nao_responde_como_fixa():  # cai no passo 2
def test_sp_vai_para_env_sp():                  # ufs {"SP"} -> "mm", explicação contém "em SP"
def test_fora_de_sp_vai_para_env_fora_sp():     # {"MG"} -> "nasmar"
def test_uf_suja_normalizada_vale():            # UfLida já vem normalizada; "SP" com leitura "sp " normalizada pelo leitor (Task 3) — aqui: {"SP"} em duas bases -> resolve
def test_sem_customer_cnpj_nao_responde():
def test_nao_achou_em_base_nenhuma_nao_responde():
def test_linha_sem_uf_nao_responde():           # ufs {""}
def test_ufs_diferentes_entre_bases_nao_responde():  # mm {"SP"}, nasmar {"MG"}
def test_base_indisponivel_nao_responde_mesmo_com_outra_achando():
```

- [ ] **Step 2: Rodar e ver falhar:** `.venv/bin/pytest tests/test_routing_regra.py -v`
- [ ] **Step 3: Implementar** `app/routing/regra.py` (puro, sem import de persistência no topo, como `ambiente.py`).
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** `feat(routing): núcleo puro da regra comercial de faturamento`

---

### Task 3: UF do cliente no Fire

**Files:**
- Modify: `app/erp/queries.py` (SQL novo no bloco de roteamento, ao lado de `count_pedidos_cliente_desde_sql`)
- Modify: `app/routing/regra.py` (leitor real + cache)
- Test: `tests/test_routing_regra.py` (mesmo arquivo), `tests/test_queries_regra_uf.py`

**Interfaces:**
- Consumes: nome da coluna da UF da Task 0 (abaixo `<UF>`).
- Produces:
  - `queries.UF_CLIENTE_POR_CNPJ`: `SELECT TRIM(C.<UF>) FROM CADASTRO C WHERE C.RELAC_CLIENTE = 'Sim' AND REPLACE(REPLACE(REPLACE(REPLACE(C.CPF_CNPJ, '.', ''), '/', ''), '-', ''), ' ', '') = ?`. Bind: `(cnpj_14_digitos,)`. Sem `ROWS`: precisa ver duplicatas.
  - `regra.ler_uf(cnpj: str, *, envs: Sequence[dict], ler: Callable[[dict, str], list[str | None]] | None = None) -> tuple[UfLida, ...]`. Uma `UfLida` por env de `envs`. `ler` levantando → `indisponivel=True` + `logger.warning("routing.regra.uf_indisponivel ...")`. Normaliza cada valor com `(v or "").strip().upper()`. Normaliza a entrada com `cnpj_digits`.
  - `regra._uf_no_fire(env: dict, cnpj: str) -> list[str | None]`: `environments_repo.to_fb_config(env)` + `FirebirdConnection().connect_somente_leitura(cfg)`.
  - `regra.ler_uf_com_cache(cnpj: str, envs: Sequence[dict]) -> tuple[UfLida, ...]`: cache em memória do processo, chave `(cnpj, tuple(slugs))`, TTL de 600 s, `threading.Lock`. **Só guarda resultado sem `indisponivel`.**

- [ ] **Step 1: Testes**

```python
def test_sql_comeca_com_select_e_tem_um_bind():
    assert queries.UF_CLIENTE_POR_CNPJ.lstrip().upper().startswith("SELECT")
    assert queries.UF_CLIENTE_POR_CNPJ.count("?") == 1

def test_ler_uf_normaliza_cnpj_mascarado_de_entrada():  # ler recebe "12345678000190" para entrada "12.345.678/0001-90"
def test_ler_uf_normaliza_valores():                     # ["sp ", None] -> ufs {"SP", ""}
def test_ler_uf_mesmo_cnpj_duas_linhas_ufs_diferentes(): # ["SP","MG"] -> ufs {"SP","MG"} -> avaliar() devolve None
def test_ler_uf_erro_vira_indisponivel():
def test_cache_nao_guarda_indisponivel_e_expira(monkeypatch):  # time.monotonic monkeypatched
```

O teste de integração contra uma cópia do Fire fica sob o marcador já usado no repo para Firebird real (procurar o `pytest.mark` existente em `tests/` com `grep -rn "mark\." tests | grep -i fire`). Se não houver marcador, não criar teste de integração. A Task 11 verifica à mão.

- [ ] **Step 2: Rodar e ver falhar.**
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar:** `.venv/bin/pytest tests/test_routing_regra.py tests/test_queries_regra_uf.py -v`
- [ ] **Step 5: Commit** `feat(routing): UF do cliente faturado lida no Fire, só leitura`

---

### Task 4: Degrau `regra` na escada

**Files:**
- Modify: `app/routing/ambiente.py`
- Test: `tests/test_routing_ambiente.py`

**Interfaces:**
- Consumes: `ResultadoRegra`, `avaliar`, `ler_uf_com_cache` (Tasks 2-3); `regra_faturamento_repo.politica()` (Task 1).
- Produces:
  - `DEGRAUS = ("regra", "documento", "historico", "memoria", "perguntar")`
  - `Decisao.contraria: str | None = None`: slug que o **documento** apontaria quando difere do decidido pela regra.
  - `Deps.regra: Callable[[Order], ResultadoRegra | None] | None = None`
  - `ambiente_para`: antes do degrau 1, `r = deps.regra(order) if deps.regra else None`. Se `r`, então `Decisao(env_slug=r.env_slug, degrau="regra", explicacao=r.explicacao, divergiu_de=<lembrado se difere>, contraria=<deps.env_por_cnpj(fornecedor) se existe e difere>)`. Exceção de `deps.regra` propaga (mesmo contrato dos outros degraus).
  - `deps_padrao()`: `pol = regra_faturamento_repo.politica()`. Se `None` → `regra=None`. Senão, `envs = [environments_repo.get_by_slug(s) for s in (pol.env_sp, pol.env_fora_sp)]` filtrando ativos, e `regra=lambda order: avaliar(order, pol, lambda c: ler_uf_com_cache(c, envs))`. Se algum dos dois envs estiver inativo → `regra=None` (a regra não decide pela metade).
  - Atualizar a docstring do módulo (escada com 5 degraus).

- [ ] **Step 1: Testes**

```python
def test_regra_vence_documento_e_marca_contraria():
    # deps.regra -> "mm"; supplier_cnpj casa "nasmar" -> degrau "regra", env "mm", contraria "nasmar"
def test_regra_none_cai_no_documento_como_hoje():
def test_deps_regra_none_mantem_escada_atual():   # todos os testes existentes seguem verdes sem tocar neles
def test_regra_divergiu_da_memoria():             # memória "nasmar", regra "mm" -> divergiu_de "nasmar"
def test_contraria_none_quando_documento_concorda():
def test_deps_padrao_sem_politica_nao_monta_regra(shared_db):  # deps_padrao().regra is None
def test_deps_padrao_com_env_inativo_nao_monta_regra(shared_db, envs_mm_nasmar):
```

- [ ] **Step 2: Rodar e ver falhar:** `.venv/bin/pytest tests/test_routing_ambiente.py -v`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar a suíte do domínio:** `.venv/bin/pytest tests/test_routing_documento.py tests/test_routing_historico.py tests/test_routing_ambiente.py tests/test_routing_regra.py tests/test_routing_modo.py -v`. Esperado: tudo PASS.
- [ ] **Step 5: Commit** `feat(routing): degrau regra no topo da escada`

---

### Task 5: Preview — sugestão, contraria e troca

**Files:**
- Modify: `app/web/server.py` (`_roteamento_para_preview`)
- Test: `tests/test_routing_wiring.py`

**Interfaces:**
- Produces: o bloco `roteamento` do payload ganha:
  - `"contraria"` (slug | None) e `"contraria_nome"` (nome do env | None)
  - `"pode_trocar"`: `modo == LIGADO and decisao.resolveu`
  - mantém `precisa_escolher` e `opcoes` como estão. Em `observando` não há `pode_trocar` (é `False`).

- [ ] **Step 1: Testes:** `test_preview_ligado_resolvido_pode_trocar`, `test_preview_observando_nao_pode_trocar`, `test_preview_leva_contraria_com_nome`.
- [ ] **Step 2: Rodar e ver falhar:** `.venv/bin/pytest tests/test_routing_wiring.py -v -k preview`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** `feat(web): preview mostra sugestão contrariada e permite trocar em ligado`

---

### Task 6: Commit — escolha do operador vence e fica registrada

**Files:**
- Modify: `app/web/server.py` (`_resolver_env_alvo`, `commit_preview`)
- Test: `tests/test_routing_wiring.py`

**Interfaces:**
- Consumes: `roteamento_decisao_repo.registrar` (Task 1).
- Produces:
  - `_resolver_env_alvo` em `ligado`: `body.environment_slug` (escolha explícita) > `decisao` resolvida > 409. Fora de `ligado`, nada muda. Atualizar a docstring.
  - Em `commit_preview`, dentro do mesmo `persist_ctx`, depois de `append_audit`, quando `decisao is not None`:
    - `ligado` + resolveu + escolhido == sugerido → `acao="sugestao_aceita"`
    - `ligado` + resolveu + escolhido != sugerido → `acao="troca_no_preview"`
    - `ligado` + não resolveu → `acao="escolha_sem_sugestao"`
    - `observando` → `acao="observando"` (escolhido = env do cookie)
    - sempre `decidido_por=user.email`, `motivo=decisao.explicacao`, `modo=modo`, `degrau=decisao.degrau`
  - Memória: além do caso de hoje (`ligado` + não resolveu), `memoria.lembrar(...)` também em `troca_no_preview`.
  - O bloco `roteamento` do `append_audit` ganha `"contraria"` e `"decidido_por"`.

- [ ] **Step 1: Testes**

```python
def test_ligado_operador_troca_sugestao_vence_e_registra(client_ligado):
    # decisão "nasmar" pela regra; body.environment_slug="mm" -> pedido no db da mm;
    # roteamento_decisao: acao "troca_no_preview", env_sugerido "nasmar", env_escolhido "mm", decidido_por = email do usuário
    # decisao_ambiente lembrada "mm"
def test_ligado_sem_escolha_aceita_sugestao_e_registra(client_ligado):   # acao "sugestao_aceita"
def test_ligado_nao_resolvido_com_escolha_registra_escolha_sem_sugestao(client_ligado):
def test_observando_continua_no_cookie_e_registra_observando(client_observando):
def test_desligado_nao_registra_nada(client_desligado):   # roteamento_decisao vazia
def test_ligado_escolha_para_empresa_inativa_da_412_sem_consumir_preview(client_ligado):
```

- [ ] **Step 2: Rodar e ver falhar:** `.venv/bin/pytest tests/test_routing_wiring.py -v`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar** + `.venv/bin/pytest tests/test_routing_modo.py -v`.
- [ ] **Step 5: Commit** `feat(web): operador troca a empresa sugerida e a decisão fica registrada`

---

### Task 7: Watcher — rastro do automático

**Files:**
- Modify: `app/worker/jobs/scan_environments.py` (bloco `active_env(env_alvo)` perto da linha 328)
- Test: `tests/test_scan_environments_roteamento.py`

**Interfaces:**
- Produces: depois do `append_audit` do watcher, quando `decisao is not None`:
  - `ligado` (resolvido, porque só esse importa) → `acao="automatico"`, `decidido_por=f"sistema:{decisao.degrau}"`, `env_escolhido=env_alvo["slug"]`
  - `observando` → `acao="observando"`, `decidido_por="sistema:pasta"`, `env_escolhido=env["slug"]` (varrido)
  - Arquivo retido (pendência) não registra nada.

- [ ] **Step 1: Testes:** `test_watcher_ligado_registra_automatico_com_degrau_regra`, `test_watcher_observando_registra_observando`, `test_watcher_pendencia_nao_registra`.
- [ ] **Step 2: Rodar e ver falhar:** `.venv/bin/pytest tests/test_scan_environments_roteamento.py -v`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar** + `.venv/bin/pytest tests/test_scan_environments.py -v`.
- [ ] **Step 5: Commit** `feat(worker): watcher registra a decisão automática de empresa`

---

### Task 8: Mover pedido para a outra empresa

**Files:**
- Create: `app/persistence/mover_pedido.py`
- Modify: `app/web/server.py` (rota nova)
- Test: `tests/test_mover_pedido.py`

**Interfaces:**
- Consumes: `router.env_connect(slug)`, `env_do_import_id`, `check_order`, `preco_trava.check_preco_para_gravar`, `repo.update_fire_metadata`, `roteamento_decisao_repo`.
- Produces:
  - `class PedidoNaoMovivel(Exception)` com `motivo: str`
  - `mover_import(import_id: str, *, origem: dict, destino: dict, por: str) -> None`:
    1. Lê a linha em `origem`. Se não existir e existir no destino → retorna (já movido). Se existir nos dois → pula para o passo 4 (conclusão de mover interrompido).
    2. Guarda: `portal_status == 'parsed'`, `fire_codigo IS NULL` e `output_files_json` vazio/`'[]'`/NULL. Senão, `PedidoNaoMovivel("já foi para o Fire" | "já foi exportado em XLSX" | "não está em revisão")`.
    3. No destino, numa transação: `INSERT` da linha com `environment_id = destino["id"]` e com `check_json`, `check_preco_json`, `cliente_override_*` e `sem_preco_ack_*` = NULL. Copia `audit_log` e `order_lifecycle_events` do import (com `environment_id` do destino, sem o `id` autoincremento).
    4. Na origem, numa transação: `DELETE` em `erp_item_vinculo`, `erp_xlsx_linha`, `preco_revisao`, `preco_revisao_piso`, `preco_decisao`, `outbox`, `audit_log`, `order_lifecycle_events` e por fim `imports`, todos `WHERE import_id = ?` (`id = ?` em `imports`). Não confiar no `ON DELETE CASCADE` (o PRAGMA pode estar desligado).
    5. `append_audit` no destino: `"movido_de_empresa"` com `{de, para, por}`.
  - Rota `POST /api/imported/{import_id}/mover-empresa`, body `{"destino": "<slug>"}`, `require_user`:
    - 409 se `roteamento_repo.modo() == DESLIGADO`
    - 404 se `env_do_import_id` não acha; 422 se destino == origem ou destino inativo
    - 409 com `motivo` em `PedidoNaoMovivel`
    - depois de mover, dentro de `env_context.active_env(destino)`: re-roda `check_order(order, env=destino)` (grava só se `available`) e `preco_trava.check_preco_para_gravar(order, destino)` → `update_fire_metadata`
    - `roteamento_decisao_repo.registrar(acao="movido_apos_importar", env_sugerido=<ultima(import_id).env_sugerido ou None>, env_escolhido=destino slug, decidido_por=user.email, modo=modo, degrau=None, motivo=f"movido de {origem} para {destino}")`
    - resposta `{"import_id", "de", "para"}`
  - O arquivo original não se mexe (`original_path` continua valendo).

- [ ] **Step 1: Testes**

```python
def test_move_pedido_em_revisao_com_audit_e_eventos(envs_mm_nasmar):  # some na origem, nasce no destino com mesmo id, audit copiado + "movido_de_empresa"
def test_move_limpa_dados_derivados_da_origem(envs_mm_nasmar):        # check_json/check_preco_json NULL no destino; erp_item_vinculo/preco_* vazios na origem
def test_nao_move_pedido_com_fire_codigo(envs_mm_nasmar):              # PedidoNaoMovivel "já foi para o Fire"
def test_nao_move_pedido_exportado_xlsx(envs_mm_nasmar):
def test_nao_move_pedido_fora_de_revisao(envs_mm_nasmar):
def test_mover_interrompido_conclui_sem_duplicar(envs_mm_nasmar, monkeypatch):
    # falha simulada no passo 4; 2ª chamada: um só pedido (no destino), audit sem duplicar
def test_rota_409_em_desligado(client): ...
def test_rota_registra_movido_apos_importar_com_usuario(client_ligado): ...
def test_rota_refaz_check_no_destino(client_ligado, monkeypatch):  # check_order chamado com env do destino
```

- [ ] **Step 2: Rodar e ver falhar:** `.venv/bin/pytest tests/test_mover_pedido.py -v`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** `feat(web): mover pedido em revisão para a outra empresa`

---

### Task 9: API do admin e do rastro

**Files:**
- Modify: `app/web/routes_roteamento.py`
- Test: `tests/test_routes_regra_faturamento.py`

**Interfaces:**
- Produces (nenhuma dessas rotas existe hoje):
  - `GET /api/roteamento/regra` (`require_user`) → `{"config": config() | None, "redes": listar_redes(), "empresas": [{slug, name}] ativas}`
  - `PUT /api/roteamento/regra` (`require_admin`), body `{env_sp, env_fora_sp, ativa}` → `set_config`. `ValueError` → 400 com a mensagem.
  - `POST /api/roteamento/regra/redes` e `PUT /api/roteamento/regra/redes/{rede_id}` (`require_admin`), body `{nome, env_slug, chaves: [{tipo, chave}]}` → `salvar_rede`. `ValueError` → 400.
  - `DELETE /api/roteamento/regra/redes/{rede_id}` (`require_admin`). Se a regra está ativa e a rede é a última com chave, não há guarda extra: a regra continua valendo para as outras.
  - `GET /api/roteamento/decisoes?import_id=...` (`require_user`) → `listar_por_import`
  - Bodies validados com pydantic (`tipo: Literal["raiz", "cnpj"]`, `chave: str` com `max_length=18`).

- [ ] **Step 1: Testes:** operador comum recebe 403 nas escritas; 400 com a mensagem do repo; GET devolve as 4 redes da semente; decisões por import em ordem.
- [ ] **Step 2: Rodar e ver falhar.** `.venv/bin/pytest tests/test_routes_regra_faturamento.py -v`
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar e ver passar.**
- [ ] **Step 5: Commit** `feat(web): API da regra de faturamento e do rastro de decisão`

---

### Task 10: Telas — admin, preview e pedido

**Files:**
- Modify: `app/web/static/admin-roteamento.html` (seção "Regra comercial")
- Modify: `app/web/static/index.html` (`renderRoutingBanner` e o detalhe do pedido)
- Modify: `app/web/static/js/roteamento-labels.js` (rótulo do degrau `regra`)

**Interfaces:**
- Consumes: as rotas da Task 9, o payload da Task 5, a rota da Task 8 e o 409 da Task 6.
- Produces (comportamento):
  - **Admin**: escolher a empresa de SP e a de fora (selects com as ativas); interruptor "Regra ligada" que mostra o erro do 400 em texto quando há rede sem chave; lista das redes fixas com chaves editáveis (tipo + valor) e aviso "sem chave: a regra não liga" por rede.
  - **Preview**: o degrau `regra` aparece como "Regra comercial", com a explicação em texto. Se `contraria`: linha de atenção "O pedido traz o CNPJ da {contraria_nome}". Se `pode_trocar`: seletor com a sugestão marcada; escolher outra empresa abre confirmação **inline** (nunca `confirm()`) e manda `environment_slug` no commit.
  - **Detalhe do pedido**: linha do tempo de `GET /api/roteamento/decisoes` ("19/10 14:02 · sugerido Nasmar pela regra · trocado para MM por fulano@..."); botão "Mover para {outra empresa}" só quando o pedido está em revisão, sem número do Fire e com o modo diferente de desligado; motivo do 409 em texto.
  - Seguir os tokens, o foco visível e os componentes que as duas páginas já usam. Nada de estilo novo solto.

- [ ] **Step 1: Subir o portal local por `portless`** (skill `portless`; nunca `python ui.py` cru) com dois ambientes de teste e o modo `ligado`.
- [ ] **Step 2: Implementar admin → preview → detalhe.**
- [ ] **Step 3: Percorrer à mão:** cadastrar chaves → ligar a regra → importar um sample Centauro (sugere MM) e um de outra rede com CNPJ de fora de SP no Fire de teste (sugere Nasmar) → trocar → ver a linha do tempo → mover. Screenshot de cada estado.
- [ ] **Step 4: Validação de design obrigatória:** skill `hm-designer` sobre as três telas. Corrigir o que reprovar.
- [ ] **Step 5: Commit** `feat(ui): regra comercial no admin, troca no preview e mover no pedido`

---

### Task 11: Docs do módulo e verificação final

**Files:**
- Modify: `docs/ai/modules/routing.md` (escada com 5 degraus, a regra, as tabelas novas, o mover)
- Modify: `docs/ai/modules/persistence.md` (schema das 4 tabelas), `docs/ai/modules/web.md` (rotas novas)
- Modify: `docs/ai/00-index.md` (testes novos na linha `routing`)

- [ ] **Step 1: Atualizar só as seções afetadas** (regra do CLAUDE.md do projeto).
- [ ] **Step 2: Lint:** `ruff check app/ tests/` → sem erros.
- [ ] **Step 3: Suíte completa:** `.venv/bin/pytest tests/ -q` → tudo verde; anotar o total.
- [ ] **Step 4: Prova contra a cópia do Fire** (se houver cópia local; senão, registrar que não foi verificado): `ler_uf` para 3 CNPJs reais (1 SP, 1 fora, 1 inexistente) com o resultado esperado.
- [ ] **Step 5: Commit** `docs(routing): regra comercial de faturamento`
- [ ] **Step 6: Push, PR, deploy e migration em produção: perguntar ao Samuel.** Nada disso sai sem ok.
