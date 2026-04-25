# Template — Bug Fix

## Sintoma
<o que está acontecendo, com input que reproduz>

## Esperado
<o que deveria acontecer>

## Domínio
<parsers / erp / web / persistence / llm / exporters / pipeline>

## Passo a passo (Claude segue exatamente)
1. Ler `docs/ai/00-index.md` → confirmar domínio.
2. Ler `docs/ai/modules/<dominio>.md` → identificar arquivos críticos e teste.
3. Reproduzir com sample em `samples/` (ou criar um).
4. Adicionar teste **falhando** primeiro (TDD).
5. Corrigir no menor diff possível.
6. Rodar `.venv/bin/pytest tests/<arquivo> -v`. Verde → suite completa.
7. Atualizar seção "Armadilhas" do `modules/<dominio>.md` se relevante.
