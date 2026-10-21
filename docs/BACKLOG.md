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

### 1.5 Idempotência do modo `db` barra pedido legítimo
`CHECK_ORDER_EXISTS` (`app/erp/queries.py`) trata `PEDIDO_CLIENTE + CLIENTE` como chave
única, e ela não é: na Fire viva (21/09/2026), **198 de 324** pedidos de 2026 da Nasmar
e **276 de 1137** da MM repetem a chave de outro pedido. O código de loja da H2S4 volta
todo mês (`AW064` 8x, `AF203` 6x), e a MM digita texto livre (`NASMAR` 12x). Dormente
porque produção roda `EXPORT_MODE=xlsx`; no dia em que ligar `db`, o segundo pedido
legítimo da loja vira `FirebirdOrderAlreadyExistsError`.
**Bloqueia `EXPORT_MODE=db`.** Fix: a chave precisa de mais uma perna (data do pedido
ou hash do arquivo), ou a idempotência sai do Fire e fica no portal.

---

## 2. Dívida e melhorias

### 2.1 ~~De-para de produto não vale no caminho de inserção direta ("I1")~~ — RESOLVIDO 2026-10-17
Branch `fix/depara-fire-direto`. O insert direto aplica o de-para e busca o item vinculado
pelo SEQ (o `fire_codigo` é `str(SEQ)`; relido como `CODPROD_ALTERN` casaria outro produto).
Item sem produto no Fire passou a recusar o pedido (`PRODUCT_NOT_FOUND`) em vez de entrar
com `CODPRODUTO` NULL. Detalhe em `docs/ai/modules/erp.md`.

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

### 2.9 Número do pedido editável no preview

Pedido sem número no documento recebe `SN-<hash>` do pipeline e o preview mostra o
selo "Gerado pelo portal" (ver `docs/ai/modules/pipeline.md`). Falta a operadora poder
**trocar** pelo número real quando o cliente informar: `CommitRequest` só carrega
`preview_id` (`app/web/server.py`), não há rota nem campo. Guardar cada troca como par
(arquivo, número) — é o dado que diz se o parser devia ter achado o número.

### 2.10 Reconciliação com chave repetida
Mesmo dado do 1.5: `PEDIDO_CLIENTE + CNPJ` casa N pedidos do Fire quando é código de
loja (`AF198` todo mês). Hoje a guarda de 90 dias e `_escolher_representante` escolhem
um. **Fix:** desempatar por data e total, e depois do 1º casamento gravar o `CODIGO`
do Fire no import e parar de depender do número.

### 2.11 Dedup por hash só existe no worker
`imports.file_sha256` só é checado em `app/worker/jobs/scan_environments.py`. Upload
web e lote aceitam o mesmo arquivo de novo sem aviso. Barato de portar; independe do
número do pedido.
**Mesma raiz:** lote (`server.py`, `_guardar_original(src.read_bytes())` e depois
`_process_file` lê de novo) e worker (`_sha256(p)` em chunks, depois `p.read_bytes()`)
leem o arquivo **duas vezes**. Arquivo ainda sendo copiado no share dá `file_sha256`
diferente do que foi parseado — e o `SN-<hash>` deixa de ser o prefixo dele. Fix: ler
uma vez e passar os mesmos bytes para a guarda, o hash e o parse.

### 2.12 Desmembramento NBA sai com texto como número
`PEDIDO NBA 3.xlsx` vira `order_number = 'NBA DEZEMBRO'` no `DesmembramentoXlsParser` —
mesma classe do FANTASIA-nome corrigido no template (2026-09-21): texto repetível vira
PEDIDO_CLIENTE e xPed da nota.

### 2.13 Texto em `--warn` abaixo do AA
`--warn` (#D97706) mede 3.05:1 sobre `--surface`. O `.badge-warn` passou a usar
`--warn-ink` (6.27:1), mas `index.html` ainda usa `var(--warn)` como cor de texto em
`.toolbar-path.missing`, no aviso "item(s) sem match no Fire" e no status `parsed`.
No celular, o rodapé do modal "Revisar pedido" quebra palavra por palavra.

### 2.14 Preview não avisa quando itens diferentes dividem o mesmo código
O pedido 4932 entrou com 12 linhas e 2 códigos, e nada no caminho percebeu:
`product_check` deduplica os códigos num `set` e apaga a pista. Guarda barata e
independente de parser: avisar no preview quando N itens com descrições diferentes
têm o mesmo `product_code`. É a rede para o próximo parser que gravar o modelo no
lugar da variante.

### 2.15 Troca de produto pelo importador de Excel do Fire — aviso no portal feito, restam 3 pontas

> **Resolvido com `produto_chave_modo=ligado` (2026-10-20, branch `feat/produto-chave-exata`):**
> a planilha leva o `CODPROD_ALTERN` do produto decidido e a trava recusa
> `troca_no_fire`/`sem_codigo_fire` em vez de só avisar (`docs/ai/modules/erp.md`, "Casamento
> por chave exata"). Em `desligado`/`observando` o aviso e as pontas abaixo continuam valendo;
> o chamado com a Fire Sistemas segue útil para confirmar a previsão.

O aviso (`troca_no_fire`) foi implementado em 24/09/2026 — detalhe em
`docs/ai/modules/erp.md`. O defeito de origem continua sendo do Fire (casa por
prefixo, primeiro da ordem física); medido em 24/09: 33 trocas na MM, 5 na Nasmar,
mais os códigos duplicados (`5041G` nas duas). Pontas abertas:

- **Chamado com a Fire Sistemas** (mensagem pronta, a MM envia). Quando responderem,
  revisar a previsão: ela reproduz o comportamento medido, não o código deles.
- **De-para exporta SEQ como código.** `fire_codigo` = `str(SEQ)`. Não sei se o
  importador do Fire tenta SEQ exato antes do prefixo de `CODPROD_ALTERN`. Prova
  barata: importar no Fire uma planilha com 1 item vinculado e ver o produto
  gravado. Na mesma importação, 1 item com EAN certo e código que colide por
  prefixo com outro produto responde se o Fire usa o EAN antes. Até lá, esses
  dois casos saem do aviso como `conferido: false` ("confira"), nunca "troca certa".
- **Código com mais de 30 caracteres derruba o check inteiro.** A query de match
  exato (`find_products_by_codes_sql`, `IN (...)`) estoura "string right
  truncation" (coluna `VARCHAR(30)`) e o preview mostra "check indisponível". Bug
  antigo; a query nova do aviso já filtra.
- Desmembramento AF/MF: nos samples, 0 de 570 / 276 itens casam no portal e o
  Fire casaria todos por prefixo (`AFK2E-L-` → `AFK2E-L-100-3338`). O aviso agora
  mostra isso; a causa (código truncado no parser do desmembramento) não foi
  investigada.

### 2.16 Template de kits ignora `DATA DE FATURAMENTO` (mês de entrega)
Achado em 05/10/2026 no lote Authentic Feet (AF072/AF082/AF108/AF119): as planilhas
NOVEMBRO e DEZEMBRO de cada loja só diferem na célula `DATA DE FATURAMENTO` e o
`NasmarTemplateParser` não a lê (`delivery_date = None` nos dois). O pedido de cada mês
sai idêntico (mesmo número, CNPJ e itens), a operadora corrige a data à mão e o 2º
pode bater em "pedido já existe" (1.5). Fix: ler o rótulo `DATA DE FATURAMENTO` do bloco de
cabeçalho para `delivery_date` dos itens, e decidir como o mês vira data (1º dia? último?).
**Decisão do Samuel/MM:** qual data o Fire espera para "DEZEMBRO".

### 2.17 Pedido lido sem CNPJ ou com item "CNPJ:" passa sem alarme
O pipeline descarta o retorno do validador e o preview deixa seguir um pedido sem
cliente/CNPJ. Em 05/10/2026 isso deixou o `GenericParser` entregar lixo à operadora. A
entrega 1 do log de leituras só **registra** os sinais (`suspeito.sinais`); decidir se o
preview passa a avisar o operador ("este arquivo caiu no leitor genérico, confira") e/ou
bloquear o commit sem CNPJ. Depende de algumas semanas do log em produção para calibrar
os falsos positivos.

### 2.18 Log de leituras, entrega 2
Eventos pós-leitura no mesmo `logs/leituras.jsonl` (commit, export xlsx, envio ao Fire,
bloqueio), campo de roteamento (`modo`, `degrau`, `env_sugerido`, `env_gravado`) e
`sha_guardado` vs lido (2.11). Spec: `docs/superpowers/specs/2026-10-05-log-de-leituras-design.md`,
seção "Fora desta entrega".

### 2.19 Trava entre processos por ambiente na volta de cadastro
A volta Flow → Fire (`docs/ai/modules/flowpcp.md` §5) assume **um consumidor por ambiente**
(a thread `flowpcp-envio`); nada no código impede dois processos do portal no mesmo
ambiente (o lock de gravação `cadastro_fire._lock` é só do processo). Hoje a proteção é
indireta: recuperação só depois de 15 min e leitura bloqueante (NO_REC_VERSION). Dois
consumidores ainda poderiam reservar o mesmo item ou o mesmo `MAX(SEQ)+1`.
**Fix:** trava por ambiente (arquivo com lock do SO ou linha de lease no `app_shared.db`
com validade) tomada no começo do ciclo de gravação e da recuperação. Medir antes se a
situação acontece de fato (reinício do serviço com o processo antigo ainda vivo).

### 2.20 Casamento de produto por chave exata — pontas deferidas
Feature em `feat/produto-chave-exata` (spec `docs/superpowers/specs/2026-10-19-casamento-produto-chave-exata-design.md`).
Itens abaixo foram vistos nas revisões e deixados de fora de propósito; nenhum bloqueia ligar `observando`.

**Fora do Portal / lacunas de contrato**
- **Flow recebe `erpProdutoId` (Task 9 adiada).** O Portal ainda não manda `erpProdutoId`/`confiancaParser` por item
  ao Flow (o Flow não consome e a IA dele já saiu). Ligar o item pelo `fire_produto_id` e nunca casar código ou EAN de
  pedido vindo do Portal é plano do pcp-app; depois o Portal passa a mandar (digit string, ruling R3).
- **Revisão já enviada ao Flow que vira `nao_avaliado` fica pendente lá.** Qualquer transição de um pedido
  com revisão enviada para item `nao_avaliado` (escolha pontual sem código na fonte, produto que deixou de
  casar, unidade, cópia desatualizada...) bloqueia no portão, mas ninguém retira a revisão no Flow: a
  aprovação fica aberta lá até o pedido voltar a ser avaliável (nova revisão) ou ser cancelado.
- **Produto ligado + preço desligado:** `is_blocking`/`_regra_de_hoje` usa o `price_status` do match legado, não o do
  produto decidido.

**Vínculo v2 (`produto_vinculo_repo`, `vincular`)**
- `vincular` aceita escopo vazio ou sem prefixo `rede:`/`raiz:`.
- Mesmo SEQ já vinculado ignora `fire_codigo`/`fire_descricao` novos, sem documentar.
- `revogar` devolve a linha de antes do update (sem `revogado_em` no dataclass).
- `_eh_unico_do_vinculo` depende do texto da mensagem do SQLite; comentar se o índice virar expressão.
- Falha pós-commit do v2 (v1/audit) deixa o v2 sem audit; corrida na 2ª chave deixa a 1ª sem audit.
- Upsert do v1 sobrescreve vínculo antigo sem 409 e o audit diz "criado".
- Revogar 2x grava 2 audits; fila e recheck truncam em 500 (os mais novos).
- Revogar não é atômico: v2 revogado + falha ao apagar o v1 deixa o v1 órfão (retry dá 404).
- Comentários "raiz:<8 dígitos>" em `produto_vinculo_repo.py:4` e `schema_env.py:194`: agora 8 caracteres alfanuméricos.

**Escopo e check**
- Teste de rede por CNPJ completo e de rede inativa ausente.
- Catálogo existente fica com `codprod_altern` NULL até o próximo sync (o check lê do Fire ao vivo, não do catálogo).
- `_vinculos_discordam` lista só os 2 candidatos dos vínculos, não os de EAN/código.
- `router.env_connect` de slug inexistente cria banco vazio (check sai "completo sem vínculo").
- `_check_da_fonte` usa o env da fonte (mm) para pedido Nasmar; `resolucao_para(slug="mm")` pode não resolver o cliente real.
- Previsão de prefixo roda depois da busca exata; cursor envenenado degrada o aviso legado em `desligado`.
- Escolha pontual em `ligado` casa só por posição (descrição não conferida).

**Interruptor, trava e métricas**
- Troca do `produto_chave_modo` só loga (loguru), sem audit consultável; avaliar antes de ligar em produção.
- `produto_observado` é auditado em toda chamada, mesmo com itens vazios (ruído).
- Bloqueio de produto conta em `portal_price_check_blocks_total{reason="produto"}` (mistura com preço).
- `env=None` lê o `app_shared.db` em toda chamada (docstring "desligado inerte" imprecisa); env inativo ligado não conta.
- Testes da rota do interruptor moraram em `tests/test_produto_trava.py` (não existe `test_routes_environments.py`).

**Tela (Task 8)**
- Roving tabindex no picker; limiar mínimo de similaridade nas sugestões; M1/M4-M7 do design review; cartão mobile completo.
- `<button>` com `<div>` dentro na fila; `escJs` sem aspas; `ppConfErro` não limpa ao escolher outro produto; `PP_ERRO_CATALOGO` casa por igualdade de string.
- Duas redações para o estado "troca" (preview x fila); intro da fila "sem produto confirmado" imprecisa; "Vincular produto" segue primário após erro de catálogo; topbar do telefone estoura (pré-existente).

**Migração e regressão (Task 10)**
- Dry-run cria `app_state` vazio com slug errado; `criado_em` do v1 se perde; guarda de revenda só com CNPJ completo; sem teste de `main()` em dry-run; `;` em literal recusado.
- Guarda de SELECT não pega procedure selecionável por vírgula (`FROM a, sp(1)`); `READ ONLY` protege; teste do `query_only` é fraco.
- Arquivo todo pendente conta como comparado na cobertura.
- Cobertura da Nasmar insuficiente na regressão de 20/10 (48 arquivos sem pedido no Fire na janela de 72h): repetir com cópia mais nova antes de ligar a Nasmar.

### 2.21 Lote: `fora_do_lote` some na semana seguinte
A tela só mede a janela corrente: na segunda, a lista do que precisa ser faturado à mão na
MM (pedido da Nasmar que entrou no Fire por fora do portal) desaparece. Gravar a lista no
lote no fechamento e mostrá-la no lote fechado.

### 2.22 Lote: `lote_fiscal_ok = 0` sem caminho de reenvio do rastro
Falha no `lote_fiscal` do SP1 fica visível na tela ("Rastro NÃO enviado"), mas não há
como reenviar. Botão "reenviar rastro" que remonta as pernas a partir da `composicao` do
lote + `erp_item_vinculo`.

### 2.23 Lote: dois envios simultâneos do mesmo espelho e a reserva do SP1
A reserva é por `chave_lote`: dois "Cadastrar no Fire" concorrentes do mesmo espelho podem
apagar a reserva do primeiro (sai `lote_fiscal_ok = 0`, visível). Desabilitar o botão
durante o envio.

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

### 3.7 Fiscal confirmar o `SN-<hash>` na nota — **portão do deploy do número gerado**
O Fire copia `PEDIDO_CLIENTE` pro xPed da NF-e (15 chars). Pedido sem ordem de compra
passa a sair com `SN-6CF05353` ali, onde hoje já saem `NASMAR`, `STUDIO Z`, nome de loja.
Confirmar com o Elias (fiscal MM) antes de subir o pacote.

### 3.8 Lote intercompany: portão antes de ligar em produção
O espelho (preço × 0,93) passa pelo portão de preço (`preco_trava.decidir_envio`). A trava
por rede (PR #90) deixa intercompany fora da tabela, mas em todos os modos ele cai na regra
de hoje (`is_blocking`), que compara com `PRODUTOS.PRECO_VENDA` da MM. Medido na cópia do
`.7` (19/10): `PRECO_VENDA` vazio em 3.421 de 3.421 produtos, então cada linha do espelho sairia
`no_price_in_fire` e pediria a confirmação "sem preço no Fire" toda segunda.
**Resolvido (21/10, decisão do Samuel):** o espelho nasce com essa confirmação feita pelo lote
(`fechamento._confirmar_sem_preco`: `sem_preco_ack_by = lote:<chave>` + audit com `origem=lote`).
Divergência de preço e preço ausente no pedido continuam bloqueando; `product_check` e
`is_blocking` não mudaram. O preço do lote é conferido pela composição antes do envio.
Pendência menor (revisão de 21/10): em `EXPORT_MODE=both`, `_send_one_to_fire` passa
`planilha=True` ao `_recusa_planilha_pontual` também para o espelho, que nunca gera XLSX
(`server.py`, `not espelho`). Com escolha pontual de produto sem `CODPROD_ALTERN` numa linha do
espelho, recusa à toa (lado seguro). Corrigir com `planilha=... and not integracao.e_espelho(import_id)`.
---

## 4. Decisões pendentes (aguardam o Samuel)

### 4.1 Insert direto no Fire para pedidos 100% match
Código do `FirebirdExporter` já existe; é ligar com trava. Risco central: escrever na
produção do ERP é irreversível, ao contrário do XLS. Trava proposta: opt-in + canário +
modo `both` (XLS de backup) + cair para o XLS na dúvida. **Exige corrigir o §2.1 antes.**
Próximo passo se aprovado: brainstorm → spec.

### 4.2 Regra mais forte de validação de preço por cliente — **coberto pela trava de preço por rede (branch `feat/tabela-preco-por-rede`, 2026-10-17)**
> Implementada em `docs/ai/modules/erp.md` ("Trava de preço por rede"): tabela por rede,
> check indisponível/cópia velha bloqueia (fail-closed em `ligado`). **Falta a Task 7**
> (sync/pendências/decisões com o Flow, aguarda contrato do pcp-app) para ligar. Texto
> original abaixo, mantido como histórico.

Samuel pediu isto como próximo trabalho, antes de habilitar a inserção direta no Fire.

**O que existe hoje:** `check_order` compara o preço do item do pedido contra o preço do
Fire (`TABELA_PRECO_PRODS`), e `is_blocking` (`app/erp/product_check.py:295`) bloqueia em
`mismatch`, `no_order_price` e `no_price_in_fire` sem ack. A regra é **por produto**, não
por cliente.

**O furo:** `is_blocking` devolve `(False, ...)` quando `check["available"]` é `False` —
docstring: "sem dados pra avaliar, não bloqueia". Se o portal não conseguiu consultar o
Fire, a trava de preço **desliga em silêncio** e o pedido passa. Foi exatamente isso que
tornou o Crítico da entrega de 12/09 tão grave: `check_order(env=None)` devolvia
indisponível e ninguém era avisado de que a validação nem tinha acontecido. O bind da
empresa foi corrigido (`dbf2708`), mas a regra de fundo segue permissiva.

**Decisões que são do Samuel, não minhas:**
- Check indisponível deve **bloquear** em vez de passar? (Inverter o default muda o
  comportamento de hoje e pode travar a operação quando a VPN cai.)
- A regra é por cliente, por marca, por faixa de desconto, ou combinação?
- Quem pode dar o ack de uma divergência — qualquer usuário logado, ou só admin?
- O que acontece quando o Fire está fora do ar: fila, bloqueio, ou passar com marca?
- Tolerância: centavos de arredondamento contam como divergência?

**Próximo passo:** brainstorming antes de qualquer código. É mudança de regra de negócio
num caminho que decide se pedido entra ou não, então spec antes de plano.

### 4.3 Menores deferidos da trava de preço
- `registrar_aplicados` (audit `preco_aplicado`) roda depois do export; revisar a posição se houver falha parcial.
- Em `ligado` bloqueado o botão primário "Gerar XLS" fica habilitado (o servidor recusa com motivo); desabilitar com explicação.
- Check salvo validado só pelo tamanho: com Fire fora e de-para trocado após o commit, o KIT avaliado pode diferir do exportado.
- Preview marca intercompany como liberado, mas o portão aplica a regra de hoje e pode bloquear (`price_check_failed`); sem teste de paridade.
- Troca do `preco_trava_modo` só vai pro loguru; falta trilha consultável de quem ligou a trava.
- Item com vínculo de-para para SEQ diferente do casamento direto (A/B) sai `unidade_divergente` em `ligado` (a unidade do check é a de A); ler a `UNIDADE` do SEQ do vínculo via `find_products_by_seqs_sql` no check ao vivo do portão para o caso A/B parar de bloquear.
- Paridade do worker docker: `python -m app.worker` não roda `precos.rodar` (sync da tabela, envio, decisões, retenção da trava); só a thread `flowpcp-envio` do processo web. Fora de escopo de propósito: no cliente roda o `ui.py`, onde a thread já existe. Só vira pendência se o deploy docker for usado com a trava ligada.

### 4.4 Lote retroativo
Perna importada numa semana sem lote e enviada ao Fire semanas depois abre o lote daquela
janela, já vencida, que fecha no ciclo seguinte com 1 perna. Fiscalmente correto,
operacionalmente estranho. **Decisão de produto:** aceitar, ou mandar para o lote corrente.

---

## 5. Verificação pendente (não dá para afirmar hoje)

### 5.3 Sam's GRADE: quem é o cliente de cada perna do cross-docking?

O layout consolidado passou a usar o **Local de Entrega** como cliente em 2026-09-10
(a MM cadastra o CD no Fire, não o clube que compra — confirmado pela MM). A GRADE
ficou **deliberadamente de fora**: lá o `Local de Entrega` do cabeçalho é um CD de
trânsito (`00.063.960/0591-70` = CD SAM'S RS) e a mercadoria é cross-docked para N
lojas, cada uma já virando um arquivo próprio no split por `delivery_ean`.

Hoje cada um desses arquivos sai com `CNPJ_CLIENTE` = **Comprador** do cabeçalho
(`/0094-08`), que por acaso também é uma das lojas do sample. As duas leituras
plausíveis são: cliente = o CD de trânsito, ou cliente = a loja de cada perna
(caminho que o exporter já sabe fazer — é o mesmo do Riachuelo, com
`customer_cnpj = None`). Sem um pedido GRADE reportado pela MM não dá para escolher,
e chutar quebra um fluxo que hoje funciona.

**Próximo passo:** perguntar à Camila como um pedido GRADE é cadastrado hoje no Fire —
um pedido por loja ou um só para o CD.

### 5.4 `.title()` mastiga nome com sigla e apóstrofo

`OrderNormalizer.normalize` faz `customer_name.strip().title()`. Com o cliente do Sam's
passando a ser o CD, isso virou visível: `CD SAM'S DF` → **`Cd Sam'S Df`**, e o nome do
arquivo sai `Cd_Sam_S_Df_...xlsx`. O `str.title()` do Python quebra em apóstrofo e
rebaixa qualquer sigla.

Já estava catalogado na auditoria de UI de 24/08 e não foi corrigido porque **muda o
nome de todo cliente** (`Sbf Comercio Produtos Esportivos` está pinado em teste). Não
entrou no hotfix do Sam's por isso — decisão do Samuel.

### 5.1 Os campos do de-para intercompany estão preenchidos no cliente?
**Metade resolvido em 2026-08-24:** a dúvida sobre a versão acabou — o cliente passou a
rodar uma versão que contém o de-para. **Produção hoje: `20260826-1925`** (commit
`a201910`, aplicada 26/08, confirmada pelo Samuel). Duas releases depois daquela, e o
de-para segue embarcado.

**O que segue aberto:** ninguém confirmou que `intercompany_cnpj` e
`intercompany_env_slug` foram preenchidos em `/admin/ambientes`. Precisa do share
`/Volumes/SamFlowsAI` montado ou da VPN para ler a tabela `environments`.
**Enquanto os dois campos estiverem vazios, a feature é inerte** — não muda nada em
produção.

### 5.2 O CNPJ da Tennis Station é escolha do comprador ou default?

`samples/PEDIDO TENNIS STATION.xlsx` traz `52.671.393/0001-69` no campo `CNPJ:` — e ele
é o **primeiro de uma lista escondida de 39 CNPJs** (11 raízes) nas colunas X+ da linha 6,
que é a fonte de validação do dropdown de filiais do grupo. Com um sample só não dá
para distinguir "o comprador escolheu" de "ficou o default". Se for default, o pedido
vai para a filial errada no Fire.

Todo o resto do cabeçalho veio em branco (razão social, ordem de compra, data), o que
reforça a hipótese de formulário pouco preenchido.

**Destravado do nosso lado:** a `20260826-1925` está em produção desde 26/08, então o
portal já lê pedido da TS. **O que falta é dado do cliente:** um segundo pedido real, de
preferência de outra filial. Até lá, a nota de release manda a operação conferir o nome
do cliente na tela — o `check_order` resolve a razão social do Fire pelo CNPJ
(`app/erp/product_check.py:143`), então filial errada é visível para quem olha.

### 5.5 `PORTAL_RECONCILE_PERIODICO=0` não desligou o loop de reconciliação ao subir localmente

Observado em 2026-10-17, subindo o portal local para a tela de lotes: com
`PORTAL_RECONCILE_PERIODICO=0`, o loop de reconciliação com o Fire subiu mesmo assim.
**Não investigado.** O guard (`_periodico_habilitado`) mora em
`reconciliar_todos_os_ambientes` (`app/reconcile/runner.py`), não no start da thread;
não se sabe se o que se viu foi só a thread de pé ou uma reconciliação de verdade.

**Próximo passo:** reproduzir com o log do `reconcile.runner` e decidir se o guard
também deve impedir a thread de subir.
