# Estratégia de testes

## Princípio
Rode o **menor conjunto de testes que prova a mudança**. Suíte completa é gate de commit,
não de iteração.

Hoje: **877 testes em 84 arquivos**, todos verdes.

## Mapa módulo → suíte
O mapa completo e com o comando pronto está em [`00-index.md`](00-index.md), seção
"domínio → testes". Não duplicar aqui: um único mapa, um único lugar para envelhecer.

Regra para achar a suíte de um módulo sem consultar o mapa:
`tests/test_<assunto>*.py`, e o `modules/<domínio>.md` sempre lista a sua na seção
**Testes**.

## O que NÃO tem cobertura automatizada
- **Nada toca Firebird de verdade.** Todo teste de `erp/` e do `firebird_exporter` roda
  contra fake/mock. Mudança em SQL, mapper ou charset exige validação manual com `.fdb`
  de **cópia** (nunca produção) e sample real, com `EXPORT_MODE=both`.
- **Nenhum teste chama o LLM de verdade** — `test_smoke_llm_fallback.py` valida o
  contrato, não a resposta do modelo.
- **`extractors/`, `validators/`, `classifiers/`, `ingestion/` não têm suíte própria.**
  São exercitados indiretamente por `test_new_parsers.py` e `test_smoke_pipeline.py`.
- **Integrações externas (Gestor, FlowPCP) rodam contra stub HTTP**, nunca contra o
  ambiente real.

## Quando rodar a suíte completa
Antes de qualquer commit, depois de refactor, e antes de abrir PR.

## Comandos
```bash
.venv/bin/pytest tests/ -v              # suíte completa
.venv/bin/pytest tests/<arquivo>.py -v  # durante a iteração
python -m pytest tests/ -v              # fallback sem o venv ativado
```

## Cobertura
Não há gate de cobertura. A regra que vale: **cada parser tem ao menos um teste com
sample real em `samples/`**, e cada bug de produção corrigido entra com o teste que o
reproduz.
