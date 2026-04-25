# Módulo: llm (fallback)

## Responsabilidade
Último recurso quando todos os parsers determinísticos falham. Custo só nessa rota.

## Arquivos críticos
- `app/llm/fallback_parser.py` — `LLMFallbackParser`.

## Provider
**OpenRouter** via OpenAI SDK (compatível). Default: Gemini Flash 1.5. Configurável via `OPENROUTER_MODEL`.
> O `ARCHITECTURE.md` antigo cita Anthropic SDK direto — está desatualizado. A verdade está em `app/llm/fallback_parser.py`.

## Variáveis de ambiente
```
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=google/gemini-flash-1.5  # opcional
ANTHROPIC_API_KEY=...                     # legado
```

## Testes
Sem testes isolados. Validar manualmente com sample que sabidamente falha nos parsers.

## Armadilhas
- Não chamar LLM em loop. É última saída por arquivo.
- Estrutura de saída do LLM passa pelo mesmo `OrderValidator` — não pular.
