# Estratégia de documentação incremental

## Princípio
**Atualizar é mais barato que reescrever.** Toque a menor seção possível.

## Quando atualizar (e o quê)
| Mudança | Atualize |
|---|---|
| Novo parser | `modules/parsers.md` (lista) + `00-index.md` se mapeamento mudou |
| Novo helper compartilhado | `00-index.md` (seção Helpers) + `modules/<onde-mora>.md` |
| Mudança em modelo Pydantic | `modules/models.md` + cada módulo consumidor |
| Nova rota | `modules/web.md` (seção Rotas) |
| Nova envvar | `CLAUDE.md` (seção env) + `.env.example` |
| Mudança de provider LLM | `modules/llm.md` |
| Decisão arquitetural | `01-project-overview.md` (Decisões inegociáveis) |

## O que NÃO documentar
- Detalhes que o código já expressa (assinaturas, types, etc.).
- Fluxo de tarefa específica (vai pro PR description, não pro repo).
- Roadmap detalhado (mantém só macro em `01-project-overview.md`).

## Anti-padrão
- Reescrever `modules/<x>.md` inteiro por uma mudança pontual.
- Criar arquivo novo em `docs/ai/` sem justificar no `00-index.md`.
- Duplicar conteúdo entre `CLAUDE.md` e `docs/ai/`. **Regra:** `CLAUDE.md` = protocolo + visão executiva; `docs/ai/` = detalhe operacional.
