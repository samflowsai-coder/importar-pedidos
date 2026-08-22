# Portal de Pedidos

Porta de entrada de pedidos de varejistas para um fornecedor de calçados. Recebe
pedidos de compra em **PDF / XLS / XLSX**, parseia, apresenta um preview para
validação humana e importa no ERP **Fire Sistemas** (Firebird) — ou gera `.xlsx`
prontos para importação, conforme o modo de exportação.

Opera **várias empresas em paralelo** (multi-ambiente), com autenticação, worker
de background e integrações com o Gestor de Produção e o FlowPCP.

---

## Setup local

```bash
git clone <repo-url> importar-pedidos
cd importar-pedidos

python -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows

pip install -e ".[dev]"

cp .env.example .env             # edite: OPENROUTER_API_KEY é o mínimo
python ui.py                     # → http://127.0.0.1:3636
```

Primeiro acesso: o portal pede o bootstrap do usuário admin. Para criar ou
resetar usuário pela linha de comando:

```bash
.venv/bin/python tools/create_user.py voce@exemplo.com --role admin
.venv/bin/python tools/create_user.py voce@exemplo.com --reset
```

### Via Docker

```bash
cp .env.example .env
docker compose up --build
```

### Instalação em servidor (cliente)

Windows, com serviço e auto-start: [`INSTALACAO-SERVIDOR.md`](INSTALACAO-SERVIDOR.md).

---

## Uso

**Web** (principal) — upload → preview → commit, em `http://127.0.0.1:3636`.

**CLI em lote:**

```bash
cp meus_pedidos/*.pdf input/
python main.py                   # resultado em output/
```

**Worker** (outbox, polls, retenção, backup):

```bash
python -m app.worker
```

---

## Formatos suportados

Dez parsers determinísticos, tentados em cascata; o LLM só entra se todos falharem.

| Fornecedor | Tipo | Particularidade |
|---|---|---|
| Riachuelo — Mercado Eletrônico | PDF | Split por CNPJ de loja |
| Riachuelo — Pedido Compras Revenda | PDF | Blocos PREPACK |
| SBF / Centauro | PDF | EAN na tabela "Dados Variante"; CNPJ de **faturamento**, não de cobrança |
| Beira Rio | PDF | Ranges de tamanho (33/38 + 39/44) |
| Kolosh / Dakota Nordeste | PDF | Números em formato americano (`500.000` = 500 un.) |
| Sam's Club | PDF | Dois layouts: consolidado e GRADE (Cross Docking, qty em embalagens) |
| Kallan | XLSX | Colunas por código de loja |
| Authentic Feet / Magic Feet / Pulmão (Grupo Afeet) | XLSX | Loja única; quantidade em `TOTAL KITS` |
| NBA / desmembramento | XLSX | Um arquivo de saída por loja |
| Genérico | PDF/XLS | Regex + heurística de tabela |
| _(fallback)_ | qualquer | LLM via OpenRouter, só se a cascata inteira falhar |

---

## Testes

```bash
.venv/bin/pytest tests/ -v
```

**877 testes** em 84 arquivos. Samples reais em `samples/`.

---

## Documentação

| Onde | O quê |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Contrato de execução: protocolo, stack, mapa do repo, env vars |
| [`docs/ai/00-index.md`](docs/ai/00-index.md) | Roteador: task → domínio → arquivos → testes |
| [`docs/ai/modules/`](docs/ai/modules/) | Um doc por domínio (parsers, erp, web, worker, …) |
| [`docs/BACKLOG.md`](docs/BACKLOG.md) | Escopo aberto |
| [`docs/superpowers/`](docs/superpowers/) | Specs e planos de implementação (histórico) |
| [`docs/history/`](docs/history/) | PRD e ARCHITECTURE da v1 — congelados, não são fonte de verdade |

Para adicionar um parser novo, siga o passo a passo em
[`docs/ai/modules/parsers.md`](docs/ai/modules/parsers.md).

---

## Variáveis de ambiente

Lista completa e comentada em [`CLAUDE.md`](CLAUDE.md#variáveis-de-ambiente). O mínimo:

| Variável | Obrigatório | Descrição |
|---|---|---|
| `OPENROUTER_API_KEY` | Sim, para o fallback LLM | Chave OpenRouter |
| `PORTAL_PORT` | Não | Porta do web (padrão `3636`) |
| `PORTAL_HOST` | Não | `127.0.0.1` (padrão) ou `0.0.0.0` para a rede local |
| `EXPORT_MODE` | Não | `xlsx` (padrão), `db` ou `both` |
| `APP_DATA_DIR` | Não | Onde ficam os SQLite (padrão `data/`) |

---

## Health check

```
GET /health   → {"status": "ok", "service": "importar-pedidos"}
GET /metrics  → métricas Prometheus
```
