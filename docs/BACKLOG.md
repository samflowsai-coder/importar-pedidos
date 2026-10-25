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
**Bloqueia `EXPORT_MODE=db`.** ~~Fix: a chave precisa de mais uma perna (data do pedido
ou hash do arquivo), ou a idempotência sai do Fire e fica no portal.~~ Superado pela
proposta abaixo (impressão digital + bloqueio brando).

Remedido em 24/10 (Fire viva, `ULT_INS_DTHR` de 2026, só leitura, contagens):

| | pedidos c/ `PEDIDO_CLIENTE` | chaves repetidas | pedidos dentro delas | `VALOR_TOTAL`+`DT_ENTREGA` separa todos | não separa |
|---|---:|---:|---:|---:|---:|
| `.7` MM | 1.258 | 97 | 347 (28%) | 80 | 17 |
| `.4` Nasmar | 345 | 55 | 209 (61%) | 53 | 2 |

**Proposta (aguarda o Samuel):** "mesmo envio" fica no portal (`portal_status = sent_to_fire`,
que já existe). No Fire, `CHECK_ORDER_EXISTS` vira impressão digital (`PEDIDO_CLIENTE` +
`CLIENTE` + `VALOR_TOTAL` + `DT_ENTREGA`) e **bloqueia brando**: mostra o pedido que já
está no Fire e o operador confirma, com audit. Hoje bloqueia duro (409 "Fire rejeitou o
pedido: ORDER_ALREADY_EXISTS") e, em `db`, a tela só oferece enviar e cancelar; "Verificar no
Fire" e a reconciliação periódica (ligada por padrão, `PORTAL_RECONCILE_PERIODICO`) marcariam o
pedido legítimo como `found_in_fire` pela mesma chave repetida (§2.10).

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

### 2.24 Daju e Nasmar: migrar `_parse_number`/`_to_number` para `app/parsers/celulas.py`

`DajuParser._parse_number` e `NasmarTemplateParser._to_number` são cópias idênticas, e
`_coerce_text`/`_coerce_date` da Nasmar repetem `celulas.texto`/`celulas.data`. O parser da
Centauro (ORDER CONFIRMATION) já usa o módulo; migrar os dois como refactor próprio, sem
mudança de comportamento — `tests/test_new_parsers.py` (Daju/Nasmar) é a rede.

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

### 3.9 Espécie dos volumes na NF sai "FARDO" por padrão — chamado na Fire Sistemas
Pedido da Camila (MM) em 05/10/2026: na emissão da nota o campo da espécie dos volumes
vem preenchido com "FARDO" e eles nunca usam; querem "CAIXA" como padrão, para as duas
empresas. Verificado na Fire viva (.4 e .7, só leitura): é `NOTA.NF_TRANSP_ESPECIE`,
VARCHAR(25) de texto livre, **sem default no banco e sem gatilho/procedure que o
preencha**, e o texto "FARDO" não está em nenhuma tabela de configuração. Inferência (não
provada): o padrão está fixo na tela de faturamento do programa. O portal não participa (o
export não tem coluna de espécie e a linha da nota só nasce na emissão, dentro do Fire).
A MM abre o chamado com a Fire Sistemas; o Samuel acompanha. **Não editar o banco** para
forçar "CAIXA". Em 2026 já predomina "CAIXA" (.7: 1.092; .4: 350).

---

## 4. Decisões pendentes (aguardam o Samuel)

### 4.1 Gravar direto no Fire (`EXPORT_MODE=db`) — **Samuel quer virar nos próximos dias (24/10)**
O botão "Cadastrar no Fire" (`/api/imported/{id}/send-to-fire`) e o `FirebirdExporter` já
existem; `EXPORT_MODE=db` só troca o botão primário de "Gerar XLS" para ele (a rota em si não
recusa em `xlsx`, `docs/ai/modules/erp.md`). O §2.1 (de-para no insert) já está resolvido.
Risco central: escrever na produção do ERP não se desfaz como um XLS. Trava: canário + voltar
para `xlsx` na dúvida.

**Medido na Fire viva em 24/10 (só leitura):**
- **Zero** pedidos com `ULT_INS_USER = 'IMPORTADOR'` em 2026, nos dois bancos (dados até
  23/10). O insert direto **nunca rodou em produção**; quem grava são as operadoras pelo
  importador de Excel. Volume: `.7` ~120–220 pedidos/mês (ago–out), `.4` ~25–55.
- CNPJ de cliente com mais de um cadastro: **0** nos dois. O `FIND_CLIENT_BY_CNPJ`
  (`ROWS 1`, sem `ORDER BY`) não é risco hoje.
- `CAB_VENDAS.CODIGO` é PK nos dois bancos, **sem gerador e sem trigger ativo** (o único
  gerador de venda é `GEN_CORPO_VENDAS_CODIGO`). O `MAX+1` do portal é coerente com o Fire.
- Ganho de virar: o vínculo item↔ERP passa a ser exato (`erp_item_vinculo` gravado no
  INSERT); no caminho XLSX é por posição e "nunca chuta" (`app/erp_eventos/vinculo.py`).

**P0 (sem isso não grava em produção):**
1. **(Parte do "por empresa" substituída em 24/10: o Samuel decidiu modo global; ver "Decisões do Samuel" abaixo. O diagnóstico vale.)** **O modo é global, não por empresa.** `export_mode` vale para o processo inteiro
   (`app/config.py`): `_cfg_para_env` (`app/web/server.py`) só troca as pastas, e o
   `config.json` salvo vence a variável de ambiente. Ligar `db`/`both` para a MM liga o
   "Cadastrar no Fire" também na Nasmar (`.4`, sem cópia validada). Além disso
   `POST /api/config` troca o modo com `require_user` (qualquer logado). E **nada no servidor
   recusa o envio por causa do modo**: `send_to_fire` e `/api/batch/send-to-fire` gravam no Fire
   mesmo em `xlsx` (`_send_one_to_fire` só repassa o modo; a tela o relê ao carregar e entre abas
   do mesmo navegador, não em outras máquinas). Logo voltar o modo não segura quem está com a
   aba aberta em outro computador, nem quem chamar a API direto. Precisa de:
   `export_mode` por empresa (muda o schema de `environments`: pede o ok do Samuel); `send-to-fire`
   e o batch responderem 409 quando o modo da empresa, lido no servidor, não for `db`/`both`;
   rota de config só para admin. Levar o modo para a empresa troca **todos** os leitores do
   global, não só `_cfg_para_env`: `_get_cfg`/`app_config.load()` (`server.py:229`),
   `_export_mode_atual` (`:255`, alimenta a trava de produto e `:4429`), `_run_exporters`
   (`:573`) e `GET`/`POST /api/config` (`:1268`, `:1295`). Sem isso não existe "canário só
   na MM" nem freio de verdade.
2. **Idempotência** (§1.5): 28% dos pedidos da MM e 61% dos da Nasmar caem numa chave repetida.
3. **Paridade com o importador de Excel.** Não existe ferramenta de comparação:
   `tools/erp_medir.py contrato` só conta o contrato do SP1 em pedidos abertos. Falta gravar o
   mesmo pedido real pelo portal numa cópia fresca e comparar `CAB_VENDAS` e `CORPO_VENDAS`
   coluna a coluna com o que a operadora gerou. Inclui `VALOR_TOTAL` e `DT_ENTREGA`: o portal
   os calcula (soma em Decimal, menor data dos itens), e se o importador gravar outro valor a
   impressão digital do §1.5 falha aberta e o pedido duplica. **Não existe cópia da `.4`**
   (Nasmar, `CODFIGFISCAL` 5); o único ensaio em cópia foi a `.7` de abril. Cópia nova por
   backup server-side (o modo streaming de `tools/fire_copia_local.sh`, que só existe no
   checkout principal e não é versionado, gera `.fbk` inválido).
4. **Como desfazer** um pedido gravado errado (quem cancela, como, até quando). Sem resposta
   da Camila/Elias/Fire Sistemas não há canário.
5. **(Plano B substituído em 24/10: sem plano B; ver "Decisões do Samuel" abaixo. A análise do `both` vale.)** **`both` não serve de canário:** o XLS nasce antes do insert, na mesma `output_dir`
   (`app/web/server.py`, envio ao Fire), e fica lá mesmo se o insert falhar; se a operadora o
   importar, o pedido entra duas vezes. Pasta separada exigiria mudar o código. Por isso o
   canário roda em `db`. Como `db` vale para a empresa inteira, todo pedido da MM (não só o do
   cliente piloto) perde o "Gerar XLS" na tela, que em `parsed` só oferece "Baixar arquivo
   original", cancelar, fechar e o botão primário. Plano B por pedido: expor
   `POST /api/imported/{id}/export-xlsx` (a rota já gera a planilha em qualquer modo,
   `server.py`) como ação secundária no preview, **com o envio ao Fire respondendo 409 depois de
   `xlsx_exported`**: o pedido segue `parsed` depois do XLS e `_send_one_to_fire` não confere
   `xlsx_exported` (as travas de produto/preço e o `ORDER_ALREADY_EXISTS` do §1.5 também não
   olham o XLS, e a idempotência do §1.5 só segura em parte: falha aberta), então sem esse 409
   a operadora importa o XLS e ainda clica "Cadastrar no Fire" (lança duas vezes). Alternativa:
   uma lista de clientes liberados para gravar direto, **com `send-to-fire` e o batch
   respondendo 409 para cliente fora da lista (lida no servidor, não só na tela)** e esses
   clientes mantendo o "Gerar XLS". Aí o cliente piloto é o da lista; pedido ruim de cliente da
   lista sai tirando o cliente da lista ou pelo freio do item 1. Sem nenhum dos dois, o único
   plano B é o freio do item 1 (voltar a `xlsx` na empresa toda).
6. **Trava de produto:** `produto_chave_modo` estava desligado na última checagem (20/10,
   relato da sessão; não reverificado). Sem ela o insert casa produto pela busca antiga e
   erro de produto vira pedido errado no ERP. Ligar pelo caminho das "Decisões do Samuel" abaixo (`observando` → `ligado`), nas duas empresas.

**P1 (antes de tirar o canário):**
- PK do cabeçalho: `GET_NEXT_CABVENDAS_CODIGO` é `MAX(CODIGO)+1` (`app/erp/queries.py`) e o
  `_insert_lock` serializa só o portal. Operadora gravando no Fire na mesma janela colide; por
  ser PK deve falhar limpo (**não testado**). Fix: repetir com PK novo na violação, reusando
  `classificar_erro` (`app/erp/cadastro_fire.py`), que já devolve `"colisao"`.
- Preço: `PRECO_VENDA` da MM é vazio em 100% dos produtos, então todo item cai em
  `no_price_in_fire` e o envio pede a confirmação manual (já é assim hoje). A trava de preço
  segue `desligado`; ligar é decisão à parte (§4.2).
- Flow: o envio faz `push_new_order` (`externalId = import_id`) e a vigia do SP1 também lê o
  pedido do ERP. Provar no canário que o Flow não duplica (o vínculo exato é o que deveria
  impedir).
- Operação: backup server-side antes do canário; desligar = trocar o modo (hoje no
  `config.json`/tela de config, global, decisão de 24/10). Aba aberta em outra máquina
  mantém o botão até recarregar (as do mesmo navegador se atualizam): avisar a operadora e
  confiar no 409 do servidor, não na tela. Quem acompanha
  e em que janela.

**Decisões do Samuel em 24/10 (tarde; o item do teste em produção é de 25/10) — onde conflitam com o P0 acima, valem estas:**
- **Modo global, sem parâmetro por empresa** (substitui o P0.1 "modo por empresa"). Com `db`
  ligado e o lote semanal, não haverá XLS. Consequência: a Nasmar (`.4`) entra junto, então o
  **backup server-side da `.4` e a paridade nela viram pré-requisito da virada**, não etapa
  posterior. Continuam valendo: servidor responde 409 fora do modo e a rota de config só para
  admin, com auditoria (proposta do assistente, não discutida).
- **Sem plano B** (substitui o P0.5): saem o botão secundário de XLS e a lista de clientes
  liberados. Fica o parâmetro BD/XLS nas configurações, pedido já exportado em XLS não grava
  direto sem confirmação, e falha do Fire (fora do ar, lock) vira erro claro, nunca pedido
  meio-gravado. (O Canário foi reescrito abaixo.)
- **Empresa certa sem o operador selecionar:** a gravação depende 100% do roteamento
  (documento > histórico > memória + regra de faturamento MM × Nasmar). Sem decisão ou ambíguo,
  o pedido fica retido, nunca grava por padrão. Proposta do assistente para o canário: o sistema sugere
  e o operador confirma com um clique, até a taxa de acerto estar provada. Estado do
  `roteamento_modo` em produção: relato de 20/10, **a reverificar**.
- **Pedido que já existe no Fire:** o portal acha o anterior pela chave CNPJ do cliente + número
  do documento (só vale para número de pedido de compra de verdade, não código de loja) e
  mostra o diff para o usuário confirmar. Pedido digitado à mão ou anterior à virada não tem
  vínculo no portal: vale a impressão digital no Fire (§1.5) mais a confirmação. **Atualizar o
  pedido no Fire** (UPDATE/DELETE) é trilha própria, depois do canário estável. Regras
  propostas (não decididas): item faturado não reduz abaixo do faturado; remover item com
  produção avisa; incluir item é livre. Antes, descobrir como o Fire representa remoção e
  alteração de item (tabela `HISTORICO`).
- **Desfazer pedido, 3 camadas:** faturado (mesmo parcial) bloqueia, cancela-se a NF no Fire
  primeiro; produção iniciada só avisa (depende de uma leitura do produzido no Flow/Apontaê,
  **não confirmado se existe**); cancelar é `STATUS='CANCELADO'` (medido na cópia de 23/10: 22 pedidos
  assim), nunca apagar a linha.
- **Trava de produto:** ligar. Caminho: sincronizar o catálogo, migrar o de-para para v2,
  `observando` por alguns dias, depois `ligado`. Falta o seletor mostrar a diferença antes de
  aceitar (descrição, EAN, grade, unidade, kit ou par), aprender vínculo só por escolha humana
  confirmada e o desfazer do vínculo (item 2.2 sobe de prioridade).
- **Teste direto na produção da MM:** autorizado pelo Samuel em 25/10, depois de o assistente
  avisar que o SP1 está ligado na MM e leva o pedido de teste e o cancelamento ao Flow de
  produção ("pode fazer, sem problemas"). Só roda com acesso à `.7` (exige VPN) e depois do
  ensaio equivalente na cópia restaurada de 23/10 (`mm_oct23.fdb` no `fire-local`).

**Ensaio de paridade em cópia (25/10; P0.3 parcial, só a MM):** 5 pedidos de operadora de
set/out (todos kits; `CAMILA`) foram remontados a partir do próprio Fire e regravados pelo
`FirebirdExporter` (itens pelo SEQ) na cópia local de 23/10 (`mm_oct23.fdb`), com número de
pedido de teste. Gravou 5/5, mesmos itens, QTD, preço, total por item e `ICMS_BASE`.
Diferenças reais, medidas contra os 753 pedidos de operadora desde 01/06:
- **`COND_PRAZO` e `PRAZO_MEDIO`:** a operadora preenche em 707/753 (94%); o portal grava NULL.
  Condição de pagamento do pedido não vem. Origem do valor (cadastro do cliente?) a descobrir.
- **`OBS`:** preenchida em 737/753 (98%) com número do pedido e nota do cliente
  (ex.: `SN-89B18EEE` + `NASMAR 1304`); o portal grava NULL em pedido comum.
- **`REDUCAO` do item:** 563 de 6.877 linhas não têm 61,11 (0 ou vazio; as outras 6.314 têm); o
  portal crava 61,11 do perfil em todas. Nas amostras, 4 linhas (uma em cada um de 4 dos 5
  pedidos) divergiram, com o mesmo `ICMS_BASE`. Efeito fiscal: **pergunta
  para o Elias** antes de qualquer gravação.
- **`VALOR_TOTAL`/`TOTAL_PRODUTO`:** 1/5 diferiu 1 centavo (soma 2569,1589: Fire trunca para
  2569,15, o portal arredonda para 2569,16). Com preço de espelho (÷ fator) a soma tem dízima, e
  a impressão digital do §1.5 (que usa `VALOR_TOTAL`) falharia aberta nesse caso.
- **`DESC_CLASS_FINAN`:** a operadora deixa vazio em 36% dos pedidos; o portal sempre preenche.
- **Não é divergência do portal (artefato do ensaio):** `DT_ENTREGA`/`DT_BASE_FAT` (o ensaio
  remontou a data a partir de `DT_ENTREGA_ITEM`, que a operadora nunca preenche: 0/6.877) e
  `DT_PRODUCAO` (preenchida depois, à mão). Falta repetir com a data de entrega do cabeçalho.
- **Fora do ensaio:** a Nasmar (`.4`), pedidos não-kit, escolha por EAN/código (só SEQ), e o
  cancelamento (`STATUS`/`HISTORICO`).

**Canário (reescrito em 25/10, com as decisões acima):** `db` com modo global, então MM (`.7`) e
Nasmar (`.4`) entram juntas e não há plano B por pedido nem lista de clientes. Pré-requisitos: o
roteamento provado (ou a empresa sugerida com confirmação por clique), a paridade provada nas
**duas** empresas e o 409 no servidor. Camila confere os primeiros pedidos lado a lado; o critério
de saída (quantos pedidos sem divergência) fica a definir. **A `.4` segue sem cópia utilizável:**
os dois `BKP_*_CONFECCAO_TERCA.fbk` de abril terminam truncados (`gbak`: "Backup incomplete") e o
`nasmar.fdb` do `fire-local` é, na prática, a mesma base da MM de abril. Falta um backup
server-side novo da `.4`.

**Lote semanal (§3.8):** só liga com o `db` estável nos dois ambientes.

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
