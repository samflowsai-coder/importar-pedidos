# Importar Pedidos

Pipeline de automação para importação de pedidos: converte PDFs e planilhas XLS/XLSX de fornecedores em arquivos `.xlsx` prontos para importação no ERP.

## Setup (local)

```bash
# 1. Clone e entre na pasta
git clone <repo-url> importar-pedidos
cd importar-pedidos

# 2. Crie o ambiente virtual e instale dependências
python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

pip install -e ".[dev]"

# 3. Configure as variáveis de ambiente
cp .env.example .env
# Edite .env e adicione sua ANTHROPIC_API_KEY

# 4. Rode a interface web
python ui.py
# Acesse: http://localhost:8000
```

## Setup via Docker

```bash
cp .env.example .env
# Edite .env com a ANTHROPIC_API_KEY

docker compose up
```

## Uso via CLI (lote)

```bash
# Coloque os arquivos de pedido em input/
cp meus_pedidos/*.pdf input/

# Processe
python main.py

# Arquivos gerados em output/
```

## Formatos suportados

| Fornecedor | Tipo | Particularidade |
|------------|------|----------------|
| SBF / Centauro | PDF | EAN na tabela "Dados Variante" |
| Riachuelo — Mercado Eletrônico | PDF | Split por CNPJ de loja |
| Riachuelo — Pedido Compras Revenda | PDF | Blocos PREPACK |
| Beira Rio | PDF | Ranges de tamanho (33/38 + 39/44) |
| Kolosh / Dakota Nordeste | PDF | Números no formato americano |
| Sam's Club | PDF | EAN como código de produto |
| Kallan | XLSX | Colunas por código de loja |
| Magic Feet / Authentic Feet / NBA | XLSX | Desmembramento por loja |

## Testes

```bash
.venv/bin/pytest tests/ -v
```

48 testes, todos passando. Samples em `samples/`.

## Adicionar um novo parser

1. Criar `app/parsers/novo_fornecedor_parser.py` herdando de `BaseParser`
2. Implementar `can_parse()` e `parse()`
3. Registrar em `app/pipeline.py` antes do `GenericParser`
4. Adicionar sample em `samples/` e testes em `tests/test_new_parsers.py`

Detalhes em [ARCHITECTURE.md](ARCHITECTURE.md) e [PRD.md](PRD.md).

## Variáveis de Ambiente

| Variável | Obrigatório | Descrição |
|----------|-------------|-----------|
| `ANTHROPIC_API_KEY` | Sim (fallback LLM) | Chave da API Anthropic |
| `INPUT_DIR` | Não | Diretório de entrada (padrão: `input/`) |
| `OUTPUT_DIR` | Não | Diretório de saída (padrão: `output/`) |

## Health check

```
GET /health → {"status": "ok", "service": "importar-pedidos"}
```
