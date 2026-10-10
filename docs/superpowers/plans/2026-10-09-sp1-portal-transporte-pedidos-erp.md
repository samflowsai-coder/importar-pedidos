# SP1 (lado Portal) — Transporte comum e pedidos abertos do ERP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** o Portal lê os pedidos abertos de cada base do ERP (Fire, na MM: `mm` = `.7` produtora, `nasmar` = `.4` intermediária), transforma cada mudança num evento `pedido.evento_erp.v1` com `id` estável e `seq` contíguo, entrega ao Flow em lotes seguros e manda uma foto noturna por base — só lendo o ERP.

**Architecture:** uma thread do processo web (`app/erp_eventos/runner.py`) roda, por base e sob trava, vigia (15 min) → drenagem → foto. A vigia lê o ERP numa transação READ ONLY + SNAPSHOT, calcula o diff contra `erp_pedido_estado` com uma função pura e grava estado + marca do `seq` + eventos numa única transação SQLite. A drenagem manda lotes em série com credencial própria por base, casa a resposta por `id` e persiste espera/bloqueio. O vínculo exato nasce na origem: no INSERT (modo `db`) ou pela planilha que o Portal gerou (modo `xlsx`, o de produção).

**Tech Stack:** Python 3.11, pydantic v2 (strict), SQLite (WAL, um banco por ambiente), `fdb` (produção) / `firebirdsql` (medição no Mac), httpx via `OutboundClient`, pytest.

**Spec:** `GestorProduction/pcp-app/.worktrees/origem-dos-dados/docs/superpowers/specs/2026-10-03-sp1-transporte-pedidos-fire-design.md` (v6.1) — §3, §3A, §4, §7, §10.3, §11, §12. Outro lado do contrato: `…/plans/2026-10-09-sp1-flow-transporte-pedidos-fire.md` (Tasks 2, 3, 5, 6). Padrão: `…/docs/ai/padroes/produto.md` (P-PROD-08). Lote semanal: `docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md` (diretriz 11).

## Global Constraints

- **ERP só leitura.** Todo SQL novo é SELECT (`queries.py`, bloco "SP1"), lido em `FirebirdConnection.connect_somente_leitura` (TPB `isc_tpb_read` + `isc_tpb_concurrency`). Medição nova só por `tools/erp_medir.py` (recusa não-SELECT, transação READ ONLY). Nenhum UPDATE novo; o único UPDATE que o Portal já fazia (`UPDATE_DT_ENTREGA`) não muda.
- **P-PROD-08.** Tabela, coluna, chave e valor novos: `erp_*`, `base`, `produtora`, `intermediaria`. Bases e papéis são configuração (`erp_bases`), nunca enum/CHECK. `imports.fire_codigo` e afins ficam.
- **Credencial por base**, `Authorization: Bearer fpe_…` + `X-Tenant-Id`, cifrada por (ambiente, base) em `erp_bases.token_enc`. Não mexe em `flowpcp_service_token_enc`.
- **`base` em todo evento e toda foto**, igual à da credencial.
- **Tipos estritos** (contrato §4.1): número JSON, códigos inteiros ≥ 0, CNPJ 14 dígitos ou null, data `YYYY-MM-DD`, instante ISO com fuso (`+00:00`), todo campo presente (null em vez de omitir), limites de tamanho. Validado por `app/erp_eventos/contrato.py` antes de entrar na fila.
- **Lote:** ≤ 500 eventos e ≤ 2 MB (conta sobre 1,8 MB do JSON compacto); lote vazio a cada vigia de cada base.
- **Ritmo:** ≥ 3 s entre POSTs por (tenant, rota) — 20/min, abaixo do teto de 30/min por tenant do Flow (plano do Flow, Task 4). Vale para a primeira carga.
- **Integração fora do request path:** nada de HTTP ao Flow numa request de usuário; gravações na origem (vínculo, linhas do XLSX) são best-effort e nunca derrubam o export.
- **Sem deploy, sem escrita no ERP de produção, sem push na `main`.** Branch `feat/sp1-portal-transporte-pedidos-erp`, base `feat/log-leituras` (a linha que está em produção; a `main` está em 20c9784, de 12/09).
- **Comandos:** `.venv/bin/python -m pytest tests/test_erp_eventos_*.py -v` por tarefa; suíte completa antes do commit; `ruff check app/ tests/`.

## Ajustes à spec (decididos nesta sessão)

1. **Vínculo exato em dois caminhos (Samuel, 2026-10-09).** A spec (§2, §3.5) supõe que o Portal faz o INSERT no ERP. Em produção roda `EXPORT_MODE=xlsx` (o modo `db` nunca rodou e está bloqueado pelo BACKLOG 1.5): quem grava é o importador de Excel do ERP, e o XLSX sai dividido por loja. Então: caminho **insert** (modo `db`) + caminho **xlsx** (casamento por posição com as linhas que o Portal gerou, conferido contra o payload do `/recebimento`). Ambíguo = sem vínculo.
2. **Índice por identidade, não por contador.** O exportador insere agrupado por local de entrega; `indiceOriginal` é a posição em `order.items` (a mesma do array `itens` do `/recebimento`, `mapper.py`).
3. **Lote fiscal: emissor pronto, sem produtor (Samuel, 2026-10-09).** O consolidador (Fase 2 do roteamento) não existe; `intercompany_lote`/`fire_codigo_espelho` não existem no código (a spec os chama de "existentes"). Entregue: `reservar_lote` → INSERT (Fase 2) → `registrar_lote_fiscal`, com a vigia segurando pedido novo da produtora para intermediário enquanto houver reserva. Pernas por item em `erp_lote_perna` (PK = item de origem: um item, um lote). Pelo desenho da Fase 2 (soma por produto/data/preço, um pedido numa janela), um item de origem nunca se divide entre lotes.
4. **Releitura completa a cada vigia** em vez de `ULT_ALT_DTHR > última leitura` (§11): nada prova que `ULT_ALT_DTHR` muda com item ou nota fiscal; ler tudo custa 0,3 s para 323 pedidos (cópia local).
5. **"Aberto" = STATUS fora de FATURADO/CANCELADO**, a mesma regra de reabertura do Flow. A cópia tem `EM ANÁLISE` (1 pedido, 2022): entra como aberto.
6. **Acordos de contrato com o Flow (pcp-app, 2026-10-09):** `codProduto` nulo vai no envio (o Flow abre pendência; o pedido não fica fora); estorno de faturamento sai com `codNf: null`.
7. **Eco sem marca de "já ecoou":** uma decisão pode tocar N pedidos (a chave `PEDIDO_CLIENTE + CLIENTE` não é única, BACKLOG 1.5); correlaciona por (base, CODIGO tocado, data nova) em 7 dias. `flowpcp_decisoes_mapping` ganha `base`, `erp_codigo` (quando 1), `erp_codigos_json`, `nova_data`, `aplicada_em`.
8. **Rejeitado é terminal na fila.** Corrigir e reenviar é seguro do lado do Flow, mas reenviar o mesmo corpo é laço; a foto seguinte refaz a base. 400/401/4xx bloqueiam a base até ação humana (tela).

## Review Focus

1. **Pedido com mais de 499 itens** → a unidade não cabe num lote; a base é bloqueada com motivo `unidade_grande` e aparece na tela (nunca corta o pedido). Medido: máximo 45 itens. Teste: `test_unidade_maior_que_o_lote_e_erro_explicito`.
2. **Cliente com CPF ou CNPJ sujo no cadastro** → `clienteErpCnpj: null`, nunca 11 dígitos. Teste: `test_leitor_monta_pedido_com_faturado_acumulado_e_cnpj_so_de_14_digitos`.
3. **XLSX regerado depois de editado** → `erp_xlsx_linha` é substituído; o vínculo só nasce se a quantidade bate com o que o `/recebimento` levou. Teste: `test_casar_novos_exige_que_o_recebimento_tenha_levado_a_mesma_quantidade`.
4. **Credencial trocada no Flow (401)** → base bloqueada, sem laço; salvar credencial nova libera. Testes: `test_401_bloqueia_e_nao_reenvia`, `test_credencial_nova_tira_o_bloqueio_de_401…`.
5. **Pedido com status fora do contrato (> 40 caracteres)** → fica de fora sem consumir `seq`; a foto do dia não é criada (uma foto sem ele diria ao Flow que saiu de aberto). Testes: `test_pedido_fora_do_contrato_fica_de_fora_sem_consumir_seq`, `test_foto_com_pedido_fora_do_contrato_nao_e_criada`.

---

## Fase 0 — Portões

### Task 0: Medições e aprovação de schema
- [x] **Schema novo (SQLite, por ambiente):** `erp_item_vinculo`, `erp_xlsx_linha`, `erp_pedido_estado`, `erp_seq_marca`, `erp_eventos_saida`, `erp_envio_estado`, `erp_foto`, `erp_foto_pagina`, `erp_lote_reserva`, `erp_lote_perna`; colunas novas em `flowpcp_decisoes_mapping`. **Compartilhado:** `erp_bases`. Todas `CREATE TABLE IF NOT EXISTS`/`ADD COLUMN` (o padrão do repo: aplicadas no primeiro `connect`). Nenhuma FK para `imports`. → **apresentar ao Samuel no PR** (guard "mudança de schema").
- [x] Medição de contrato na cópia local (abril, `.7`): 323 abertos, 2.875 itens, 0 sem `CODPRODUTO`, 0 sem `QTD`, 0 clientes sem CNPJ de 14, máx. 45 itens/pedido, descrição ≤ 71, unid ≤ 4, pedido_cliente ≤ 18, 20 itens com nota parcial.
- [ ] **M1** (produção, só leitura): ordem do importador de Excel (`CORPO_VENDAS.CODIGO` crescente na ordem da planilha?) e preço copiado da planilha? — liga `vinculo.COMPARAR_PRECO`.
- [ ] **M2** (produção, `HISTORICO`): edição de item preserva `CORPO_VENDAS.CODIGO`?
- [ ] **M5**: cobertura do `produtoProdutor` (precisa do `.4` real; os dois `.fbk` da Confecção em `bkp fire/` estão incompletos).
- [ ] **P0 (produção):** confirmar se o outbox `flowpcp` é drenado no Windows da MM (só o `ui.py` sobe; o drain e o `poll_decisoes` estão só no worker). Sem isso, não há `/recebimento` nem eco em produção.

## Fase A — Contrato e núcleo puro

### Task 1: Serialização canônica e `id`
**Files:** Create `app/erp_eventos/canonico.py`; Test `tests/test_erp_eventos_canonico.py`
**Produces:** `id_evento(*, tenant, base, tipo, erp_codigo, erp_item_codigo, campo, antes, depois, observado_em) -> str`, `decimal_canonico`, `formatar_instante`, `texto_canonico`, `numero_json`.
- [x] Vetores fixos (4 hashes), equivalência `Decimal('1000.0000') == 1000 == 1000.0`, base no hash, 10→20→10→20 = 4 ids, dois itens = ids distintos, recusa do separador.

### Task 2: Espelho do contrato
**Files:** Create `app/erp_eventos/contrato.py`; Modify `pyproject.toml` (N815 no arquivo); Test `tests/test_erp_eventos_eventos.py`
**Produces:** `validar_evento(dict)`, `validar_pagina(dict)`, `ContratoInvalidoError`.
- [x] Um modelo por `tipo` (`extra="forbid"`, `strict=True`): número como string recusado, campo omitido recusado, limites, `lote_fiscal` com pernas, página com `pagina ≤ totalPaginas`.

### Task 3: Modelo, diff e eventos
**Files:** Create `app/erp_eventos/modelo.py`, `mudancas.py`, `eventos.py`; Tests `tests/test_erp_eventos_mudancas.py`, `tests/test_erp_eventos_eventos.py`
**Produces:** `PedidoErp`, `ItemErp`, `para_estado/de_estado`, `calcular_mudancas(anterior, *, anterior_excluido, atual) -> list[Mudanca]`, `montar_eventos(...) -> list[EventoFila]`, `pedido_json`, `item_json`.
- [x] Ordem: visto+itens; reabertura primeiro; data; removido/incluído; qtd → preço → faturamento; encerramento por último; excluído; reaparecido.
- [x] `codProduto` null vai; estorno com `codNf` null; correlação só no evento de data.

### Task 4: Corte de lote e leitura da resposta
**Files:** Create `app/erp_eventos/lotes.py`; Test `tests/test_erp_eventos_lotes.py`
**Produces:** `proximo_lote`, `dividir_lote`, `ler_resultados`, `decidir`, `espera_crescente`, `UnidadeGrandeDemaisError`.
- [x] Nunca parte unidade; bytes; vazio; 413 divide; resposta por id (fora de ordem, id vazio, id alheio); 202/413/401/400/404/5xx/rede; `Retry-After` em segundos e data.

## Fase B — Persistência e leitura do ERP

### Task 5: Schema e repositórios
**Files:** Modify `app/persistence/schema_env.py`, `schema_shared.py`; Create `erp_bases_repo.py`, `erp_estado_repo.py`, `erp_vinculo_repo.py`, `erp_foto_repo.py`
- [x] Repos recebem `conn` e não dão commit (a vigia decide a transação). `avancar_seq` recusa voltar. `enfileirar` é INSERT puro (id repetido derruba a transação).
- [x] `erp_bases`: validação (base, papel, UUID, https, hora, formato `fpe_`), token cifrado, `None` mantém / `""` limpa, base ligada sem credencial não roda (log).

### Task 6: Leitura só leitura
**Files:** Modify `app/erp/connection.py` (`connect_somente_leitura`), `app/erp/queries.py` (bloco SP1); Create `app/erp_eventos/leitura.py`; Test `tests/test_erp_eventos_origem.py`
- [x] Todo SQL do bloco começa com SELECT (teste estático). Leitor: blocos de 200, faturado acumulado de `CORPO_VENDAS_NFS`, CNPJ só 14 dígitos.

### Task 7: Cliente real em lote
**Files:** Modify `app/erp/depara_cliente.py` (`resolver_clientes_reais`)
- [x] Mesma regra (`_decidir`), cache positivo e cool-down do resolvedor unitário; uma conexão só leitura para N chaves.

## Fase C — Vigia, vínculo, eco, lote fiscal

### Task 8: Vigia
**Files:** Create `app/erp_eventos/vigia.py`; Test `tests/test_erp_eventos_vigia.py`
**Produces:** `vigiar(cfg, *, leitor, agora, resolver_clientes, resolver_produtores, casar_vinculos) -> ResultadoVigia`, `enriquecer(...)`.
- [x] Primeira vigia; sem mudança; qtd continua o seq; **seq perene após apagar o estado**; saída de aberto por releitura (faturamento antes do status); excluído e reaparecido; reabertura; mesmo `CODIGO` nas duas bases; fora do contrato sem consumir seq; reserva de lote segura intermediário; eco com `correlacaoId`.

### Task 9: Vínculo na origem
**Files:** Modify `app/exporters/erp_exporter.py` (`linhas_por_arquivo`, `_planilhas`), `app/exporters/firebird_exporter.py` (`_to_erp_rows_indexados`, `FirebirdExportResult.itens`), `app/web/server.py` (3 ganchos best-effort); Create `app/erp_eventos/vinculo.py`
- [x] Índice por identidade com lojas intercaladas; linhas alinhadas com os arquivos do `export`; casamento único/ambíguo/divergente/outro cliente; número truncado em 20; exige quantidade igual ao `/recebimento`; `registrar_insert`.
- [x] As rotas continuam chamando `ERPExporter.export` (os dublês dos testes existentes espiam esse método).

### Task 10: produtoProdutor
**Files:** Create `app/erp_eventos/produtor.py`
- [x] Produtora: o próprio `CODPRODUTO`. Intermediária: documento do item vinculado → catálogo da produtora por EAN, depois código, exato e único; None tenta de novo em 24 h.
- [x] Teste com catálogo dublê (`tests/test_erp_eventos_produtor.py`); a cobertura real é a M5.

### Task 11: Eco
**Files:** Create `app/erp_eventos/eco.py`; Modify `app/erp/fire_update.py` (`codigos_da_chave`, `data_no_fire`), `app/integrations/flowpcp/poll_decisoes.py` (`erp_base`), `app/worker/jobs/poll_flowpcp.py`
- [x] Registro só quando a base tem SP1 configurada; `update_dt_entrega` intocado (testes existentes seguem iguais).

### Task 12: Lote fiscal (emissor)
**Files:** Create `app/erp_eventos/lote_fiscal.py`
- [x] Sem reserva = erro; entra sem seq e libera a reserva; item de origem num lote só; reserva abandonada (> 1 h) não segura.

## Fase D — Envio, foto, thread, tela

### Task 13: HTTP, ritmo e drenagem
**Files:** Create `app/erp_eventos/envio.py`, `drenagem.py`; Test `tests/test_erp_eventos_drenagem.py`
- [x] Lote vazio (sinal de vida); nunca corta pedido; id sem resultado reenviado com os mesmos bytes; resposta fora de ordem; 401 bloqueia; 429 espera; 413 divide; Bearer + tenant + envelope; rede fora; ritmo.

### Task 14: Foto noturna
**Files:** Create `app/erp_eventos/foto.py`, `app/persistence/erp_foto_repo.py`
- [x] Páginas ≤ 200 pedidos / ≤ 4,5 MB, total constante, uma vazia quando não há aberto, fora do contrato = sem foto, uma por dia após a hora; envio em série; 24 h → expirada.

### Task 15: Runner e tela
**Files:** Create `app/erp_eventos/runner.py`; Modify `app/web/server.py` (startup), `tests/conftest.py` (`PORTAL_ERP_VIGIA=0`), `app/web/routes_environments.py`, `app/web/static/admin-ambiente-edit.html`; Test `tests/test_erp_eventos_admin.py`
- [x] Rotas `GET/PUT /api/admin/environments/{id}/erp-base` e `POST …/erp-base/desbloquear` (admin); credencial nunca volta; 422 em config inválida; credencial nova tira o bloqueio de 401.
- [ ] Validação de design da tela (hm-designer).

### Task 16: Ferramenta de medição e documentação
**Files:** Create `tools/erp_medir.py`, `docs/ai/modules/erp_eventos.md`; Modify `docs/ai/00-index.md`, `CLAUDE.md`

## Fase E — Ponta a ponta local (com a sessão do Flow)

### Task 17: Ambiente
- [x] Firebird 2.5 (mesmo motor da produção) em container, porta 3056, cópias de trabalho em scratch; originais intocados. `mm` = `BKP_MM_AMERICANENSE_TERCA.fbk` (abril); `nasmar` = segunda cópia do mesmo `.fbk` (os `.fbk` da Confecção estão incompletos) — de quebra, `CODIGO` iguais nas duas bases.
- [x] Vigia real contra a cópia: 323 pedidos → 3.198 eventos, 0 fora do contrato, 0,3 s.
- [ ] Flow local (`feat/sp1-erp-eventos`, Supabase DEV, tenant `dddddddd-…`) com credenciais `fpe_` das duas bases.

### Task 18: Roteiro
- [ ] (i) lote vazio por base → 202 e sinal de vida; (ii) primeira carga das duas bases no ritmo; (iii) mudanças nas CÓPIAS (QTD, DT_ENTREGA, STATUS → FATURADO/CANCELADO/reabertura, item novo e removido, nota em `CORPO_VENDAS_NFS`, exclusão) → linha do tempo conferida no Flow; (iv) `lote_fiscal` sintético antes do consolidado; (v) foto das duas bases; (vi) reenvio idempotente (derrubar a resposta) e 401 com token errado.

## Execução — modelo e esforço

Implementação nesta sessão (loop do Samuel). Revisão por **Fable** antes de cada commit + push + PR; revisão da branch inteira antes de pedir merge.
