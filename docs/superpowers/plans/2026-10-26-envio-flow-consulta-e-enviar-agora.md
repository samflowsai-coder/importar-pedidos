# Envio ao Flow: consulta por pedido e Enviar agora — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tela de admin `/admin/envio-flow` que consulta o envio SP1 por pedido, lista os últimos lotes e dispara "Enviar agora" pela thread `erp-eventos`.

**Architecture:** Registro de cada lote em `erp_envio_lote` (gravado por `drenagem.drenar`). O clique vira um pedido em memória, guardado por lock, que `runner.processar_base` atende no tick (drenar se houver pendentes → vigia forçada → drenar). A consulta é um módulo puro sobre o SQLite do ambiente, exposto por rotas admin em `routes_environments.py`.

**Tech Stack:** Python 3.11, FastAPI, SQLite (`router.env_connect`), pytest; HTML/JS estático com `tokens.css` + `shell.css`.

**Spec:** `docs/superpowers/specs/2026-10-26-envio-flow-consulta-e-enviar-agora-design.md`

## Global Constraints

- Somente leitura no Fire; nenhum request HTTP abre conexão com Fire ou Flow (só SQLite local).
- Sem dependência nova. Sem token CSRF (proteção = sessão + `SameSite=Strict`, igual às rotas admin).
- Toda rota nova: `Depends(require_admin)`; ambiente inexistente → 404; base não configurada → 404.
- Retenção de `erp_envio_lote`: 30 dias (`RETENCAO_FILA`), na mesma rotina de `_reter`.
- Intervalo mínimo entre envios manuais da mesma base: 60 s. Teto de polling na tela: 5 min, a cada 2 s. Resultado manual em memória: 1 h.
- `q`: `strip`, 1 a 40 caracteres (422 fora disso); lista de repetidos `LIMIT 50`.
- Rótulos de tipo (exatos): `pedido_visto` Pedido novo · `pedido_excluido` Pedido excluído · `status_alterado` Status mudou · `item_incluido` Item incluído · `item_removido` Item removido · `quantidade_alterada` Quantidade mudou · `preco_alterado` Preço mudou · `data_entrega_alterada` Data de entrega mudou · `faturamento_detectado` Faturado · `lote_fiscal` Lote fiscal; desconhecido = nome cru.
- Situação na tela: `pendente` sem `enviado_em` = "Na fila"; `pendente` com `enviado_em` = "Enviado, sem resposta"; `aceito` = "Aceito"; `duplicado` = "Aceito (duplicado)"; `rejeitado` = "Recusado". (Não existe situação `enviado` na tabela.)
- Testes: venv `scratchpad/venv311` (o repo não tem `.venv`): `$V/bin/pytest`, `V=/private/tmp/claude-501/-Users-samuelalves-SamFlowsAI---Projeto-Cursor-importar-pedidos/5b62414d-8dd6-41f6-b6e5-27865495c63a/scratchpad/venv311`. `ruff check` do que tocar.

## Review Focus

1. Clique em empresa com envio ligado mas credencial ilegível → 409 com a mensagem da credencial, nunca 202 órfão (Task 4 + Task 7).
2. Dois cliques quase simultâneos (dois admins) → exatamente um 202 (Task 5).
3. Portal reiniciado com a tela aberta acompanhando → GET do `envio_id` 404 e a tela para (Task 7 + Task 8).
4. Pedido do cliente digitado com espaço/caixa diferente (" uy44549110 ") acha o pedido (Task 6).
5. Fila vazia no clique → um lote vazio só, registrado como sinal de vida (Task 5).

---

### Task 1: Registro de lotes (`erp_envio_lote`) + retenção

**Files:**
- Modify: `app/persistence/schema_env.py` (depois de `erp_eventos_saida`)
- Create: `app/persistence/erp_envio_lote_repo.py`
- Modify: `app/erp_eventos/runner.py:292-297` (`_reter`)
- Test: `tests/test_erp_envio_lote_repo.py`

**Interfaces:**
- Produces:
  - `abrir(conn, *, lote_id: str, base: str, origem: str, quem: str | None, envio_id: str | None, enviado_em: str, eventos: int) -> None`
  - `fechar(conn, lote_id: str, *, http: int | None, aceitos: int, duplicados: int, rejeitados: int, respondido_em: str) -> None`
  - `recentes(conn, base: str, limite: int = 20) -> list[LoteRegistro]` (mais novo primeiro)
  - `origens(conn, lote_ids: list[str]) -> dict[str, LoteRegistro]`
  - `apagar_antigos(conn, antes_de: str) -> int`
  - `@dataclass(frozen=True) LoteRegistro`: `lote_id, base, origem, quem, envio_id, enviado_em, eventos, http, aceitos, duplicados, rejeitados, respondido_em`
  - `ORIGENS = ("janela", "foto", "manual")`; `abrir` com origem fora disso → `ValueError`.

- [ ] **Step 1: Testes** — `test_abrir_e_fechar_lote` (abre com eventos=3, fecha com http=202/aceitos=2/rejeitados=1; `recentes` devolve 1 linha com esses valores e `respondido_em`); `test_recentes_ordem_e_limite` (25 lotes → 20, o mais novo primeiro); `test_origens_por_lote_id` (ids inexistentes ausentes do dict); `test_apagar_antigos` (só `enviado_em < antes_de`); `test_origem_invalida` (`ValueError`); `test_retencao_do_runner_apaga_lotes` (chama `runner._reter` e confere que lote de 31 dias sumiu, o de 1 dia ficou).
- [ ] **Step 2:** rodar `$V/bin/pytest tests/test_erp_envio_lote_repo.py -v` → FAIL (módulo inexistente).
- [ ] **Step 3:** DDL exatamente o da spec §3 (tabela + índice `ix_erp_envio_lote_base_em`), repo com as funções acima, `_reter` chama `erp_envio_lote_repo.apagar_antigos` com o mesmo corte.
- [ ] **Step 4:** rodar o arquivo → PASS; `$V/bin/pytest tests/test_erp_eventos_runner.py -q` → PASS.
- [ ] **Step 5:** commit `feat(erp_eventos): registro de lotes erp_envio_lote`.

### Task 2: `drenar` registra cada lote com origem

**Files:**
- Modify: `app/erp_eventos/drenagem.py` (`drenar`)
- Modify: `app/erp_eventos/runner.py` (`_enviar_foto` passa `origem="foto"`)
- Test: `tests/test_erp_eventos_drenagem.py`

**Interfaces:**
- Consumes: Task 1 `abrir`/`fechar`.
- Produces: `drenar(cfg, *, cliente, ritmo, agora_fn=..., relogio=..., prazo_s=..., ate=None, origem: str = "janela", quem: str | None = None, envio_id: str | None = None) -> ResultadoDrenagem` (retorno inalterado).

- [ ] **Step 1: Testes** — `test_lote_aceito_grava_registro_janela` (origem `janela`, `eventos`=n, `http`=202, `aceitos`=n); `test_lote_vazio_de_sinal_grava_registro` (`eventos`=0, http 202); `test_lote_manual_grava_quem_e_envio_id`; `test_lote_com_erro_5xx_fecha_com_http_e_zero_aceitos`; `test_lote_400_bloqueia_e_fecha_registro`; `test_enviar_foto_registra_origem_foto` (em `test_erp_eventos_runner.py`, drenagem antes da foto com pendentes → linha `foto`).
- [ ] **Step 2:** rodar → FAIL.
- [ ] **Step 3:** `abrir` na mesma transação de `marcar_enviados` (antes do POST); `fechar` na mesma transação de `aplicar_resultados` + `registrar_202` no caminho `aplicar`, e na transação de `registrar_espera`/`bloquear` nos caminhos de erro (aceitos 0, `http=resp.status`). O caminho `dividir` fecha o lote com o `http` recebido (413) antes do `continue`.
- [ ] **Step 4:** `$V/bin/pytest tests/test_erp_eventos_drenagem.py tests/test_erp_eventos_runner.py -q` → PASS.
- [ ] **Step 5:** commit `feat(erp_eventos): drenar registra origem de cada lote`.

### Task 3: `proxima_janela`

**Files:**
- Modify: `app/utils/janelas_envio.py`
- Test: `tests/test_janelas_envio.py` (criar se não existir; senão acrescentar)

**Interfaces:**
- Produces: `proxima_janela(agora: datetime) -> datetime` — início (aware, UTC) da próxima janela que **começa depois** de `agora`, pulando domingo; usa `_horas_das_janelas()`.

- [ ] **Step 1: Testes** (horário de São Paulo nos comentários): seg 18:00 → ter 08:00; seg 09:30 → seg 11:00; sáb 18:00 → seg 08:00; dom 10:00 → seg 08:00; com `PORTAL_FLOW_JANELAS="9,15"` seg 10:00 → seg 15:00.
- [ ] **Step 2:** FAIL. **Step 3:** implementar reaproveitando o fuso e as horas que `janela_aberta` já usa. **Step 4:** PASS. **Step 5:** commit `feat(utils): proxima_janela`.

### Task 4: Predicado único `base_atendida`

**Files:**
- Modify: `app/persistence/erp_bases_repo.py:159-173`
- Test: `tests/test_erp_eventos_admin.py` (ou o teste do repo de bases que já existir)

**Interfaces:**
- Produces: `base_atendida(cfg: BaseErpConfig | None) -> str | None` — `None` se a thread atende; senão o motivo: `"desligada"` (base `ativo` falso ou ambiente inativo) ou `"sem_credencial"` (token ilegível/ausente). `listar_ativas()` passa a filtrar com ele (mesmo log de aviso para `sem_credencial`).
- Requer que `BaseErpConfig` exponha `ativo` e se o ambiente está ativo; se não expõe, `obter` passa a carregar esses dois campos (mesmo `_SELECT`).

- [ ] **Step 1: Testes** — `test_base_atendida_ok`; `test_base_desligada`; `test_ambiente_inativo`; `test_sem_credencial`; `test_listar_ativas_concorda_com_base_atendida` (para 4 configs, `cfg in listar_ativas()` ⇔ `base_atendida(cfg) is None`).
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS + `tests/test_erp_eventos_runner.py`. **Step 5:** commit `refactor(erp_bases): predicado base_atendida`.

### Task 5: Pedido manual atendido pela thread

**Files:**
- Modify: `app/erp_eventos/runner.py`
- Test: `tests/test_erp_eventos_manual.py`

**Interfaces:**
- Consumes: Task 2 `drenar(origem=, quem=, envio_id=)`, Task 4 `base_atendida`.
- Produces:
  - `class EnvioManualRecusado(Exception)` com `.codigo` ∈ `{"vigia_desligada", "desligada", "sem_credencial", "bloqueada", "recuo", "em_andamento", "intervalo"}` e `.detalhe: str` (texto da tela, em português; `recuo` inclui o horário local "até HH:MM").
  - `pedir_envio_manual(cfg: BaseErpConfig, quem: str, *, agora: datetime | None = None) -> str` (devolve `envio_id` uuid4; levanta `EnvioManualRecusado`).
  - `resultado_manual(envio_id: str) -> dict | None` — `{"envio_id", "situacao": "pendente"|"em_andamento"|"terminado", "pedido_em", "iniciado_em", "terminado_em", "vigia": "ok"|"pulada"|"falhou"|None, "lotes", "aceitos", "duplicados", "rejeitados", "parou_por"}`; `None` se desconhecido ou expirado (> 1 h).
  - Estado: `_MANUAL_GUARD = threading.Lock()`, `_PEDIDO_MANUAL: dict[tuple[str, str], str]`, `_RESULTADOS: dict[str, dict]`.
- Ordem no tick (dentro de `processar_base`, depois de `_lotes_da_janela`, sob a trava da base): `pop` do pedido sob `_MANUAL_GUARD` e marca `em_andamento`; (1) `drenar(origem="manual", ...)` **só se** `erp_estado_repo.pendentes(conn, base, limite=1)` não vazio; (2) `_vigiar` forçada se `erp_liberado` (respeita `_FALHA_ERP`), senão `vigia="pulada"`; exceção → `vigia="falhou"` e marca `_FALHA_ERP`; (3) `drenar(origem="manual", ...)` sempre; soma os dois `ResultadoDrenagem`; `terminado`. A vigia normal do tick não roda de novo quando a forçada rodou.

- [ ] **Step 1: Testes** (cliente HTTP falso como em `test_erp_eventos_runner.py`):
  - `test_pedido_recusado_por_codigo` (parametrizado: `PORTAL_ERP_VIGIA=0`; base desligada; sem credencial; `bloqueio`; `proxima_tentativa_em` futuro; pedido pendente; atendido há 30 s → `intervalo`; atendido há 61 s → aceito).
  - `test_dois_pedidos_concorrentes_um_aceito` (duas threads chamam `pedir_envio_manual` ao mesmo tempo com `threading.Barrier`; exatamente uma devolve id, a outra `em_andamento`).
  - `test_tick_atende_na_ordem` (fila com 2 pendentes, vigia que enfileira 1: espera 2 POSTs, o 1º com 2 eventos e o 2º com 1, ambos `origem='manual'`, `quem` e mesmo `envio_id`; resultado `terminado`, `vigia="ok"`, `lotes=2`).
  - `test_fila_vazia_um_lote_vazio_so` (sem pendentes, vigia sem mudança → exatamente 1 POST com 0 eventos).
  - `test_vigia_falha_ainda_drena` (vigia levanta; pendentes antigos saem; `vigia="falhou"`).
  - `test_vigia_em_pausa_pulada` (`_FALHA_ERP` recente → `vigia="pulada"`, drena).
  - `test_pedido_atendido_uma_vez` (dois ticks seguidos → um atendimento).
  - `test_dentro_da_janela_passo1_pulado` (às 14:10 a janela drena; o manual não acha pendentes no passo 1; lotes da janela ficam `origem='janela'`).
  - `test_resultado_expira_em_1h` e `test_resultado_desconhecido_none`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** `$V/bin/pytest tests/test_erp_eventos_manual.py tests/test_erp_eventos_runner.py -q` → PASS. **Step 5:** commit `feat(erp_eventos): envio manual atendido pela thread`.

### Task 6: Consulta por pedido (módulo puro)

**Files:**
- Create: `app/erp_eventos/consulta.py`
- Test: `tests/test_erp_eventos_consulta.py`

**Interfaces:**
- Consumes: Task 1 `origens`, Task 3 `proxima_janela`.
- Produces:
  - `ROTULOS: dict[str, str]` (Global Constraints) e `rotulo(tipo: str) -> str`.
  - `buscar(conn, base: str, q: str, *, limite: int = 50) -> list[PedidoResumo]` — `q` já validado; só dígitos → `erp_codigo = int(q)` OU pedido do cliente = `q`; senão só pedido do cliente; comparação `UPPER(TRIM(json_extract(estado_json,'$.pedido_cliente'))) = UPPER(TRIM(?))`; ordem `visto_em DESC`.
  - `detalhe(conn, base: str, erp_codigo: int, *, ultima_vigia_em: str | None, bloqueio: str | None, agora: datetime) -> PedidoDetalhe`.
  - `PedidoResumo`: `erp_codigo, pedido_cliente, cliente_codigo, cliente_cnpj, status, visto_em`.
  - `PedidoDetalhe`: resumo + `itens: int`, `aberto: bool`, `leitura_em: str | None`, `leitura_tipo: "ultima_leitura"|"ultima_mudanca"`, `origem_pedido: "portal"|"fire"`, `cliente_nome: str | None`, `eventos: list[EventoView]`, `frase: str`, `retidos: bool` (estado existe e a fila não tem eventos).
  - `EventoView`: `tipo, rotulo, detalhe, observado_em, situacao_tela, enviado_em, origem_lote ("Janela das 8h"|"Antes da foto"|"Manual · <quem>"|"origem não registrada"|None), respondido_em, motivo`.
- Regras: `aberto` = status não está em {FATURADO, CANCELADO} e o pedido não foi excluído; `leitura_em` = `ultima_vigia_em` se aberto, senão `atualizado_em`. `origem_pedido="portal"` se existe `imports.fire_codigo = erp_codigo` (nome do cliente do `imports` quando houver). Frases exatamente as da spec §1, com `<próxima>` de `proxima_janela(agora)` em horário de São Paulo ("amanhã às 08:00" / "hoje às 11:00" / "seg às 08:00") e a de bloqueio usando os textos de `BLOQUEIOS` da tela de ambiente (copiar para o módulo como dict; a tela passa a ler da API, não duplicar). Detalhe curto do evento: `pedido_visto` "N item(ns)"; `status_alterado` "DE → PARA"; `data_entrega_alterada` "dd/mm → dd/mm"; `quantidade_alterada`/`preco_alterado` "antes → depois"; demais vazio.

- [ ] **Step 1: Testes** — `test_busca_por_codigo_fire`; `test_busca_pedido_cliente_com_digitos` (UY44549110 e um `erp_codigo` 44549110 inexistente → acha pelo pedido do cliente); `test_busca_ignora_espaco_e_caixa` (`" uy44549110 "`); `test_busca_repetido_lista_limitada` (60 estados com o mesmo pedido → 50, mais novo primeiro); `test_inexistente_lista_vazia`; `test_detalhe_frases` (parametrizado: todos aceitos; algum pendente; algum enviado sem resposta; algum rejeitado; base bloqueada `credencial`) com o texto exato; `test_leitura_aberto_usa_ultima_vigia` e `test_leitura_encerrado_usa_atualizado_em`; `test_origem_portal_por_imports`; `test_join_vazio_origem_nao_registrada`; `test_retidos_quando_fila_vazia`; `test_rotulo_desconhecido_cru`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS. **Step 5:** commit `feat(erp_eventos): consulta por pedido`.

### Task 7: Rotas admin

**Files:**
- Modify: `app/web/routes_environments.py`
- Modify: `app/web/static/admin-ambiente-edit.html` (link "ver envio por pedido" junto do resumo; `BLOQUEIOS` continua lá até a Task 8 trocar a fonte)
- Test: `tests/test_erp_eventos_admin.py`

**Interfaces:**
- Consumes: Tasks 4, 5, 6, 1, 3.
- Produces (todas `Depends(require_admin)`, 404 se ambiente ou base inexistente):
  - `GET /api/admin/environments/{env_id}/erp-base/pedidos?q=` → `{"pedidos": [PedidoResumo], "limitado": bool}`; com 1 resultado inclui `"detalhe": PedidoDetalhe`; 422 se `q` vazio ou > 40.
  - `GET .../erp-base/pedidos/{erp_codigo}` → `PedidoDetalhe` (404 se não houver estado).
  - `GET .../erp-base/painel` → `{"ultima_vigia_em", "fila_pendente", "proxima_janela", "ultimo_202_em", "recusados_30d", "bloqueio", "bloqueio_texto", "envio_manual": {"pode": bool, "motivo": str | None}, "envios": [LoteRegistro…20], "foto_hoje": {...} | None}`; `envio_manual.pode` usa as mesmas checagens de `pedir_envio_manual` sem gravar (função `runner.checar_envio_manual(cfg, agora) -> EnvioManualRecusado | None`, extraída na Task 5 se ainda não existir).
  - `POST .../erp-base/enviar-agora` → 202 `{"envio_id"}`; `EnvioManualRecusado` → 409 `{"detail": detalhe, "codigo": codigo}`; `quem` = `user.email`.
  - `GET .../erp-base/envio-manual/{envio_id}` → `resultado_manual` ou 404 `{"detail": "pedido não encontrado (o portal pode ter reiniciado); veja Últimos envios"}`.

- [ ] **Step 1: Testes** (padrão do arquivo, `TestClient` + bypass de admin já usado lá): cada rota 403 para não-admin; 404 ambiente desconhecido; `q` 422; busca com 1 e com 2 resultados; `painel` com `pode=False` e motivo quando sem credencial; POST 202 e então GET do id `pendente`; POST 409 `sem_credencial` com a mensagem da spec; GET id desconhecido 404 com o texto exato; **o POST não abre conexão com Fire nem Flow** (monkeypatch `FirebirdConnection` e `ClienteErpEventos` para explodirem se instanciados).
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** `$V/bin/pytest tests/test_erp_eventos_admin.py -q` → PASS. **Step 5:** commit `feat(web): rotas de consulta e envio manual ao Flow`.

### Task 8: Tela `/admin/envio-flow`

**Files:**
- Create: `app/web/static/admin-envio-flow.html`
- Modify: `app/web/server.py` (rota da página, igual a `/admin/ambientes`)
- Modify: `app/web/static/js/shell.js` (item "Envio ao Flow" no menu admin, `route: 'admin-envio-flow'`, mapa de rota)
- Modify: `app/web/static/admin-ambiente-edit.html` (passa a usar `bloqueio_texto` da API em vez do dict local, removendo a duplicação)
- Test: `tests/test_web_pages.py` (ou o teste que já cobre `/admin/ambientes` servindo HTML)

**Interfaces:**
- Consumes: Task 7.
- Comportamento (fiel ao mockup aprovado, `scratchpad/mockup-envio-flow/index.html`): seletor de ambiente no topo (ambientes com base configurada, padrão o do cookie `portal_env`); faixa de situação com os 5 números e o botão; "Consultar um pedido" (Enter ou botão; 1 resultado abre, vários mostram a lista, 0 mostra a mensagem de não encontrado); "Últimos envios" com "sinal de vida" para `eventos=0` e a linha da foto; diálogo próprio (não `confirm()`) com "Lê o Fire agora e manda tudo o que estiver na fila (N eventos já esperando)"; polling 2 s, para em `terminado`, em 404 ou em 5 min com as mensagens da spec; botão desabilitado com o motivo de `envio_manual.motivo`.
- Acessibilidade: foco visível, `aria-live` no status do envio, `role="dialog"` + `aria-modal` no diálogo, Esc fecha.

- [ ] **Step 1: Teste** — `GET /admin/envio-flow` sem cookie → redirect `/login`; com sessão → 200 HTML contendo `id="btn-enviar-agora"` e `id="q"`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar página + rota + menu + troca do `BLOQUEIOS`. **Step 4:** PASS; subir o portal local (`portless`, ou `python ui.py` com `PORTAL_ERP_VIGIA=0` e um SQLite de teste semeado) e conferir no Chrome os 4 estados da consulta e o diálogo; validação visual pela skill `hm-designer`. **Step 5:** commit `feat(web): tela Envio ao Flow`.

### Task 9: Documentação do módulo

**Files:**
- Modify: `docs/ai/modules/erp_eventos.md` (arquivos críticos: `consulta.py`, `erp_envio_lote_repo.py`; fluxo: pedido manual no tick; tabela `erp_envio_lote`)
- Modify: `docs/ai/modules/web.md` (3+2 rotas novas e a página)

- [ ] **Step 1:** atualizar só as seções afetadas. **Step 2:** suíte completa `$V/bin/pytest tests -q` (as 2 falhas conhecidas de `test_env_leak_guard` são pré-existentes; qualquer outra falha bloqueia) e `ruff check app/ tests/`. **Step 3:** commit `docs(ai): envio ao Flow por pedido`.
