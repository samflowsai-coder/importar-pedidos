# CLAUDE.md — Portal de Pedidos

## O que é este projeto

**Portal de Pedidos** — porta de entrada de pedidos de varejistas para um fornecedor de calçados. Pipeline de automação que recebe pedidos de compra (PDF + XLS/XLSX), parseia, apresenta preview para validação humana e importa direto no ERP Fire Sistemas (Firebird). Exporta `.xlsx` como fallback opcional.

Dois pontos de entrada: CLI (`main.py`) para lote e interface web (`ui.py`, rota `/`) com fluxo preview → commit.

---

## Stack

- **Python 3.11+** — `requires-python = ">=3.11"`. `X | Y` union syntax e `match` são suportados. O venv local deve ser 3.11+.
- **pydantic v2** — modelos de dados em `app/models/`
- **pdfplumber** — extração de texto e tabelas de PDF
- **openpyxl / xlrd** — leitura XLSX/XLS legado e geração do output
- **FastAPI + uvicorn** — interface web em `app/web/server.py`
- **openai SDK (OpenRouter)** — LLM fallback via OpenRouter (default: Gemini Flash 1.5); configurável por `OPENROUTER_MODEL`
- **firebird-driver** — conexão com banco Firebird do Fire Sistemas ERP (embedded + TCP); configurável por `FB_DATABASE`
- **loguru** — logging estruturado com rotação automática
- **ruff** — lint + formato (`ruff check` + `ruff format`)
- **pytest** — 48 testes em `tests/`

---

## Estrutura de Pastas

```
app/
├── classifiers/         # Detecção PDF vs XLS/XLSX
├── extractors/          # pdfplumber (PDF) + openpyxl/xlrd (XLS)
├── ingestion/           # FileLoader: disco → LoadedFile
├── parsers/             # 8 parsers específicos + genérico + base
│   ├── base_parser.py   # BaseParser: _find(), _parse_br_number()
│   ├── mercado_eletronico_parser.py
│   ├── pedido_compras_revenda_parser.py
│   ├── sbf_centauro_parser.py
│   ├── beira_rio_parser.py
│   ├── kolosh_parser.py
│   ├── sams_club_parser.py
│   ├── kallan_xls_parser.py
│   ├── desmembramento_xls_parser.py
│   └── generic_parser.py
├── llm/                 # LLMFallbackParser (Claude Haiku)
├── normalizers/         # OrderNormalizer: datas, uppercase, title case
├── validators/          # OrderValidator: campos obrigatórios, qty > 0
├── exporters/           # ERPExporter → output/*.xlsx (split por loja)
├── models/              # Order, OrderHeader, OrderItem, ERPRow
├── consolidators/       # Reservado para v2
├── utils/               # logger compartilhado (loguru)
├── web/                 # FastAPI: server.py + static/index.html
└── pipeline.py          # Orquestrador: recebe path → chama tudo em sequência
main.py                  # Entrada CLI (processa input/ em lote)
ui.py                    # Entrada web (sobe uvicorn)
samples/                 # Arquivos reais de pedido usados como fixtures de teste
```

---

## Pipeline (fluxo de dados)

```
input file
    → FileLoader
    → FormatClassifier
    → PDFExtractor | XLSExtractor
    → Parser Chain (cascade, para no primeiro match):
        1. MercadoEletronicoParser
        2. PedidoComprasRevendaParser
        3. SbfCentauroParser
        4. BeiranRioParser
        5. KoloshParser
        6. SamsClubParser
        7. KallanXlsParser
        8. DesmembramentoXlsParser
        9. GenericParser
        → (se None) → LLMFallbackParser (Claude Haiku)
    → OrderNormalizer
    → OrderValidator
    → ERPExporter → output/*.xlsx
```

**Regra fundamental:** parsers determinísticos primeiro, LLM só como última saída. Custo zero em ~80% dos arquivos.

---

## Como Adicionar um Novo Parser

1. Criar `app/parsers/novo_parser.py` herdando de `BaseParser`
2. Implementar `can_parse(self, extracted: dict) -> bool` com assinatura única do formato
3. Implementar `parse(self, extracted: dict) -> Optional[Order]`
4. Registrar em `app/pipeline.py` na lista `_parsers`, **antes** do `GenericParser`
5. Adicionar sample em `samples/`
6. Escrever testes em `tests/test_new_parsers.py`

**Helpers no BaseParser:**
- `_find(text, pattern)` → `Optional[str]`
- `_parse_br_number(value)` → `Optional[float]` — formato brasileiro (`1.000,50`)
- Kolosh usa `_parse_us_number` próprio (ponto = milhar: `500.000` = 500 unid.)

---

## Modelos de Dados

**OrderItem** — campos relevantes: `description`, `product_code`, `ean`, `quantity`, `unit_price`, `total_price`, `obs`, `delivery_date`, `delivery_cnpj`, `delivery_name`

**ERPRow** — colunas do output: `PEDIDO`, `NOME_CLIENTE`, `CNPJ_CLIENTE`, `CODIGO_PRODUTO`, `EAN`, `DESCRICAO`, `QUANTIDADE`, `PRECO_UNITARIO`, `VALOR_TOTAL`, `OBS`, `DATA_ENTREGA`, `CNPJ_LOCAL_ENTREGA`

---

## Lógica de Desmembramento (split por loja)

O `ERPExporter` agrupa itens por chave de entrega e gera um `.xlsx` por grupo:

| Condição | Chave | Resultado |
|----------|-------|-----------|
| `delivery_cnpj` ≠ CNPJ do cliente | CNPJ da loja | Split por CNPJ (ex: Riachuelo) |
| `delivery_cnpj` ausente + `delivery_name` preenchido | nome da loja | Split por nome (ex: NBA) |
| Sem delivery | `""` | Arquivo único |

**Nomenclatura:** `{NOME_CLIENTE}_{CNPJ}_{PEDIDO}_{SUFIXO}.xlsx`

---

## Interface Web

- **URL:** `http://localhost:8000`
- **Start:** `python ui.py`
- **Rotas:** `GET /`, `GET /api/config`, `POST /api/process`, `GET /api/download?path=`, `GET /api/fs?path=`
- **Segurança:** whitelist de extensões (pdf/xls/xlsx), limite 50MB, path traversal bloqueado no download (somente `.xlsx`)

---

## Testes

```bash
.venv/bin/pytest tests/ -v
```

48 testes. Samples reais em `samples/`. Ao adicionar parser, adicionar teste correspondente antes de abrir PR.

---

## Variáveis de Ambiente

```env
ANTHROPIC_API_KEY=sk-ant-...   # obrigatório para LLM fallback
INPUT_DIR=input/               # CLI: diretório de entrada
OUTPUT_DIR=output/             # CLI: diretório de saída

# Firebird / Fire Sistemas ERP
EXPORT_MODE=xlsx               # xlsx | db | both
FB_DATABASE=/path/to/emp.fdb   # arquivo .fdb (embedded) ou path no servidor
FB_HOST=192.168.1.10           # host TCP (omitir para embedded)
FB_PORT=3050                   # porta TCP (padrão)
FB_USER=SYSDBA                 # padrão Firebird
FB_PASSWORD=masterkey          # padrão Firebird
```

Copiar de `.env.example`. Nunca commitar `.env`.

---

## Comandos Frequentes

```bash
# Instalar
pip install -e ".[dev]"

# Lint + format
ruff check app/ tests/
ruff format app/ tests/

# Testes
.venv/bin/pytest tests/ -v

# Web
python ui.py

# CLI em lote
python main.py

# Docker
docker compose up
docker compose up --build
```

---

## Decisões Arquiteturais (não mudar sem razão explícita)

- **LLM como fallback, não padrão** — custo zero nos casos cobertos por parsers
- **Pipeline stateless** — cada arquivo é independente; paralelização futura sem refatoração
- **Split no exportador, não no parser** — `Order` permanece simples; agrupamento centralizado
- **Pydantic em todos os modelos** — contratos explícitos, erros aparecem cedo
- **Parsers específicos antes do genérico** — alta precisão nos formatos conhecidos

---

## Como Adicionar um Novo Exporter (Firebird)

1. Rodar `python tools/explore_firebird.py --database empresa_COPIA.fdb > schema_report.txt` (nunca em produção)
2. Identificar tabelas de pedido, itens, clientes e produtos no report
3. Preencher queries em `app/erp/queries.py` (CHECK_ORDER_EXISTS, INSERT_ORDER_HEADER, etc.)
4. Implementar `app/erp/mapper.py` com os nomes reais de colunas
5. Implementar `_insert_order()` em `app/exporters/firebird_exporter.py`
6. Testar com `EXPORT_MODE=both` em arquivo real

---

## Roadmap

- **v2:** `consolidators/` — merge de múltiplos pedidos do mesmo fornecedor
- **v3 (em andamento):** importação direta no ERP Firebird — `app/erp/` + `tools/explore_firebird.py`
- **v4:** dashboard de auditoria, alertas
- **v5:** autenticação (OAuth Google), multi-tenant

---

## Protocolo de execução com Claude Code

**Antes de qualquer task, leia `docs/ai/00-index.md` PRIMEIRO.** Ele aponta exatamente quais arquivos carregar para o domínio da task. Não carregue o projeto inteiro.

### Regras de contexto mínimo

1. **Identifique o domínio** da task pelo `00-index.md` (parsers / erp / web / persistence / llm / exporters / pipeline).
2. **Carregue apenas:** o módulo do domínio + helpers compartilhados (`base_parser.py`, `models/`, `utils/logger.py` quando relevantes) + o arquivo de teste correspondente.
3. **Não leia parsers irmãos** quando a task é em um único parser. Não leia `app/web/` em task de `app/erp/`. E vice-versa.
4. Se a task cruzar domínios, carregue cada `docs/ai/modules/<dominio>.md` antes do código.

### Disciplina de execução

- **Diff pequeno:** uma intenção por commit/PR. Refactor não anda junto com bug fix.
- **Testes direcionados:** rodar `.venv/bin/pytest tests/<arquivo>.py -v` do módulo afetado. Suíte completa só antes do commit final.
- **Doc incremental:** se a mudança altera contrato (modelo, rota, query, helper), atualize APENAS a seção relevante de `docs/ai/modules/<dominio>.md`. Não reescreva o módulo todo.
- **Sem invenção:** se um helper já existe (`_find`, `_parse_br_number`, `BaseParser`, `OrderNormalizer`), reuse. Não duplique lógica.

### Auto-protocolo (passo a passo de cada task)

1. Ler `docs/ai/00-index.md` → identificar domínio.
2. Ler `docs/ai/modules/<dominio>.md` → identificar arquivos críticos e testes.
3. Ler somente os arquivos críticos.
4. Implementar (diff pequeno).
5. Rodar teste direcionado do módulo. Se passar, rodar suíte completa.
6. Atualizar a seção afetada do `docs/ai/modules/<dominio>.md` se contrato mudou.
7. Resumir mudança em 1–2 frases.

### Templates de task

Use os templates em `docs/ai/templates/` ao iniciar:
- Bug → `templates/bug.md`
- Feature → `templates/feature.md`
- Refactor → `templates/refactor.md`
- Investigação → `templates/investigation.md`
