# Flow como fonte da verdade do cadastro de produto e material

**Status:** rascunho para revisão do Samuel, 16/10/2026.
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
| P0 | Descoberta: o que a tela do Fire grava ao criar um par, um kit e um material (diff de cópia do `.fdb`) | Portal | P3 gravar |
| P1 | Saneamento no Fire: NCM, unidade, tipo SPED e kits congelados, por planilha aprovada pelo Elias | Portal (script único, com backup) | Carga limpa |
| P2 | Ida ampliada: campos fiscais no produto, carga de material, NCM da última entrada como referência | Portal e Flow | Flow com tudo |
| P3 | Volta: fila de gravação, gravação no Fire e confirmação, desligada / simular / gravar | Portal e Flow | Virada |
| P4 | Cadastro e aprovação no Flow: telas, status, fila do Elias, herança do modelo | Flow | Uso real |
| P5 | Envio ao Apontaê pela API oficial | Flow | Virada do Apontaê |
| P6 | Pedido com produto desconhecido fica parado até o produto ser gravado | Portal | Virada |
| P7 | Virada: ida vira conferência, bloqueio no Fire (BLOQ_INSERIR e BLOQ_ALTERAR), bloqueio no Apontaê | Operação | — |

Ordem: P0 e P1 rodam em paralelo com P2. Depois vêm P3 e P4, juntas, pelo contrato da seção 6.3. Em seguida P5 e P6, e por último P7. A virada é feita **primeiro para produto e depois para material**, depois de testar se a nota de entrada do Fire precisa da tela de cadastro (seção 9).

## 6. Desenho do lado Portal

### 6.1 Ida: campos fiscais no produto (P2)

O contrato `catalogo.produtos.v1` ganha campos **opcionais**. Ausente = "não mexer", como em `componentes`.

| Campo (wire) | Origem no Fire | Regra |
|---|---|---|
| `ncm` | `CLASSIF_FISCAL` | Tira pontos e espaços. Só vai se ficar com 8 dígitos. Inválido vai ausente e entra em `pendenciasFiscais` |
| `unidadeFire` | `UNIDADE` | Texto cru, para conferência. A unidade normalizada é decidida no Flow |
| `tipoItemSped` | `CODITEMSPED` | Número de 0 a 10 ou 99. Fora disso vai ausente |
| `grupoFire` | `GRUPO` + `GRUPO_PRODUTOS.GRUPO` | `{codigo, nome}` |
| `codigoNfe` | `CODPARANFE` | Texto aparado |
| `cor` | `DESCRICAO2` | Texto aparado |

- Interruptor por empresa: `PORTAL_FLOWPCP_CATALOGO_FISCAL=mm`. Só se liga depois de o Flow aceitar os campos, porque hoje ele recusa campo desconhecido com 400. É o mesmo cuidado da composição.
- O relatório ganha `pendencias_fiscais`: a lista por SEQ de NCM inválido ou vazio, tipo SPED fora da tabela e unidade fora da lista de unidades do Fire (`PRODUTOS_UNIDADE`). A tela do ambiente mostra a contagem e a amostra.

### 6.2 Ida: carga de material (P2)

- **Contrato novo:** `catalogo.materiais.v1`. **Rota nova no Flow, a criar:** `POST /api/portal-pedidos/materiais`.
  - Mesma mecânica de `dryRun`/`fullSync`, relatório e trava do catálogo de produtos.
  - Interruptor próprio por ambiente: `flowpcp_materiais_push`, desligado por padrão, como os outros. Cópia local `materiais_fire` sempre.
- **Recorte:** `PRODUTOS` com `GRUPO <> 8` e `BLOQUEADO <> 'Sim'`.
- **Campos do item:** `fireProdutoId`, `codigo` (= SEQ), `nome`, `unidadeFire`, `ncm`, `tipoItemSped`, `grupoFire`, `ean`, `ativo` e `exigeAprovacao` (D5): verdadeiro quando o grupo é de produção **ou** o tipo SPED é 00 a 06 ou 10. O grupo 12 OUTROS, por exemplo, tem 30 itens com tipo 01 (matéria-prima), e esses passam pelo Elias.
- **Referência da nota:** `ncmUltimaEntrada` e `ultimaEntradaEm` (`NOTAPROD` com CFOP 1xxx, 2xxx ou 3xxx, nota mais recente). É só referência: quando difere do `ncm`, vira pendência para o Elias.
- **Casamento no Flow:** primeiro por `fire_produto_id`, depois por código igual ao SEQ, depois por nome normalizado único. O que não casar é criado. Ambíguo não grava e aparece no relatório.

### 6.3 Volta: gravação no Fire (P3)

**Contrato. Rotas novas no Flow, a criar:**
- `GET /api/portal-pedidos/cadastros/pendentes?limite=` devolve cadastros `aprovado` ainda sem gravação, cada um com:
  - `cadastroId` (uuid);
  - `versao`;
  - `acao`: `criar` ou `atualizar`;
  - `fireProdutoId`, quando é atualização;
  - `tipo`: `simples`, `kit`, `pack` ou `material`;
  - os campos da seção 6.1;
  - `componentes`;
  - `codigoMes`.
- `POST /api/portal-pedidos/cadastros/{cadastroId}/gravado` recebe `{versao, fireProdutoId, gravadoEm}` ou `{versao, erro}`. É idempotente por `(cadastroId, versao)`.

**Interruptor por ambiente:** `flowpcp_cadastro_volta`, com os valores `desligado` (padrão), `simular` e `gravar`.
- `simular` busca a fila, valida e monta a gravação, mas só registra no log e no relatório. Não grava no Fire e não confirma no Flow.
- `gravar` exige a P0 concluída e o ok do Samuel.

**Gravação, numa transação Firebird por cadastro:**
1. Livro local `cadastro_volta`, no SQLite do ambiente: grava `(cadastroId, versao, estado=reservado)` antes de tocar no Fire.
2. **Criar:**
   - calcula `SEQ = MAX(SEQ)+1`;
   - faz o `INSERT` em `PRODUTOS` e nas tabelas que a P0 mostrar obrigatórias;
   - para kit e pack, insere em `PRODUTOS_KIT`;
   - se outra gravação tomar o mesmo SEQ (violação de chave), recalcula e tenta de novo, até 3 vezes.
3. **Atualizar:** `UPDATE` só nas colunas que o Flow manda, ou seja, as da seção 6.1 mais nome, unidade, EAN e `CODPROD_ALTERN`. Para kit e pack, a composição é substituída em bloco.
4. Commit no Fire. Livro → `gravado`. Confirmação no Flow → livro `confirmado`.
5. **Recuperação:** um item que ficou `reservado` (queda entre o passo 1 e o 4) é procurado no Fire por nome exato e `DATA_CAD` do dia antes de qualquer nova tentativa. Se achar, adota o SEQ e confirma. Nunca grava em dobro.

**Regras de segurança:**
- A volta nunca apaga nada no Fire. Inativar = `BLOQUEADO='Sim'`.
- Toda escrita usa parâmetros, nunca texto concatenado.
- Lote máximo de 20 por ciclo.
- Erro em um cadastro não para os outros: vai para o Flow como `erro` e aparece na tela do ambiente.

### 6.4 Pedido com produto desconhecido (P6)

- O pedido cujo item não casa com produto do Fire, nem pelo de-para, fica em um estado novo, "aguardando cadastro".
- Quando a volta grava o produto, o pedido volta para a revisão.
- O detalhe fica numa spec própria, porque mexe na máquina de estados do pedido (`app/state/`).

### 6.5 Saneamento (P1)

- Script de linha de comando que lê uma planilha aprovada pelo Elias (SEQ, campo, valor atual, valor novo).
- Confere se o valor atual ainda é o mesmo e grava em uma transação.
- Antes, faz backup gbak.
- Só roda com o ok do Samuel, como foi feito na troca ATF→AF de 26/08.
- **Escopo:** NCM das meias (73 casos), unidade e tipo SPED dos pares (decisão do Elias, perguntas 2 e 3) e os 4 kits congelados (2538, 2614, 3151 e 3152).

## 7. Requisitos para o Flow (spec própria no `pcp-app`)

1. **`produtos`:** novos campos `ncm`, `unidade` (lista fechada), `tipo_item_sped`, `grupo_fire`, `codigo_nfe`, `cor`, `origem` (opcional) e `cest` (opcional), mais `produto_modelo_id`.
2. **`insumos`:** passa a ter `fire_produto_id`, `fonte`, `fire_sincronizado_em`, `ncm`, `tipo_item_sped`, `grupo_fire`, `ean` e `unidade` (lista fechada). O BOM e a lista de compras mostram só os grupos de produção.
3. **Status do cadastro**, igual para produto e material:
   - `rascunho → aguardando_fiscal → aprovado → gravado_fire`;
   - `devolvido` volta para `rascunho`, com observação obrigatória;
   - material de uso e consumo pula `aguardando_fiscal`;
   - toda edição de campo fiscal ou de composição em item já gravado volta para `aguardando_fiscal` e gera `versao` nova.
4. **Fila do Elias:** permissão nova `cadastro.aprovar_fiscal`. A tela mostra o que mudou em relação ao modelo e as conferências automáticas: NCM com 8 dígitos e existente, unidade compatível com o tipo, nome único e quantidade do kit igual ao nome.
5. **Ida:** aceitar os campos da seção 6.1 e a rota de materiais da seção 6.2. Depois da virada, só conferir.
6. **Volta:** rotas da seção 6.3.
7. **Apontaê** (P5), com a API oficial e o HMAC já previsto no Flow:
   - par aprovado vai ao Apontaê;
   - o id devolvido vira `codigoMes` e segue para o Fire numa atualização (`CODPROD_ALTERN`);
   - o Fire não espera o Apontaê.
8. **Herança da nota de entrada, fase 2:** cadastrar material a partir do XML da NF-e de compra, preenchendo NCM, EAN, unidade e descrição. A unidade e o tipo SPED continuam sendo decisão de quem cadastra e do Elias.

## 8. Testes e critérios de aceite (lado Portal)

- **Contrato:**
  - campos opcionais ausentes não aparecem no JSON;
  - NCM `61.159.500` → `61159500`;
  - `6115950061159500` → ausente + pendência;
  - tipo SPED 3 e 4 passam, 12 fica ausente.
- **Material:** o recorte exclui o grupo 8 e os bloqueados; `exigeAprovacao` segue a regra da 6.2 (grupo ou tipo SPED); a última entrada é escolhida pela data.
- **Volta:**
  - `simular` não escreve no Fire (o teste falha se houver INSERT ou UPDATE);
  - a corrida de SEQ tenta de novo;
  - a recuperação de `reservado` adota sem duplicar;
  - erro em um item não para o lote;
  - confirmação repetida é idempotente.
- **Prova real antes de `gravar`:** criar 1 par, 1 kit e 1 material numa **cópia** do `.fdb`. Abrir os três na tela do Fire, emitir uma NF-e de teste sem transmitir, se o Fire permitir, e comparar com um produto criado pela tela (P0).

## 9. Riscos

| Risco | Mitigação |
|---|---|
| A tela do Fire grava tabelas auxiliares que a volta não grava, e o produto fica "meio criado" | P0 obrigatória antes de `gravar`. Diff de cópia, prova na tela do Fire |
| A nota de entrada do Fire exige a tela de cadastro para item novo | Testar na MM com usuário bloqueado antes de fechar a porta do material. Material vira depois do produto |
| `LIBERA_TUDO` passa por cima do BLOQ_* | Testar na MM. Se passar, tirar `LIBERA_TUDO` dos 8 logins na virada |
| Ida e volta disputando o mesmo campo durante a transição | Antes da virada, a volta só **cria**. Atualizar só depois que a ida virar conferência |
| Saneamento grava valor errado em massa | Planilha aprovada, conferência do valor atual, backup gbak e ok explícito |
| SEQ disputado com a tela do Fire antes do bloqueio | Chave primária + nova tentativa. Na prática, some depois do bloqueio |

## 10. Fora de escopo

- Imposto no produto (D6).
- Os confeccionados fora do catálogo (D8).
- O Fire da Confecção (.4), que vira depois, com a mesma mecânica.
- Preço: a tabela de preço segue o caminho próprio (`project_tabela_preco_trava`).
- Bloco K (registro 0210), que fica possível depois, porque a composição e o material passam a ter dono.

## 11. Em aberto

- **Respostas do Elias**, no material enviado em 16/10: origem no produto, par 03 ou 04, unidades, figura fiscal padrão, CEST, campos pouco usados, NCM com ponto na nota e campo faltando.
- **Bloqueio no Apontaê:** resposta do João.
- **Testes na MM** (17/10): LIBERA_TUDO × BLOQ_*, aba "Itens do Kit" e nota de entrada com item novo.
