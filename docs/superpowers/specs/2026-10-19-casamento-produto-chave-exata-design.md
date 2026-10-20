# Casamento de produto só por chave exata — design

**Data:** 2026-10-19 · **Status:** v2, decisões do §9 fechadas pelo Samuel em 19/10
**Origem:** caso Kings SN-EB95FA5D (16/10) no Flow e auditoria Fire × pedido original
(19/10). Regra pedida pelo Samuel em 19/10.
**Sistemas:** **Portal** (este repo) é onde o produto é decidido, antes do Fire.
**Flow** (pcp-app) passa a receber o produto já decidido e não casa mais nada sozinho.
São dois planos, um por repo. O do Portal vem primeiro.

---

## 1. Por que

O produto errado é o pior erro que o pipeline pode cometer. Produto desconhecido é
inofensivo: alguém vê e resolve. Produto casado errado passa batido e vira OP do
produto errado, preço de tabela errado e divergência de preço falsa na fila do
Rafael e da Erica.

O que aconteceu e o que existe hoje:

- **Flow, 16/10:** o casamento do `/recebimento` ligou a linha KG02PR ("KIT SAPATAILHA
  KINGS - 6 PARES - Preto - 39 - 44") ao produto 3948 ("KIT 3 PARES KINGS INVISÍVEL
  SILICONE"). Quem decidiu foi uma IA lendo só a descrição
  (`matching-produto.ts:101-116`, aceite com confiança ≥ 0,7). **Desligado em 19/10**
  pelo PR pcp-app #332. Na Fire o mesmo pedido (Nasmar 1301) está certo.
- **Portal → Fire, auditoria de 19/10:** relendo os 338 arquivos guardados desde 09/09,
  nenhum pedido ficou com produto errado. Mas o caminho tem três brechas:
  1. **Item sem produto não trava.** A trava de hoje (`preco_trava.decidir_envio`,
     chamada em `server.py:2463` e `:2697`) só olha preço. O `POST /api/commit` não
     confere produto. Resultado: em 19/10, 18
     pedidos da Riachuelo (MM 5091–5108) foram para o Fire com um item sem produto, e
     o importador de Excel do Fire gravou `CODPRODUTO = -1`.
  2. **Chave repetida decide em silêncio.** EAN ou `CODPROD_ALTERN` com dois ou mais
     produtos: vence o menor SEQ (`_fetch_map_by_key`, `product_check.py:34-46`).
     Produto `BLOQUEADO = 'Sim'` casa normalmente.
  3. **A planilha pode trocar o produto depois do Portal.** O importador de Excel do
     Fire casa por prefixo, sem `ORDER BY` (BACKLOG 2.15). O Portal escreve o código
     do cliente na coluna de código (`erp_exporter.py:163`). Hoje ele só avisa
     (`troca_no_fire`), não trava.
- **O de-para existe, mas a chave do cliente é frágil.** `produto_depara` usa o CNPJ
  do cabeçalho e, sem ele, o **nome** do cliente normalizado
  (`produto_depara_repo.py:44-52`). Com CNPJ, o vínculo vale para uma filial só. Com
  nome, depende de o texto vir igual em todo pedido. Além disso, o de-para hoje é o
  **último** passo (EAN → `CODPROD_ALTERN` → de-para, `product_check.py:255-275`) e
  grava o SEQ em texto como código (`catalogo_fire.codigo`, `queries.py:321`), que
  vai para a coluna de código da planilha (`depara_apply`).
- **`confiancaParser` é fixo em `"alta"`** (`app/integrations/flowpcp/mapper.py:80-81`).
  Não informa nada.

## 2. A regra

Um item de pedido só fica ligado a um produto por **chave exata**. As chaves, a
mesma lista para todo cliente:

1. **EAN do item** → `PRODUTOS.CODIGO_EAN13` igual, entre os produtos **ativos**
   (`BLOQUEADO <> 'Sim'`). Vale só se for **exatamente 1**.
2. **De-para confirmado** → (escopo do cliente, chave do item) → SEQ. Gravado por uma
   pessoa uma vez, automático daí em diante (§4).
3. **Código do item igual a `CODPROD_ALTERN`**, entre os ativos, exatamente 1
   (decidido, §9.1).
4. **Nenhuma chave dá um produto** → o item fica **"a identificar"**, e o pedido não
   segue para o Fire até alguém escolher o produto. A escolha vira de-para (chave 2).

**Como as chaves se combinam** (minha decisão, para revisão). O check avalia **todas**
as chaves do item, e não só a primeira que responde:

- Cada chave dá um de três resultados: um produto, nenhum, ou um problema
  (`ambiguo`, `produto_bloqueado`, `vinculo_invalido`).
- **Todas as chaves que deram produto apontam para o mesmo SEQ** → `ok`. A fonte
  registrada é a primeira da lista acima.
- **Duas chaves apontam para SEQs diferentes** → `chaves_discordam`, vai para "a
  identificar" mostrando os dois. Uma chave nunca ganha de outra em silêncio. É o
  caso do EAN do cliente estar num produto e o código noutro.
- **Nenhuma chave deu produto, e alguma deu problema** → o item fica com esse
  problema, mostrando os candidatos.
- **O de-para resolve o problema de outra chave.** Se o EAN é ambíguo e o de-para
  daquele EAN ou código aponta para um dos candidatos, o resultado é `ok` por
  de-para. É para isso que a pessoa escolheu.

Regras que valem para todas as chaves:

- **Nunca** casa por descrição, semelhança de texto, prefixo ou IA. Descrição só serve
  para **sugerir** candidatos na tela de escolha, e a sugestão nunca vira vínculo
  sozinha.
- **Ambiguidade não é escolha.** Dois ou mais produtos ativos para a mesma chave fazem
  o item ir para "a identificar", mostrando os candidatos. Não vence o menor SEQ.
- **Produto bloqueado não casa automaticamente.** Se a chave só aponta para um
  bloqueado, o item vai para "a identificar" com o motivo `produto_bloqueado`.
- **De-para que aponta para um produto que sumiu ou ficou bloqueado** não vale. O item
  vai para "a identificar" com o motivo `vinculo_invalido`, e o vínculo antigo aparece
  na tela.
- **A confiança do parser não autoriza nada.** Só a chave autoriza.

## 3. Objetivos, não-objetivos e anti-objetivos

**Objetivos**

1. Nenhum item chega ao Fire ou ao Flow sem produto decidido por chave exata ou por
   uma pessoa.
2. Cada escolha manual ensina o sistema: na próxima vez, o mesmo código do mesmo
   cliente casa sozinho.
3. Uma escolha vale para todas as lojas do mesmo cliente, não só para um CNPJ.
4. O produto que o Portal decidiu é o produto que entra no Fire e no Flow, sem
   segunda decisão no caminho.
5. Tudo fica auditado: quem vinculou, quando, de quê para quê, e quem desfez.

**Não-objetivos**

- Corrigir o cadastro do Fire (EAN repetido, nomes trocados). O Portal mostra a
  ambiguidade, não conserta o cadastro.
- Ligar `EXPORT_MODE=db`. Esta frente ajuda, mas ligar é decisão separada
  (BACKLOG 4.1).
- Reprocessar pedidos antigos já no Fire. A auditoria de 19/10 não achou produto
  errado.
- Tela nova de cadastro de de-para em massa (importar planilha de vínculos). Fica para
  depois, se for preciso.

**Anti-objetivos (é falha mesmo se "funcionar")**

- Qualquer casamento automático que não seja por chave exata, inclusive "só quando a
  similaridade for altíssima".
- O usuário precisar vincular o mesmo código duas vezes para o mesmo cliente.
- O pedido ir para o Fire com item sem produto, ou com produto diferente do que o
  Portal decidiu.
- Fila de "a identificar" que trava o pedido sem dizer por quê nem o que fazer.

## 4. De-para por cliente

### 4.1 Escopo: a quem vale um vínculo

O código que o cliente manda pertence à **empresa** (o sistema de compras dela), não
à loja. Por isso o vínculo vale para um **escopo**, resolvido pelo CNPJ do cliente
nesta ordem:

1. **Rede**, quando o CNPJ pertence a uma rede ativa da tabela de preço
   (`precos_repo.resolver_rede`: CNPJ completo e depois raiz de 8 dígitos). Escopo =
   `rede:<rede_id>`.
2. **Raiz do CNPJ** (8 primeiros dígitos), se não houver rede. Escopo =
   `raiz:<8 dígitos>`. Cobre todas as filiais da mesma empresa.
3. Sem CNPJ válido, o vínculo não pode ser reaproveitado. Ele vale só para aquele
   pedido (vínculo `pontual`, não vai para a tabela).

**Qual CNPJ** (minha decisão):

- O CNPJ do cabeçalho do pedido, quando existe.
- Sem ele, o CNPJ de entrega do **item** (`item.delivery_cnpj`). É o caso da
  Riachuelo pelo `mercado_eletronico_parser`, que só põe o CNPJ da loja no item
  (`:55-59`, `:89`). Como as lojas têm a mesma raiz, todas caem no mesmo escopo.
- **Intercompany (pedido no nome da Nasmar):** vale o **cliente real** resolvido
  pelo `depara_cliente`, não a revenda, porque o código é do sistema de compras de
  quem comprou.

O lookup procura primeiro pelo escopo de rede e depois pelo de raiz. Assim, um
vínculo feito antes de a rede existir continua valendo.

### 4.2 Chave do item

- `codigo`: o código do item normalizado (sem espaços nas pontas, maiúsculas).
- `ean`: só dígitos. Serve para o caso de EAN ambíguo ou ausente no Fire: o EAN do
  cliente aponta para um SEQ escolhido por uma pessoa.

Quando um item tem as duas chaves, a escolha grava as duas.

### 4.3 Tabela

Substitui `produto_depara` (2 linhas na prod MM, que migram). Fica no banco do
ambiente, porque o SEQ é do Fire daquele ambiente.

```
produto_vinculo_v2
  id              INTEGER PK
  escopo          TEXT NOT NULL      -- 'rede:<id>' | 'raiz:<8 dígitos>'
  chave_tipo      TEXT NOT NULL      -- 'codigo' | 'ean'
  chave_valor     TEXT NOT NULL      -- normalizado
  fire_seq        INTEGER NOT NULL
  fire_codigo     TEXT               -- CODPROD_ALTERN na hora do vínculo (só exibição)
  fire_descricao  TEXT NOT NULL      -- na hora do vínculo (só exibição)
  origem          TEXT NOT NULL      -- 'manual' | 'migrado_v1'
  criado_em, criado_por
  revogado_em, revogado_por          -- NULL = vigente
  UNIQUE (escopo, chave_tipo, chave_valor) WHERE revogado_em IS NULL
```

- **Nunca apagar:** desfazer é revogar, e o histórico fica.
- **Trocar um vínculo vigente** exige ação explícita na tela ("este código já aponta
  para X, trocar para Y?"). A troca revoga o antigo e grava o novo na mesma transação.
- Auditoria em `audit_log` para criar, trocar e revogar, com usuário e pedido de
  origem.

### 4.4 Quem vincula

Qualquer usuário logado no Portal que importa pedidos (decisão do Samuel, 19/10). O
nome e a data aparecem no vínculo e no histórico do item.

## 5. "Produto a identificar"

### 5.1 Estado do item

O resultado do check por item ganha um campo `produto` com um destes valores:

| valor | quando |
|---|---|
| `ok` | casou por EAN, de-para ou código (§2) |
| `a_identificar` | nenhuma chave casou |
| `ambiguo` | a chave casou 2+ produtos ativos (lista os candidatos) |
| `produto_bloqueado` | a chave só achou produto bloqueado |
| `vinculo_invalido` | o de-para aponta para SEQ que não existe ou está bloqueado |
| `chaves_discordam` | duas chaves apontam para produtos diferentes (mostra os dois) |

Cada item `ok` leva `fonte` (`ean` / `depara_codigo` / `depara_ean` /
`codprod_altern`) e `fire_seq`.

### 5.2 O que trava

Pedido com algum item fora de `ok` está **travado para produto**. Travar **não
recusa** o pedido: ele é salvo e fica em revisão, e o que fica parado é a saída para
o Fire (planilha ou insert). É o mesmo modelo do `xlsx_adiado` da trava de preço, com
um motivo novo, `produto_a_identificar`. Assim a pessoa consegue abrir o pedido e
vincular (o vínculo só funciona em pedido em revisão, `server.py:3462`).

A trava fica no mesmo ponto da trava de preço (`preco_trava.decidir_envio` e seus
chamadores), e não no `is_blocking`, que ninguém chama. Ela vale em **todo** caminho
que gera planilha ou grava no Fire:

- `POST /api/commit` (gera a planilha na hora via `_run_exporters`,
  `server.py:477-481`);
- `POST /api/imported/{id}/send-to-fire` e `/export-xlsx`, com os lotes;
- os caminhos sem preview: `POST /api/import` (`server.py:1396` → `_process_file`
  `:558`), `POST /api/reimport` (`:1730`), `POST /api/process` (`:3740`, `:3770`), o
  worker `scan_environments.py:161` e o CLI `main.py:25`. Nesses, o pedido travado
  fica salvo em revisão sem planilha. O CLI, que não tem revisão, pula o pedido e
  diz por quê.

O envio para o Flow só acontece depois do Fire ou da planilha
(`flowpcp/hook.py:3-6`), então já fica coberto pela mesma trava.

**Fire fora do ar** (check `available=False`), minha decisão: em `ligado`, o pedido
**trava** com o motivo `fire_indisponivel` e volta a ser checado na próxima tentativa
de envio. A trava nunca desliga em silêncio, a mesma regra da tabela de preço.

**Não existe "ciente" para item sem produto**, ao contrário do preço. Para destravar,
só escolhendo o produto. Retirar a linha do pedido fica fora desta fase (minha
decisão): se o cliente pediu algo que a MM não faz, o pedido é corrigido com o
cliente.

A trava de produto vem **antes** da de preço. A trava de preço (PR #90) já trata item
sem produto como `nao_avaliado`, e passa a ler o `fire_seq` decidido aqui.

### 5.3 A tela

Na tela do pedido, que já tem o picker de produto (`openProductPicker`,
`app/web/static/index.html:1901`):

- O item travado aparece em destaque com o motivo em português ("o cliente mandou o
  código KG02PR, que não está cadastrado no Fire", "o EAN 789… está em 2 produtos").
- O picker abre já com:
  - os **candidatos exatos** (no caso `ambiguo`);
  - **sugestões por descrição**, que já existem (`product_ranking.py`), marcadas como
    sugestão;
  - a busca livre no catálogo.
- **A confirmação mostra lado a lado** a linha do cliente (código, EAN, descrição,
  quantidade, preço) e o produto escolhido (SEQ, código, descrição). Atributos
  divergentes entre as duas descrições (tamanho, número de pares, cor) aparecem em
  destaque, usando a mesma leitura da auditoria de 19/10. É um alerta, não bloqueia.
- Ao confirmar, o vínculo é gravado com o escopo (§4.1) e todos os itens com a mesma
  chave, neste pedido e nos outros pedidos parados do mesmo escopo, são rechecados.
- Uma lista **"Itens a identificar"** reúne todos os pedidos parados por produto no
  ambiente, para quem prefere resolver em fila.

## 6. O que vai para o Fire

O produto decidido precisa ser o produto gravado. Por caminho:

- **Insert direto (`EXPORT_MODE=db`):** entra pelo SEQ (`produtos_por_item`), como já
  faz a branch `fix/depara-fire-direto` (393ceeb, 02f7f9d). O SEQ passa a vir de todo
  item `ok`, não só do de-para. SEQ inexistente recusa o pedido inteiro, como a
  branch já faz.
- **Planilha (`EXPORT_MODE=xlsx`, o modo de produção hoje):** a coluna de código passa
  a levar o `CODPROD_ALTERN` do produto decidido, não o código do cliente nem o SEQ
  em texto que o `depara_apply` escreve hoje. O catálogo local (`catalogo_fire`) não
  tem `CODPROD_ALTERN`, então ganha essa coluna, preenchida no mesmo sync, e o
  check usa o valor lido do Fire na hora. O código
  do cliente vai na observação do item, como `depara_apply` já faz. Antes de gerar a
  planilha, o Portal roda a mesma previsão do importador do Fire (`_prever_import_fire`)
  para esse código. Se a previsão der outro SEQ, o pedido **trava**
  (`troca_no_fire`, que hoje só avisa), porque a planilha trocaria o produto. Sai
  pelo insert direto ou espera o cadastro ser ajustado.
- Produto decidido **sem `CODPROD_ALTERN`**: a planilha não consegue apontá-lo. Trava
  com o mesmo motivo.

## 7. O que vai para o Flow

- O `ItemRecebimento` (`app/integrations/flowpcp/schema.py:44`) ganha
  `erpProdutoId` (o SEQ decidido) e `fonteProduto` (`ean` / `depara` /
  `codprod_altern`). O código e o EAN do cliente continuam indo, para exibição.
- `confiancaParser` deixa de ser fixo: passa a vir do resultado do check (§9).
- **Lado Flow (plano próprio no pcp-app):** com `erpProdutoId`, o `/recebimento`
  liga o item ao produto cujo `fire_produto_id` é aquele SEQ (mesmo tenant, ativo).
  Sem `erpProdutoId`, ou com SEQ que o Flow não conhece, o item fica
  `nao_encontrado`. O Flow não casa por código nem por EAN para pedido vindo do
  Portal. Isso também tira o risco de o código do cliente coincidir com
  `produtos.codigo`, que é o SEQ em texto. A IA já saiu (#332).
- Os pedidos que o Flow já casou por `descricao_llm` são revisados à parte, com
  levantamento só de leitura e correção com OK do Samuel. Fora desta spec.

## 8. Migração e ordem de entrega

1. **Schema:** `produto_vinculo_v2`, a coluna `codprod_altern` em `catalogo_fire` e
   a migração das linhas de `produto_depara`, com OK do Samuel. Linha com CNPJ vira
   escopo pela regra §4.1. Linha com **nome** no lugar do CNPJ é casada com o CNPJ
   dos pedidos daquele cliente no histórico de `imports`. Se não houver um CNPJ único,
   ela não migra e aparece numa lista para revincular. A tabela antiga fica só para
   leitura até a v2 estar ligada.
2. **Check novo** (§2 e §5.1) atrás de um interruptor por ambiente de 3 estados, como
   o roteamento: `desligado` mantém a regra antiga; `observando` calcula e mostra,
   mas não trava; `ligado` trava. Começa em `observando` na MM.
3. **Trava** em commit, envio e exportação (§5.2) e tela (§5.3).
4. **Planilha e insert** com o produto decidido (§6).
5. **Contrato com o Flow** (§7): Portal primeiro (manda os campos novos, o Flow
   ignora o que não conhece) e depois o pcp-app.

## 9. Decisões (fechadas pelo Samuel em 19/10)

1. **`CODPROD_ALTERN` igual ao código do cliente casa sozinho?** Hoje casa, e é como
   Centauro, Kings e as famílias AFK/AWK funcionam: cerca de 3.300 itens conferidos em
   19/10 casaram assim, todos certos. O risco é o cliente mandar um código que, por
   coincidência, é o código de outro produto da MM. **Recomendação: casa sozinho**,
   com as mesmas regras de ativo e único, porque é igualdade exata contra um campo
   que a própria MM cadastra. **Decidido: casa sozinho.**
2. **Escopo do vínculo:** rede, depois raiz do CNPJ (§4.1). A alternativa é só o CNPJ
   exato, que obriga a vincular loja por loja. **Decidido: rede, depois raiz do CNPJ.**

**Decisões minhas, para conferir na leitura:** como as chaves se combinam (§2), qual
CNPJ define o escopo (§4.1), travar sem recusar (§5.2), Fire fora do ar trava
(§5.2), retirar linha fora desta fase (§5.2), e `confiancaParser`: `alta` só se
todos os itens estão `ok` por EAN ou código, `media` se algum veio de de-para. Pedido
com item fora de `ok` não vai ao Flow.

## 10. Testes

- Unitários do check novo: EAN único, EAN em 2 produtos, EAN só em bloqueado,
  de-para por rede, de-para por raiz, vínculo revogado, vínculo para SEQ bloqueado,
  código igual a `CODPROD_ALTERN`, item sem nada.
- Check: chaves que discordam (EAN → A, código → B) dão `chaves_discordam`; de-para
  que escolhe um dos candidatos de um EAN ambíguo dá `ok`.
- Trava: commit, envio, exportação e os caminhos sem preview seguram a planilha do
  pedido com item fora de `ok`, nos 3 estados do interruptor. Fire fora do ar em
  `ligado` trava.
- Planilha: código escrito = `CODPROD_ALTERN` do SEQ decidido; previsão de prefixo
  divergente trava.
- Regressão com os 338 arquivos guardados (`data/recebidos`): com o check novo, todo
  item que hoje está certo no Fire continua `ok` com o mesmo SEQ, e os 18 da
  Riachuelo de 19/10 saem `a_identificar`. Isso usa a cópia da auditoria de 19/10,
  fora do git.
