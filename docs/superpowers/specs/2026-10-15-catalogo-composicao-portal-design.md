# Composição de kits e vínculo com o Apontaê no catálogo (lado Portal)

**Data:** 15/10/2026 · **Status:** proposta para o Samuel aprovar
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

- vai **só para `tipo = simples`** (par). Kit e pack mandam `null`;
- valor = `CODPROD_ALTERN` sem espaços, quando for só dígitos; senão `null`;
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
| produto sem linha em `PRODUTOS_KIT` | `[]` |
| composição válida | a lista |
| algum filho fora do payload (excluído com `excluir=` ou fora do recorte) | ausente nesse item |
| filho repetido no mesmo pai | ausente nesse item |
| alguma quantidade ≤ 0 ou > 100000 | ausente nesse item |
| mais de 50 filhos | ausente nesse item |
| filho igual ao pai | ausente nesse item |

Cada caso "ausente nesse item" vai para o log com o SEQ e o motivo.

Medido no Fire da MM em 15/10 (recorte de 1.026):

- 271 produtos com composição, nenhum caiu nas regras de omissão;
- 321 kits/packs sem composição iriam com `[]`. Hoje nenhum deles tem composição no
  Flow, então o `[]` não remove nada;
- os 263 kits com composição no Flow têm composição idêntica no Fire (263/263).

A "trava" do Flow (para a etapa de composição se o apply remover a composição de
mais de 10 kits ou de mais de 5%) protege contra o caso de a MM esvaziar
composições no Fire por engano.

## 5. Onde muda no Portal

| Arquivo | Mudança |
|---|---|
| `app/erp/queries.py` | `LIST_PRODUTOS_KIT_COMPOSICAO`: `SELECT CODPRODUTO_PAI, CODPRODUTO, QTD FROM PRODUTOS_KIT` (uma consulta, sem filtro) |
| `app/erp/catalog_extract.py` | `ProdutoFireDTO` ganha `codigo_alternativo`; função nova `extract_composicao(fire_conn) -> dict[str, list[tuple[str, float]]]`; leitura isolada, a falha devolve `None` |
| `app/integrations/flowpcp/catalogo_composicao.py` (novo) | regras do §4: recebe DTOs + composição lida + `fora_do_envio`, devolve, por SEQ, `componentes` (ou ausente) e `codigoMes`; puro, sem I/O |
| `app/integrations/flowpcp/catalogo_schema.py` | `CatalogoComponente(erpProdutoId, quantidade)`; `CatalogoProdutoItem.componentes: list | None` e `codigoMes: str | None`; serializar **sem** a chave quando ausente (`exclude_unset`) |
| `app/integrations/flowpcp/catalogo_mapper.py` | monta os campos novos |
| `app/integrations/flowpcp/catalogo_sync.py` | chama a composição depois do extract; log do resumo (com lista, `[]`, ausente e motivos) |

A cópia local (`catalogo_fire`) não muda nesta entrega: nada no Portal lê composição
hoje (§8).

## 6. Liga-desliga e ordem de entrada

1. **Flow publica a Fatia A**: aceita os campos novos e grava no staging, sem
   promover. Antes disso, `componentes` na forma nova dá 400 no lote inteiro, e o
   `codigoMes` é descartado em silêncio.
2. **Portal publica esta entrega com o envio desligado**:
   `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO` (`0` padrão / `1`). Desligado, o payload é
   idêntico ao de hoje.
3. Samuel liga; o dryRun mostra no staging do Flow o que viria. A sessão do Flow
   confere.
4. **Flow publica a Fatia B** (grava composição e vínculo). O primeiro apply com
   composição passa pelo ok do Samuel, com as contagens do relatório do Flow.

Interruptor por variável de ambiente, e não por campo no ambiente: é uma chave de
migração, temporária, que sai quando as duas pontas estiverem no ar. Não justifica
migração de schema.

## 7. Testes

- `catalogo_composicao` (puro): cada linha da tabela do §4, mais `codigoMes` (par
  numérico, par com letras, kit numérico → `null`, espaços).
- schema: chave ausente não aparece no JSON; `[]` aparece; `codigoMes: null` aparece.
- sync: falha na leitura de `PRODUTOS_KIT` → nenhum item com a chave; interruptor
  desligado → payload igual ao de hoje (comparação byte a byte com o atual).
- validação real, só leitura, no Fire de produção: contagens do §4 batem.
- contrato: validar um payload real contra o Zod do Flow (Fatia A), como foi feito no
  SP1.

## 8. Depois desta entrega (não incluído)

- **Visão de kits e packs no Portal.** O Samuel pediu que Portal e Flow mostrem kit e
  pack corretamente. No Portal isso pede guardar a composição na cópia local (tabela
  nova → migração) e uma tela. Spec própria, depois que a composição estiver fluindo.
- **Ajuste em massa no Fire** a partir da planilha revisada pela MM, depois do fiscal.
- **Ligar o envio diário em `gravar`** só depois do Fire corrigido (listrado Sam's) e
  sem exclusão pendente.

## 9. Riscos

| Risco | Mitigação |
|---|---|
| Portal afirmar `[]` por falha de leitura e o Flow apagar composição boa | falha de leitura = chave ausente em todos; trava de volume no Flow |
| Código de cliente virar vínculo com o Apontaê | `codigoMes` só para par; Flow valida contra `external_id` e nunca sobrescreve |
| Deploy fora de ordem quebra o catálogo (400) | interruptor nasce desligado; ligar só depois da Fatia A no ar |
| Composição do Fire mudar por causa do fiscal | o dryRun de cada envio traz as contagens; apply com ok do Samuel |
