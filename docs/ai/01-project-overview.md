# 01 — Visão Geral do Projeto

**Portal de Pedidos** é a porta de entrada de pedidos de varejistas (Riachuelo, Centauro, Sam's Club, NBA/Kallan, Kolosh, Beira Rio, Mercado Eletrônico, etc.) para um fornecedor de calçados. Recebe PDF / XLS / XLSX, parseia, deixa o operador validar via preview web e importa direto no ERP **Fire Sistemas** (Firebird).

## Pontos de entrada
- **CLI** (`main.py`): processa lote em `input/` → `output/*.xlsx`.
- **Web** (`ui.py` → `app/web/server.py`): upload → preview em `/api/process` → commit (download xlsx ou import Firebird).

## Pipeline (alto nível)
```
arquivo → FileLoader → FormatClassifier → Extractor (PDF|XLS)
       → cascata de 8 parsers específicos → GenericParser → LLM fallback
       → OrderNormalizer → OrderValidator → ERPExporter (xlsx | firebird | ambos)
```

## Decisões inegociáveis (não mudar sem razão explícita)
- LLM é fallback, não default. ~80% dos arquivos passam só por parsers determinísticos.
- Pipeline stateless (cada arquivo independente).
- Split por loja acontece no exportador, não no parser.
- Pydantic em todos os modelos.
- Parsers específicos antes do genérico na cascata.

## Onde está o quê
Use `00-index.md` para mapear tarefa → módulo. Esta página existe só para orientar quem nunca viu o projeto.
