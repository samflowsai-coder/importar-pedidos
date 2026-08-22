# Backlog — Portal de Pedidos

> **Fonte única do escopo aberto.** Só entra o que está **em aberto**: item fechado
> sai daqui e vive no histórico do git. Cada item diz o que é, onde dói e o que
> destrava.
>
> Último passe de verificação: **2026-08-21** (referências de código conferidas
> contra a `main`; o que depende de sistema externo está marcado como não verificado).

---

## 1. Bugs conhecidos (código, verificados na `main`)

### 1.1 Hang do "Vincular produto" — sem timeout
`POST /api/imported/{id}/vincular-produto` re-roda `check_order` contra o Firebird de
forma **síncrona e sem timeout**. Se o Fire demora (latência de VPN, intermitência), o
botão fica em "Vinculando…" por 10s ou mais. No Windows do cliente, com Fire local, é
rápido — mas é fragilidade real e é o único desta lista que a operação (Grazi) pode
esbarrar no dia a dia.
**Fix:** timeout, ou tornar o re-check best-effort — o vínculo já foi gravado no SQLite
antes do re-check.
**Prioridade: alta.**

### 1.2 `poll_decisoes.py` — exceção no retry da revenda segura o cursor
`app/integrations/flowpcp/poll_decisoes.py:113` — se o retry pela chave da revenda
intercompany levantar, o código dá `return False` **sem contar tentativa**. Com o
Firebird fora do ar, a decisão nunca confirma e **segura o cursor** do poll.
**Fix:** contar tentativa também no caminho de exceção.

### 1.3 `poll_fire.py:67` — `conn.execute` não existe em `fdb.Connection`
`app/worker/jobs/poll_fire.py:67` chama `conn.execute(...)` (só existe em `Cursor`) e
indexa `row["STATUS"]` numa tupla. Um commit, mesmo padrão do fix `fea7ee7`.
**Por que ainda não estourou:** `list_pending_for_fire_poll` exige
`fire_codigo IS NOT NULL`, e hoje é sempre NULL (o cliente é XLS-only). **Não** é por
`FIRE_TRIGGER_STATUS` vazio — o trigger só é lido depois do crash. Quebra a cada 60s no
dia em que alguém usar "Cadastrar no Fire" com sucesso.

### 1.4 Cool-down do de-para de cliente arma largo demais
`app/erp/depara_cliente.py` — o cool-down de 45s arma no bloco inteiro (`to_fb_config`
+ `connect` + `execute` + `fetch`). Um erro que **não** é de conexão (linha ruim,
charset) suprime o de-para do ambiente inteiro por 45s.
**Fix:** armar só em volta do `connect_with_config`.

---

## 2. Dívida e melhorias

### 2.1 De-para de produto não vale no caminho de inserção direta ("I1")
`app/erp/depara_apply.py` está ligado só em `_export_one_xlsx` (modo `xlsx`), **não** em
`_send_one_to_fire` (modo `db`/`both`). Um item casado só por vínculo de-para entra no
Fire sem FK de produto. Só importa se ligar o insert direto — é pré-requisito daquela
decisão (ver §4.1).

### 2.2 Undo total do vínculo na UI
"Revincular" cobre o dia a dia (upsert last-write-wins). Remover um vínculo de vez
exige expor o `depara_id` — nenhum payload devolve hoje. A rota
`DELETE /api/produtos/depara/{depara_id}` já existe.

### 2.3 Parser: linhas "CNPJ:" viradas item
~5 linhas com `CNPJ:` sendo interpretadas como item. Ruído, sem impacto de dado.

### 2.4 Minors do de-para de produto
- `_norm_key` promover a público (virou contrato cross-module).
- Ownership-check no `delete_depara` (IDOR dentro do mesmo ambiente).
- Órfão-check no `depara_apply` (confia no `fire_codigo` sem revalidar o SEQ).

### 2.5 `.env.example` desatualizado
Traz 4 chaves das ~24 que o código lê, e uma delas (`LOG_DIR`) não é lida por ninguém.
Quem instala do zero não sabe que `APP_DATA_DIR`, `EXPORT_MODE`, `PORTAL_*` e os `FB_*`
existem. A lista correta está no `CLAUDE.md`.

### 2.6 `app/sync/` vazio
Só resta `__pycache__`. Ou remover o diretório, ou explicar o que era.

---

## 3. Bloqueado em terceiros

### 3.1 Ajuste do cadastro de produtos da MM Confecção — **parado 2026-08-21**
Extrações prontas e enviadas; **Grazi e Bianca** ficaram de conferir. Não avançar em
ajuste de cadastro antes do retorno. Lacunas mapeadas: tabela de preço morta
(`TABELA_PRECO_PRODS` cobre SEQ 238–615, o que vende hoje é 1771–2040), `UNIDADE='KIT'`
sem composição, `CODSUBGRUPO` 100% vazio no `.4`, EAN-13 em 52%.
Pergunta aberta pro time: a queda de volume jun→ago (116.255 → 6.077 → 1.859 unidades,
com nº de pedidos quase igual) é sazonalidade ou pedido grande não lançado?

### 3.2 Tela de reconciliação no Flow (Fatia 1 §4.6)
"Quem olha quando não casa": mostrar o resultado do último promote (criados,
atualizados, divergências flow-only, ambíguos, erros por item). Rota candidata a
confirmar **no build do Flow, sem inventar**. RBAC: leitura `produtos.read`, ações
`produtos.write`; registrar no `screen-registry.ts`. O motor já popula sem ela — é
observabilidade, não bloqueio.

### 3.3 Fatia 2 — botão "Sincronizar" dentro do Flow
Só necessária para disparar o sync **de dentro do Flow**; hoje o importador força
sozinho (`--promover` ou botão na tela de ambiente). Precisa de migration nova
(`importador_comandos`) + `GET /comandos` + server action.
**Depende de:** aprovação da migration pelo Samuel.

### 3.4 Limpar os 181 `AME-` legado do catálogo MM no Flow
Resquício do seed Americanense errado de 2026-07-14 (banco e decisão já revertidos;
produção é o `.7`). `codigo LIKE 'AME-%'`, `ativo=false`, `fonte='legado_americanense'`,
`fire_produto_id=NULL`. Checar referências antes de deletar (`produto_componentes`,
`pedido_items`, `ordens_producao`, `produto_codigos_cliente`) — o DELETE atômico já
falhou antes por FK. **Urgência baixa:** inativos e sem `fire_produto_id`, não afetam
match de pedido nem de catálogo.

### 3.5 Cliente criado automático no Flow nasce sem marca
`resolverClienteId` (pcp-app) insere só `{tenant_id, nome, cnpj}` — sem `grupoCodigo`.
Mandar o CNPJ certo faz o Flow achar ou criar o cliente, mas **a marca fica vazia**, que
é justamente o ponto do de-para para o chão de fábrica. A carga de clientes não resolve:
`CODGRUPO` é NULL em 100% do `CADASTRO` do Fire (registrado em `app/erp/queries.py`).
**Decisão:** puxar a marca de outra fonte do Fire, ou classificar manual no Flow.

### 3.6 Pedidos Nasmar já enviados continuam como Nasmar
`/recebimento` é insert-only e deduplica por `externalId` — re-enviar não conserta.
Corrigir o histórico exige patch do lado do Flow, mesmo padrão do
`tools/reprocessar_prazos_flow.py`. **Escopo:** os que estavam abertos no `.7` no
momento do corte.

---

## 4. Decisões pendentes (aguardam o Samuel)

### 4.1 Insert direto no Fire para pedidos 100% match
Código do `FirebirdExporter` já existe; é ligar com trava. Risco central: escrever na
produção do ERP é irreversível, ao contrário do XLS. Trava proposta: opt-in + canário +
modo `both` (XLS de backup) + cair para o XLS na dúvida. **Exige corrigir o §2.1 antes.**
Próximo passo se aprovado: brainstorm → spec.

---

## 5. Verificação pendente (não dá para afirmar hoje)

### 5.1 O cliente já roda a versão com o de-para de cliente?
O pacote `dist/portal-pedidos-20260725.zip` **contém** o de-para (verificado), mas não
há como afirmar que foi aplicado no cliente, nem que `intercompany_cnpj` /
`intercompany_env_slug` foram preenchidos em `/admin/ambientes`. Precisa do share
`/Volumes/SamFlowsAI` montado ou da VPN para checar `data/applied_update.json` + a
tabela `environments`.
**Enquanto os dois campos estiverem vazios, a feature é inerte** — não muda nada em produção.
