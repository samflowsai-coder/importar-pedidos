# Módulo: llm (fallback)

## Responsabilidade
Último recurso quando todos os parsers determinísticos falham. Custo só nessa rota.

## Arquivos críticos
- `app/llm/fallback_parser.py` — `LLMFallbackParser`. Lazy-instancia
  `OpenRouterClient` e expõe `parse(extracted, source_file) -> Order | None`.
- `app/llm/openrouter_client.py` — `OpenRouterClient` em cima de
  `app.http.OutboundClient`. Usa `llm_call_policy` (1 retry, só
  502/503/504 + erros de conexão; nunca 4xx). Lança `LLMUnavailableError`.

## Provider
**OpenRouter** via httpx direto (endpoint `POST /chat/completions`,
compatível com a wire format do OpenAI). Default: Gemini Flash 1.5.
Configurável via `OPENROUTER_MODEL`.

> Migração 2026-04: removido `openai` SDK. Cliente `httpx`-only com
> retry/timeout/trace_id observáveis. Ver `modules/http.md`.

## Variáveis de ambiente
```
OPENROUTER_API_KEY=sk-or-...                    # obrigatório
OPENROUTER_MODEL=google/gemini-flash-1.5        # opcional
```

## Testes
- `tests/test_smoke_llm_fallback.py` — 8 testes mockando
  `OpenRouterClient.chat_completion`. Cobre: texto vazio, parsing JSON
  com fences markdown, campos desconhecidos do modelo (forward-compat),
  exceção do provider, JSON malformado, override de modelo via env.
- `tests/test_outbound_client.py` — testes do cliente HTTP, incluem
  trace_id propagation, retry em 5xx, recusa de retry em 4xx,
  comportamento da `llm_call_policy`.
- `.venv/bin/pytest tests/test_smoke_llm_fallback.py tests/test_outbound_client.py -v`

## Armadilhas
- Não chamar LLM em loop. É última saída por arquivo.
- Estrutura de saída do LLM passa pelo mesmo `OrderValidator` — não pular.
- `LLMFallbackParser.parse` **nunca lança** — retorna `None` em qualquer
  falha. O pipeline trata `None` como "formato não reconhecido".
- `llm_call_policy` é deliberadamente conservadora: 1 retry só em 502/503/504.
  Não aumentar — modelo pode ter cobrado a request, retry agressivo
  duplica custo sem ganho.
