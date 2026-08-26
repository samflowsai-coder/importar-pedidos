# Backlog — Portal de Pedidos

> **Fonte única do escopo aberto.** Só entra o que está **em aberto**: item fechado
> sai daqui e vive no histórico do git. Cada item diz o que é, onde dói e o que
> destrava.
>
> Último passe de verificação: **2026-08-26** (referências de código conferidas
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

### 2.7 Starvation acima de 500 candidatos na reconciliação Fire
`repo.list_parsed_for_reconcile` (`app/persistence/repo.py:529`) pega os 500 pedidos
`parsed` mais antigos (`ORDER BY imported_at ASC LIMIT 500`). Quem não casa no Fire
continua `parsed`, ocupando as mesmas 500 vagas para sempre — um pedido novo nunca
chega a ser tentado enquanto a fila estiver cheia de velhos que nunca casam, e nada
sinaliza esse starvation. Hoje com 308 pendentes é irrelevante (cabe tudo numa
página). **Fix, se doer:** rotacionar a fila (cursor avançando por `imported_at`
em vez de sempre pegar os 500 mais antigos) ou desistir de candidato "velho demais
sem casar" depois de N tentativas, liberando a vaga.

### 2.8 Fallback LLM não alcança formato de cliente novo — o genérico chega antes

Hoje o LLM só roda quando **todo** parser da cascata devolve `None`
(`app/pipeline.py`). O `GenericParser` não sobrescreve `can_parse` — herda o `True`
do `BaseParser` — e só devolve `None` quando não encontra item nenhum. Em qualquer
outro caso ele devolve um `Order`, **mesmo errado**, e o LLM nunca é chamado.

Efeito prático: cliente novo entra com dado errado em silêncio. Foi o caso da Daju
(o genérico extraía o pedido como `'DA'`, com quantidades erradas) e antes dele o do
Authentic Feet, que lia a coluna de cor como quantidade — ver `modules/parsers.md`.
O conserto, nos dois, foi escrever mais um parser dedicado. Enquanto isso não muda,
**cada cliente novo custa um parser**, e o custo aparece só quando alguém confere.

**Agravante confirmado em 2026-08-26 (Tennis Station):** não é preciso nem ser formato
novo. O parser do template EXISTIA e cobria o arquivo; a compradora digitou `TOTAL Kits`
em vez de `TOTAL KITS`, o `can_parse` era igualdade literal, e o genérico comeu o pedido
— 8.100 kits / R$ 120.882 viraram 12 unidades / R$ 0, com o rótulo `'OBSERVAÇÃO'` de
número de pedido. Uma letra de caixa diferente basta. O match do template virou
normalizado (ver `modules/parsers.md`), mas o buraco estrutural continua: **qualquer**
parser que erre o gate por um detalhe cai num genérico que devolve dado errado em vez
de devolver `None`.

`OrderValidator.validate` já detecta parte disso (número do pedido ausente,
`quantity <= 0`), mas devolve um `bool` que `pipeline.process` **descarta** — só sobra
warning no log, e o pedido segue para o preview como se estivesse bom. No caso da TS os
dois sinais estavam lá — número do pedido ausente **e** `'OBSERVAÇÃO'` como número —
e ninguém foi avisado.

Se o LLM chegar a ser chamado, ainda há duas limitações no caminho
(`app/llm/fallback_parser.py`): manda só `extracted["text"]` cortado em
`MAX_TEXT_CHARS = 8000`, **sem sinalizar o corte** (pedido longo perde itens em
silêncio), e descarta `rows`/`tables`. Em planilha isso é grave: o
`XLSExtractor._make_text` junta as células com espaço, então a estrutura de coluna —
justamente o que identifica o formato — se perde antes de chegar ao modelo.

**O que destrava:** usar o resultado do validator como gate (parse fraco do genérico
→ tenta LLM em vez de aceitar), mandar as linhas/tabela em vez do blob de texto, e
sinalizar truncamento. Custo continua zero nos formatos que já têm parser — o LLM só
entra onde hoje o dado sai errado de graça.

### 2.9 Pedido sem número entra no Fire com `PEDIDO_CLIENTE = NULL`

`app/erp/mapper.py:64` faz `pedido_cliente = (order.header.order_number or "")[:20] or None`.
Sem número não há **chave de idempotência** (o Fire deduplica por
`PEDIDO_CLIENTE` + `CLIENTE`) e a reconciliação não tem por onde casar. Não existe
edição de número no preview: `CommitRequest` só carrega `preview_id`
(`app/web/server.py:1564`), não há rota nem campo.

Acontece hoje, em produção, nos pedidos "Pulmão" do Grupo Afeet — e agora também na
Tennis Station quando o comprador não preenche `Ordem de compra`. No template antigo
(AF/MF) o número sai da `FANTASIA`, apelido digitado livre: é daí que vem o `AF76` vs
`AF076` que ficou aberto na reconciliação.

**O que destrava:** campo editável de número no preview (`CommitRequest` +
rota + UI), ou bloquear o commit de pedido sem número. Decisão do Samuel — hoje o
comportamento é aceitar em silêncio, com warning só no log.

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

### 5.1 Os campos do de-para intercompany estão preenchidos no cliente?
**Metade resolvido em 2026-08-24:** o cliente roda `20260824-1408` (commit `212e8cd`,
aplicado 24/08 14:10, confirmado na tela), que contém o de-para. A dúvida sobre a versão
acabou.

**O que segue aberto:** ninguém confirmou que `intercompany_cnpj` e
`intercompany_env_slug` foram preenchidos em `/admin/ambientes`. Precisa do share
`/Volumes/SamFlowsAI` montado ou da VPN para ler a tabela `environments`.
**Enquanto os dois campos estiverem vazios, a feature é inerte** — não muda nada em
produção.

### 5.2 A Daju funciona com OC real?
O parser subiu em `20260824-1408` e passa em 21 testes contra o sample. **Ninguém subiu
uma OC de verdade ainda.** Confirmar com a operação antes de considerar fechado —
especialmente quantidade e preço, que é onde este repo já errou (Magic Feet, Sam's).

### 5.3 O CNPJ da Tennis Station é escolha do comprador ou default?

`samples/PEDIDO TENNIS STATION.xlsx` traz `52.671.393/0001-69` no campo `CNPJ:` — e ele
é o **primeiro de uma lista escondida de 39 CNPJs** (11 raízes) nas colunas X+ da linha 6,
que é a fonte de validação do dropdown de filiais do grupo. Com um sample só não dá
para distinguir "o comprador escolheu" de "ficou o default". Se for default, o pedido
vai para a filial errada no Fire.

Todo o resto do cabeçalho veio em branco (razão social, ordem de compra, data), o que
reforça a hipótese de formulário pouco preenchido.
**O que destrava:** um segundo pedido real da TS, de preferência de outra filial.
