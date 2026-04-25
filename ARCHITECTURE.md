# ARCHITECTURE — Automação de Pedidos (PDF + XLS → ERP)

## Stack

| Ferramenta | Por quê |
|---|---|
| **Python 3.11+** | `requires-python = ">=3.11"` no `pyproject.toml`. Match statement, `X \| Y` union syntax, type hints modernos. |
| **pydantic v2** | Validação robusta dos modelos de dados, serialização eficiente |
| **pdfplumber** | Melhor extração de texto e tabelas de PDF (vs pypdf2 ou camelot) |
| **openpyxl** | Leitura de .xlsx e geração do XLS de saída para ERP |
| **xlrd** | Suporte a .xls legado (arquivos antigos sem suporte no openpyxl) |
| **FastAPI + uvicorn** | Interface web (preview → commit) em `app/web/server.py` |
| **firebird-driver** | Conexão direta com o ERP Fire Sistemas (Firebird embedded ou TCP) |
| **openai SDK + OpenRouter** | LLM fallback via OpenRouter (default: Gemini Flash 1.5; configurável por `OPENROUTER_MODEL`). Anthropic SDK direto foi descontinuado. |
| **loguru** | Logging estruturado com rotação automática, zero config |
| **python-dotenv** | Gestão de secrets via .env sem riscos de commit acidental |
| **ruff** | Linting + formatação em um único tool, 10–100x mais rápido que flake8 |
| **pytest** | Framework de testes padrão da indústria |

## Fluxo do Pipeline

```
input/*.pdf|.xls|.xlsx
        │
        ▼
  FileLoader          ← carrega bytes, detecta extensão
        │
        ▼
  FormatClassifier    ← classifica: PDF / XLS / XLSX / UNKNOWN
        │
        ├──PDF──► PDFExtractor    (pdfplumber: texto + tabelas)
        │
        └──XLS──► XLSExtractor   (openpyxl/xlrd: linhas)
                │
                ▼
        Cascata de 8 parsers      ← Mercado Eletrônico, Pedido Compras Revenda,
                │                    SBF/Centauro, Beira Rio, Kolosh, Sam's Club,
                │                    Kallan XLS, Desmembramento XLS
                ▼
        GenericParser             ← regex + heurística de tabela
                │
          (falhou?)
                │
                ▼
        LLMFallbackParser         ← OpenRouter (Gemini Flash 1.5 default), structured output
                │
                ▼
        OrderNormalizer           ← datas, uppercase, title case
                │
                ▼
        OrderValidator            ← obrigatórios, quantidades > 0
                │
                ▼
        ERPExporter               ← output/*.xlsx (split por loja) e/ou Firebird
```

## Decisões Arquiteturais

**LLM como fallback, não como padrão:** O parser genérico é determinístico, grátis e rápido.
O LLM entra só quando o parser falha, reduzindo custo a centavos por arquivo problemático.

**Pipeline stateless:** Cada arquivo é processado independentemente. Facilita paralelismo futuro.

**Pydantic para todos os modelos:** Garante contratos de dados explícitos entre módulos.
Erros de dados aparecem cedo, não no export final.

## Como rodar

```bash
# 1. Instalar dependências
pip install -e ".[dev]"

# 2. Configurar ambiente
cp .env.example .env
# editar .env com OPENROUTER_API_KEY (e opcionais FB_DATABASE etc.)

# 3. Colocar arquivos em input/
cp meus_pedidos/*.pdf input/

# 4. Rodar
python main.py

# Output: output/pedidos_erp.xlsx
```

## Via Docker

```bash
docker compose up
```

## Estrutura de Pastas

```
app/
├── ingestion/      ← carregamento de arquivos do disco
├── classifiers/    ← identifica o formato do arquivo
├── extractors/     ← extrai texto/tabelas (PDF ou XLS)
├── parsers/        ← transforma extraído em Order (regras → LLM)
├── normalizers/    ← padroniza campos (datas, case, etc)
├── validators/     ← valida campos obrigatórios e valores
├── consolidators/  ← (Fase 2) merge de múltiplos pedidos
├── exporters/      ← gera XLS final + importa no Firebird
├── erp/            ← Firebird/Fire Sistemas: connection, queries, mapper
├── persistence/    ← log SQLite local (histórico de execuções)
├── web/            ← FastAPI server + preview cache + static
├── models/         ← Order, OrderHeader, OrderItem, ERPRow
├── llm/            ← integração OpenRouter (OpenAI SDK) para fallback
├── utils/          ← logger compartilhado
└── pipeline.py     ← orquestrador de um arquivo
```

## Custo estimado

| Operação | Custo |
|---|---|
| Parsers determinísticos (8 específicos + genérico) | $0 |
| LLM fallback (OpenRouter / Gemini Flash 1.5) | ~$0.0005/arquivo |
| Execução local | $0 |

LLM só é chamado quando o parser falha. Em produção, estima-se uso do LLM em <20% dos arquivos.

## Segurança

- `.env` no `.gitignore` — API key nunca commitada
- Docker: multi-stage build, non-root user (`app`)
- `.dockerignore` garante que `.env`, `.git` e arquivos de dev não entram na imagem
- Sem secrets hardcoded em nenhum arquivo
