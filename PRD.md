# PRD — Importar Pedidos
**Versão:** 1.0  
**Data:** 2026-04-09  
**Autor:** SamFlowsAI  
**Status:** Produção (v1)

---

## 1. Visão do Produto

### Problema

Fornecedores de calçados recebem pedidos de compra em formatos completamente distintos: PDFs de portais de grandes varejistas (Centauro, Riachuelo, Sam's Club), PDFs de representantes regionais (Kolosh/Dakota, Beira Rio), e planilhas XLS/XLSX de redes com desmembramento por loja (Magic Feet, Authentic Feet, NBA).

Cada formato tem sua estrutura, numeração, idioma de campos e particularidades (numbers americanos vs. brasileiros, PREPACK, EAN inline, colunas ocultas de loja). Processar isso manualmente é lento, error-prone e escalalmente inviável.

### Solução

Pipeline determinístico de importação de pedidos: recebe qualquer arquivo PDF ou XLS/XLSX, identifica o formato automaticamente, extrai todos os campos do pedido, normaliza os dados e exporta arquivos `.xlsx` prontos para importação no ERP — um arquivo por pedido, ou um arquivo por loja quando o pedido é desmembrado por localidade.

### Proposta de Valor

- **Velocidade:** Processamento de um pedido em < 3 segundos (sem LLM)
- **Precisão:** 8 parsers específicos por fornecedor garantem extração sem ambiguidade
- **Custo:** LLM (Claude Haiku) só acionado como fallback (~20% dos casos, ~$0.001/arquivo)
- **Rastreabilidade:** Logs com rotação, nomes de arquivo com CNPJ + número de pedido
- **Operabilidade:** Interface web para drag-drop de arquivos sem precisar de terminal

---

## 2. Usuários

| Perfil | Descrição | Uso Primário |
|--------|-----------|--------------|
| **Analista de PCP** | Opera a importação diariamente | Upload via UI, download dos `.xlsx` gerados |
| **TI / Desenvolvedor** | Mantém e estende o sistema | CLI (`main.py`), testes, adição de novos parsers |
| **Gestor Comercial** | Valida pedidos importados | Verifica arquivos exportados no ERP |

---

## 3. Escopo (v1)

### Dentro do Escopo

- Parsing de 8 formatos de fornecedores + fallback genérico + fallback LLM
- Exportação de `.xlsx` com schema ERP padronizado
- Desmembramento por loja (quando aplicável): um arquivo por CNPJ ou por nome de loja
- Interface web para operação sem terminal
- CLI para processamento em lote

### Fora do Escopo (v1)

- Importação direta no ERP via API
- OCR de imagens dentro de PDFs
- Validação de regras de negócio (ex: preço mínimo, prazo viável)
- Notificações / alertas por email ou Slack
- Autenticação de usuários na interface web
- Histórico / auditoria de pedidos importados
- Consolidação de múltiplos pedidos do mesmo fornecedor (diretório `consolidators/` reservado para v2)

---

## 4. Formatos Suportados

### 4.1 PDFs

| Fornecedor | Signature de Detecção | Particularidades |
|------------|----------------------|-----------------|
| **SBF / Centauro** | `GrupoSaf@centauro.com.br` | EAN na tabela "Dados Variante"; lookup por prefixo de código |
| **Riachuelo — Mercado Eletrônico** | `Mercado Eletrônico` | Multi-localidade; cada item pode ter CNPJ de loja distinto |
| **Riachuelo — Pedido Compras Revenda** | `PEDIDO DE COMPRAS REVENDA` | Blocos PREPACK; data de entrega calculada por semana |
| **Beira Rio** | `BEIRA RIO` (case-insensitive) | Dois ranges de tamanho (33/38 + 39/44); variantes de cor |
| **Kolosh / Dakota Nordeste** | `DAKOTA NORDESTE` | Numbers no formato americano (500.000 = 500 unid.) |
| **Sam's Club / Walmart** | `00.063.960` + `Itens do Pedido` | EAN como código de produto; qty = embalagem × pedida |

### 4.2 XLS / XLSX

| Fornecedor | Signature de Detecção | Particularidades |
|------------|----------------------|-----------------|
| **Kallan** | `KALLAN` no texto | Código de loja no header (K01, K02…); colunas dinâmicas |
| **Desmembramento** (Magic Feet, Authentic Feet, NBA) | `DESMEMBRAMENTO`, `NBA`, `ADULTO`, `INFANTIL`, `SHOPPING CENTER` | Uma linha de produto → N linhas de output (uma por loja com qty > 0); CNPJs detectados na linha acima do header |

### 4.3 Fallback

| Tipo | Condição | Comportamento |
|------|----------|--------------|
| **Parser Genérico** | Nenhum parser específico reconhece o arquivo | Extração heurística por regex de tabelas e quantidades |
| **LLM (Claude Haiku)** | Parser genérico retorna None | Envia até 4.000 chars ao Claude Haiku; aguarda JSON estruturado |

---

## 5. Modelos de Dados

### OrderHeader
```
order_number   str | None    Número do pedido (normalizado: uppercase)
issue_date     str | None    Data de emissão (formato: DD/MM/YYYY)
customer_name  str | None    Razão social do cliente (normalizado: Title Case)
customer_cnpj  str | None    CNPJ do cliente (mantém formatação original)
```

### OrderItem
```
description    str | None    Descrição do produto (stripped)
product_code   str | None    Código interno do produto
ean            str | None    EAN-13 (13 dígitos)
quantity       float | None  Quantidade pedida
unit_price     float | None  Preço unitário
total_price    float | None  Valor total do item
obs            str | None    Observações (ex: range de tamanho "33/38")
delivery_date  str | None    Data de entrega do item (DD/MM/YYYY)
delivery_cnpj  str | None    CNPJ do local de entrega (quando diferente do cliente)
delivery_name  str | None    Nome do local de entrega (ex: nome da loja)
```

### ERPRow — Schema de Exportação
```
PEDIDO             Número do pedido
NOME_CLIENTE       Razão social (ou nome da loja p/ desmembramento sem CNPJ)
CNPJ_CLIENTE       CNPJ do cliente (ou da loja, no caso Riachuelo)
CODIGO_PRODUTO     Código do produto
EAN                EAN-13
DESCRICAO          Descrição do produto
QUANTIDADE         Quantidade
PRECO_UNITARIO     Preço unitário
VALOR_TOTAL        Valor total
OBS                Observações
DATA_ENTREGA       Data de entrega
CNPJ_LOCAL_ENTREGA CNPJ do local de entrega (quando diferente do cliente)
```

---

## 6. Pipeline de Processamento

```
Arquivo (PDF/XLS/XLSX)
        │
        ▼
  FormatClassifier
  (detecta extensão)
        │
   ┌────┴────┐
   PDF      XLS/XLSX
   │            │
PDFExtractor  XLSExtractor
(pdfplumber)  (openpyxl/xlrd)
   │            │
   └────┬────┘
        │  extracted = {text, tables, rows}
        ▼
  Parser Chain (cascade — para no primeiro match)
  1. MercadoEletronicoParser
  2. PedidoComprasRevendaParser
  3. SbfCentauroParser
  4. BeiranRioParser
  5. KoloshParser
  6. SamsClubParser
  7. KallanXlsParser
  8. DesmembramentoXlsParser
  9. GenericParser
        │
        │  (se retornar None)
        ▼
  LLMFallbackParser (Claude Haiku)
        │
        ▼
  OrderNormalizer
  (uppercase, datas, title case)
        │
        ▼
  OrderValidator
  (warnings se campos críticos ausentes)
        │
        ▼
  ERPExporter
  (agrupa por delivery CNPJ ou nome de loja)
        │
        ▼
  output/*.xlsx
```

---

## 7. Lógica de Desmembramento

O exportador agrupa itens por chave de entrega e gera um arquivo `.xlsx` por grupo.

**Regras de chave:**

| Condição | Chave usada | Resultado |
|----------|------------|-----------|
| `delivery_cnpj` diferente do CNPJ do cliente | CNPJ da loja | Split por CNPJ (ex: Riachuelo) |
| `delivery_cnpj` igual ao do cliente ou vazio + `delivery_name` preenchido | Nome da loja | Split por nome (ex: NBA) |
| Sem `delivery_cnpj` e sem `delivery_name` | `""` (default) | Arquivo único |

**Nomenclatura dos arquivos gerados:**
```
{NOME_CLIENTE}_{CNPJ}_{PEDIDO}_{SUFIXO}.xlsx

Exemplos:
  Sbf_Comercio_De_Produtos_Espor_06347409000165_Pedido_29852487.xlsx
  Lojas_Riachuelo_Sa_33200056034396_Pedido_6702604131_1.xlsx
  SEM_CLIENTE_Pedido_NBA_DEZEMBRO_24_Gramado.xlsx
```

---

## 8. Interface Web

**Acesso:** `http://localhost:8000`  
**Launcher:** `python ui.py` → `uvicorn app.web.server:app --reload`

### Rotas da API

| Método | Rota | Descrição | Segurança |
|--------|------|-----------|-----------|
| `GET` | `/` | Serve `index.html` | — |
| `GET` | `/api/config` | Retorna diretório de saída padrão | — |
| `POST` | `/api/process` | Upload + processamento de arquivos | Whitelist de extensões, limite 50MB |
| `GET` | `/api/download?path=` | Download do `.xlsx` gerado | Apenas `.xlsx`, path must exist |
| `GET` | `/api/fs?path=` | Navegação de diretórios | Apenas diretórios listados; fallback para home se inválido |

### Medidas de Segurança

- **Path traversal:** `/api/download` rejeita qualquer extensão diferente de `.xlsx` (HTTP 403)
- **Upload injection:** Extensões validadas server-side (`.pdf`, `.xls`, `.xlsx` apenas)
- **Tamanho:** Limite de 50MB por arquivo
- **Directory browser:** Lista apenas diretórios, sem acesso a arquivos; paths inválidos caminham até o diretório home
- **onclick injection:** `escPath()` no frontend sanitiza paths antes de injetar em atributos HTML

### Fluxo do Usuário

1. Arrastar PDFs/XLS para a zona de upload (ou clicar para selecionar)
2. Definir pasta de saída (digitar ou navegar pelo modal)
3. Clicar em **Processar**
4. Ver resultado: cards com arquivos gerados + links de download

---

## 9. Infraestrutura

### Dependências de Produção

| Pacote | Versão | Uso |
|--------|--------|-----|
| `pydantic` | ≥2.0 | Modelos de dados e validação |
| `pdfplumber` | ≥0.11 | Extração de texto e tabelas de PDF |
| `openpyxl` | ≥3.1 | Leitura/escrita XLSX |
| `xlrd` | ≥2.0 | Leitura de XLS legado |
| `fastapi` | — | API web |
| `uvicorn` | — | Servidor ASGI |
| `anthropic` | ≥0.40 | Claude Haiku (fallback LLM) |
| `python-dotenv` | ≥1.0 | Variáveis de ambiente |
| `loguru` | ≥0.7 | Logging estruturado com rotação |

### Variáveis de Ambiente

```env
ANTHROPIC_API_KEY=sk-ant-...   # Obrigatório para fallback LLM
INPUT_DIR=input/               # Diretório de entrada (CLI)
OUTPUT_DIR=output/             # Diretório de saída (CLI)
```

### Docker

```bash
docker compose up              # Sobe o container
docker compose up --build      # Rebuild após mudanças de código
```

- Multi-stage build (builder + runtime)
- Roda como usuário não-root (`app:app`)
- Volumes: `input/` (read-only), `output/`, `logs/`
- **Sem modo debug/reload em produção**

---

## 10. Testes

**Suite completa:** `48 testes`, todos passando

```bash
.venv/bin/pytest tests/ -v
```

### Cobertura por Módulo

| Arquivo | Testes | O que cobre |
|---------|--------|-------------|
| `test_web_server.py` | 8 | Smoke, config, segurança (download/upload), file browser, E2E com samples |
| `test_normalizer.py` | 4 | Uppercase, normalização de datas, ano 2-dígitos, title case |
| `test_generic_parser.py` | 3 | Parsing via tabela, retorno None sem itens, campos de header |
| `test_new_parsers.py` | 33 | Centauro EAN, Studio Z EAN, Beira Rio (14 itens, variantes de tamanho e cor), Kolosh (15 itens, preços, data), Sam's Club (18 itens, EAN, data), Kallan (9 itens), Magic Feet (9 arquivos), Authentic Feet, NBA (21 arquivos, nomes de loja) |

### Samples (arquivos de teste)

Localizados em `samples/`:
- `PEDIDO CENTAURO.pdf`
- `PEDIDO STUDIO Z.pdf`
- `PEDIDO BEIRA RIO.pdf`
- `PEDIDO KOLOSH.pdf`
- `PEDIDO SAMS CLUB.pdf`
- `PEDIDO KALLAN K01.xlsx`
- `Desmembramento Magic Feet.xlsx`
- `Desmembramento Authentic feet (1).xlsx`
- `PEDIDO NBA 3.xlsx`
- `RIACHUELO - PEDIDO.pdf`
- PDFs de mercado eletrônico e revenda da Riachuelo

---

## 11. Logging

```
logs/pipeline.log    DEBUG, rotação 10MB, retenção 30 dias
stderr               INFO, colorido, formato HH:mm:ss | LEVEL | message
```

**Pontos de log:**
- Arquivo carregado + parser selecionado
- Ativação do fallback LLM
- Warnings de validação (campos ausentes)
- Exportação: nome do arquivo + contagem de linhas

---

## 12. Estrutura do Repositório

```
importar pedidos/
├── app/
│   ├── classifiers/         # Detecção de formato (PDF/XLS)
│   ├── extractors/          # pdfplumber + openpyxl/xlrd
│   ├── ingestion/           # FileLoader (disk → LoadedFile)
│   ├── parsers/             # 8 parsers específicos + genérico
│   │   ├── base_parser.py   # BaseParser com helpers (_find, _parse_br_number)
│   │   ├── beira_rio_parser.py
│   │   ├── desmembramento_xls_parser.py
│   │   ├── generic_parser.py
│   │   ├── kallan_xls_parser.py
│   │   ├── kolosh_parser.py
│   │   ├── mercado_eletronico_parser.py
│   │   ├── pedido_compras_revenda_parser.py
│   │   ├── sams_club_parser.py
│   │   └── sbf_centauro_parser.py
│   ├── llm/                 # Claude Haiku fallback
│   ├── normalizers/         # OrderNormalizer
│   ├── validators/          # OrderValidator
│   ├── exporters/           # ERPExporter → .xlsx
│   ├── models/              # Order, OrderHeader, OrderItem, ERPRow
│   ├── consolidators/       # (reservado — v2)
│   ├── utils/               # Logger (loguru)
│   ├── web/                 # FastAPI server + index.html
│   └── pipeline.py          # Orquestrador principal
├── tests/
├── samples/                 # Arquivos de pedido reais (test fixtures)
├── input/                   # Drop de arquivos (gitignored)
├── output/                  # Arquivos gerados (gitignored)
├── logs/                    # Logs de execução (gitignored)
├── main.py                  # Entrada CLI (batch)
├── ui.py                    # Entrada Web (uvicorn)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

---

## 13. Como Adicionar um Novo Parser

1. Criar `app/parsers/novo_fornecedor_parser.py`
2. Herdar de `BaseParser` e implementar:
   ```python
   def can_parse(self, extracted: dict) -> bool:
       return "ASSINATURA_UNICA" in extracted.get("text", "")

   def parse(self, extracted: dict) -> Optional[Order]:
       ...
   ```
3. Registrar em `app/pipeline.py` na lista `_parsers` **antes** do `GenericParser`
4. Adicionar sample file em `samples/`
5. Escrever testes em `tests/test_new_parsers.py`

**Helpers disponíveis em `BaseParser`:**
- `_find(text, pattern)` → `str | None`
- `_parse_br_number(value)` → `float | None` (formato brasileiro: 1.000,50)
- Kolosh usa `_parse_us_number` próprio (500.000 = 500)

---

## 14. Decisões de Arquitetura

| Decisão | Rationale |
|---------|-----------|
| **LLM como fallback, não padrão** | Custo zero nos 80% de casos cobertos por parsers determinísticos |
| **Pipeline stateless** | Cada arquivo é independente → paralelização futura sem refatoração |
| **Split no exportador, não no parser** | Modelo `Order` permanece simples; toda lógica de agrupamento centralizada no `ERPExporter` |
| **Pydantic para todos os modelos** | Contratos explícitos, detecção precoce de erros, serialização gratuita |
| **Parsers específicos antes do genérico** | Alta precisão nos formatos conhecidos; genérico só para novos formatos |
| **Segurança por padrão** | Path traversal, extensões, tamanho validados desde o commit inicial |
| **Desmembramento por nome quando sem CNPJ** | NBA e similares não têm CNPJ por loja; nome funciona como chave de split |

---

## 15. Roadmap (v2+)

### Fase 2 — Consolidação
- Módulo `consolidators/` para mesclar múltiplos pedidos do mesmo fornecedor em um único arquivo ERP
- Agrupamento por período (semana/mês)

### Fase 3 — Integração ERP
- Importação direta via API do ERP (sem download manual de `.xlsx`)
- Webhook para notificação pós-importação

### Fase 4 — Observabilidade e Auditoria
- Dashboard de pedidos importados (data, fornecedor, total de itens, status)
- Histórico de erros e retentativas
- Alertas por email/Slack em caso de falha

### Fase 5 — Autenticação e Multi-tenant
- Login (OAuth Google ou SSO)
- Perfis de acesso (analista vs. admin)
- Separação por empresa/CNPJ fornecedor

---

## 16. Glossário

| Termo | Definição |
|-------|-----------|
| **PREPACK** | Bloco de pedido da Riachuelo contendo múltiplos SKUs por referência |
| **Desmembramento** | Planilha que distribui um pedido entre múltiplas lojas (coluna = loja) |
| **EAN** | European Article Number (código de barras de 13 dígitos) |
| **ERP** | Enterprise Resource Planning — sistema interno de gestão do fornecedor |
| **Delivery CNPJ** | CNPJ da loja de destino quando diferente do CNPJ central do cliente |
| **LLM Fallback** | Ativação do Claude Haiku para parsear formatos não reconhecidos |
| **US Number Format** | Notação americana: ponto como separador de milhar (500.000 = quinhentos) |
| **BR Number Format** | Notação brasileira: ponto como milhar, vírgula como decimal (1.500,75) |

---

*Documento gerado automaticamente a partir da inspeção completa do codebase em 2026-04-09.*
