# Flow como fonte da verdade do cadastro de produto e material

**Status:** revisada (Fable, 16/10) e com contrato fechado com a sessão do Flow; aguardando revisão do Samuel.
**Repositórios:** Portal de Pedidos (este, dono da ponte com o Fire) e FlowPCP (`pcp-app`). Este documento é a spec-mãe e detalha o lado Portal. O lado Flow vira spec própria no `pcp-app`, a partir da seção 7.
**Relacionados:** `2026-10-15-catalogo-composicao-portal-design.md` (composição e vínculo com o Apontaê, em produção), `docs/ai/modules/flowpcp.md` §4.

---

## 1. Objetivo

Hoje o cadastro de produto nasce em três lugares: Fire, Apontaê e Flow. O Portal copia o Fire para o Flow todo dia. A partir da virada:

- Produto (par, kit, pack) e material (matéria-prima, embalagem, aviamento, uso e consumo) **nascem no Flow**.
- O que entra na produção passa pela **aprovação fiscal do Elias** antes de existir em outro sistema.
- O Flow grava no Fire **pelo Portal**, e no Apontaê **pela API oficial**.
- O cadastro fica **bloqueado** nas telas do Fire e do Apontaê.

**Pronto quando:**
1. Todo produto e material ativo do Fire existe no Flow com o código do Fire e os campos fiscais.
2. Um cadastro novo aprovado no Flow aparece no Fire sem ninguém abrir a tela do Fire.
3. Ninguém consegue cadastrar produto direto no Fire nem no Apontaê.

## 2. Decisões já tomadas (Samuel, 16/10)

| # | Decisão |
|---|---|
| D1 | O Flow é a fonte da verdade do cadastro. A carga inicial vem do Fire. Depois da virada, tudo nasce no Flow. |
| D2 | O caminho para o Fire é Flow → Portal → Fire. Para o Apontaê, é direto do Flow pela API oficial (doc TL 207). |
| D3 | Bloquear **todo** o cadastro na tela "Produtos e Materiais" do Fire, não só as meias. |
| D4 | Material também nasce no Flow, aproveitando o cadastro de insumos que já existe (`/materiais`). |
| D5 | O Elias aprova só o que entra na produção: produto, fio, matéria-prima, tecido, aviamento, embalagem e resíduo. Uso e consumo vai direto, com conferência automática. |
| D6 | O imposto continua fora do produto, na figura fiscal do Fire (por operação). O Flow não cuida de CFOP, CST nem alíquota. |
| D7 | Cadastro novo pode herdar os dados de um produto modelo. |
| D8 | Confeccionados que não são meias do catálogo ficam no Fire como histórico e não são carregados. |
| D9 | Apontaê: implementar entre o fim de outubro e o início de novembro. Empresa e cliente da integração são os mesmos já combinados com o João. |

## 3. Estado atual (medido em 16/10, Fire da MM Americanense, só leitura)

**Tela "Produtos e Materiais" (tabela `PRODUTOS`):** 3.842 cadastros, 22 grupos.

| Bloco | Grupos do Fire | Não inativos | Entra no Flow |
|---|---|---|---|
| Meias do catálogo | 8 CONFECCIONADO, recorte "só meias" | 771 (503 pares, 268 kits) | Já entram (produtos) |
| Outros confeccionados | 8 | ~2.655 | Não (D8) |
| Produção | 1 FIO, 4 MATÉRIA-PRIMA, 5/6 TECIDO, 11 AVIAMENTOS, 13 RESÍDUOS, 19 EMBALAGEM | ~273 | Sim, como material com aprovação |
| Uso e consumo | 2, 9, 12, 14, 15, 16, 17, 18, 20, 21, 22 | ~108 | Sim, como material sem aprovação |

- O campo `INATIVO` quase não é usado: só 35 estão marcados. O catálogo usa `BLOQUEADO <> 'Sim'` como ativo, e a carga de material segue o mesmo critério.
- Em 2026 foram 835 cadastros. Destes, 502 vieram numa leva única em abril; fora isso, ~30 confeccionados por mês, 3 a 7 matérias-primas por mês e ~60 itens de uso e consumo entre junho e julho.

**Campos fiscais no produto:**
- NCM (`CLASSIF_FISCAL`), nas 771 meias: 698 certos, 51 com pontos no lugar errado, 3 inválidos e 19 vazios.
- Tipo do item SPED (`CODITEMSPED`): os pares estão divididos entre 03 e 04.
- Unidade em texto livre.
- Código na NF-e (`CODPARANFE`) em 752 das 771.
- ICMS, IPI e ORIGEM vazios. A tributação vem de `FIGURA_FISCAL` (35 figuras).

**Gravação no Fire:**
- `PRODUTOS.SEQ` é a chave primária, sem gerador: a aplicação faz MAX+1. Hoje o maior é 3991.
- Não há gatilho nem chave estrangeira em `PRODUTOS`.
- A composição fica em `PRODUTOS_KIT`, com chave `CODIGO`, também sem gerador.
- 38 tabelas referenciam o produto, entre elas `ESTOQUE`, `TABELA_PRECO_PRODS` e `NOTAPROD`. O que a tela do Fire grava junto ao criar um produto ainda **não é conhecido** (Fase 0).

**Nota de entrada:** `NOTAPROD` guarda o NCM por item.
- Desde 2025, matéria-prima (grupo 4) teve 1.520 itens de entrada, de 111 produtos.
- Todos os itens têm NCM.
- Em 986 deles (65%), o NCM da nota é igual ao do cadastro.

**Flow, código em `origin/main` 7da8046:**
- `produtos` tem `fire_produto_id`, `fonte`, `fire_sincronizado_em`, `id_produto_mes`, `tipo` e `composicao_origem`. Não tem NCM, tipo SPED nem origem.
- `insumos` tem só código, nome, unidade, estoque mínimo e ativo. Não tem vínculo com o Fire, campos fiscais nem status.
- Não existe status de aprovação em nenhum cadastro.

## 4. Arquitetura alvo

```
                 aprovação (Elias)
 cadastro ──► FLOW ───────────────┬──► API oficial ──► APONTAÊ   (pares)
                ▲                 │
                │ ida (carga e    │ volta (fila de gravação)
                │ conferência)    ▼
              PORTAL ◄──── busca a fila (pull), grava e confirma
                │ ▲
                ▼ │ SELECT / INSERT / UPDATE
                FIRE
```

- **Quem chama quem:** o Portal busca a fila no Flow (pull) e confirma a gravação. O Flow não alcança o Fire, que está na rede da MM. É o mesmo padrão de `poll_decisoes`, que já grava decisões do Flow no Fire.
- **Ida (Fire → Flow):** é o sincronismo de hoje, ampliado com os campos fiscais e com material. Antes da virada, o Fire manda. Depois da virada, a ida vira **conferência**: só aponta divergência e cadastro feito por fora, nunca sobrescreve o Flow.
- **Volta (Flow → Fire):** é nova. Cria e atualiza produto, composição e material aprovados. Nasce desligada.

## 5. Peças e sequência

| Peça | O quê | Onde | Libera |
|---|---|---|---|
| P0 | Descoberta. Criar um par, um kit e um material pela tela do Fire numa cópia do `.fdb` e fazer o diff. Medir os tamanhos de coluna. Fazer o diff de `DIREITOS` com e sem `LIBERA_TUDO` | Portal | P3 em `gravar` |
| P1 | Saneamento no Fire: NCM, unidade, tipo SPED e kits congelados, a partir de planilha aprovada pelo Elias | Portal (`tools/sanear_cadastro_fire.py`, com dry-run e backup) | Carga limpa |
| P2 | Ida ampliada: campos fiscais no produto, carga de material, NCM da última entrada | Portal e Flow | Flow com tudo |
| P3 | Volta: fila de gravação, gravação no Fire, confirmação | Portal e Flow | Virada |
| P4 | Cadastro e aprovação no Flow: telas, status, fila do Elias, herança do modelo | Flow | Uso real |
| P5 | Envio ao Apontaê pela API oficial | Flow | Virada do Apontaê |
| P6 | Pedido com produto desconhecido fica parado até o produto ser gravado | Portal | Melhoria, **fora do caminho crítico** |
| P7 | Virada: a ida vira conferência e o cadastro é bloqueado no Fire (BLOQ_INSERIR/BLOQ_ALTERAR) e no Apontaê | Operação | — |

- P0 e P1 rodam em paralelo com P2. P3 e P4 andam juntas, presas ao contrato da 6.3. Depois vêm P5 e P7.
- P6 não é pré-requisito da virada. O fluxo manual cobre: cadastrar no Flow, a volta grava e o pedido é reprocessado.
- A virada é **primeiro para produto, depois para material**. Antes de virar o material, é preciso saber se a nota de entrada do Fire exige a tela de cadastro (seção 9).

**Etapas de entrega, combinadas com o Flow em 16/10:**
- **Etapa 1** (sem migração em nenhum dos lados). Portal: ida fiscal, carga de material em dryRun e volta em `simular`. Flow: rotas 1 a 4 validando o contrato.
- **Etapa 2** (migração do Flow, com ok do Samuel). Colunas, promote fiscal e `dryRun=false` de material.
- **Etapa 3** (P4 + P0). Fila real (`cadastros.v1`) e volta em `gravar`.

## 6. Desenho do lado Portal

### 6.0 Capacidades: como o Portal sabe o que o Flow aceita

O Zod do Flow (`catalogoProdutosV1`) é `z.object` sem `.strict()`. Ele **descarta em silêncio** a chave que não conhece, sem devolver 400. Mandar campo novo para um Flow que ainda não o aceita dá 200, e o dado some sem aviso.

Por isso toda resposta do Flow passa a trazer `capacidades: string[]`, e o Portal decide pelo que a lista diz:

| Capacidade | O que significa | O que o Portal faz sem ela |
|---|---|---|
| `fiscal.v1` | o catálogo aceita e guarda os campos fiscais | Com o interruptor fiscal ligado, registra ERROR, põe o diário em atenção e a tela mostra "o Flow ignorou os campos fiscais". O lote não falha |
| `materiais.dryrun.v1` | a rota de materiais existe e calcula o relatório | Não envia material |
| `materiais.v1` | `dryRun=false` de materiais grava | Recusa o apply antes de enviar (422 no Portal). O Flow responderia 409 `promote_indisponivel` |
| `cadastros.v1` | a fila de gravação é real | `simular` roda assim mesmo (só lê, não grava nem confirma); só os modos de gravação (`criar`, `criar_e_atualizar`) a exigem e recusam iniciar sem ela |

Os nomes no wire seguem o papel do campo (regra P-PROD-08 do Flow): `erpProdutoId`, `unidadeErp`, `grupoErp`. O `fireProdutoId` do catálogo de produtos fica como está, por compatibilidade.

### 6.1 Ida: campos fiscais no produto (P2)

`catalogo.produtos.v1` ganha campos **opcionais**. A regra vale nos dois sentidos: ausente = "não mexer", e `null` também = "não mexer". O Portal **nunca** manda valor inválido: o que não passa na regra vai ausente, e o Flow derruba o lote com 400 se receber algo fora do formato.

| Campo (wire) | Origem no Fire | Regra do Portal |
|---|---|---|
| `ncm` | `CLASSIF_FISCAL` (varchar 20) | Tira pontos, espaços e hífens. Vai só se sobrar exatamente 8 dígitos (`^\d{8}$`). Senão vai ausente e entra em `pendencias_fiscais` |
| `tipoItemSped` | `CODITEMSPED` (inteiro) | Inteiro de 0 a 10 ou 99. Fora disso vai ausente + pendência |
| `grupoErp` | `GRUPO` (inteiro) + `GRUPO_PRODUTOS.GRUPO` | `{codigo: str, nome: str 1..80}`. Sem nome vai ausente |
| `codigoNfe` | `CODPARANFE` (varchar 20) | Aparado, de 1 a 60 caracteres |
| `cor` | `DESCRICAO2` (varchar 200) | Aparado, de 1 a 120 caracteres. Se for maior, vai ausente + pendência |

- A unidade continua no campo `unidade`, que já existe e já leva o `UNIDADE` cru. A lista fechada de unidades é decisão do Flow (P4).
- Interruptor por empresa: `PORTAL_FLOWPCP_CATALOGO_FISCAL=mm`, mesmo formato de `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO`. É variável de ambiente, sem migração.
- O relatório do Portal ganha `fiscal_portal`:
  - `enviados`;
  - `pendencias` por SEQ e motivo: `ncm_invalido`, `ncm_vazio`, `sped_invalido`, `cor_longa`;
  - `capacidade_ok`.
- O Flow devolve o resumo dele em `fiscal`.

### 6.2 Ida: carga de material (P2)

**Rota no Flow:** `POST /api/portal-pedidos/materiais`, contrato `catalogo.materiais.v1`. Autenticação, `X-Tenant-Id` e rate limit são os mesmos do catálogo. A flag do tenant no Flow é `materiaisErpAtivo`; sem ela, a rota responde 403.

**Corpo:**
- `schema`, `dryRun`, `fullSync`, `origem {importadorVersao, extraidoEm}`.
- `itens`, de 1 a 5000, cada um com:
  - `erpProdutoId` (= SEQ, obrigatório);
  - `codigo` (= SEQ);
  - `nome` (1..255);
  - `unidadeErp` (1..16 ou `null`);
  - `ncm?`, `tipoItemSped?`;
  - `grupoErp {codigo, nome}`;
  - `ean` (até 32 ou `null`);
  - `ativo`, `exigeAprovacao`;
  - `ncmUltimaEntrada?`, `ultimaEntradaEm?` (data civil, `YYYY-MM-DD`).

**Recorte:** `PRODUTOS` com `GRUPO <> 8` e o grupo na tabela do ambiente (abaixo). Os bloqueados vão com `ativo=false`; o Flow não cria insumo novo inativo (`inativoSemInsumo`), então só serve para desativar o que ele já tem.

**Regra de `exigeAprovacao`:** verdadeiro quando o grupo está na lista de produção do ambiente, `PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_<SLUG>` (padrão da MM: `1,4,5,6,11,13,19`), **ou** quando o tipo SPED está entre 0 e 6 ou é 10. A regra canônica fica no Flow (D5). Se a do Flow der diferente, vale a dele, e a divergência aparece em `exigeAprovacaoDiverge`.

**Última entrada:** o NCM do item mais recente em `NOTAPROD` de nota com CFOP 1xxx, 2xxx ou 3xxx, normalizado como na 6.1, e a data de emissão dessa nota.

**Casamento no Flow:**
- No **apply**, só por `erp_produto_id`.
- Código igual ao SEQ e nome normalizado único são apenas **sugestão** no dryRun (`porCodigo`, `porNome`, `codigoNomeDiverge`). Quem confirma é uma pessoa, na tela do Flow.
- O motivo: esse vínculo vira a chave da volta, e casar errado faria a volta gravar o material errado no Fire.

**Outras regras do Flow:**
- Unidade sem mapa conhecido não cria o insumo e vai para `unidadeSemMapa`.
- A unidade de um insumo que já tem BOM não é trocada.
- `fullSync` nunca apaga nem desativa insumo do Flow: o que só existe no Flow fica em `soNoFlow`.
- Material bloqueado no Fire desativa no Flow só quando não está em BOM nem tem saldo. Se estiver, vira pendência.

**No Portal:**
- Interruptor `PORTAL_FLOWPCP_MATERIAIS=mm`.
- Cópia local `materiais_fire`, sempre gravada (tabela nova no SQLite do ambiente, criada pelo próprio código).
- Rota admin `POST /api/admin/environments/{env_id}/flowpcp/sync-materiais?apply=`. É nova e segue o padrão de `sync-clientes`.
- Ferramenta de linha de comando para simular.
- `apply=1` sem a capacidade `materiais.v1` dá 422 antes de enviar qualquer coisa.

### 6.3 Volta: gravação no Fire (P3)

#### Contrato (rotas no Flow)

**`GET /api/portal-pedidos/cadastros/pendentes?limite=50`** (máximo 50 no servidor, sem cursor: o ack tira o item da fila). Como `simular` não confirma, em simulação a volta só enxerga os 50 mais antigos.

A resposta é `{capacidades, cadastros: [...]}`. Cada cadastro traz:
- `cadastroId` (uuid);
- `versao` (inteiro ≥ 1);
- `acao`: `criar`, `atualizar` ou `inativar`;
- `erpProdutoId` (`null` em `criar`);
- `tipo`: `simples`, `kit`, `pack` ou `material`;
- `nome`, `unidadeErp`, `ean`, `ncm`, `tipoItemSped`, `grupoErp {codigo}`, `codigoNfe`, `cor`;
- `componentes`: em `criar` de kit/pack, obrigatório e não vazio; em `atualizar` de kit/pack, `null` = não mexer na composição (lista não nula segue as regras do `criar`); em `inativar`, ignorado; em `simples`/`material`, sempre `null` (qualquer outro valor, inclusive `[]`, é `contrato_invalido`);
- `codigoMes`;
- `aprovadoEm`.

Garantia do Flow: um kit só entra na fila quando todos os componentes já têm `erpProdutoId`.

**`POST /api/portal-pedidos/cadastros/{cadastroId}/gravado`** recebe um destes corpos:
- `{versao, resultado: "gravado", erpProdutoId, gravadoEm}`;
- `{versao, resultado: "erro", erro: {tipo: "transitorio"|"definitivo", codigo, mensagem}}`.

Respostas do Flow e o que o Portal faz com cada uma:

| Resposta | Portal |
|---|---|
| 200 `confirmado` / `ja_confirmado` | ok, encerra o item no livro |
| 409 `versao_superada` | descarta a versão local e pega a nova no próximo ciclo |
| 409 `conflito_erp_id` / `erp_id_diverge` | ERROR + alerta, item `incerto`, sem nova tentativa |
| 404 | ERROR, item `incerto` |

#### Validação de borda (antes de qualquer acesso ao Fire)

1. O item passa por um modelo pydantic com `extra="forbid"`, enums fechados e limites por coluna medidos no Fire:
   - `nome` até 200, `cor` até 200, `unidadeErp` até 10, `ean` até 15, `codigoNfe` até 20, `codigoMes` até 30;
   - `ncm` com `^[0-9]{8}$`, e `tipoItemSped` de 0 a 10 ou 99;
   - texto opcional (`unidadeErp`, `ean`, `codigoNfe`, `cor`, `codigoMes`) é `null` ou não vazio: `""` é `contrato_invalido`;
   - IDs só com dígitos.

   A P0 confirma esses limites.
2. **Codificação WIN1252:** caractere que não codifica dá erro definitivo `caractere_invalido`. Nunca grava texto corrompido.
3. **`atualizar` e `inativar`:** só valem quando o SEQ está na cópia local do ambiente (`catalogo_fire` ou `materiais_fire`). Um SEQ fora dela, como os confeccionados históricos de D8, dá erro definitivo `seq_fora_do_escopo`.
4. **`inativar`** é ação explícita, nunca deduzida de um campo `ativo`, e tem portão próprio (`PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR`, desligado por padrão).
5. **Componentes:** todos existentes no Fire e não bloqueados, sem o próprio pai, até 50, quantidade com 0 < q ≤ 100000 (as mesmas regras de `catalogo_composicao`). `criar` de kit ou pack sem componentes dá erro definitivo; `atualizar` com `componentes: null` não mexe na composição nem consulta componentes no Fire.
6. **`criar`:** o nome não pode existir no Fire, comparado aparado e sem diferença de maiúsculas (em Python, `casefold`, sobre a leitura de `SEQ, DESCRICAO` uma vez por ciclo: o `UPPER` do Firebird não é confiável para Ç/Ã/Ú em WIN1252). Se existir, dá erro definitivo `nome_existe` com o SEQ encontrado, para uma pessoa resolver (pode ser cadastro feito por fora).
7. Item inválido nunca é gravado pela metade. A mensagem de erro que vai ao Flow tem até 500 caracteres, sem SQL, sem caminho de arquivo e sem segredo.

#### Quem roda

A volta roda na thread `flowpcp-envio` (`envio_portal.ciclo`), no mesmo lugar do catálogo diário. Fora dela, com `PORTAL_FLOWPCP_ENVIO=0`, não roda: um consumidor só por ambiente.

Interruptor: `PORTAL_FLOWPCP_CADASTRO_VOLTA=<slug>:<modo>` (exemplo: `mm:simular`), sem migração.

| Modo | Comportamento |
|---|---|
| `desligado` (padrão) | não consulta a fila |
| `simular` | busca a fila, valida (inclusive contra o Fire, numa conexão **somente leitura** `connect_somente_leitura`) e monta o plano de gravação. Registra em log, métrica e livro com estado `simulado`. **Não grava no Fire e não confirma no Flow** |
| `criar` | grava só `acao=criar` (até a virada) |
| `criar_e_atualizar` | também `atualizar` (e `inativar` com o portão próprio). Só depois que a ida virar conferência |

Os modos `criar` e `criar_e_atualizar` exigem a capacidade `cadastros.v1` e a P0 concluída. Até lá, o código desses modos **recusa iniciar** e registra o motivo.

#### Gravação (modos `criar` e `criar_e_atualizar`)

Uma transação Firebird por cadastro, com `commit()` e `rollback()` explícitos, como em `fire_update.py`.

1. **Lease no livro local** `cadastro_volta`, no SQLite do ambiente: `INSERT (cadastro_id, versao, estado='em_andamento', iniciado_em)` com chave `(cadastro_id, versao)`. Se a chave já existir, outro ciclo já pegou o item e este pula.
2. **Reserva do SEQ**, sob um lock de processo (o mesmo princípio do `_insert_lock` de `firebird_exporter.py`):
   - transação nova em READ COMMITTED;
   - `SEQ = MAX(SEQ)+1`, gravado no livro (`seq_reservado`) **antes** do INSERT;
   - `INSERT` em `PRODUTOS` e nas tabelas que a P0 mostrar obrigatórias;
   - kit e pack: `INSERT` em `PRODUTOS_KIT`, com `CODIGO = MAX(CODIGO)+1` por linha na mesma transação;
   - violação de chave (SEQ ou CODIGO tomado): rollback e tentativa nova **em transação nova**, até 3 vezes, regravando `seq_reservado`.
3. **`atualizar`:**
   - `UPDATE` só nas colunas que o Flow manda, sempre com `WHERE SEQ = ?`;
   - composição: dentro da mesma transação, as linhas do pai em `PRODUTOS_KIT` são substituídas pela lista nova;
   - é idempotente: repetir dá o mesmo resultado.
4. **`inativar`:** `UPDATE PRODUTOS SET BLOQUEADO='Sim' WHERE SEQ = ?`. A volta nunca apaga linha de `PRODUTOS`. A única troca de linhas é a composição de `PRODUTOS_KIT`, e só pelo pai.
5. Depois: commit, livro → `gravado`, ack ao Flow, livro → `confirmado`. Se o ack falhar, o livro fica em `gravado` e o próximo ciclo só refaz o ack, sem tocar no Fire.

#### Recuperação

Um item que ficou `em_andamento` (o processo caiu) é resolvido assim:
1. `SELECT` por `SEQ = seq_reservado`.
   - Se a linha existe e nome, unidade, grupo, EAN e NCM batem com o cadastro: adota o SEQ e segue para o ack.
   - Se a linha existe mas é diferente: outra gravação tomou o SEQ. Recria.
   - Se a linha não existe: o INSERT não comitou. Recria.
2. Sem `seq_reservado` (caiu antes da reserva), procura por nome e aceita **só** um resultado exato em (nome, `DATA_CAD`, `GRUPO`, `UNIDADE`). Zero resultados: recria. Mais de um: estado `incerto`.
3. **`incerto`:** o item fica bloqueado, aparece na tela do ambiente e no log como ERROR, e uma pessoa resolve. Nunca grava em dobro e nunca adota no chute.

#### Lote e erro

- Até 20 cadastros por ciclo. Erro em um não para os outros.
- **Erro transitório:** conexão com o Fire, lock ou timeout. O item continua na fila.
- **Erro definitivo:** validação da borda, `nome_existe`, componente ausente. O item sai da fila e aparece na tela do cadastro no Flow.

### 6.4 Pedido com produto desconhecido (P6, fora do caminho crítico)

Fica para uma spec própria. Mexe na máquina de estados do pedido (`PortalStatus` não tem estado para isso). Até lá, o fluxo manual cobre: cadastrar no Flow, a volta grava e o pedido é reprocessado no Portal.

### 6.5 Saneamento (P1)

**Ferramenta:** `tools/sanear_cadastro_fire.py` (nova). Lê uma planilha aprovada pelo Elias, com SEQ, campo, valor atual e valor novo, e faz o seguinte:
1. Por padrão roda em **dry-run**: imprime o diff e não grava.
2. Com `--gravar`, confere se o valor atual ainda é o mesmo, faz backup gbak e grava numa transação só.

Só roda com o ok do Samuel.

**Escopo:**
- NCM das meias: 73 casos (51 com ponto, 3 inválidos e 19 vazios);
- unidade e tipo SPED dos pares, conforme a decisão do Elias;
- os 4 kits congelados: 2538, 2614, 3151 e 3152.

### 6.6 Observabilidade

**Métricas em `/metrics`:**
- `portal_flowpcp_cadastro_volta_total{resultado}`, com os resultados `simulado`, `criado`, `atualizado`, `inativado`, `adotado`, `erro_transitorio`, `erro_definitivo`, `incerto`;
- `portal_flowpcp_cadastro_volta_pendentes`;
- `portal_flowpcp_cadastro_volta_mais_antigo_segundos`.

**Auditoria por gravação**, com `cadastroId`, `versao`, SEQ, ação e modo.

**Tela do ambiente:**
- o modo da volta;
- os itens `incerto`;
- o último ciclo;
- as pendências fiscais da ida.

## 7. Requisitos para o Flow (spec própria no `pcp-app`, em andamento na branch `feat/flow-fonte-cadastro`)

1. **`produtos`:** novos campos `ncm`, `unidade` normalizada (lista fechada, com mapa por tenant), `tipo_item_sped`, `grupo_erp`, `codigo_nfe` (até 20), `cor`, `origem` (opcional) e `cest` (opcional), mais `produto_modelo_id`.
2. **`insumos`:**
   - novos campos `erp_produto_id`, `fonte`, `erp_sincronizado_em`, `ncm`, `tipo_item_sped`, `grupo_erp_*`, `ean` e `unidade_erp`;
   - o BOM e a lista de compras mostram só os grupos de produção.
3. **Status do cadastro**, igual para produto e material:
   - fluxo `rascunho → aguardando_fiscal → aprovado → gravado_erp`, e `devolvido` volta para `rascunho`, com observação obrigatória;
   - material de uso e consumo pula `aguardando_fiscal`;
   - edição fiscal ou de composição num item já gravado gera versão nova e volta para a aprovação.
4. **Fila do Elias**, com a permissão `cadastro.aprovar-fiscal`. O nome é validado contra os limites da 6.3 já na aprovação.
5. **Precedência na transição:**
   - produto ou insumo com `fonte=flow` ou já `gravado_erp` nunca é sobrescrito pela ida;
   - depois da virada, a ida só confere.
6. **Capacidades** (6.0) em toda resposta das rotas `/api/portal-pedidos/*` novas e do catálogo.
7. **Apontaê** (P5): o par aprovado vai pela API de entrada; o id devolvido vira `codigoMes` e segue para o Fire como `atualizar`.
8. **Herança da nota de entrada** (fase 2): material criado a partir do XML da NF-e de compra.

## 8. Testes e critérios de aceite (lado Portal)

**Ida fiscal:**
- campos ausentes ou `None` não aparecem no JSON;
- NCM: `61.159.500` → `61159500`, enquanto `6115950061159500`, `611595` e `6115950A` ficam ausentes e entram em `pendencias`;
- tipo SPED: 3, 4 e 99 passam; 12 e `None` ficam ausentes;
- com o interruptor desligado, o payload é byte a byte igual ao de hoje;
- com o interruptor ligado e a resposta sem `fiscal.v1`, o log tem ERROR e o diário fica em atenção.

**Material:**
- o recorte exclui o grupo 8; bloqueados vão com `ativo=false`;
- `exigeAprovacao` segue o grupo **ou** o tipo SPED (o grupo 12 com tipo 1 dá verdadeiro);
- a última entrada escolhe a nota mais recente com CFOP de entrada;
- `apply` sem `materiais.v1` dá 422 e não envia nada;
- a cópia local é gravada mesmo com o interruptor desligado.

**Volta:**
- `extra` desconhecido é recusado;
- cada limite de coluna é testado;
- caractere fora do WIN1252 dá erro definitivo;
- `atualizar` num SEQ fora do escopo dá erro definitivo;
- `simular` usa a conexão somente leitura, e o teste falha se houver qualquer `execute` de escrita;
- o lease impede o processamento em dobro;
- os modos de gravação recusam iniciar sem `cadastros.v1`;
- cada 409 tem o tratamento da tabela;
- o ack que falhou é refeito sem tocar no Fire.

**Testes que entram com a gravação** (etapa 3):
- a corrida de SEQ tenta de novo em transação nova;
- a recuperação por `seq_reservado` cobre os três casos, e a busca por nome com mais de um resultado vira `incerto`.

**Prova real antes de `criar`:** criar 1 par, 1 kit e 1 material numa **cópia** do `.fdb`, abrir os três na tela do Fire e comparar com produtos criados pela tela (P0).

## 9. Riscos e rollback

| Risco | Mitigação |
|---|---|
| Produto "meio criado" no Fire, faltando tabela que a tela grava | P0 obrigatória, com prova numa cópia aberta na tela do Fire |
| Adotar SEQ errado na recuperação | `seq_reservado` antes do INSERT, conferência de campos e `incerto` em vez de chute |
| Flow manda dado errado ou malicioso | Validação de borda da 6.3, com escopo restrito à cópia local e `inativar` com portão próprio |
| Campo fiscal some em silêncio | Capacidades (6.0) |
| Material casado errado vira chave da volta | Apply só por `erp_produto_id`; código e nome são sugestão confirmada por uma pessoa |
| Ida e volta disputando campo | Modo `criar` até a virada, precedência do Flow para `fonte=flow` |
| A nota de entrada exige a tela de cadastro para item novo | Testar na MM com usuário bloqueado; o material vira depois do produto |
| `LIBERA_TUDO` passa por cima do BLOQ_*, e tirá-lo mexe em todas as telas | P0 mede o diff de `DIREITOS`. Conceder os direitos específicos antes de remover o `LIBERA_TUDO` |
| Saneamento em massa errado | Dry-run com diff, conferência do valor atual, backup e ok explícito |

**Rollback da virada:**
1. Reabrir o bloqueio no Fire (BLOQ_*='Nao' nos logins) e no Apontaê.
2. A volta passa para `desligado`, e a ida volta a mandar: sai de conferência e passa a gravar.
3. A tela lista os cadastros criados no Flow durante a janela que já foram gravados no Fire. Esses ficam: o Fire tem o SEQ, e a ida os reconhece pelo `erpProdutoId`.
4. A tela também lista os cadastros aprovados e ainda não gravados. Esses voltam para `rascunho` no Flow, ou são cadastrados no Fire à mão.
5. Nada é apagado.

## 10. Fora de escopo

- Imposto no produto (D6).
- Os confeccionados fora do catálogo (D8).
- O Fire da Confecção (.4), que vira depois, com a mesma mecânica.
- Preço: a tabela de preço segue o caminho próprio (`project_tabela_preco_trava`).
- Bloco K (registro 0210), que fica possível depois, porque a composição e o material passam a ter dono.

## 11. Em aberto

- **Respostas do Elias**, no material enviado em 16/10: origem no produto, par 03 ou 04, unidades, figura fiscal padrão, CEST, campos pouco usados, NCM com ponto na nota e campo faltando.
- **Bloqueio no Apontaê:** resposta do João.
- **Perfil medido em 18/10/2026** (P0 parte A, `tools/perfil_cadastro_fire.py` com a mesma lógica, Fire da MM Americanense, só leitura; últimos 30 kits e os 11 pares com `UNIDADE='PAR'` do grupo 8). Colunas que a tela preenche em todos e que o gravador ainda não manda: `EST_MINIMO_GERENCIA='Nao'` e `PRODUZIDO='Nao'` (30/30 e 11/11); quase sempre: `CODORIG_MERC=0` (29/30 kits, 4/11 pares), `CODSUBGRUPO=1` (26/30 kits, 1/11 pares), `PRE_CADASTRO='Nao'` (15/30, 8/11). `DESCRICAO2` (descrição interna) vem preenchida em 27/30 kits e 11/11 pares, e a volta não a grava (R3 da Etapa 3). Candidatos a `PADROES_P0`, a confirmar no diff da tela (parte B).
- **Testes na MM** (17/10): LIBERA_TUDO × BLOQ_*, aba "Itens do Kit" e nota de entrada com item novo.
