# Portal: envio ao Flow por necessidade — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O Portal para de segurar a fila nas janelas de 8h/11h/14h/17h: os lotes do Fire (SP1) e a fila do `/recebimento` saem assim que há dado, com um lote vazio de sinal de vida quando passar 1 h sem envio aceito.

**Architecture:** No runner do SP1, a etapa "lotes da janela" vira "drenar a fila" em todo tick, com o pedido de sinal de vida por silêncio de 60 min; a drenagem registra `auto`/`sinal` como origem. Na fila do `/recebimento`, sai o portão de janela (thread web e worker) e o resumo por janela. O módulo de janelas encolhe para `FUSO`. Tela, docs e CHANGELOG acompanham.

**Tech Stack:** Python 3.11, FastAPI, SQLite por ambiente, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-27-envio-flow-por-necessidade-design.md` (seções do Portal: §3, §5, §6).

## Global Constraints

- Entra em produção **depois** do deploy 1 do Flow (processar ao receber) e **antes** do deploy 2 (alerta 90 min). Pacote só com ok do Samuel.
- Sinal de vida: `SINAL_DE_VIDA = timedelta(minutes=60)`, 24 h por dia, todos os dias.
- Continuam valendo, sem mudança: ritmo de 3 s entre POSTs, pausa de 5 min após falha do ERP, recuo do Flow (`proxima_tentativa_em`), bloqueio da base, teto de 500 eventos/lote, foto das 5h e o drenar-antes-da-foto.
- `erp_envio_lote.origem`: novos valores `auto` (lote com evento, envio normal) e `sinal` (lote vazio do envio normal); `janela` continua aceito (histórico); `foto` e `manual` iguais. Sem migration (coluna texto, validada só em Python).
- Rótulos: `auto` → "Automático"; `sinal` → "Sinal de vida"; `janela` → "Janela das Xh" (histórico).
- `PORTAL_FLOW_JANELAS` deixa de existir; se presente no ambiente, log de aviso **uma vez** por processo.
- Textos de tela da fonte única do servidor (`app/erp_eventos/consulta.py`); o JS só exibe.
- Diff mínimo; `ruff check` e `ruff format --check` do que for tocado; teste direcionado por task; suíte completa antes do PR (`$V/bin/python -m pytest` a partir da raiz do worktree; as 2 falhas de `test_env_leak_guard` em worktree aninhado são conhecidas).

## Review Focus

1. Fire fora do ar (vigia falhando) → o sinal de vida de hora em hora continua saindo, porque a drenagem roda antes da vigia. (Task 1: teste com vigia lançando.)
2. Flow pediu recuo (`proxima_tentativa_em` no futuro) → tick a tick, nenhum POST até a hora; nada de martelar a cada 30 s. (Task 1.)
3. Base bloqueada → nenhum lote, nem o de sinal de vida. (Task 1.)
4. Portal reinicia → não manda rajada de sinais: o pedido de sinal depende de `ultimo_202_em` gravado no SQLite. (Task 1: dois ticks seguidos depois de um 202 recente → nenhum sinal.)
5. Pedido importado commitado fora do antigo horário de janela (ex.: domingo 21h) → sai no próximo tick da fila. (Task 3.)

---

### Task 1: SP1 — drenagem em todo tick e sinal de vida por silêncio

**Files:**
- Modify: `app/erp_eventos/runner.py:355-470` (`_lotes_da_janela`, `_garantir_lote_na_janela`, chamada em `processar_base`, docstring de `_enviar_foto`)
- Modify: `app/erp_eventos/drenagem.py:41-110` (default de `origem`, origem do lote vazio)
- Modify: `app/persistence/erp_envio_lote_repo.py:12` (`ORIGENS`)
- Test: `tests/test_erp_eventos_runner.py`, `tests/test_erp_eventos_drenagem.py` (ou o arquivo que cobre `drenar`), `tests/test_erp_envio_lote_repo.py`, `tests/test_erp_eventos_manual.py`

**Interfaces:**
- Produces:
  - `runner.SINAL_DE_VIDA: timedelta = timedelta(minutes=60)`
  - `runner._drenar_fila(cfg: BaseErpConfig) -> None` (substitui `_lotes_da_janela`; etapa `_etapa(cfg, "envio", ...)`)
  - `runner._garantir_sinal_de_vida(cfg: BaseErpConfig, agora: datetime) -> None` (substitui `_garantir_lote_na_janela`)
  - `drenagem.drenar(..., origem: str = "auto", ...)`; lote com 0 eventos e `origem == "auto"` é registrado como `"sinal"`
  - `erp_envio_lote_repo.ORIGENS = ("janela", "auto", "sinal", "foto", "manual")`

- [ ] **Step 1: Testes que falham**
  - tick em domingo 21:00 com 3 eventos pendentes → 1 POST, lote registrado com `origem == "auto"` e `eventos == 3`.
  - fila vazia e `ultimo_202_em` há 61 min → 1 POST de lote vazio com `origem == "sinal"`; há 59 min → nenhum POST.
  - dois ticks seguidos logo após um 202 → nenhum sinal (Review Focus 4).
  - vigia lança (Fire fora) e `ultimo_202_em` há 61 min → o lote de sinal sai no mesmo tick (Review Focus 1).
  - `proxima_tentativa_em` no futuro com fila cheia → nenhum POST (Review Focus 2).
  - base com `bloqueio` → nenhum POST, nem sinal (Review Focus 3).
  - Enviar agora com fila vazia continua registrando `origem == "manual"` (não `sinal`).
  - `abrir(..., origem="xyz")` continua levantando; `auto` e `sinal` aceitos.
  - Reescrever os testes que fixam "fora da janela nada sai" (`test_erp_eventos_runner.py:61-142`, `test_erp_eventos_manual.py` `FORA_DA_JANELA`/`test_dentro_da_janela_passo1_pulado`) para o comportamento novo, sem apagar o que eles provam do Enviar agora.

- [ ] **Step 2: Rodar** `$V/bin/python -m pytest tests/test_erp_eventos_runner.py tests/test_erp_eventos_manual.py tests/test_erp_envio_lote_repo.py -q` → FAIL.

- [ ] **Step 3: Implementar.** `_drenar_fila`: chama `_garantir_sinal_de_vida(cfg, _relogio())` e `drenagem.drenar(cfg, cliente=..., ritmo=_ritmo)` (mesmo formato de hoje, sem o `janela_aberta`). `_garantir_sinal_de_vida`: se `not envio.sinal_pendente and (envio.ultimo_202_em or "") < formatar_instante(agora - SINAL_DE_VIDA)` → `pedir_sinal_de_vida`. O comentário do bloco em `processar_base` explica por que a drenagem vem antes da vigia (vigia travada não segura o envio nem o sinal). Docstring de `_enviar_foto` sem "janela".

- [ ] **Step 4: Rodar** os mesmos testes → PASS; `ruff check` + `ruff format --check` dos arquivos.

- [ ] **Step 5: Commit** `feat(erp-eventos): fila sai em todo tick e sinal de vida por silêncio de 1 h`.

---

### Task 2: Consulta, painel e tela sem janelas

**Files:**
- Modify: `app/erp_eventos/consulta.py:126-135` (`_origem_lote`), `:222-265` (`_proxima_texto`, `_frase`), imports de `janelas_envio`
- Modify: `app/web/routes_environments.py:41,495-525` (painel)
- Modify: `app/web/static/admin-envio-flow.html` (texto do topo, card "Próxima saída", diálogo do Enviar agora; linhas ~101, 307, 324-327)
- Test: `tests/test_erp_eventos_consulta.py`, `tests/test_erp_eventos_admin.py:276-277`, `tests/test_web_envio_flow_page.py:70`

**Interfaces:**
- Consumes: `ORIGENS` com `auto`/`sinal` (Task 1); `runner.INTERVALO_VIGIA`.
- Produces: painel sem `proxima_janela` e sem `janelas`; mantém `intervalo_vigia_min`.

- [ ] **Step 1: Testes que falham**
  - `rotulo_lote` de lote `auto` → origem "Automático"; `sinal` → "Sinal de vida"; `janela` às 11:05 SP → "Janela das 11h".
  - frase de pendente sem bloqueio nem recuo → termina em "Saem no próximo envio, em instantes." (qualquer dia e hora, inclusive domingo 21h).
  - frase com recuo (`proxima_tentativa_em` 14:30 SP, agora 14:10) → "O Flow ainda não recebeu N eventos. Saem às 14:30, quando o Flow liberar." e sem "Enviar agora".
  - painel não tem as chaves `proxima_janela` nem `janelas`; tem `intervalo_vigia_min`.
  - página `/admin/envio-flow` não contém "Próxima saída" nem "janela"; contém "Envio" com "automático".

- [ ] **Step 2: Rodar** os testes → FAIL.

- [ ] **Step 3: Implementar.** `_proxima_texto` sai; o recuo formata `proxima_tentativa_em` em HH:MM SP. Na tela: topo "Pedidos do Fire que o portal lê e manda para o Flow assim que mudam."; card "Envio" com valor "Automático" e linha "a cada leitura do Fire (N min)" vinda de `intervalo_vigia_min`; o diálogo do Enviar agora mantém os itens atuais, sem nenhuma menção a janela.

- [ ] **Step 4: Rodar** → PASS; ruff dos arquivos Python.

- [ ] **Step 5: Commit** `feat(web): tela do Envio ao Flow sem janelas`.

---

### Task 3: Fila do `/recebimento` sem janela

**Files:**
- Modify: `app/integrations/flowpcp/envio_portal.py` (doc do módulo ~l. 30-40; imports :71,:75; `_JANELAS_GUARDADAS`, `_EVENTOS`, `_RESULTADOS`, `resumo_da_janela`, `_atualizar_gauge`, `_trocar_janela` :93-180; `drenar` :326-340)
- Modify: `app/worker/jobs/drain_outbox.py:66-68`
- Modify: `app/observability/metrics.py:68-70` (remover `flowpcp_janela_pedidos`)
- Test: `tests/test_flowpcp_envio_portal.py` (casos de janela :352, :426-566), teste do worker que cobre `drain_outbox`

**Interfaces:**
- Produces: `envio_portal.drenar(agora: datetime | None = None) -> None` — drena todos os ambientes habilitados em toda chamada.

- [ ] **Step 1: Testes que falham**
  - `drenar` num domingo 21:00 com 1 pedido na fila → `drenar_ambiente` chamado e o pedido enviado (Review Focus 5).
  - worker `drain_outbox` fora do antigo horário → chama `drenar_ambiente` para o ambiente FlowPCP habilitado.
  - `/metrics` não expõe mais `portal_flowpcp_janela_pedidos`.
  - Remover os testes de `janelas_recentes`, `resumo_da_janela` e do gauge; manter os de pausa ERP/Flow, `LOTE`, `LIMITE_ATRASO` e retenção.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar** removendo o portão e o resumo por janela (com `update_outbox_metrics()` mantido onde já é chamado). Os contadores de envio e recusa existentes ficam.

- [ ] **Step 4: Rodar** → PASS; ruff dos arquivos.

- [ ] **Step 5: Commit** `feat(flowpcp): fila do recebimento sai sem esperar janela`.

---

### Task 4: Módulo de janelas, docs e notas da versão

**Files:**
- Modify: `app/utils/janelas_envio.py` (fica só `FUSO` e o aviso de variável obsoleta)
- Delete: `tests/test_janelas_envio.py` (substituído pelo teste do aviso)
- Test: `tests/test_janelas_envio.py` → recriar só com o teste do aviso
- Modify: `CLAUDE.md` (linha de `PORTAL_FLOW_JANELAS` sai; `PORTAL_FLOWPCP_PRECOS` "independe das janelas" vira texto sem janela), `docs/ai/modules/erp_eventos.md:50-65`, `docs/ai/modules/flowpcp.md:92-95,492,556`, `docs/ai/modules/web.md:60`, `CHANGELOG.md` (seção "Não publicado")

**Interfaces:**
- Produces: `janelas_envio.FUSO`; `janelas_envio.avisar_variavel_obsoleta() -> None` — se `PORTAL_FLOW_JANELAS` estiver no ambiente, `logger.warning` uma vez por processo ("PORTAL_FLOW_JANELAS não é mais usada: o envio ao Flow sai quando há dado"); chamada na subida da thread do SP1 e da thread do FlowPCP.

- [ ] **Step 1: Teste que falha** — com `PORTAL_FLOW_JANELAS=8,11` no ambiente, duas chamadas de `avisar_variavel_obsoleta()` geram exatamente 1 warning; sem a variável, nenhum.

- [ ] **Step 2: Rodar** → FAIL.

- [ ] **Step 3: Implementar**; `git grep -n -E "janela_aberta|proxima_janela|janelas_recentes|horas_das_janelas|inicio_da_janela|PORTAL_FLOW_JANELAS" -- app tests` só pode achar `janelas_envio.py` (aviso) e o teste dele.

- [ ] **Step 4: Docs e CHANGELOG.** Só as seções afetadas. Nota da versão, para a operação: "Pedidos vão ao Flow assim que mudam: o que for digitado ou alterado no Fire chega ao Flow na próxima leitura (até 15 minutos), e os pedidos importados no portal saem em segundos, em vez de esperar 8h, 11h, 14h ou 17h. A tela Envio ao Flow mostra o envio como automático."

- [ ] **Step 5: Rodar** a suíte completa → só as 2 falhas conhecidas de `test_env_leak_guard`; `ruff check app/ tests/` limpo.

- [ ] **Step 6: Commit** `refactor(envio): janelas de envio aposentadas; docs e notas da versão`.
