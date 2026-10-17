# Tabela de preço por rede + aprovação de preço — design

**Data:** 2026-10-17 · **Status:** rascunho v2 para revisão do Samuel
**Origem:** regras combinadas entre Samuel e Rafael (MM), fechadas em conversa 17/10.
**Sistemas:** **Flow** (pcp-app) é dono da tabela e da aprovação; **Portal** (este repo)
é o portão antes do Fire. Este documento cobre os dois lados; a implementação vira dois
planos, um por repo (mesmo formato do SP1).

---

## 1. Por que

A gravação direta no Fire (`EXPORT_MODE=db`, BACKLOG 4.1) e o lote Nasmar→MM dependem
de uma trava de preço confiável. Hoje não existe uma:

- **O Fire não tem tabela de preço em uso.** `check_order`
  (`app/erp/product_check.py:124`) compara com dados do Fire que não cobrem os KITs
  vendidos; na prática quase tudo cai em `no_price_in_fire`.
- A regra atual é por produto, não por cliente; desliga em silêncio se o Fire não
  responde (`is_blocking`, `:377`); o "ciente, sem preço" aceita qualquer usuário
  logado (`server.py:3420`); divergência não tem caminho de aprovação.

A partir da tabela assinada pelo Rafael (PDF, chega 19/10, só KITs), toda a gestão de
preço passa a ser feita **no Flow**, que já está na internet e é onde Rafael e Erica
conseguem entrar de qualquer lugar.

## 2. Objetivos

1. Tabela de preço **por rede**, por empresa, mantida no **Flow**.
2. Pedido com preço igual à tabela entra no Fire sem intervenção humana.
3. Qualquer outra situação de preço para numa **fila de aprovação no Flow** que só
   Rafael e Erica resolvem.
4. A decisão deles alimenta a tabela quando eles dizem que deve.
5. Histórico completo de preço e de decisão.
6. A trava **nunca desliga em silêncio**.
7. Queda de internet não para pedido que bate com a tabela.

## 3. Não-objetivos

- Ligar `EXPORT_MODE=db`. Esta feature é pré-requisito; ligar é decisão separada.
- Escrever preço no Fire como tabela. O Fire continua recebendo só o preço do pedido.
- Preço da perna intercompany Nasmar→MM (fica para o desenho do lote).
- Preço por par/variante. A tabela é de KIT, que é o que vende.
- Aviso por e-mail para Rafael/Erica: a infraestrutura existe no Flow (Resend), mas
  ligar exige OK do Samuel à parte (envio para fora).

## 4. Anti-objetivos (se acontecer, falhou mesmo funcionando)

- Pedido com preço errado entrar no Fire porque algo não foi avaliado.
- Operador conseguir aprovar preço.
- Reajuste reescrever o preço de pedido já aprovado.
- Tabela com dois donos (Portal e Flow editando) que divergem.
- Pedido entrar no pool de planejamento do Flow antes de ter o preço aprovado.

## 5. Regras de negócio (decididas)

### 5.1 Onde mora o preço
- **Dono:** o Flow, por tenant (empresa). MM e Nasmar têm tabelas independentes.
- **Cópia:** o Portal guarda uma cópia de leitura, sincronizada do Flow, e avalia
  contra ela. O Portal nunca edita preço.
- **Cliente = rede inteira** (Samuel). A rede é identificada pela **raiz do CNPJ**
  (8 dígitos); filiais e CDs da rede usam a mesma tabela.
- Chave do preço: (tenant, rede, KIT). KIT = produto do catálogo do Flow.

### 5.2 Estado da rede
- **Normal:** a regra da tabela vale (5.3).
- **Travada:** **todo** pedido da rede vai para aprovação, mesmo com preço igual.
  Travar é sobre o cliente, não sobre o item (Samuel). Não existe trava por item.

### 5.3 Avaliação de cada item (feita pelo Portal)

| Situação do item | Resultado |
|---|---|
| Produto ou cliente não identificado | **Bloqueia** ("não avaliado") |
| Cliente é empresa do grupo (perna intercompany) | Fora da trava, marcado |
| Rede travada | Fila de aprovação |
| KIT sem preço para a rede (recém-cadastrado) | Fila de aprovação (preço inicial) |
| Pedido sem preço no item | Fila de aprovação (aprovador informa) |
| Preço do pedido = vigente, ao centavo | **Entra direto** |
| Preço acima ou abaixo do vigente | Fila de aprovação |

O pedido só pode ser enviado (Fire ou XLSX) quando **todos** os itens estão OK ou
aprovados.

### 5.4 Aprovação (Rafael/Erica, no Flow)
Por item:

1. **Acordo comercial:** vale só para este pedido. Vai para o Fire o **preço do
   pedido**. A tabela não muda.
2. **Virar regra:** vai o preço do pedido e ele passa a ser o vigente da rede para o
   KIT, dali pra frente. Em preço acima da tabela o aprovador **tem que** escolher
   entre 1 e 2 (Samuel). Vale também para abaixo.
   - **KIT sem preço:** a aprovação sempre vira regra (preço inicial).
3. **Recusar:** o pedido não segue; volta ao operador do Portal com o motivo.

Pedido sem preço no item: o aprovador informa o preço (pré-preenchido com o da tabela,
se houver); esse é o que vai para o Fire.

A decisão grava quem, quando, preço do pedido, preço da tabela no momento, decisão e
observação. Pedido aprovado **congela** o preço.

### 5.5 Gestão da tabela (Rafael/Erica, no Flow)
- Cadastro de redes (nome + raízes de CNPJ). Clientes do Flow se ligam à rede pela raiz.
- Ver e editar o preço de um KIT numa rede, com motivo.
- **Reajuste em massa** por rede, percentual ou valor, com **data de início de
  vigência**. Preço antigo fica no histórico, nunca é apagado.
- Travar / destravar a rede, com motivo.

### 5.6 Permissão
- Permissão nova no Flow, **"aprova e gere preço"**, separada de `pedidos.aprovar`
  (que vale para qualquer `operator_pcp` e é larga demais;
  `src/lib/auth/tenant-permissions.ts`). Só Rafael e Erica, no começo.

## 6. Arquitetura

```
 Pedido chega no Portal
        │
        ▼
 Portal avalia cada item contra a CÓPIA da tabela ──── tudo OK ──► Fire / XLSX
        │                                                              │
        │ algum item na fila                                           ▼
        ▼                                                  (hoje) push do pedido
 Portal envia "pedido aguardando preço" ──► Flow: fila de aprovação     ao Flow
        │                                        │
        │                                Rafael/Erica decidem
        │                                        │
        ◄────────── Portal busca decisões ◄──────┘
        │           (e a cópia da tabela, se virou regra)
        ▼
 todos aprovados ──► Fire / XLSX com o preço aprovado
 algum recusado  ──► volta ao operador
```

### 6.1 Contrato Portal ↔ Flow (rotas novas no Flow — ainda não existem)
Seguem o padrão de `/api/portal-pedidos/*`: `X-Service-Token` + `X-Tenant-Id`,
idempotência, cursor com confirmação.

- **Tabela (Flow → Portal):** leitura incremental por cursor das redes (raízes,
  travada) e dos preços (rede, KIT, preço, vigência). O Portal mantém a cópia.
- **Pedido aguardando preço (Portal → Flow):** o pedido com os itens e o motivo de cada
  um que está na fila. Idempotente por pedido + versão do conteúdo.
- **Decisões (Flow → Portal):** por cursor, com confirmação, igual ao canal de prazo
  (`/api/portal-pedidos/decisoes`), que é reaproveitado como padrão, não como rota.

### 6.2 Fila de aprovação ≠ pedido do PCP
A aprovação de preço é uma **entidade própria** no Flow, separada de `pedido_items`.
O pedido só entra em `pedido_items` (via `/recebimento`, como hoje) **depois** de ir
ao Fire. Assim nada entra no pool de planejamento antes do preço aprovado.

### 6.3 Pré-requisitos encontrados
- **Jobs no processo web do Portal: já existe na `main`.** A fila do `/recebimento` e
  as decisões do Flow rodam numa thread do processo web
  (`app/integrations/flowpcp/envio_portal.py`; `PORTAL_FLOWPCP_ENVIO`, decisões
  desligadas por padrão via `PORTAL_FLOWPCP_DECISOES`). O sync da tabela e as decisões
  de preço entram nessa mesma thread.
- **Catálogo do Flow completo.** KIT que não existe no Flow não tem preço e cai em
  "não avaliado". Em 15/10 faltavam 45% dos produtos dos pedidos abertos; houve
  promote de 376 depois.
- **Spec do Flow de 03/10** (`2026-10-03-origem-dos-dados-fire-portal-flow-design.md`)
  diz que o preço nasce no Fire. Precisa ser corrigida para "preço nasce no Flow".

## 7. Encaixe no código

### Portal
- `check_order` deixa de buscar preço no Fire e passa a avaliar contra a cópia.
  `is_blocking` (`_send_one_to_fire` `server.py:2340`, `_export_one_xlsx` `:2561`)
  continua o ponto único de decisão e consulta as decisões recebidas.
- "Não avaliado bloqueia" inverte o default de `is_blocking` quando `available` é falso.
- Preço aprovado no Fire: hoje `order.unit_price` vai direto para `ERPRow`
  (`erp_exporter.py:271` → `mapper.py:136`), sem override. É preciso aplicar o preço
  informado pelo aprovador (caso "pedido sem preço").
- `PortalStatus` (`app/state/machine.py:22`) não muda; a situação de preço é derivada.
- O ack de sem-preço (`ack-sem-preco`, colunas `sem_preco_ack_*`) sai quando o
  ambiente estiver ligado e estável.
- Persistência: cópia da tabela e decisões no `app_state_<slug>.db` (`schema_env.py`).
  Mudança de schema: OK do Samuel antes.
- Tela: o preview mostra a situação de preço de cada item e "aguardando Rafael/Erica no
  Flow". Nenhuma tela de gestão no Portal.

### Flow (pcp-app)
- Não existe hoje: tabela de preço, preço em produto, rede/CNPJ raiz (só o rótulo
  livre `clientes.grupo_economico`), fila de aprovação de preço, permissão de preço,
  auditoria genérica.
- A criar: rede, tabela de preço com vigência e histórico, fila de aprovação, permissão,
  telas (Tabelas de preço, Aprovações de preço, Redes), as três rotas de 6.1, flag do
  tenant em `parametros_pcp.portalPedidos` (padrão das outras flags). Migration: OK do
  Samuel antes.

## 8. Ligar sem susto

- **Flow:** flag do tenant, nasce desligada.
- **Portal:** interruptor por ambiente com 3 estados, mesmo padrão do roteamento:
  - **desligado:** comportamento de hoje;
  - **observando:** avalia contra a cópia e mostra no preview, **não bloqueia** e não
    manda nada para a fila do Flow; mede quantos pedidos cairiam na fila;
  - **ligado:** regras da seção 5 valem.

Ordem: Flow com redes, tabela e carga do PDF → Portal
observando na MM → fila do Flow → Portal ligado na MM → Nasmar.

## 9. Carga inicial (PDF de 19/10)

- PDF assinado pelo Rafael, **só KITs**, carregado **no Flow**, tenant MM.
- Antes de carregar: conferir 1 preço por KIT, cada referência existe no catálogo do
  Flow, cobertura contra os KITs vendidos desde jul/26, e se há separação por cliente.
- **Depende do PDF:** se trouxer preço único, cada rede começa com cópia dele ou o
  modelo vira "base + exceção por rede". Samuel tende a uma tabela por rede; decide
  vendo o PDF.
- Linhas que não acham o KIT viram relatório, não entram em silêncio.
- Vigência da carga: data da assinatura.

## 10. Em aberto (do Samuel, decide com o PDF na mão)

- **Modelo de tabela** (9): uma por rede ou base + exceção. Trava o schema; por isso o
  plano do Flow deixa a migration da tabela para depois dessa decisão.
- **Base do preço:** o PDF é preço bruto (lista) ou líquido? Cada parser hoje entrega
  um preço (o Sam's traz "Preço Bruto", `sams_club_parser.py:270`). Na carga, comparar
  o PDF com os últimos pedidos de cada rede: se a base não bater, quase tudo cairia na
  fila. A resposta decide se algum parser precisa normalizar.

## 11. Chamadas minhas (Claude) — conferir na revisão

1. **Unidade:** o preço é por **produto vendido no Fire**. KIT tem sua linha; o PACK
   do Sam's é produto próprio no Fire (`UNIDADE='PACK'`) e tem **linha própria** na
   tabela, sem derivar de KIT × N (composição ainda não é confiável). Unidade
   desconhecida bloqueia.
2. **Rede** vem do cliente que vai para o Fire, depois do de-para. Pedido sem CNPJ
   bloqueia. A rede tem 1..N raízes; uma raiz pertence a **uma** rede. Exceção: um CNPJ
   completo pode apontar para outra rede e vence a raiz (grupos com bandeiras de acordo
   diferente sob a mesma raiz).
3. **Vigência** pela data de emissão do pedido; sem ela, data de recebimento.
4. **Aprovação presa à linha do pedido** (pedido + item + KIT + preço + quantidade).
   Editar ou reprocessar o item invalida a aprovação e reenvia para a fila. Recusado
   pode ser corrigido e voltar.
5. **"Virar regra" libera a fila:** o Flow reavalia na hora os itens pendentes da mesma
   rede e KIT; os que agora batem saem da fila sozinhos.
6. **Flow reavalia ao receber:** se a tabela mudou e o item já bate com o preço atual,
   o Flow aprova sozinho ("tabela atualizada"), sem pedir nada a Rafael/Erica.
7. **Frescor da cópia:** antes de enviar um pedido ao Fire, o Portal tenta atualizar a
   cópia. Se o Flow não responde, usa a cópia por até **24 h**; mais velha que isso,
   bloqueia com motivo visível.
8. **Pedido intercompany** fica fora da trava até o desenho do lote, com marca visível.
9. **Observando** mantém a trava de hoje valendo.
10. **Permissão por usuário**, não por papel: o Flow só tem permissão por papel hoje;
    "aprova preço" precisa ser concedida a pessoas específicas.
11. **Fila de preço separada do pedido do PCP** (6.2).
12. **Chave do produto entre os dois lados:** `produtos.fire_produto_id` do Flow =
    `str(SEQ)` do Fire (conferido: `app/erp/catalog_extract.py:10`).
13. **Preço aprovado também no XLSX:** o override vale para os dois caminhos (Fire e
    XLSX). O aprovador só muda o preço que vai no caso "pedido sem preço"; nos demais
    vai sempre o do pedido.
14. **Sync da tabela e decisões de preço reusam a thread do processo web** que já leva a
    fila do Flow (`envio_portal.py`), sem scheduler novo.
