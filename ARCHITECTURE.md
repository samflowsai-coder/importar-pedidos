# ARCHITECTURE — Automação de Pedidos (PDF + XLS → ERP)

## Stack

| Ferramenta | Por quê |
|---|---|
| **Python 3.12** | Match statement nativo, type hints modernos, melhor performance |
| **pydantic v2** | Validação robusta dos modelos de dados, serialização eficiente |
| **pdfplumber** | Melhor extração de texto e tabelas de PDF (vs pypdf2 ou camelot) |
| **openpyxl** | Leitura de .xlsx e geração do XLS de saída para ERP |
| **xlrd** | Suporte a .xls legado (arquivos antigos sem suporte no openpyxl) |
| **anthropic SDK** | LLM como fallback inteligente quando parser genérico falha |
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
        GenericParser             ← regex + heurística de tabela
                │
          (falhou?)
                │
                ▼
        LLMFallbackParser         ← claude-haiku, structured output
                │
                ▼
        OrderNormalizer           ← datas, uppercase, title case
                │
                ▼
        OrderValidator            ← obrigatórios, quantidades > 0
                │
                ▼
        ERPExporter               ← output/pedidos_erp.xlsx
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
# editar .env com ANTHROPIC_API_KEY

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
├── exporters/      ← gera XLS final para ERP
├── models/         ← Order, OrderHeader, OrderItem, ERPRow
├── llm/            ← integração Anthropic para fallback
├── utils/          ← logger compartilhado
└── pipeline.py     ← orquestrador de um arquivo
```

## Custo estimado

| Operação | Custo |
|---|---|
| Parser genérico (regras) | $0 |
| LLM fallback (Haiku) | ~$0.001/arquivo |
| Execução local | $0 |

LLM só é chamado quando o parser falha. Em produção, estima-se uso do LLM em <20% dos arquivos.

## Segurança

- `.env` no `.gitignore` — API key nunca commitada
- Docker: multi-stage build, non-root user (`app`)
- `.dockerignore` garante que `.env`, `.git` e arquivos de dev não entram na imagem
- Sem secrets hardcoded em nenhum arquivo
