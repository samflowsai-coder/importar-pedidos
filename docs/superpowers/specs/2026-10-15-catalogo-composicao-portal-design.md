# Composição de kits e vínculo com o Apontaê no catálogo (lado Portal)

**Data:** 15/10/2026 · **Status:** revisão 3: C3 decidido (kit/pack sem composição no Fire vai sem a chave)
**Par do lado Flow:** `pcp-app` `docs/superpowers/specs/2026-10-15-catalogo-erp-fase2-composicao-vinculo-mes-design.md`
(Fase 2, Fatias A e B). Contrato fechado com a sessão do Flow em 15/10.

## 1. Objetivo

O Flow só produz par. Para produzir kit e pack, ele precisa saber de que cada um é
feito, e para mandar a produção ao Apontaê precisa saber qual par do Apontaê é cada
par do Fire. As duas informações já existem no Fire:

- composição: `PRODUTOS_KIT` (pai, filho, quantidade);
- código do par no Apontaê: `PRODUTOS.CODPROD_ALTERN`, só dígitos. Em 426 produtos
  esse código é o `external_id` do Apontaê; 388 deles já batem com o vínculo que o
  Flow tem, sem nenhuma divergência.

Hoje o catálogo que o Portal manda não leva nenhuma das duas. Esta spec faz o
Portal mandar.

**Fora do escopo:** gravar composição no Flow (Fatia B do Flow); corrigir o cadastro
do Fire (revisão da MM + validação do fiscal); tela de kits no Portal (§8).

## 2. Decisões já tomadas

| Decisão | Quem | Quando |
|---|---|---|
| O Fire é a fonte da verdade do produto e da composição | Samuel | 15/10 |
| Composição divergente entre Fire e Flow: vale sempre o Fire | Samuel (via sessão Flow) | 15/10 |
| Código do par no Apontaê fica no código alternativo do Fire, só o número | Samuel (opção A) + medição | 15/10 |
| Ordem: Flow aceita no staging (Fatia A) → Portal manda → Flow grava (Fatia B) | as duas sessões | 15/10 |
| Kit/pack sem composição no Fire vai **sem a chave**; o PCP pode cadastrar uma composição provisória no Flow, que o Fire substitui quando for corrigido. "Congelar composição" fica para depois | Samuel (C3, opção 1) | 15/10 |
| Kit com quantidade divergente do nome vai congelado (sem a chave) até o Fire ser corrigido | Samuel | 16/10 |

## 3. Contrato (aditivo em `catalogo.produtos.v1`)

Dois campos novos e opcionais em cada item:

```jsonc
{
  "fireProdutoId": "2544",          // já existe
  "tipo": "kit",                    // já existe: simples | kit | pack
  "componentes": [                  // NOVO, um nível só
    { "erpProdutoId": "3306", "quantidade": 2 },
    { "erpProdutoId": "3364", "quantidade": 2 },
    { "erpProdutoId": "3308", "quantidade": 2 }
  ],
  "codigoMes": null                 // NOVO: string só de dígitos, ou null
}
```

Regras de `componentes` (as mesmas da spec do Flow):

| O Portal manda | Significa para o Flow |
|---|---|
| chave ausente | "não mexer" na composição desse produto |
| `[]` | "o ERP afirma que não tem composição": o Flow remove a dele |
| lista com itens | "a composição é exatamente esta": o Flow substitui a dele |

- `erpProdutoId` é o SEQ do filho, como string. O nome segue o padrão P-PROD-08 do
  Flow (papel do sistema, não nome do produto). `fireProdutoId` do item fica como está.
- `quantidade` > 0 e ≤ 100000, decimal aceito. No máximo 50 filhos por item.
- Todo filho tem que estar no mesmo payload. O recorte já inclui os componentes
  (CTE recursiva, desde o 20261015-1810).

Regra de `codigoMes`:

- vai **só para `tipo = simples` e ativo** (par). Kit, pack e par inativo mandam `null`;
- valor = `CODPROD_ALTERN` sem espaços, quando for só dígitos ASCII e tiver **até 9
  dígitos** (cabe no `integer` do Flow; código de cliente de 12 dígitos não passa);
  senão `null`;
- valor **repetido** em mais de um par do mesmo envio → `null` em todos os repetidos,
  com log (quem ganharia o vínculo dependeria da ordem; é o caso dos pares duplicados
  do listrado Sam's);
- o Portal não valida contra o Apontaê. Quem valida é o Flow (`external_id` do
  tenant, grava só se o vínculo estiver vazio, nunca sobrescreve).

Por que só para par: no recorte da MM, 116 kits têm código alternativo numérico, e
nenhum bate com o Apontaê (são códigos de cliente). Dos 410 pares com código
numérico, 405 batem.

## 4. Quando o Portal omite, manda `[]` ou manda a lista

O Portal só afirma o que leu. Na dúvida, omite a chave, e o Flow não mexe.

| Situação no Fire | `componentes` |
|---|---|
| leitura de `PRODUTOS_KIT` falhou | **ausente em todos os itens** do lote |
| `simples` sem linha em `PRODUTOS_KIT` | `[]` |
| kit/pack sem linha em `PRODUTOS_KIT` | ausente (C3): o Flow mantém a dele, inclusive a provisória do PCP |
| composição válida | a lista |
| algum filho fora do payload (excluído com `excluir=` ou fora do recorte) | ausente nesse item |
| filho repetido no mesmo pai | ausente nesse item |
| alguma quantidade ≤ 0 ou > 100000 | ausente nesse item |
| mais de 50 filhos | ausente nesse item |
| filho igual ao pai | ausente nesse item |
| filho ou quantidade nulos numa linha | ausente nesse item |

Cada caso "ausente nesse item" vai para o log com o SEQ e o motivo.

A leitura também conta como **falha** (ausente em todos) quando volta vazia ou
quando nenhum dos produtos marcados como pai no extract (`IS_KIT=1`) aparece nela:
base errada ou cópia vazia não pode virar "nenhum kit tem composição".

**Conferência de quantidade** (congela o kit): quando o nome traz a quantidade
("KIT C/5", "KIT 6", "PACK C/36") e a soma das quantidades da composição é diferente,
o kit vai **sem a chave** `componentes` (o Flow mantém o que tem) e o Portal registra
`composicao congelada` no log e em `divergencia_nome` no resumo. Pega erro de
digitação na carga da MM (12 no lugar de 2 vira OP 6× maior). Corrigido o Fire (a
composição ou o nome), o kit volta a ir no envio seguinte. Decisão do Samuel em
16/10, antes da Fatia B: os 4 kits da MM (2538, 2614, 3151, 3152) ficam congelados.

**Consequência do C3:** apagar no Fire a composição de um kit não chega ao Flow
(o kit passa a ir sem a chave e o Flow mantém a última). O caminho certo é corrigir a
composição, não apagar. Para não ficar invisível, o Portal registra no log e no
resumo quantos kits/packs foram sem composição, e o Flow avisa quando um kit com
composição vinda do ERP deixa de receber a chave.

Quando a MM corrigir um kit que tinha composição provisória no Flow, o Fire substitui
(vale sempre o Fire). No ajuste em massa isso pode passar da trava de substituição;
aí o apply é manual, com `confirmar_substituicao=1` e o ok do Samuel.

Medido no Fire da MM em 15/10 (recorte de 1.026):

- 271 produtos com composição, nenhum caiu nas regras de omissão;
- 321 kits/packs sem composição vão sem a chave (C3). O PCP pode cadastrar a
  composição deles no Flow como provisória enquanto a MM corrige o Fire;
- os 263 kits com composição no Flow têm composição idêntica no Fire (263/263).

As travas do Flow seguram só o tipo de mudança em massa: remoção (mais de 10 kits
ou 5%) e substituição (mais de 20 ou 10%); o resto do apply segue. Para liberar, o
corpo leva `confirmarRemocaoComposicao` / `confirmarSubstituicaoComposicao` (topo do
corpo, só com `dryRun=false`). No Portal, os dois chegam só pela rota manual
(`sync-catalogo?apply=true&confirmar_remocao=1&confirmar_substituicao=1`), nunca
pelo diário.

## 5. Onde muda no Portal

| Arquivo | Mudança |
|---|---|
| `app/erp/queries.py` | `LIST_PRODUTOS_KIT_COMPOSICAO`: `SELECT CODPRODUTO_PAI, CODPRODUTO, QTD FROM PRODUTOS_KIT` (uma consulta, sem filtro) |
| `app/erp/catalog_extract.py` | `ProdutoFireDTO` ganha `codigo_alternativo`; função nova `extract_composicao(fire_conn) -> dict[str, list[tuple[str, float]]]`; leitura isolada, a falha devolve `None` |
| `app/integrations/flowpcp/catalogo_composicao.py` (novo) | regras do §4: recebe DTOs + composição lida + `fora_do_envio`, devolve, por SEQ, `componentes` (ou ausente) e `codigoMes`; puro, sem I/O |
| `app/integrations/flowpcp/catalogo_schema.py` | `CatalogoComponente(erpProdutoId, quantidade)`; `CatalogoProdutoItem.componentes: list | None` e `codigoMes: str | None`; serializar **sem** a chave quando ausente (`exclude_unset`) |
| `app/integrations/flowpcp/catalogo_mapper.py` | monta os campos novos |
| `app/integrations/flowpcp/catalogo_sync.py` | chama a composição depois do extract; log do resumo (com lista, `[]`, ausente, motivos e divergência de quantidade) |
| `app/integrations/flowpcp/client.py` | apply (`dryRun=false`) **sem repetição** em timeout de leitura e com timeout próprio de 300 s; o dry-run segue com a política atual. O Flow não lê `Idempotency-Key` e passa a ter um apply por vez (409) |
| `app/web/routes_environments.py` | `confirmar_remocao` e `confirmar_substituicao` na rota manual; 409 do Flow vira "já há uma gravação em andamento" |
| `app/web/static/admin-ambiente-edit.html` | mostra os blocos `composicao` e `vinculoMes` do relatório, as travas ativas e `leitura_ok`; texto da confirmação diz que o apply troca composição e cria vínculo com o Apontaê |
| `app/integrations/flowpcp/catalogo_diario.py` | o resumo registra os blocos de composição e vínculo; trava ativa, leitura com falha ou `erpSemChave` > 0 viram WARNING; nunca manda as confirmações. A exclusão guardada (§6) chega pelo sync, sem código no diário |
| `app/http/client.py` · `app/http/policies.py` | `post_json` aceita uma política de repetição por chamada; política nova do apply: repete só quando a conexão nem abriu |

A cópia local (`catalogo_fire`) não muda nesta entrega: nada no Portal lê composição
hoje (§8).

## 6. Liga-desliga e ordem de entrada

1. **Flow publica a Fatia A**: aceita os campos novos e grava no staging, sem
   promover. Antes disso, `componentes` na forma nova dá 400 no lote inteiro, e o
   `codigoMes` é descartado em silêncio.
2. **Portal publica esta entrega com o envio desligado**:
   `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO` = lista de slugs (ex.: `mm`); vazio = ninguém.
   Por ambiente, para a Nasmar não entrar junto sem medição. Desligado, o payload é
   idêntico ao de hoje. Se o Flow recusar os campos (400 de contrato, por exemplo num
   rollback da Fatia A), a mensagem diz para desligar o interruptor.
3. Samuel liga; o dryRun mostra no staging do Flow o que viria. A sessão do Flow
   confere.
4. **Flow publica a Fatia B** (grava composição e vínculo). O primeiro apply com
   composição passa pelo ok do Samuel, com as contagens do relatório do Flow.

Interruptor por variável de ambiente, e não por campo no ambiente: é uma chave de
migração, temporária, que sai quando as duas pontas estiverem no ar. Não justifica
migração de schema.

**Diário em `gravar` com composição ligada:** a exclusão `excluir=` vale por rodada e
o diário não a conhece; com composição, os kits C/5 do listrado passariam a apontar
para os pares duplicados. Quando definida,
`PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>` (lista de SEQs) é aplicada pelo envio manual
**e** pelo diário, com a composição ligada ou não (o par duplicado do listrado é
problema mesmo sem composição). Assim a exclusão fica guardada sem migração. Valor
inválido ou SEQ que não está no extract: o envio falha sem mandar nada.

## 7. Testes

- `catalogo_composicao` (puro): cada linha da tabela do §4, mais `codigoMes` (par
  numérico, par com letras, kit numérico → `null`, espaços).
- schema: chave ausente não aparece no JSON; `[]` aparece; `codigoMes: null` aparece.
- sync: falha na leitura de `PRODUTOS_KIT`, leitura vazia ou incoerente com o
  `IS_KIT` → nenhum item com a chave; interruptor desligado (ou ligado para outro
  slug) → payload igual ao de hoje (comparação byte a byte com o atual).
- client: apply não repete em timeout de leitura; dry-run repete como hoje.
- rota: confirmações só com `apply=true`; 409 do Flow vira mensagem clara.
- diário: exclusão guardada aplicada; confirmações nunca enviadas.
- validação real, só leitura, no Fire de produção: contagens do §4 batem.
- contrato: validar um payload real contra o Zod do Flow (Fatia A), como foi feito no
  SP1.

## 8. Depois desta entrega (não incluído)

- **Visão de kits e packs no Portal.** O Samuel pediu que Portal e Flow mostrem kit e
  pack corretamente. No Portal isso pede guardar a composição na cópia local (tabela
  nova → migração) e uma tela. Spec própria, depois que a composição estiver fluindo.
- **Ajuste em massa no Fire** a partir da planilha revisada pela MM, depois do fiscal.
- **"Congelar composição" no Flow** (o Fire deixa de substituir um kit específico): adiado, a provisória cobre a urgência.
- **Ligar o envio diário em `gravar`** só depois do Fire corrigido (listrado Sam's) e
  sem exclusão pendente.

## 9. Riscos

| Risco | Mitigação |
|---|---|
| Portal afirmar `[]` por falha de leitura e o Flow apagar composição boa | falha de leitura = chave ausente em todos; trava de volume no Flow |
| Código de cliente virar vínculo com o Apontaê | `codigoMes` só para par; Flow valida contra `external_id` e nunca sobrescreve |
| Deploy fora de ordem quebra o catálogo (400) | interruptor nasce desligado; ligar só depois da Fatia A no ar |
| Composição do Fire mudar por causa do fiscal | o dryRun de cada envio traz as contagens; apply com ok do Samuel |
| Apply demorado repetido pelo Portal → dois applies ao mesmo tempo | apply sem retry em timeout (300 s); Flow com um apply por vez (409) |
| Código do Apontaê digitado errado liga a outra meia | Flow confere o nome normalizado contra o Apontaê (`nomeDivergente` não grava); ação de admin para corrigir |
| Edição do Fire pela metade lida no meio | Flow só remove ou tira filhos depois de duas leituras iguais |
| QTD errado na carga da MM | conferência contra a quantidade do nome: o kit vai congelado (sem a chave), com log, resumo e atenção no diário |
| Composição apagada no Fire não chega ao Flow (C3) | contagem de kits sem composição no resumo; aviso do Flow para kit de origem ERP que deixou de receber a chave |
