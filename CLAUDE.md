# CLAUDE.md — Importar Pedidos

## O que é este projeto

Pipeline de automação que converte pedidos de compra (PDF + XLS/XLSX) recebidos de varejistas em arquivos `.xlsx` prontos para importação no ERP de um fornecedor de calçados.

Dois pontos de entrada: CLI (`main.py`) para lote e interface web (`ui.py`) para operação manual via drag-drop.

---

## Stack

- **Python 3.9** — `requires-python = ">=3.9"`. Usar `Optional[X]` e `Union[X, Y]`, **nunca** `X | Y` nem `match`. O venv local é 3.9.6.
- **pydantic v2** — modelos de dados em `app/models/`
- **pdfplumber** — extração de texto e tabelas de PDF
- **openpyxl / xlrd** — leitura XLSX/XLS legado e geração do output
- **FastAPI + uvicorn** — interface web em `app/web/server.py`
- **openai SDK (OpenRouter)** — LLM fallback via OpenRouter (default: Gemini Flash 1.5); configurável por `OPENROUTER_MODEL`
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

## Roadmap

- **v2:** `consolidators/` — merge de múltiplos pedidos do mesmo fornecedor
- **v3:** importação direta no ERP via API
- **v4:** dashboard de auditoria, alertas
- **v5:** autenticação (OAuth Google), multi-tenant
