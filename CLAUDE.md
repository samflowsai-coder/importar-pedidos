# CLAUDE.md — Portal de Pedidos

> **Contrato de execução.** Detalhe técnico não mora aqui — mora em `docs/ai/`.
> Se um fato aparecer nos dois lugares, `docs/ai/` ganha.
> Números deste arquivo verificados contra o código em **2026-08-26**.

---

## O que é

**Portal de Pedidos** — porta de entrada de pedidos de varejistas para um fornecedor
de calçados. Recebe pedidos de compra (PDF + XLS/XLSX), parseia, apresenta preview
para validação humana e importa no ERP **Fire Sistemas** (Firebird). Exporta `.xlsx`
como caminho principal ou fallback, conforme `EXPORT_MODE`.

Roda **N empresas em paralelo** (multi-ambiente): cada uma com pastas, Firebird e
SQLite próprios. Integra com o **Gestor de Produção** e com o **FlowPCP**.

Dois pontos de entrada: CLI (`main.py`) para lote e web (`ui.py` → `app/web/server.py`)
com fluxo preview → commit.

---

## Protocolo de execução

**Antes de qualquer task, leia `docs/ai/00-index.md` PRIMEIRO.** Ele roteia
task → domínio → arquivos a carregar → testes a rodar. Não carregue o projeto inteiro.

### Contexto mínimo

1. **Identifique o domínio** pelo `00-index.md`.
2. **Carregue apenas:** o `docs/ai/modules/<domínio>.md` + os arquivos críticos que
   ele lista + o teste correspondente.
3. **Não leia módulos irmãos.** Task em um parser não lê os outros nove. Task em
   `app/erp/` não lê `app/web/`. E vice-versa.
4. Task que cruza domínios: leia cada `modules/<domínio>.md` antes do código.

### Disciplina

- **Diff pequeno.** Uma intenção por commit/PR. Refactor não anda junto com bug fix.
- **Teste direcionado** do módulo afetado durante o trabalho; suíte completa só antes
  do commit final.
- **Doc incremental.** Mudou contrato (modelo, rota, query, helper)? Atualize **só a
  seção afetada** de `docs/ai/modules/<domínio>.md`. Não reescreva o módulo.
- **Sem invenção.** Helper que já existe (`_find`, `_parse_br_number`, `BaseParser`,
  `OrderNormalizer`) se reusa, não se duplica.

### Auto-protocolo

1. `docs/ai/00-index.md` → domínio.
2. `docs/ai/modules/<domínio>.md` → arquivos críticos e testes.
3. Ler só os arquivos críticos.
4. Implementar (diff pequeno).
5. Teste direcionado → se passar, suíte completa.
6. Atualizar a seção do module doc se o contrato mudou.
7. Resumir em 1–2 frases.

### Templates

`docs/ai/templates/` — `bug.md`, `feature.md`, `refactor.md`, `investigation.md`.

---

## Stack

| Camada | Escolha |
|---|---|
| Linguagem | **Python 3.11+** (`requires-python = ">=3.11"`). `X \| Y` e `match` liberados. |
| Modelos | **pydantic v2** — `app/models/` |
| PDF | **pdfplumber** — texto + tabelas |
| Planilha | **openpyxl** (xlsx, leitura e escrita) + **xlrd** (.xls legado) |
| Web | **FastAPI + uvicorn** — `app/web/server.py` |
| LLM | **openai SDK via OpenRouter** — fallback; modelo em `OPENROUTER_MODEL` |
| ERP | **firebird-driver** — Fire Sistemas (embedded + TCP) |
| Agendamento | **APScheduler** — `app/worker/` |
| Log | **loguru** com rotação |
| Lint/format | **ruff** |
| Testes | **pytest** — **1111 testes** em 92 arquivos |

---

## Mapa do repositório

Cada linha aponta pro doc que tem o detalhe. Este mapa é roteamento, não referência.

| Pacote | Papel | Doc |
|---|---|---|
| `app/ingestion/`, `app/classifiers/`, `app/extractors/` | disco → bytes → formato → texto/tabelas | `modules/extractors.md` |
| `app/parsers/` | cascata determinística: 11 específicos + genérico | `modules/parsers.md` |
| `app/llm/` | fallback OpenRouter quando a cascata inteira falha | `modules/llm.md` |
| `app/normalizers/`, `app/validators/` | datas/case/CNPJ; obrigatórios e qty > 0 | `modules/normalizers.md`, `modules/validators.md` |
| `app/exporters/` | XLSX (split por loja) e Firebird | `modules/exporters.md` |
| `app/erp/` | Firebird do Fire: queries, mapper, check de produto/preço, de-para | `modules/erp.md` |
| `app/web/` | FastAPI (68 rotas): preview → commit, admin, app shell | `modules/web.md` |
| `app/persistence/` | SQLite compartilhado + por ambiente, repos, roteador | `modules/persistence.md`, `modules/environments.md` |
| `app/state/` | máquina de estados do pedido + eventos de ciclo de vida | `modules/state.md` |
| `app/security/` | bcrypt, HMAC, rate-limit, `secret_store` (Fernet) | `modules/security.md` |
| `app/http/` | cliente HTTP de saída + políticas de retry | `modules/http.md` |
| `app/integrations/gestor/` | Gestor de Produção: outbox, mapper, webhooks | `modules/gestor.md` |
| `app/integrations/flowpcp/` | FlowPCP: push de pedido, catálogo, clientes, intercompany | `modules/flowpcp.md` |
| `app/worker/` | APScheduler: `drain_outbox`, `poll_fire`, `poll_flowpcp`, `retention`, `scan_environments` | `modules/worker.md` |
| `app/updates/` | auto-update pelo próprio portal (`/admin/atualizacao`) | `modules/updates.md` |
| `app/observability/` | métricas Prometheus (`/metrics`) + `trace_id` | `modules/observability.md` |
| `app/models/` | `Order`, `OrderHeader`, `OrderItem`, `ERPRow` | `modules/models.md` |
| `app/pipeline.py` | orquestrador stateless do fluxo de arquivo | `modules/pipeline.md` |
| `app/consolidators/` | vazio — reservado para merge de pedidos (v2) | — |
| `tools/` | scripts operacionais (build, usuários, exploração do Firebird, syncs) | — |
| `samples/` | arquivos reais de pedido usados como fixture | — |

---

## Comandos

```bash
pip install -e ".[dev]"                  # instalar

ruff check app/ tests/                   # lint
ruff format app/ tests/                  # format

.venv/bin/pytest tests/ -v               # suíte completa (1111 testes)
.venv/bin/pytest tests/<arquivo>.py -v   # direcionado (use este durante a task)

python ui.py                             # web → http://127.0.0.1:3636
python main.py                           # CLI em lote (input/ → output/)
python -m app.worker                     # worker (APScheduler)

docker compose up --build                # container
```

---

## Variáveis de ambiente

```env
# LLM (fallback) — a única chave obrigatória pro pipeline completo
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=                # default definido em app/llm/

# Diretórios e modo de exportação
APP_DATA_DIR=data/               # app_shared.db + app_state_<slug>.db
INPUT_DIR=input/
OUTPUT_DIR=output/
EXPORT_MODE=xlsx                 # xlsx | db | both

# Servidor web
PORTAL_HOST=127.0.0.1            # 0.0.0.0 para expor na rede local
PORTAL_PORT=3636
PORTAL_RELOAD=false
PORTAL_COOKIE_SECURE=1           # 0 em dev/HTTP local
SESSION_TTL_HOURS=24
RATE_LIMIT_ENABLED=true

# Firebird singleton — LEGADO. Só vale se NÃO houver ambiente ativo;
# em deploy multi-empresa a config vive em /admin/ambientes (senha cifrada).
FB_DATABASE=/path/emp.fdb        # .fdb (embedded) ou path no servidor
FB_HOST=192.168.1.10             # omitir = embedded
FB_PORT=3050
FB_USER=SYSDBA
FB_PASSWORD=
FB_CLIENT_LIBRARY=               # path ESTÁVEL da lib cliente (nunca /tmp)
FB_CODEMPRESA=1                  # código da empresa usado pelo mapper
FIRE_TRIGGER_STATUS=             # vazio = poll_fire só observa, não dispara

# Worker
RETENTION_DAYS=180
BACKUP_DIR=                      # vazio = backup desligado

# Reconciliação com o Fire
PORTAL_RECONCILE_PERIODICO=      # 0 desliga os gatilhos automáticos (periódico
                                 # no processo web + job do worker). O botão
                                 # "Verificar no Fire" continua funcionando.
                                 # Qualquer outro valor, ou ausente = ligado.

# Webhooks do Gestor
WEBHOOK_SECRET_GESTOR=
WEBHOOK_SECRET_GESTOR_PREVIOUS=  # janela de rotação

# Só em teste
TEST_AUTH_BYPASS=                # 1 desliga auth — NUNCA em produção
```

Nunca commitar `.env`.
**Atenção:** `.env.example` está desatualizado (traz 4 chaves das ~24 lidas pelo
código, e uma delas — `LOG_DIR` — não é lida por ninguém). Ver `docs/BACKLOG.md`.

---

## Multi-ambiente

O Portal opera N empresas em paralelo. Cada **ambiente** tem pastas próprias e banco
Firebird próprio, configurados em `/admin/ambientes`. Pedidos de uma empresa nunca
aparecem em listagens de outra: cada uma tem seu SQLite (`app_state_<slug>.db`); auth
e metadata vivem no `app_shared.db`.

Fluxo: admin cria ambientes → operador loga → escolhe em `/selecionar-ambiente` →
cookie `portal_env` ativo → toda a navegação dele é naquela empresa até trocar.

Detalhe em [`docs/ai/modules/environments.md`](docs/ai/modules/environments.md).

---

## Decisões arquiteturais (não mudar sem razão explícita)

- **LLM é fallback, não default** — custo zero nos formatos cobertos por parser.
- **Pipeline stateless** — cada arquivo é independente; paralelizar depois não exige
  refatoração.
- **Split por loja no exportador, não no parser** — `Order` fica simples, agrupamento
  centralizado.
- **Pydantic em todos os modelos** — contrato explícito, erro aparece cedo.
- **Parsers específicos antes do genérico** na cascata.
- **Integração externa passa por outbox + worker**, nunca pelo request path — a UI não
  espera Gestor nem Flow.
- **Segredo nunca em texto plano no banco** — `app/security/secret_store.py` (Fernet).

---

## Escopo aberto

Backlog vivo em [`docs/BACKLOG.md`](docs/BACKLOG.md). Planos e specs históricos em
`docs/superpowers/`. Documentos congelados da v1 em `docs/history/`.

## Onde NÃO procurar

`dist/`, `.superpowers/`, `.venv/`, `output/`, `logs/` e `data/` são ignorados pelo
git. Cópias de doc que aparecem dentro de `dist/` são artefato de build — nunca edite lá.
