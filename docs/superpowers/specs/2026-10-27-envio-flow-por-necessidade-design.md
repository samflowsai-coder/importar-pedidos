# Envio ao Flow por necessidade (fim das janelas)

Data: 2026-10-27 · Repos: Portal de Pedidos (`importar-pedidos`) e Flow (`pcp-app`)

## 1. Intenção

Hoje o Portal só manda dados ao Flow em quatro janelas fixas (8h, 11h, 14h e 17h, seg–sáb), e o Flow só processa o que recebeu em horário fixo de cron (20 e 50 min depois de 5h, 8h, 11h, 14h e 17h SP). Resultado: pedido digitado no Fire às 17h30 só aparece no Flow às 8h20 do dia seguinte, a MM começa às 7h, e o botão "Enviar agora" (20261026-2236) entrega um lote que o Flow aceita na hora mas só processa no próximo horário de cron.

O Samuel decidiu (27/10):
- os dados vão ao Flow **quando existem**, não por relógio;
- é aceitável que o PCP veja pedidos entrando **ao longo do dia**;
- os **pedidos importados pelo Portal** (fila do `/recebimento`) também saem na hora, porque pedido com preço a aprovar precisa estar no Flow para a aprovação acontecer.

Sucesso: um pedido novo ou alterado no Fire está processado no Flow em até ~16 min (vigia de 15 min + envio + processamento); um pedido importado no Portal chega ao Flow em segundos; nada depende de hora marcada, exceto a foto noturna.

## 2. Não-objetivos

- Não muda o conteúdo dos eventos, o formato do lote, a foto das 5h nem o modo sombra do Flow.
- Não muda a frequência da vigia (leitura do Fire a cada 15 min).
- Não cria push do Fire (o Fire não avisa nada; a vigia continua lendo).
- Não muda catálogo, trava de preço, decisões ou cadastro-volta (já rodam fora das janelas).

Anti-objetivos (falha mesmo funcionando):
- duas rodadas de processamento do mesmo tenant ao mesmo tempo no Flow;
- alerta falso de "sem sinal" durante a transição entre versões;
- lote vazio em excesso (rajada de lotes sem dado).

## 3. Como fica — Portal

### 3.1 SP1 (pedidos do Fire)
- **Drenagem sem janela.** Em todo tick (`TICK_S=30`) o runner drena a fila se houver evento pendente, respeitando o que já existe: ritmo de 3 s entre POSTs, pausa após falha do ERP, recuo pedido pelo Flow (`proxima_tentativa_em`) e bloqueios da base. Sai `janela_aberta` de `_lotes_da_janela` (`runner.py:450-454`).
- **Sinal de vida de hora em hora.** Sem 202 há 60 min, o runner pede um lote vazio (substitui `_garantir_lote_na_janela`, que pedia um por janela). Vale 24 h por dia, todos os dias: a vigia já roda sempre e o custo é ~24 lotes vazios/dia por base.
- **Foto das 5h**: sem mudança.
- **Origem do lote** (`erp_envio_lote.origem`): novos valores `auto` (lote com dado) e `sinal` (lote vazio de sinal de vida); `janela` fica válido só para linhas antigas; `foto` e `manual` seguem. Rótulos na tela: "Automático", "Sinal de vida", e "Janela das Xh" só no histórico antigo.
- **Enviar agora** continua: força a leitura do Fire agora e manda o que houver. O resto (TTL 5 min, intervalo 60 s, recusas) fica igual. O diálogo deixa de falar em janela.
- **Tela `/admin/envio-flow`**: o card "Próxima saída" sai; entra "Envio: automático, a cada leitura do Fire (15 min)". A frase de pendente vira "Sai no próximo envio, em instantes." quando nada bloqueia; com bloqueio ou recuo, a frase do bloqueio/recuo (já existe). O texto do topo deixa de citar horários.

### 3.2 Fila do `/recebimento` (pedidos importados)
- Sai o portão de janela em `app/integrations/flowpcp/envio_portal.py:326-334` (thread do processo web) e em `app/worker/jobs/drain_outbox.py:66-68` (worker). A fila drena a cada tick (15 s), com o lote de 20 e as pausas que já existem.
- O resumo por janela (gauge `portal_flowpcp_janela_pedidos`, log "janela fechada", `_JANELAS_GUARDADAS`) sai. Os contadores de envio/recusa que já existem ficam.

### 3.3 Módulo de janelas
- `app/utils/janelas_envio.py` perde janelas, `PORTAL_FLOW_JANELAS` e funções de janela; fica só `FUSO` (usado por foto e catálogo) — ou `FUSO` muda para um módulo de tempo, a critério do builder, mantendo uma fonte só.
- `PORTAL_FLOW_JANELAS` sai do CLAUDE.md e dos docs; se estiver no `.env` do cliente, é ignorada (log de aviso uma vez, para ninguém achar que ainda vale).

## 4. Como fica — Flow (pcp-app)

### 4.1 Processar ao receber
- `POST /api/portal-pedidos/eventos`: depois de gravar e responder 202, **se o lote trouxe pelo menos um evento**, dispara o processamento **daquele tenant** em segundo plano com `after()` do Next 16 (processo `app`, máquina sempre ligada no Fly). A resposta ao Portal não espera o processamento. Lote vazio (sinal de vida) não dispara nada.
- Só processa se a flag `receber` do tenant estiver ligada (mesma regra do cron).
- **Rodada curta na chegada, rodada completa no cron.** `processarTenant` foi desenhado para poucas rodadas por dia: todo ciclo roda também os passos de fundo (5 `vinculosAtrasados`, 6 conciliação da foto, 7 `classificarEConferirLotes`, 8 `revincularPendentes`; `processar.ts:1588`). Na chegada roda só o processamento dos eventos pendentes (passos 1–4); os passos 5–8 ficam no cron de hora em hora (§4.3). `processarTenant` ganha um modo (ex.: `{ somenteEventos: true }`) em vez de uma segunda função, para a ordem e as regras dos passos 1–4 continuarem num lugar só.
- **Foto não dispara.** `POST /snapshot-erp` segue só gravando; a conciliação da foto das 5h roda no cron das 5h20 SP, antes de a MM começar às 7h.

### 4.2 Uma rodada por tenant (trava)
- `processarTenant` hoje não tem trava. Entra um **claim por tenant com validade**, no mesmo padrão do sync do Apontaê (`apontae/sync.ts`: UPDATE condicional de um timestamp "ocupado até", 0 linhas = outra rodada em andamento → pula). Validade = prazo da rodada + folga (rodada tem teto de 240 s).
- **Exige coluna nova** (ex.: `erp_processando_ate timestamptz` numa tabela por tenant). **É migration: precisa do ok do Samuel antes.**
- **Não perder lote que chegou durante a rodada**: ao terminar, a rodada curta solta o claim e, se houver evento pendente recebido depois do início dela, roda de novo (no máximo 3 voltas seguidas; o resto fica para o próximo lote ou o cron). O `after()` que encontra o claim ocupado só sai: a rodada em curso ou a volta seguinte pega o lote dele.
- O claim é o mesmo para a rodada curta e para a completa do cron.

### 4.3 Cron vira rede de segurança
- `processar-eventos-erp` passa a rodar **de hora em hora** (`20 * * * *` UTC), respeitando o mesmo claim. Ele cobre: lote cujo `after()` falhou ou morreu num deploy, e a **conciliação da foto**, que só roda dentro de `processarTenant` (passo 6).

### 4.4 Alerta e telas
- `sem_lote_janela` vira **`sem_sinal`**, **por base** como hoje (`ultimo_lote_em` é por credencial/base: `saude-eventos-erp/route.ts:63-81`, `saude.ts:66-69`), para a Nasmar calada não ficar escondida pela MM: último lote da base há mais de **90 min**, avaliado **seg–sáb das 6h às 20h SP** (fora disso não alerta). Throttle de 6 h e liberação quando o sinal volta: iguais aos de hoje. O alerta da foto (prazo 6h) fica.
- Cron `saude-eventos-erp`: de hora em hora no minuto 45, além do 6h45 da foto.
- `JANELAS_ENVIO_HORA_SP`, `janelaVencidaSp`, `janelaSemLote` saem; `rotulos-tela` ("Sem sinal") usa a mesma régua de 90 min — uma função só, usada pelo alerta e pela tela.
- Textos que citam as janelas: `configuracoes/integracoes/erp/page.tsx`, `central-ajuda/integracao-erp/page.tsx`, `senso/screen-registry.ts` (2 entradas), comentários do `cron-fly.mjs` e de `rate-limit-portal.ts`.

## 5. Ordem de entrada em produção (decisão minha)

A régua nova de alerta (90 min) dispara falso com o Portal velho, que fica até 3 h sem mandar lote entre janelas. Por isso:
1. **Flow, deploy 1:** processar ao receber + trava + cron de hora em hora. A régua de alerta **não** muda ainda. Funciona com o Portal velho (lotes ainda vêm nas janelas e agora são processados na hora).
2. **Portal, pacote:** envio por necessidade + sinal de vida horário + fila do `/recebimento` sem janela.
3. **Flow, deploy 2:** alerta `sem_sinal` de 90 min e textos.

Entre 1 e 2 o Enviar agora já vira imediato de verdade.

## 6. Restrições

- Portal: Python 3.11, testes pytest; Flow: Next 16, vitest; lint e type-check do que for tocado nos dois.
- Uma regra em um lugar só: a régua de "sem sinal" no Flow é uma função; o intervalo do sinal de vida no Portal é uma constante.
- Migration no Flow (coluna do claim) e deploys pedem ok do Samuel.
- Testes que fixam as janelas hoje mudam junto (lista no levantamento: `tests/test_janelas_envio.py`, `test_flowpcp_envio_portal.py`, `test_erp_eventos_runner.py`, `test_erp_eventos_manual.py`, `test_erp_eventos_consulta.py`, `test_erp_eventos_admin.py`, `test_web_envio_flow_page.py`; no Flow `saude.test.ts`, `rotulos-tela.test.ts`, `cron-fly-scheduler.test.ts`, `saude-eventos-erp/__tests__/route.test.ts`).

## 7. Decisões minhas (o Samuel pode trocar)

- Sinal de vida a cada **60 min, 24 h**; alerta só **seg–sáb 6h–20h** com régua de **90 min**.
- Origens `auto` e `sinal` no registro de lotes; `janela` só no histórico.
- Gauge de janela do `/recebimento` sai, sem substituto.
- Trava pelo padrão do Apontaê (coluna com validade), não advisory lock.
- Até 3 voltas seguidas quando chegam lotes durante a rodada.
- Na chegada só os eventos (passos 1–4); passos de fundo e conciliação da foto só no cron de hora em hora; lote vazio e foto não disparam processamento.
- Painel do Portal: `app/web/routes_environments.py:41,501,512` (usa `proxima_janela`/`horas_das_janelas`) muda junto com a tela.
- Ordem de produção em 3 passos (§5).

## 8. Fica com o builder

- Nome e tabela da coluna do claim no Flow.
- Onde mora `FUSO` no Portal.
- Forma exata do texto das telas, seguindo o tom das existentes.
