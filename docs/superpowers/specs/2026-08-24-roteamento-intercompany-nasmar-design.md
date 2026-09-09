# Roteamento intercompany Nasmar → MM — design

**Data:** 2026-08-24 · **Revisão 3** (2026-09-09 — validação comercial fechada)
**Status:** regras confirmadas pelo Rafael. Falta só a aprovação nominal da lista de
CNPJs da rota (fato 13). Fases 0 a 2 liberadas para implementação.
**Domínios:** `environments`, `erp`, `persistence`, `web`, `worker`, `consolidators`

---

## O problema

O Portal importa todo pedido no **ambiente que o operador selecionou** — na prática,
quase sempre a MM. Mas boa parte dos varejistas não compra da MM: compra da **Nasmar**,
que é revenda. DAJU, Beira Rio, Dakota, Calcenter/Studio Z, Authentic Feet, Art Walk
faturam contra a Nasmar, não contra a MM. **A Centauro não** — ver fato 13.

A cadeia comercial real tem duas pernas:

```
DAJU  ──compra──▶  NASMAR  ──compra──▶  MM AMERICANENSE
                  (revende)             (produz e fatura pra Nasmar)
```

Isso já é operado hoje — **à mão, em duplicidade**. A Grazi cadastra o mesmo pedido duas
vezes via planilha: uma no Firebird da Nasmar com o cliente final, outra no da MM
trocando o CNPJ do cliente para Nasmar e aplicando −7% no preço. O Portal não participa
de nenhuma das duas.

O que a feature entrega: o operador loga no ambiente de sempre, o Portal reconhece que o
pedido é de um cliente que compra da Nasmar, **importa a perna de origem no ambiente
Nasmar**, e monta **um pedido consolidado por janela** no ambiente MM com a Nasmar como
cliente e rastro auditável dos pedidos de origem.

---

## Diretriz comercial (Rafael, 2026-08-25)

Decisões que vieram do negócio e mandam sobre qualquer inferência dos dados:

1. **Preço da perna MM = preço da nota − 7%**, e o percentual é **parâmetro**, não
   constante. Palavras dele: *"deixa como parâmetro.. e a porcentagem tbm"*. Ao ser
   perguntado se é exatamente 7%, respondeu **"aproximadamente"**.
2. **Um único pedido por janela**, juntando **todos os clientes da Nasmar** — não um por
   cliente, não um por rede. *"Eu juntaria tudo da semana ou da quinzena"*.
3. **Janela é parâmetro:** semanal ou quinzenal. *"acho que o ideal seria por semana, ou
   por quinzena"*.
4. Premissa dele para justificar (2): *"cada cliente tem seu produto"* — ver fato 10.

**Fechado pelo Samuel em 2026-08-25, depois de ver os números do fato 3:**

5. **A janela entra ligada em SEMANAL.** Continua parametrizável (quinzenal na tela,
   sem código), mas o default de produção é semanal.
6. **O percentual e o método valem para TODOS os clientes da rota, sem exceção
   cadastrada.** Não existe coluna de override por cliente. A uniformidade é o ponto: é
   ela que corrige o desvio medido em 2026. (Até 2026-09-09 esta linha dizia "inclusive
   Centauro" — errado, a Centauro nunca esteve na rota. Ver fato 13.)

**Respondido pelo Rafael em 08 e 09/09/2026** (PDF de validação + formulário):

7. **A conta é `preço × 0,93`** — "tirar 7% do valor", não `÷ 1,07`. Ele escolheu a
   leitura literal sabendo que muda a base em relação à prática e sabendo do custo
   (~R$ 15 mil/ano). Fecha o fato 3b. `modo_preco` entra em `'desconto'`.
8. **O percentual é exatamente 7,00%.** Fecha o "aproximadamente" de 25/08 e destrava
   a Fase 2 — era o único bloqueio real.
9. **A semana fecha na segunda, às 8h** (hora fechada pelo Samuel; ele pediu "de
   manhã"). A semana anterior é a que é montada.
10. **O percentual congela na ABERTURA da semana**, não no fechamento. Palavras da
    resposta: o novo "só vale da segunda seguinte em diante" — mudar o parâmetro numa
    quarta não pode pegar a semana já aberta.
11. **No Aponta continua tudo por pedido.** O lote existe só para o faturamento
    MM→Nasmar; produção, status por solicitação e acompanhamento do representante
    seguem por pedido individual, e a conciliação acontece no Flow. O consolidador
    **não** tem perna no Aponta.

Esta revisão substitui o eixo de agrupamento por rede/marca da Revisão 1.

---

## Fatos medidos na Fire viva (2026-08-24 e 25, via VPN, somente leitura)

Tudo abaixo foi lido dos dois bancos de produção. Nada é suposição.

**Os dois bancos.** `nasmar` = `192.168.15.4` / `MM_CONFECCAO.FDB` (NASMAR COMÉRCIO DE
ROUPAS LTDA, 34.513.679/0001-34). `americanense` = `192.168.15.7` /
`MM_AMERICANENSE.FDB` (M.M. AMERICANENSE, 35.394.871/0001-11). A Nasmar existe como
cliente no `.7` em `CADASTRO.CODIGO = 2`, `RELAC_CLIENTE = 'Sim'`.

**1. O Portal não escreve no Fire hoje.** `ULT_INS_USER` dos últimos 12 meses, nos dois
bancos: FISCAL, GRAZIELLY, GRASIELY, KAROLAINE, CAMILA, MILENA, BIANCA. **Zero
`IMPORTADOR`.** O caminho `EXPORT_MODE=db` existe no código mas nunca rodou em produção.
Torna a Fase 0 (mapper completo) pré-requisito, não polimento.

**2. A tabela de preço do Fire está morta.** `CADASTRO.CODTABELAPRECO` da Nasmar é
`NULL`. E não é exceção: **603 dos 640 clientes ativos** estão sem tabela vinculada. As
9 tabelas cadastradas têm no máximo 176 produtos com `VALOR > 0` de ~600 linhas. Não é
fonte de preço utilizável — some com a ideia.

**3. O histórico do fator NÃO é 7% uniforme — a operação erra nas duas direções.**
Casei todos os pedidos de 2026 pelas duas pernas e comparei o faturado contra o que a
regra mandaria. A coluna **devido** usa `nota × 0,93`, a regra que o Rafael escolheu em
08/09 (a Revisão 2 media contra `÷ 1,07`; os dois valores estão no rodapé):

| Cliente final | ped. | nota Nasmar | faturado MM | devido (×0,93) | desvio |
|---|---:|---:|---:|---:|---:|
| Calçados Beira Rio | 5 | 1.813.866,00 | 1.728.764,69 | 1.686.895,38 | **+41.869,31** |
| **Calcenter / Studio Z** | **60** | 1.084.111,80 | 1.079.212,92 | 1.008.223,97 | **+70.988,95** |
| Dakota Nordeste | 3 | 268.080,00 | 247.680,00 | 249.314,40 | **−1.634,40** |
| DAJU | 1 | 76.932,00 | 71.899,08 | 71.546,76 | +352,32 |
| Outros seis clientes | 6 | 23.774,30 | 23.774,30 | 22.110,10 | **+1.664,20** |
| **Total** | **75** | **3.266.764,10** | **3.151.330,99** | **3.038.090,61** | **+113.240,38** |

- **R$ 114.874,78 faturado ACIMA da regra** — base de cálculo inflada, imposto pago a
  mais.
- **R$ 1.634,40 abaixo**, na Dakota — base a menor.
- **Nenhum dos 75 pedidos bate a regra nova.** Sob `÷ 1,07` um batia (DAJU, R$ 0,01);
  sob `× 0,93` ele desvia R$ 352,32. Isso facilita a comunicação: a regra vale a partir
  da data em que entrar no ar, não é cobrança retroativa de nada.
- Sob a regra antiga (`÷ 1,07`) o desvio líquido era **+R$ 98.280,43** (devido
  R$ 3.053.050,56). A troca de método aumenta o desvio histórico em R$ 14.959,87 —
  é a mesma diferença de 0,46% do fato 3b, agora sobre a carteira inteira.
- A Calcenter/Studio Z nunca teve o ajuste aplicado: 60 pedidos pelo valor cheio da
  nota, respondendo sozinha por R$ 66.024,32 da base inflada.

Este é o **argumento central da feature**, não um risco dela. A conta é feita à mão em
centenas de pedidos por ano, cada um com dezenas de linhas — é exatamente o erro que some
quando o cálculo passa a ser do sistema. Daí a diretriz 6: percentual uniforme, sem
exceção por cliente.

**3b. "Nota −7%" e a prática são contas DIFERENTES.** A frase do Rafael lê naturalmente
como `preço × 0,93`. O que está gravado no Fire é `preço ÷ 1,07`:

```
16,12 ÷ 1,07 = 15,0654205607477   <- valor real em CORPO_VENDAS do .7 (pedido 4676)
16,12 x 0,93 = 14,9916            <- leitura literal de "menos 7%"
```

Divergência de **0,46% do valor**. Sobre os R$ 3.266.764,10 casados em 2026, são
**~R$ 14.958/ano** de base de cálculo. Economicamente são coisas distintas: `÷1,07` é
*"a Nasmar aplica 7% de markup sobre o custo da MM"*; `×0,93` é *"a MM dá 7% de desconto
sobre a nota da Nasmar"*. As duas são leituras honestas de "7%", e só a primeira bate com
o que a operação digita hoje.

**Respondido em 08/09: `× 0,93`.** O Rafael marcou "tirar 7% do valor" com os dois
números na frente. Consequência: `modo_preco` entra em `'desconto'` e o markup implícito
da Nasmar passa a ser 7,53%, não 7,00% — as duas opções foram apresentadas como leituras
do mesmo "7%" e só a `÷ 1,07` dava 7% exatos. Decisão comercial dele, informada.

Ganho técnico: `× 0,93` sobre preço de 2 casas é exato em 4 casas decimais. O `÷ 1,07`
gerava dízima (15,0654205607477 gravado no Fire). A regra "Decimal, nunca float"
continua valendo, mas a quantização deixa de ser risco.

Consequência de desenho, inalterada: o parâmetro guarda **modo + valor**, nunca só o
percentual, e a tela mostra a conta feita num exemplo real antes de salvar. Ver
"Fator de preço".

**4. O agrupamento já existe, hand-typed.** `CAB_VENDAS.OBS` (que o mapper deixa `NULL`,
`app/erp/mapper.py:74`) está preenchido em 2.232 de 4.263 pedidos e guarda a **lista dos
`CAB_VENDAS.CODIGO` do `.4`** — o número interno do pedido na Nasmar:

```
.7 #4619 (14/08)  OBS='PEDIDOS: 1161, 1160, 1159, 1167, 1162, 1157,1158, 1189.'
   → .4 #1157 AF179 H2S4    #1158 AF200 H2S4    #1159 AW026 H2S4    #1160 AW070 H2S4
     .4 #1161 AW086 H2S4    #1162 AW094 H2S4    #1167 AF017 H2S4    #1189 MF072 FKSHOES

.7 #4587 (05/08)  OBS='PEDIDOS 1177 1179 1180 1183 1186 1187 '
   → seis clientes finais DIFERENTES num lote só
```

O formato do lote pedido pelo Rafael é o que já se faz — só que maior e com regra.

**5. Os itens são somados por produto.** Quantidade preservada exata, linhas colapsadas:

| Lote MM | pernas `.4` | linhas | quantidade |
|---|---|---|---|
| #4616 | 3 pedidos | 20 → **8** | 468 → **468** |
| #4619 | 8 pedidos | 168 → **65** | 1.739 → **1.739** |
| #4587 | 6 pedidos | 71 → **36** | 1.410 → **1.410** |

**6. A data de entrega do lote é digitada e está errada.** `DT_ENTREGA_ITEM` é `0/65` nos
lotes — ninguém preenche por item. A data vai só no header, à mão. Resultado: o lote
`#4616` tem `DT_ENTREGA = 2028-08-21` enquanto suas três pernas têm `2026-08-21`. Ano
digitado errado, em produção, hoje. O `#4587` tem `DT_ENTREGA = NULL`.

**7. A idempotência do exporter já estaria quebrada.** `CHECK_ORDER_EXISTS`
(`app/erp/queries.py:91`) casa por `PEDIDO_CLIENTE + CLIENTE`. Nos 332 pedidos da Nasmar
no `.7` desde 2025 há só **272 chaves distintas**: `''` ×15, `'NASMAR'` ×12, `'AF184'`
×5, `'STUDIO Z'` ×5. Reproduzir a convenção atual faria o Portal recusar o segundo lote
como duplicata.

**8. O prefixo da OBS é hábito, não convenção.** Coexistem `PEDIDO`, `PEDIDOS`,
`PEDIDO N.`, `PEDIDO:`, `PEDIDO NASMAR` e `PEDODO` (typo), com separador ` - `, `, ` ou
espaço.

**9. O mapper escreve 9 de 98 colunas.** Perfil de 373 pedidos do `.7` e 90 do `.4`,
de 2026-06-01 em diante — ver "Fase 0".

**10. A premissa "cada cliente tem seu produto" é falsa na letra, quase verdadeira na
prática.** Varri todas as linhas de item do `.4` em 2026 agrupadas por janela:

- **513 produtos** aparecem em pedidos de clientes diferentes na mesma semana (502 na
  quinzena) — compartilhamento é a norma, não a exceção.
- Mas só **3 produtos** têm preço divergente entre clientes na mesma janela, todos num
  único evento: rede **Nacional Lojas**, um CD a R$ 32,00 e seis a R$ 36,00 no `KIT C/3
  PARES DE MEIA CANO MEDIO BRANCA`.

Consequência de desenho: consolidar por produto é seguro em 99,4% dos casos, mas o caso
raro existe e não pode gerar preço errado. Regra adotada: **linhas com preços diferentes
para o mesmo produto não se fundem** — ver "Consolidador".

**11. Tamanho do lote é viável.** Semanas de 2026 no `.4`: mediana ~6 pedidos, máximo 43
(semana 3), até 21 clientes e 427 linhas de item. Após consolidação (~2,5× de colapso,
fato 5) uma semana cheia vira ~170 linhas — grande, mas dentro do que o Fire já opera
(o `#4619` tem 65).

**12. Sem observação por item; `OBS` de cabeçalho é BLOB sem limite.** `CORPO_VENDAS` não
tem campo de observação — só `DESCRICAO VARCHAR(100)`, que é o nome do produto.
`CAB_VENDAS.OBS` é BLOB (tipo 261); a maior já gravada tem 179 caracteres. A lista de 43
pedidos de uma semana cheia cabe folgada.

---

**13. A Centauro não compra da Nasmar. Nunca comprou.** Corrigido em 2026-09-09 depois
que o Rafael apontou o erro. Na Revisão 2 a linha da tabela do fato 3 estava rotulada
"Calcenter (Centauro)" — glosa errada, não veio do dado. Medido nos dois bancos:

| | `.4` NASMAR | `.7` MM AMERICANENSE |
|---|---|---|
| SBF/Centauro `06347409` | **nenhum cadastro**, nem por nome nem por CNPJ | `CADASTRO.CODIGO=498`, `/0296-51`, **245 pedidos e R$ 11.977.112,56 em 2026** |
| Calcenter `15048754` | `CODIGO=29443`, `/0075-25`, 69 pedidos, R$ 1.270.207,44 | nenhum cadastro |

A Centauro é **cliente direta da MM e o maior cliente dela** — fatura acima da própria
Nasmar (R$ 9,3 mi em 2026). Cadastrá-la em `rota_intercompany` mandaria 245 pedidos e
R$ 11,98 milhões por ano para o ambiente errado, e é justamente o caso que o radar de
CNPJ fora de rota **não** detecta (ele só vê quem falta, não quem sobra).

`CALCENTER CALÇADOS CENTRO OESTE LTDA` (`15.048.754/0075-25`, Palhoça/SC) **é o
Studio Z** — provado, não inferido: as 7 pernas da Nasmar no `.7` gravadas com
`PEDIDO_CLIENTE='STUDIO Z'` citam na `OBS` os números `10556 10557 10594 2600009023
9014 9015 9018 9044 9045 9903`, e 12 dos 13 resolvem para a Calcenter no `.4`.

Consequência de desenho: a lista de `rota_intercompany` **precisa de aprovação humana
nominal, por CNPJ**, antes de qualquer roteamento entrar no ar. Nome comercial não é
chave; CNPJ é.

---

## Escopo

**Dentro:** cadastro de rota intercompany por CNPJ; roteamento do pedido para o ambiente
de origem; consolidação em lote por janela (semanal/quinzenal); criação da perna espelho
no ambiente MM; registro explícito do vínculo entre as pernas; fechamento da lacuna de
colunas do mapper; correção da perna de volta (Flow) para o vínculo registrado.

**Fora:** ressuscitar a tabela de preço do Fire (fato 2); reabertura de lote fechado;
estorno automático da perna espelho; consolidar pedidos que não caem numa rota
cadastrada; percentual ou método por cliente.

---

## Arquitetura

### Decisão central: o vínculo passa a ser registrado, não inferido

Hoje `app/erp/depara_cliente.py` deduz quem é o cliente real por **coincidência de
`PEDIDO_CLIENTE`** nos dois bancos. Isso funciona porque as duas pernas são digitadas com
o mesmo número. Com lote, essa coincidência deixa de existir por construção: o lote tem
chave própria e junta N pedidos de N clientes.

Como o Portal passa a criar as duas pernas, ele **sabe** o vínculo. Grava. O
`depara_cliente.py` continua existindo como fallback para pedidos legados e para os que a
operadora cadastrar à mão — não é removido, é rebaixado de fonte da verdade para plano B.

### Modelo de dados (`app_shared.db`, transversal — `db.connect_shared()`)

```
rota_intercompany            quem compra da revenda
  id, cnpj_cliente (UNIQUE), env_origem_slug, rotulo, ativo, created_at

lote_config                  um por par origem -> espelho
  env_origem_slug, env_espelho_slug, cnpj_revenda,
  janela ('semanal'|'quinzenal', default 'semanal'), dia_fechamento, hora_fechamento,
  modo_preco ('divisor'|'desconto', default 'desconto'), fator_preco (NULLABLE), ativo
    dia_fechamento  0=segunda .. 6=domingo; na quinzena, fecha dias 15 e ultimo
    hora_fechamento 8 = 08:00 hora local do servidor (resposta do Rafael)
    modo_preco      'divisor'  -> preco / (1 + fator)   [pratica ate 2026]
                    'desconto' -> preco * (1 - fator)   [ESCOLHIDO em 08/09]
    fator_preco     0.07 = 7%. NULL = nao fecha lote, pergunta ao operador.
                    Confirmado 7,00% EXATO em 09/09 — mas continua sem default
                    no schema: quem cadastra a rota digita, ninguem herda

intercompany_lote
  id, chave_lote (UNIQUE), env_origem_slug, env_espelho_slug,
  janela_inicio, janela_fim,
  status ('aberto'|'aguardando_fator'|'fechado'|'enviado'|'erro'),
  fator_preco_aplicado, fator_informado_por, fator_informado_em,
  import_id_espelho, fire_codigo_espelho,
  created_at, closed_at, closed_by

intercompany_lote_item
  lote_id, import_id, env_origem_slug, fire_codigo_origem,
  pedido_cliente, cnpj_cliente_final, razao_cliente_final
```

`rota_intercompany` e `lote_config` são transversais: a decisão de roteamento acontece
**antes** de existir ambiente ativo. Vão no shared, ao lado de `environments` — mesma
regra de `environments.md`.

`fator_preco` é **NULLABLE de propósito**. Ver abaixo.

### Roteamento da perna de origem

Novo módulo `app/routing/intercompany.py`:

```python
def rota_para(order: Order) -> Rota | None:
    """Normaliza o CNPJ do cliente e busca em rota_intercompany.

    Achou    -> Rota(env_origem_slug, env_espelho_slug)
    Nao achou -> None (comportamento de hoje, ambiente selecionado)
    """
```

Reusa `app/erp/cnpj.py::cnpj_digits` — não duplica normalização.

**Onde é chamado:** no boundary de criação da linha em `imports`, **antes** do
`repo.insert_import`. `environment_id` é bind imutável (`environments.md`, "Bind
imutável") — não dá pra corrigir depois. Dois call-sites:

- `app/web/server.py` — o commit do preview
- `app/worker/jobs/scan_environments.py` — o watcher de pasta

A UI mostra o desvio explicitamente no preview: *"Este pedido vai para o ambiente
NASMAR"*. Roteamento silencioso é como o bug de hoje nasceu; não vamos trocar um silêncio
por outro.

### Perna de origem: comportamento inalterado

Preview → de-para de produto → check de preço → "Cadastrar no Fire" → INSERT no `.4`.
Cliente = cliente final. `PEDIDO_CLIENTE` = número do pedido do cliente. Preço = preço do
pedido, **sem fator**.

Única adição: capturar o `CAB_VENDAS.CODIGO` gerado (o exporter já o conhece — é o
`header_pk` que ele passa pro mapper), gravar em `imports.fire_codigo` e anexar ao lote
aberto da janela via `intercompany_lote_item`.

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
mão. Regras, todas derivadas do comportamento medido ou da diretriz:

| Campo | Regra | Origem |
|---|---|---|
| Cliente | CNPJ e razão da revenda (Nasmar) | diretriz 2 |
| Agrupamento de linha | por `(produto, data_entrega, preço_unitário)` | fato 5 + fato 10 |
| Preço unitário | `divisor`: preço ÷ (1 + fator) · `desconto`: preço × (1 − fator) | diretriz 1 + fato 3b |
| `DT_ENTREGA` (header) | **menor** data entre as pernas | fato 6 |
| `DT_ENTREGA_ITEM` | preenchido por item | fato 6 |
| `PEDIDO_CLIENTE` | chave de lote | fato 7 |
| `OBS` | `LOTE <chave> \| PEDIDOS .4: 1157, 1158, ...` | fatos 4, 8, 12 |

**O preço entra na chave de agrupamento de propósito.** O fato 10 mostra o mesmo produto
vendido a R$ 32,00 e R$ 36,00 para clientes diferentes na mesma semana. Agrupar só por
`(produto, data)` obrigaria a escolher um preço — e escolher errado é base de cálculo
errada na nota. Com o preço na chave, esse produto vira **duas linhas** no lote, cada uma
com seu preço e sua quantidade. Nos 99,4% dos casos sem divergência o comportamento é
idêntico ao de hoje.

**`DT_ENTREGA` como a menor data** é escolha conservadora: é a restrição real de produção,
e errar pra antes não atrasa nada enquanto errar pra depois perde prazo. Corrige de graça
o `2028-08-21` gravado hoje.

**`DT_ENTREGA_ITEM` por item** preserva as datas combinadas com cada cliente final. O Fire
suporta a coluna e ninguém usa.

**Descartado:** gravar o desconto em `CORPO_VENDAS.PERC_DESCONTO` em vez de embutir no
preço. A coluna existe, mas a prática de produção embute no `PRECO_UNITARIO` (medido:
16,12 → 15,0654) e divergir disso mudaria como a nota é emitida. Segue a prática.

### Chave de lote

Formato: `NAS-<ano>-<janela>` → `NAS-2026-S34` (semana ISO 34) ou `NAS-2026-Q16`
(quinzena 16 = 2ª de agosto).

- **Única por construção** — resolve as 60 colisões do fato 7 e destrava
  `CHECK_ORDER_EXISTS`
- **Opaca** — não expõe cliente final nem número de pedido de ninguém
- **Legível** — a operação sabe de que semana é o lote sem abrir
- **12 caracteres** — nunca truncada pelo `[:20]` do `mapper.py:64`

O prefixo vem de `lote_config` (derivado do slug do ambiente de origem), então um segundo
par origem→espelho no futuro não colide.

### Fator de preço: sem fator, o lote não fecha

`fator_preco` é `NULLABLE` e **não tem default**. Quando o lote vence a janela e a config
não tem fator, o lote entra em `status='aguardando_fator'` e para. A tela de lotes pede o
fator ao operador; `fator_informado_por` e `fator_informado_em` gravam quem respondeu e
quando.

Por quê: preço errado na perna espelho é **base de cálculo errada na nota fiscal** —
imposto recolhido a menos ou a mais, com prejuízo real. Um default "para não travar" é a
falha silenciosa que a regra do projeto proíbe. O fato 3 mostra que o histórico varia de
0% a 8,2% entre clientes; qualquer default seria um chute com consequência fiscal.

O fator vigente é copiado para `fator_preco_aplicado` **na abertura do lote** — na
primeira perna anexada à janela —, não no fechamento. Mudar o parâmetro depois não
reescreve lote nenhum, aberto ou fechado.

Foi resposta do Rafael em 09/09: o percentual novo "só vale da segunda seguinte em
diante". Congelar no fechamento faria uma mudança de quarta-feira pegar a semana já
aberta, que ainda não fechou. Congelando na abertura, o preço de uma semana fica
travado no dia em que ela começa.

**A tela mostra a conta, não o parâmetro.** Por causa do fato 3b, salvar
`modo_preco` + `fator_preco` exibe ao lado um exemplo real calculado:
*"R$ 16,12 na nota → R$ 15,07 para a MM"*. Ninguém confirma um percentual abstrato; a
pessoa vê o número que vai ser gravado no ERP antes de aprovar. Também é o que torna a
divergência de R$ 15 mil/ano visível a olho nu em vez de enterrada num campo.

Cálculo interno em `Decimal`, nunca em `float`: `unit_price` é `float` em
`app/models/order.py:20`, e conversão só na borda, com quantização explícita antes de
gravar. Erro de ponto flutuante em base de cálculo fiscal é o mesmo problema que o fator
errado, só que silencioso.

### Fechamento do lote

Job `app/worker/jobs/fechar_lotes.py` no APScheduler existente, em
`cron day_of_week='mon', hour=8` (hora local do servidor, mesma convenção do `retention`
às 03:00). Fecha o lote cuja janela venceu: consolida, cria o `imports` da perna espelho
no ambiente MM, e o pedido fica **esperando conferência humana** — o INSERT no Fire
continua sendo o botão "Cadastrar no Fire", não sai sozinho.

A hora foi escolhida para que o pedido da semana anterior esteja na tela quando a
operação chega. Ela **não** decide quem entra na semana: isso é a data em que a perna de
origem foi importada. Pedido importado na segunda de manhã, antes ou depois das 8h, entra
na semana nova.

Tela `/lotes`: lote aberto com as pernas dentro, total, quantidade de clientes, fator
vigente, botão "fechar agora", e o campo de fator quando estiver `aguardando_fator`.
Mostra também os CNPJs vistos na janela que **não** estão em `rota_intercompany` — é o
radar de cliente novo não cadastrado.

Pedido que chega depois do fechamento entra no **próximo** lote. Lote fechado nunca
reabre. Isso acontece de verdade — o `#1189` de 11/08 entrou no lote de 14/08 junto com
pedidos de 27/07.

### A perna de volta

`app/integrations/flowpcp/poll_decisoes.py` casa a decisão do Flow contra o Firebird onde
o pedido está. Com lote, **o pedido do cliente final deixa de existir 1:1 no `.7`** — lá
só existe o lote, no nome da Nasmar. A decisão tem que voltar para a perna de origem no
`.4`, resolvida por `intercompany_lote_item`. Determinístico, sem inferência.

Esta é a terceira aparição da mesma família de bug: o prazo de entrega (PR #37) e o
de-para Nasmar (PR #41) morreram os dois na perna de volta, e nos dois o Portal continuou
reportando sucesso. Entra com teste no primeiro commit da Fase 3, não depois.

---

## Fase 0 — fechar a lacuna do mapper

Pré-requisito de tudo que escreve no Fire. `app/erp/mapper.py` escreve 9 de 98 colunas.

**`CAB_VENDAS` — 100% nos pedidos manuais, ausentes no mapper:** `VALOR_TOTAL`,
`TOTAL_PRODUTO`, `DESCONTO`, `TIPO_COB` (=4), `COD_CLASS_FINAN` (=335), `SEM_IMP`
(='Nao'), `PED_ZF` (='Nao'), `EH_VENDACONSUMIDOR` (='Nao'), `CODPED_PAI`
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
projeto (`erp.md`, "Testes") continua valendo.

---

## Fases

| Fase | Entrega | Vale sozinha? |
|---|---|---|
| **0** | Mapper completo + `UNID` do cadastro + `CODFIGFISCAL` por ambiente | pré-requisito |
| **1** | `rota_intercompany` + `/admin/rotas` + roteamento da perna de origem | **sim** — acaba o pedido Nasmar caindo na MM |
| **2** | Consolidador + lote + perna espelho + `/lotes` | **sim** |
| **3** | Perna de volta (Flow, reconciliação) pelo vínculo registrado | **sim** |

---

## Testes

| Alvo | Arquivo | O que cobre |
|---|---|---|
| Roteamento | `tests/test_routing_intercompany.py` | CNPJ na rota → ambiente de origem; fora → `None`; CNPJ malformado; CNPJ duplicado no cadastro |
| Consolidador (puro) | `tests/test_consolidador_lote.py` | soma por `(produto, data, preço)`; **preços diferentes não fundem** (caso Nacional Lojas); fator aplicado; `DT_ENTREGA` = menor; `DT_ENTREGA_ITEM` por item; `OBS` formatada |
| Repos | `tests/test_rotas_repo.py`, `tests/test_lotes_repo.py` | CRUD; `cnpj` único; lote fechado é imutável |
| Fechamento | `tests/test_fechar_lotes.py` | janela semanal e quinzenal; **sem fator → `aguardando_fator`, não fecha**; pedido atrasado vai pro próximo lote; CNPJ fora de rota aparece no radar |
| Mapper | `tests/test_erp_mapper_colunas.py` | todas as colunas de 100%; `CODFIGFISCAL` por ambiente; `UNID` do cadastro |
| Perna de volta | `tests/test_flowpcp_intercompany.py` (estender) | decisão do Flow resolve para a perna de origem via `lote_item`, não para o lote |

Os dois testes que travam regressão perigosa: **"sem fator não fecha"** e **"preços
diferentes não fundem"**. Nenhum dos dois pode ser relaxado sem decisão comercial.

---

## Questões abertas

**Todas as questões comerciais foram fechadas** — as três do PDF em 08/09, as cinco do
formulário em 09/09. Estão na "Diretriz comercial", itens 5 a 11.

Resta **uma**, e ela bloqueia a Fase 1 ir pro ar:

**A lista nominal de CNPJs da `rota_intercompany` precisa de aprovação humana.** O Rafael
chegou a marcar "a lista está completa", mas a lista que ele viu trazia a Centauro por
erro de rótulo (fato 13). A aprovação tem que ser refeita sobre a lista corrigida, e por
CNPJ — nome comercial não é chave. Faltam também os nomes dos seis clientes menores
(6 pedidos, R$ 23.774,30 em 2026) que ainda estão agregados na tabela do fato 3.

Nenhuma questão bloqueia a Fase 0.

---

## Riscos

**INSERT direto é escrita em ERP de produção.** O caminho nunca rodou (fato 1). Fase 0 e
Fase 2 só validam em `.fdb` de cópia. Primeiro lote real sai com conferência manual antes
de faturar.

**O fator informado pode estar errado.** O desenho força a resposta a vir de um humano
identificado, mas não valida se está certa. Mitigação futura: alerta quando o fator
informado divergir do último aplicado.

**Cadastro de rota desatualizado é falha silenciosa.** Cliente novo que ninguém cadastrar
volta a cair no ambiente selecionado — o bug de hoje. Mitigação: o radar de CNPJs fora de
rota na tela de lotes.

**Lote grande concentra risco.** Uma semana cheia vira um pedido de ~170 linhas
(fato 11). Se ele nascer errado, erra tudo de uma vez, ao contrário do 1:1 de hoje que
erra um pedido. Daí a conferência manual no primeiro lote e o `/lotes` mostrar o
conteúdo antes de fechar.

**`intercompany_cnpj` / `intercompany_env_slug` coexistem com o modelo novo.** A config
per-ambiente do PR #41 continua alimentando o `depara_cliente.py`. Duas fontes de verdade
sobre a mesma relação comercial durante a transição. Fase 3 decide se a config antiga é
derivada da nova ou aposentada.

---

## Fora de escopo

- Ressuscitar `TABELA_PRECO_PRODS` (fato 2 — 603 de 640 clientes sem vínculo)
- Reabertura de lote fechado
- Estorno ou cancelamento automático da perna espelho
- Consolidar pedidos que não caem numa rota cadastrada
- **Percentual ou método por cliente.** Decidido em 2026-08-25: o parâmetro é global e
  não existe override. A uniformidade é o que corrige o desvio do fato 3 — uma exceção
  cadastrada reabriria exatamente o buraco que a feature fecha.
