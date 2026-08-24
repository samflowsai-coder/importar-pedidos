# Estratégia de documentação incremental

## Princípio
**Atualizar é mais barato que reescrever.** Toque a menor seção possível.

## Um fato, um lugar

| Camada | Contém | Nunca contém |
|---|---|---|
| `README.md` | porta de entrada humana: o que é, setup, formatos, links | detalhe de implementação |
| `CLAUDE.md` | contrato de execução: protocolo, stack, mapa do repo, env vars | receita de módulo, lista de rotas, campo de modelo |
| `docs/ai/00-index.md` | roteamento task → domínio → arquivos → testes | o conteúdo do domínio |
| `docs/ai/modules/<x>.md` | **a verdade** do domínio: contratos, armadilhas, testes | visão de produto |
| `docs/BACKLOG.md` | só o que está **aberto** | histórico de item fechado (isso é git) |
| `CHANGELOG.md` | o que muda **para quem opera**, por versão | detalhe técnico, nome de arquivo, número de PR |
| `docs/history/`, `docs/superpowers/` | registro **congelado** | qualquer coisa citada como estado atual |

Se um fato aparece em dois lugares, o de baixo na tabela ganha e o de cima vira link.

## Quando atualizar (e o quê)
| Mudança | Atualize |
|---|---|
| Novo parser | `modules/parsers.md` (lista) + `modules/pipeline.md` (cascata) + `README.md` (tabela de formatos) |
| Novo helper compartilhado | `00-index.md` (Helpers) + `modules/<onde-mora>.md` |
| Mudança em modelo pydantic | `modules/models.md` + cada módulo consumidor |
| Nova rota | `modules/web.md` (Rotas) — e o module doc do assunto, se tiver um |
| Novo módulo em `app/` | `modules/<novo>.md` + linha no `00-index.md` + linha no mapa do `CLAUDE.md` |
| Nova envvar | `CLAUDE.md` (env) + `.env.example` |
| Mudança de provider LLM | `modules/llm.md` |
| Decisão arquitetural | `01-project-overview.md` (Decisões inegociáveis) |
| Bug conhecido que não vai ser corrigido agora | `docs/BACKLOG.md` — com arquivo e linha |
| Item do backlog resolvido | **remova** de `docs/BACKLOG.md` |
| Mudança que a operação percebe na tela | `CHANGELOG.md`, seção `## Não publicado` |

## O que NÃO documentar
- Detalhe que o código já expressa (assinatura, tipo, campo óbvio).
- Fluxo de uma tarefa específica — vai pro PR description.
- Roadmap detalhado — só o macro em `01-project-overview.md`, o resto em `docs/BACKLOG.md`.

## Anti-padrões
- Reescrever `modules/<x>.md` inteiro por uma mudança pontual.
- Criar arquivo em `docs/ai/` sem registrar no `00-index.md`.
- Duplicar conteúdo entre camadas (ver tabela acima).
- **Citar rota, arquivo, flag ou env var sem conferir no código.** Doc que inventa
  caminho é pior que doc ausente: manda o próximo agente pro lugar errado com confiança.
- Deixar número solto envelhecendo ("48 testes", "8 parsers"). Se precisa de número,
  ele vem com a data em que foi conferido.
