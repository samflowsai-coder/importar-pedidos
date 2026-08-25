# Roteamento intercompany Nasmar → MM — design

**Data:** 2026-08-24
**Status:** aprovado para implementação
**Domínios:** `environments`, `erp`, `persistence`, `web`, `worker`, `consolidators`

---

## O problema

O Portal importa todo pedido no **ambiente que o operador selecionou** — na prática,
quase sempre a MM. Mas boa parte dos varejistas não compra da MM: compra da **Nasmar**,
que é revenda. DAJU, Beira Rio, Dakota, Centauro, Authentic Feet, Art Walk faturam
contra a Nasmar, não contra a MM.

A cadeia comercial real tem duas pernas:

```
DAJU  ──compra──▶  NASMAR  ──compra──▶  MM AMERICANENSE
                  (revende)             (produz e fatura pra Nasmar)
```

Isso já é operado hoje — **à mão, em duplicidade**. A operadora digita o pedido duas
vezes: uma no Firebird da Nasmar com o cliente final, outra no Firebird da MM com a
Nasmar como cliente. O Portal não participa de nenhuma das duas.

O que a feature entrega: o operador loga no ambiente de sempre, o Portal reconhece que
o pedido é de uma rede que compra da Nasmar, **importa a perna de origem no ambiente
Nasmar**, e monta a **perna espelho no ambiente MM** com a Nasmar como cliente e um
rastro auditável de quem é o cliente final.

---

## Fatos medidos na Fire viva (2026-08-24, via VPN, somente leitura)

Tudo abaixo foi lido dos dois bancos de produção. Nada é suposição.

**Os dois bancos.** `nasmar` = `192.168.15.4` / `MM_CONFECCAO.FDB` (NASMAR COMÉRCIO DE
ROUPAS LTDA, 34.513.679/0001-34). `americanense` = `192.168.15.7` /
`MM_AMERICANENSE.FDB` (M.M. AMERICANENSE, 35.394.871/0001-11). A Nasmar existe como
cliente no `.7` em `CADASTRO.CODIGO = 2`, `RELAC_CLIENTE = 'Sim'`.

**1. O Portal não escreve no Fire hoje.** `ULT_INS_USER` dos últimos 12 meses, nos dois
bancos: FISCAL, GRAZIELLY, GRASIELY, KAROLAINE, CAMILA, MILENA, BIANCA. **Zero
`IMPORTADOR`.** O caminho `EXPORT_MODE=db` existe no código mas nunca rodou em produção.
Isso torna a Fase 0 (mapper completo) pré-requisito, não polimento.

**2. A tabela de preço do Fire está morta.** `CADASTRO.CODTABELAPRECO` da Nasmar é
`NULL`. E não é exceção: **603 dos 640 clientes ativos** estão sem tabela vinculada. As
9 tabelas cadastradas têm no máximo 176 produtos com `VALOR > 0` de ~600 linhas. Não é
fonte de preço utilizável.

**3. O fator de preço entre as pernas existe, mas não é derivável.** Casei 50 pedidos
pelas duas pernas e comparei o total:

| Lote / pedido | Rede | razão `.4` ÷ `.7` |
|---|---|---|
| 14004066, 14004139 | Beira Rio | 1,0700 (exato, 33 itens conferidos um a um) |
| OC-70610 | DAJU | 1,0700 (exato, 18 itens) |
| #4616 (1171, 1174, 1175) | Xambre / AF | 1,0698 |
| #4619 (8 pedidos) | H2S4 / AF+AW | 1,0696 |
| #4587 (6 pedidos) | Barbara, Stela, Pinheiro, Rocave / AF+AW | **1,0000** |
| Centauro (Calcenter), 2026 inteiro | Centauro | **1,0000** |

Duas redes com a mesma marca (AF+AW) e fatores diferentes. **Não existe regra nos
dados.** Consequência de desenho: o fator é cadastrado, nunca inferido, e a ausência
dele **bloqueia** — ver "Fator de preço" abaixo.

**4. O agrupamento já existe, hand-typed, e o eixo é a rede — não o cliente final.**
`CAB_VENDAS.OBS` (que o mapper deixa `NULL`, `app/erp/mapper.py:74`) está preenchido em
2.232 de 4.263 pedidos e guarda a **lista dos `CAB_VENDAS.CODIGO` do `.4`** — o número
interno do pedido na Nasmar:

```
.7 #4619 (14/08)  OBS='PEDIDOS: 1161, 1160, 1159, 1167, 1162, 1157,1158, 1189.'
   → .4 #1157 AF179 H2S4    #1158 AF200 H2S4    #1159 AW026 H2S4    #1160 AW070 H2S4
     .4 #1161 AW086 H2S4    #1162 AW094 H2S4    #1167 AF017 H2S4    #1189 MF072 FKSHOES

.7 #4587 (05/08)  OBS='PEDIDOS 1177 1179 1180 1183 1186 1187 '
   → seis clientes finais DIFERENTES: BARBARA, STELA, AC PINHEIRO, PINHEIRO,
     ROCAVE, ALEXANDRE — todos da mesma rede (Authentic Feet / Art Walk)
```

O `#4587` é a prova de que o eixo não é o CNPJ do cliente final: Authentic Feet são ~20
CNPJs de franqueado sob uma marca.

**5. Os itens são somados por produto.** Quantidade preservada exata, linhas colapsadas:

| Lote MM | pernas `.4` | linhas | quantidade |
|---|---|---|---|
| #4616 | 3 pedidos | 20 → **8** | 468 → **468** |
| #4619 | 8 pedidos | 168 → **65** | 1.739 → **1.739** |
| #4587 | 6 pedidos | 71 → **36** | 1.410 → **1.410** |

**6. A data de entrega do lote é digitada e está errada.** `DT_ENTREGA_ITEM` é `0/65`
nos lotes — ninguém preenche por item. A data vai só no header, à mão. Resultado: o lote
`#4616` tem `DT_ENTREGA = 2028-08-21` enquanto suas três pernas têm `2026-08-21`. Ano
digitado errado, em produção, hoje. O `#4587` tem `DT_ENTREGA = NULL`.

**7. A idempotência do exporter já estaria quebrada.** `CHECK_ORDER_EXISTS`
(`app/erp/queries.py:91`) casa por `PEDIDO_CLIENTE + CLIENTE`. Nos 332 pedidos da Nasmar
no `.7` desde 2025 há só **272 chaves distintas**: `''` ×15, `'NASMAR'` ×12, `'AF184'`
×5, `'STUDIO Z'` ×5. Reproduzir a convenção atual faria o Portal recusar o segundo lote
de cada rede como duplicata.

**8. O prefixo da OBS é hábito, não convenção.** Coexistem `PEDIDO`, `PEDIDOS`,
`PEDIDO N.`, `PEDIDO:`, `PEDIDO NASMAR` e `PEDODO` (typo), com separador ` - `, `, ` ou
espaço. Formalizar não perde informação.

**9. O mapper escreve 9 de 98 colunas.** Perfil de 373 pedidos do `.7` e 90 do `.4`,
tudo de 2026-06-01 em diante — ver "Fase 0".

---

## Escopo

**Dentro:** cadastro de rede intercompany; roteamento do pedido para o ambiente de
origem pelo CNPJ do cliente final; consolidação em lote por rede e janela; criação da
perna espelho no ambiente MM; registro explícito do vínculo entre as pernas; fechamento
da lacuna de colunas do mapper; correção da perna de volta (Flow) para o vínculo
registrado.

**Fora:** ressuscitar a tabela de preço do Fire (fato 2); reabertura de lote fechado;
estorno automático da perna espelho; qualquer mudança no fluxo de pedidos que não caem
numa rede cadastrada.

---

## Arquitetura

### Decisão central: o vínculo passa a ser registrado, não inferido

Hoje `app/erp/depara_cliente.py` deduz quem é o cliente real por **coincidência de
`PEDIDO_CLIENTE`** nos dois bancos. Isso funciona porque as duas pernas são digitadas
com o mesmo número. Com lote, essa coincidência deixa de existir por construção: o lote
tem chave própria e junta N pedidos.

Como o Portal passa a criar as duas pernas, ele **sabe** o vínculo. Grava. O
`depara_cliente.py` continua existindo como fallback para os pedidos legados e para os
que a operadora cadastrar à mão — não é removido, é rebaixado de fonte da verdade para
plano B.

### Modelo de dados (`app_shared.db`, transversal — `db.connect_shared()`)

```
rede_intercompany
  id, nome, codigo, env_origem_slug, env_espelho_slug, cnpj_revenda,
  fator_preco (NULLABLE), janela ('diaria'|'semanal'), dia_fechamento, ativo
    codigo         ^[A-Z0-9]{2,6}$ — entra na chave de lote
    dia_fechamento só vale para janela='semanal' (0=segunda .. 6=domingo);
                   ignorado em 'diaria'

rede_cnpj
  rede_id, cnpj (digits, UNIQUE global), rotulo

intercompany_lote
  id, rede_id, chave_lote (UNIQUE), janela_inicio, janela_fim,
  status ('aberto'|'aguardando_fator'|'fechado'|'enviado'|'erro'),
  fator_preco_aplicado, fator_informado_por, fator_informado_em,
  env_espelho_slug, fire_codigo_espelho, import_id_espelho,
  created_at, closed_at, closed_by

intercompany_lote_item
  lote_id, import_id, env_origem_slug, fire_codigo_origem, pedido_cliente
```

`rede_intercompany` e `rede_cnpj` são transversais (um pedido pode chegar por qualquer
ambiente, e a decisão de roteamento acontece **antes** de existir ambiente ativo). Vão no
shared, ao lado de `environments` — mesma regra de `environments.md`.

`fator_preco` é **NULLABLE de propósito**. Ver abaixo.

### Roteamento da perna de origem

Novo módulo `app/routing/intercompany.py`:

```python
def rota_para(order: Order) -> Rota | None:
    """Normaliza o CNPJ do cliente e busca em rede_cnpj.

    Achou  → Rota(rede_id, env_origem_slug, env_espelho_slug)
    Não achou → None (comportamento de hoje, ambiente selecionado)
    """
```

Reusa `app/erp/cnpj.py::cnpj_digits` — não duplica normalização.

**Onde é chamado:** no boundary de criação da linha em `imports`, **antes** do
`repo.insert_import`. `environment_id` é bind imutável (`environments.md`, "Bind
imutável") — não dá pra corrigir depois. Dois call-sites:

- `app/web/server.py` — o commit do preview
- `app/worker/jobs/scan_environments.py` — o watcher de pasta

A UI mostra o desvio explicitamente no preview: *"Este pedido vai para o ambiente
NASMAR (rede Authentic Feet)"*. Roteamento silencioso é como o bug de hoje nasceu; não
vamos trocar um silêncio por outro.

### Perna de origem: comportamento inalterado

Preview → de-para de produto → check de preço → "Cadastrar no Fire" → INSERT no `.4`.
Cliente = cliente final. `PEDIDO_CLIENTE` = número do pedido do cliente. Preço = preço
do pedido, sem fator.

Única adição: capturar o `CAB_VENDAS.CODIGO` gerado (o exporter já o conhece — é o
`header_pk` que ele passa pro mapper), gravar em `imports.fire_codigo` e anexar ao lote
aberto da rede via `intercompany_lote_item`.

### Consolidador — `app/consolidators/` ganha dono

O pacote está vazio e reservado para "merge de pedidos (v2)" desde o começo do projeto
(`CLAUDE.md`, mapa do repositório). É exatamente este caso.

```python
def consolidar(
    pernas: list[PernaDoLote],
    *,
    cnpj_revenda: str,
    nome_revenda: str,
    chave_lote: str,
    fator_preco: Decimal,
) -> Order
```

Função **pura** — sem I/O, sem Firebird, sem SQLite. Testável com `Order` construído à
mão. Regras, todas derivadas do comportamento medido:

| Campo | Regra | Vem do fato |
|---|---|---|
| Cliente | CNPJ e razão da revenda (Nasmar) | — |
| Itens | somados por `(produto, data_entrega)` | 5 |
| Preço unitário | preço da perna ÷ `fator_preco` | 3 |
| `DT_ENTREGA` (header) | **menor** data entre as pernas | 6 |
| `DT_ENTREGA_ITEM` | preenchido por item | 6 |
| `PEDIDO_CLIENTE` | chave de lote | 7 |
| `OBS` | `PEDIDOS: 1157, 1158, 1159, 1189` | 4, 8 |

`DT_ENTREGA` como a **menor** data é escolha deliberada: é a restrição real de produção,
e é conservadora — errar pra antes atrasa nada, errar pra depois perde prazo. Corrige de
graça o `2028-08-21` que está gravado hoje.

`DT_ENTREGA_ITEM` por item atende o requisito de preservar as datas combinadas com cada
cliente final. O Fire suporta a coluna e ninguém usa.

### Chave de lote

Formato: `<CODIGO_REDE>-<AAAAMMDD>-<NN>` → `AF-20260814-01`.

- **Única** — resolve as 60 colisões do fato 7 e destrava `CHECK_ORDER_EXISTS`
- **Opaca** — não expõe cliente final nem número de pedido de ninguém
- **Legível** — mantém o `AF` / `AW` / `NASMAR` que a operação já lê
- **Cabe na coluna** — `rede.codigo` é validado em `^[A-Z0-9]{2,6}$`, então a chave tem
  no máximo 18 caracteres (`NASMAR-20260814-01`) e nunca é truncada pelo `[:20]` do
  `mapper.py:64`. Truncar chave de lote silenciosamente colidiria duas redes.

### Fator de preço: sem fator, o lote não fecha

`fator_preco` é `NULLABLE` e **não tem default**. Quando o lote vence a janela e a rede
não tem fator cadastrado, o lote entra em `status='aguardando_fator'` e para. A tela de
lotes pede o fator ao operador; `fator_informado_por` e `fator_informado_em` gravam quem
respondeu e quando.

Por quê: preço errado na perna espelho é **base de cálculo errada na nota fiscal** —
imposto recolhido a menos ou a mais, com prejuízo real. Um default de 1,0 "para não
travar" é exatamente a falha silenciosa que a regra do projeto proíbe. O fato 3 prova que
o valor certo não é inferível dos dados; qualquer default seria um chute com consequência
fiscal.

O fator vigente é copiado para `fator_preco_aplicado` no fechamento. Mudar o cadastro
depois não reescreve lote já fechado.

### Fechamento do lote

Job `app/worker/jobs/fechar_lotes.py` no APScheduler existente. Para cada rede ativa,
fecha o lote cuja janela venceu. Fechar = consolidar + criar o `imports` da perna
espelho no ambiente MM + enfileirar o INSERT.

Tela `/lotes`: lote aberto com as pernas dentro, total, fator vigente, botão "fechar
agora" e o campo de fator quando estiver `aguardando_fator`.

Pedido que chega depois do fechamento entra no **próximo** lote. Lote fechado nunca
reabre. Isso acontece de verdade — o `#1189` de 11/08 entrou no lote de 14/08 junto com
pedidos de 27/07.

### A perna de volta

`app/integrations/flowpcp/poll_decisoes.py` casa a decisão do Flow contra o Firebird
onde o pedido está. Com lote, **o pedido do cliente final deixa de existir 1:1 no `.7`** —
lá só existe o lote, no nome da Nasmar. A decisão tem que voltar para a perna de origem
no `.4`, resolvida por `intercompany_lote_item`. Determinístico, sem inferência.

Esta é a terceira aparição da mesma família de bug: o prazo de entrega (PR #37) e o
de-para Nasmar (PR #41) morreram os dois na perna de volta, e nos dois o Portal
continuou reportando sucesso. Entra com teste no primeiro commit da Fase 3, não depois.

---

## Fase 0 — fechar a lacuna do mapper

Pré-requisito de tudo que escreve no Fire. `app/erp/mapper.py` escreve 9 de 98 colunas.

**`CAB_VENDAS` — preenchidas em 100% dos pedidos manuais, ausentes no mapper:**
`VALOR_TOTAL`, `TOTAL_PRODUTO`, `DESCONTO`, `TIPO_COB` (=4), `COD_CLASS_FINAN` (=335),
`SEM_IMP` (='Nao'), `PED_ZF` (='Nao'), `EH_VENDACONSUMIDOR` (='Nao'), `CODPED_PAI`
(auto-referência), `VENDEDOR_COMI`, `VENDEDOR_COMI_BX`, `ULT_ALT_USER`, `ULT_ALT_DTHR`,
`ULT_INS_DTHR`.

**`CAB_VENDAS` — 82% a 100%:** `CLASSIF_FAT` (=1), `CODFIGFISCAL`, `COND_PRAZO`,
`PRAZO_MEDIO`, `DT_BASE_FAT`, `MECANICO` (=99), `DESC_CLASS_FINAN`.

`CODFIGFISCAL` é **por ambiente**: `1` no `.7`, `5` no `.4`. Vai para a config do
ambiente, não para constante no código.

**`CORPO_VENDAS` — 100%:** `ICMS_PORC` (=18), `ICMS_BASE`, `REDUCAO` (=61.11),
`DESC_SOBRE_TOTAL`, `PESO_BRUTO`, `PESO_LIQUIDO`. **96%:** `CFOP_PRINCIPAL` (='5.101').

**Bug encontrado:** `mapper.py:101` crava `UNID = "UN"`. A produção usa `'KIT'` nos kits.
Passa a vir do cadastro do produto.

`ULT_INS_USER = 'IMPORTADOR'` (já em `mapper.py:48`) vira o marcador de auditoria que
distingue pedido do Portal de pedido digitado.

**Validação:** `.fdb` de **cópia**, nunca produção. Inserir um pedido pelo Portal e
comparar coluna a coluna contra um pedido equivalente digitado pela Camila. A regra do
projeto (`erp.md`, "Testes") continua valendo: mudança em SQL ou mapper pede validação
manual com sample real.

---

## Fases

| Fase | Entrega | Vale sozinha? |
|---|---|---|
| **0** | Mapper completo + `UNID` do cadastro + `CODFIGFISCAL` por ambiente | pré-requisito |
| **1** | `rede_intercompany` + `rede_cnpj` + `/admin/redes` + roteamento da perna de origem | **sim** — acaba o pedido Nasmar caindo na MM |
| **2** | Consolidador + lote + perna espelho + `/lotes` | **sim** |
| **3** | Perna de volta (Flow, reconciliação) pelo vínculo registrado | **sim** |

---

## Testes

| Alvo | Arquivo | O que cobre |
|---|---|---|
| Roteamento | `tests/test_routing_intercompany.py` | CNPJ na rede → ambiente de origem; fora da rede → `None`; CNPJ malformado; CNPJ em duas redes (erro de cadastro) |
| Consolidador (puro) | `tests/test_consolidador_lote.py` | soma por `(produto, data)`; fator aplicado; `DT_ENTREGA` = menor; `DT_ENTREGA_ITEM` por item; `OBS` formatada; chave truncada em 20 |
| Repos | `tests/test_redes_repo.py`, `tests/test_lotes_repo.py` | CRUD; `cnpj` único global; lote fechado é imutável |
| Fechamento | `tests/test_fechar_lotes.py` | janela diária e semanal; **sem fator → `aguardando_fator`, não fecha**; pedido atrasado vai pro próximo lote |
| Mapper | `tests/test_erp_mapper_colunas.py` | todas as colunas de 100%; `CODFIGFISCAL` por ambiente; `UNID` do cadastro |
| Perna de volta | `tests/test_flowpcp_intercompany.py` (estender) | decisão do Flow resolve para a perna de origem via `lote_item`, não para o lote |

O teste de "sem fator não fecha" é o mais importante da suíte. É o que impede regressão
para o default silencioso.

---

## Riscos

**INSERT direto é escrita em ERP de produção.** O caminho nunca rodou (fato 1). Fase 0 e
Fase 2 só validam em `.fdb` de cópia. Primeiro lote real sai com conferência manual antes
de faturar.

**O fator de preço não tem dono claro.** O desenho força a resposta a chegar de um humano
identificado, mas não valida se está certa. Se a operação informar o fator errado, a nota
sai errada. Mitigação possível numa fase futura: alerta quando o fator informado divergir
do último aplicado naquela rede.

**Cadastro de rede desatualizado é falha silenciosa.** Cliente novo de uma rede existente
que ninguém cadastrar volta a cair no ambiente selecionado — o bug de hoje. Mitigação:
a tela de lotes mostra os CNPJs vistos no período que não pertencem a nenhuma rede.

**`intercompany_cnpj` / `intercompany_env_slug` coexistem com o modelo novo.** A config
per-ambiente do PR #41 continua alimentando o `depara_cliente.py`. Duas fontes de verdade
sobre a mesma relação comercial durante a transição. Fase 3 decide se a config antiga é
derivada da nova ou aposentada.

---

## Fora de escopo

- Ressuscitar `TABELA_PRECO_PRODS` (fato 2 — 603 de 640 clientes sem vínculo)
- Reabertura de lote fechado
- Estorno ou cancelamento automático da perna espelho
- Consolidar pedidos que não caem numa rede cadastrada
