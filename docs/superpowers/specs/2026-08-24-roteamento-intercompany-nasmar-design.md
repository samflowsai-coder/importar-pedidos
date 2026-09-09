# Roteamento intercompany Nasmar → MM — design

**Data:** 2026-08-24 · **Revisão 4** (2026-09-09 — o roteamento sai do documento)
**Status:** regras comerciais confirmadas. O eixo do roteamento mudou de *cadastro de
cliente* para *fornecedor impresso no pedido*, o que dissolve a última questão aberta da
Revisão 3. Fases 0 a 2 liberadas.
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

Consequência de desenho, na Revisão 3: a lista precisaria de aprovação humana nominal
por CNPJ antes de qualquer roteamento entrar no ar.

**A Revisão 4 dissolveu a consequência atacando a causa.** Este erro só foi possível
porque o roteamento dependia de alguém manter uma lista de clientes correta. Roteando
pelo fornecedor impresso no pedido, um pedido da Centauro traz o CNPJ da MM e vai para a
MM — não existe cadastro para errar. O fato 13 deixa de ser um alerta operacional e passa
a ser a evidência de por que o eixo mudou. Ver fato 14.

---

**14. O pedido diz de quem ele é. Medido nos 29 samples reais.** Todo pedido de compra
identifica o **fornecedor**, e fornecedor é exatamente a empresa que vai faturar. Rodei
a extração de texto nos 29 arquivos de `samples/` procurando os dois CNPJs:

| | arquivos | veredito |
|---|---:|---|
| CNPJ da **Nasmar** `34.513.679` | 7 | ambiente `nasmar` |
| CNPJ da **MM** `35.394.871` | 14 | ambiente `mm` |
| **os dois no mesmo arquivo** | **0** | — |
| nenhum dos dois | 8 | precisa de outra fonte |

**Resolve sozinho em 21 de 29, e a ambiguidade é zero.** Essa é a propriedade que
importa: quando o CNPJ do fornecedor está no documento, a resposta **nunca é errada,
só pode estar ausente**. É o oposto do cadastro por cliente, que erra em silêncio — foi
assim que a Centauro quase entrou na rota (fato 13).

Os 8 sem CNPJ têm padrão: **7 são planilhas internas** (`Desmembramento Authentic feet`,
`Desmembramento Magic Feet`, `PEDIDO KALLAN K01`, `PEDIDO NBA 3`, `PEDIDO TENNIS
STATION`, `Pedido Authentic Fit`, `Pedido Magic Feet MF048`) e a oitava é o
`PEDIDO BEIRA RIO.pdf`, que traz o rótulo `FORNECEDOR` mas não o CNPJ. Os que **têm**
são as OCs formais de varejista grande: Sam's Club, Centauro, Riachuelo, Kolosh,
Studio Z, Mercado Eletrônico.

⚠️ **Nada disso é lido hoje.** Não existe campo de fornecedor em `app/models/order.py`,
e nenhum dos 11 parsers extrai o CNPJ do fornecedor. O único que olha para a palavra é
`app/parsers/daju_parser.py:72`, e mesmo assim só para **pular** o bloco — "tudo a partir
da linha FORNECEDOR descreve a Nasmar, não o cliente". A informação está no documento e é
descartada na porta de entrada. Fechar isso é a Fase 1.

---

**15. O histórico do Fire resolve quase todo o resto. Medido em 09/09.** Para os 8
samples sem CNPJ de fornecedor (fato 14), rodei o pipeline real, peguei o
`customer_cnpj` de cada e perguntei aos dois bancos onde aquele cliente já teve pedido:

| sample | Nasmar | MM | veredito |
|---|---|---|---|
| `Pedido Authentic Fit.xlsx` | 4 ped, 07/05/26 | nenhum | → `nasmar` |
| `Pedido Magic Feet MF048.xlsx` | 1 ped, 09/06/26 | nenhum | → `nasmar` |
| `PEDIDO KALLAN K01.xlsx` | nenhum | 6 ped, 03/08/26 | → `mm` |
| `PEDIDO TENNIS STATION.xlsx` | nenhum | 2 ped, 27/08/26 | → `mm` |
| `PEDIDO BEIRA RIO.pdf` | 28 ped, 24/08/26 | 6 ped, **28/05/25** | → `nasmar` na janela de 12m |
| `Desmembramento Authentic feet` | — | — | **o parser não extrai CNPJ do cliente** |
| `Desmembramento Magic Feet` | — | — | idem |
| `PEDIDO NBA 3.xlsx` | — | — | idem |

**Quanto o histórico é ambíguo, na carteira inteira:**

| janela | só Nasmar | só MM | **ambos** |
|---|---:|---:|---:|
| 24 meses | 72 | 256 | **2** (0,6% de 330) |
| 12 meses | 73 | 203 | **1** (0,4% de 277) |

Cliente que compra das duas empresas existe, mas é **1 em 277**. E a Beira Rio mostra por
que a janela importa: ela comprou da MM até maio de 2025 e migrou para a Nasmar, onde tem
28 pedidos. Sem janela ela é ambígua; com 12 meses resolve limpo.

**Somando os dois mecanismos: 26 dos 29 samples roteiam sozinhos.** Os 3 que sobram
falham por um motivo diferente e consertável — `DesmembramentoXlsParser` e o formato NBA
não extraem nem o CNPJ do cliente. É lacuna de parser, não limite do desenho.

⚠️ **A janela de 12 meses é uma escolha, não um fato.** A Beira Rio pode voltar a comprar
da MM. Por isso o Portal mostra a divergência mesmo quando a janela resolve: *"pelo
histórico vai para NASMAR (28 pedidos); atenção, este cliente também já comprou da MM,
6 pedidos até 05/2025"*. Resolver em silêncio é o que não se faz.

---

## Escopo

**Dentro:** extração do CNPJ do fornecedor nos parsers; roteamento do pedido pelo
fornecedor do documento; fim da seleção de ambiente no login; consolidação em lote por
janela (semanal/quinzenal); criação da perna espelho no ambiente MM; registro explícito
do vínculo entre as pernas; fechamento da lacuna de colunas do mapper; correção da perna
de volta (Flow) para o vínculo registrado.

**Fora:** ressuscitar a tabela de preço do Fire (fato 2); reabertura de lote fechado;
estorno automático da perna espelho; percentual ou método por cliente; **cadastro curado
de quais clientes compram da revenda** — morreu na Revisão 4, o documento resolve.

---

## Arquitetura

### Decisão central: quem decide o ambiente é o documento, não um cadastro

A Revisão 3 roteava por **cadastro de cliente**: uma tabela `rota_intercompany` dizia
quais CNPJs compram da revenda. Esse desenho tem um defeito estrutural que o fato 13
expôs antes de qualquer linha ser escrita: **a tabela é uma opinião sobre o mundo, e
opinião envelhece em silêncio.** Cliente novo que ninguém cadastrar cai no ambiente
errado; cliente cadastrado por engano leva um pedido inteiro para a empresa errada. Foi
esse segundo caso que quase mandou R$ 11,98 milhões/ano da Centauro para a Nasmar.

O pedido de compra **já carrega a resposta**. Todo documento identifica o fornecedor, e
fornecedor é, por definição, quem vai faturar. Medido em 29 samples reais: 21 trazem o
CNPJ do fornecedor e **nenhum traz os dois** (fato 14).

Então o roteamento passa a ser **derivado do documento**, não consultado num cadastro:

```
CNPJ do fornecedor no pedido  ->  environments.cnpj  ->  ambiente
```

Isso não é uma otimização. Muda a natureza do erro possível:

| | cadastro por cliente (Rev. 3) | fornecedor no documento (Rev. 4) |
|---|---|---|
| erro possível | **rota errada**, silenciosa | **sem resposta**, visível |
| cliente novo | cai no ambiente errado | resolve sozinho |
| manutenção | lista curada, aprovada, revisada | nenhuma |
| caso Centauro | acontece | impossível |

Um sistema que erra por omissão, e mostra a omissão, é categoricamente melhor do que um
que erra por afirmação e não avisa. É a mesma regra do fator de preço: **sem resposta, o
Portal pergunta; ele não chuta.**

`rota_intercompany` **sai do desenho.** Não existe mais lista de clientes para curar,
aprovar ou manter — e com ela sai a única questão que ainda bloqueava a Fase 1.

`app/erp/depara_cliente.py` continua como está, atendendo os pedidos legados já
importados. Não é fonte de roteamento em nenhuma hipótese.

### Modelo de dados (`app_shared.db`, transversal — `db.connect_shared()`)

```
decisao_ambiente             memoria das escolhas do operador; NASCE VAZIA
  id, cnpj_cliente (UNIQUE), env_slug, decidido_por, decidido_em,
  divergiu_em, divergiu_de     <- preenchidos quando documento ou historico
                                  contradizem a escolha depois
    Nao e pre-requisito de nada: o Portal funciona no dia 1 com ela vazia.
    Perde para o documento E para o historico.
    Existe para AUDITORIA: "quem decidiu isso, quando" em uma linha.

lote_config                  um por par origem -> espelho
  env_origem_slug, env_espelho_slug, cnpj_revenda, nome_revenda,
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
  modo_preco_aplicado, fator_preco_aplicado,
  fator_informado_por, fator_informado_em,
  import_id_espelho, fire_codigo_espelho,
  created_at, closed_at, closed_by
    modo e fator sao copiados da config na ABERTURA do lote, nao no
    fechamento — resposta do Rafael em 09/09. Ver "Fator de preco".

intercompany_lote_item
  lote_id, import_id, env_origem_slug, fire_codigo_origem,
  pedido_cliente, cnpj_cliente_final, razao_cliente_final
```

`decisao_ambiente` e `lote_config` são transversais: a decisão de roteamento acontece
**antes** de existir ambiente ativo. Vão no shared, ao lado de `environments` — mesma
regra de `environments.md`.

`fator_preco` é **NULLABLE de propósito**. Ver abaixo.

### Roteamento: o fornecedor entra no modelo

**Novo campo:** `OrderHeader.supplier_cnpj: str | None`. Hoje ele não existe, e nenhum
parser o extrai (fato 14). A Fase 1 é exatamente fechar essa lacuna.

**Novo módulo** `app/routing/ambiente.py`:

```python
def ambiente_para(order: Order) -> Decisao:
    """Resolve o ambiente do pedido. Nunca chuta.

    1. supplier_cnpj do documento casa com environments.cnpj  -> Decisao.documento
    2. historico do cliente no Fire, 12 meses, SE inequivoco  -> Decisao.historico
    3. decisao lembrada para este cliente                     -> Decisao.lembrado
    4. nada resolveu, ou historico ambiguo                    -> Decisao.perguntar
    """
```

Cada degrau é mais fraco que o de cima, e a `Decisao` carrega **qual degrau respondeu** —
a UI mostra isso, sempre. Cobertura medida nos 29 samples: 21 pelo documento, mais 5 pelo
histórico, **26 automáticos**; 3 vão para o operador.

Reusa `app/erp/cnpj.py::cnpj_digits` — não duplica normalização.

**A precedência importa e é estrita:** documento > histórico > memória > perguntar. Cada
degrau perde para o de cima, sem exceção. Se o documento diz MM e a memória diz Nasmar,
vale MM **e a divergência é registrada** — não sobrescrita em silêncio. Documento é fato
sobre este pedido; histórico é fato sobre o passado; memória é julgamento humano.

**O histórico é um degrau de fato, não de palpite** — mas só quando é inequívoco. Se o
cliente tem pedido nos dois bancos dentro da janela de 12 meses, o histórico **se
recusa a responder** e cai para o degrau seguinte. Medido: isso acontece com 1 cliente em
277 (fato 15). Não é o caso comum, é o caso que não pode ser chutado.

**O histórico é bootstrap, não mecanismo permanente.** Ele responde "onde este cliente já
foi faturado". Para cliente genuinamente novo ele é mudo por construção — e é exatamente
aí que o Portal pergunta.

**Cliente novo, sem documento e sem histórico: o Portal pergunta, e guarda.** No preview
aparece uma escolha de ambiente obrigatória. A escolha vai para `decisao_ambiente` com
**quem decidiu e quando**.

O registro não existe só para poupar a próxima pergunta. Ele existe para **quando der
errado**: se um pedido acabar na empresa errada, a pergunta "quem decidiu isso, quando, e
com base em quê" tem resposta em uma linha de tabela em vez de uma reconstrução no
WhatsApp. É a mesma razão de `fator_informado_por` existir no lote.

E por isso a divergência é registrada em vez de silenciada: no dia em que o documento
finalmente trouxer o CNPJ do fornecedor, ou o histórico virar, o Portal marca que a
decisão lembrada era outra. Erro que aparece é erro que se conserta.

A diferença em relação à `rota_intercompany` é o que torna isso aceitável: a tabela
**não é pré-requisito de nada**. Ela nasce vazia, se preenche sozinha conforme a operação
trabalha, e cada linha registra uma decisão humana datada em vez de uma curadoria que
alguém precisa lembrar de revisar. O Portal funciona 100% no dia 1 com ela vazia.

**Onde é chamado:** no boundary de criação da linha em `imports`, **antes** do
`repo.insert_import`. `environment_id` é bind imutável (`environments.md`, "Bind
imutável") — não dá pra corrigir depois. Dois call-sites:

- `app/web/server.py` — o commit do preview
- `app/worker/jobs/scan_environments.py` — o watcher de pasta

No watcher não há humano para perguntar: pedido que cair em `Decisao.perguntar` fica
**retido**, aparece na fila de pendências e não é importado. Nunca vai para um ambiente
default.

A UI mostra a decisão sempre, mesmo quando ela é automática: *"Fornecedor NASMAR
(34.513.679/0001-34) → este pedido entra no ambiente NASMAR"*. Roteamento silencioso é
como o bug de hoje nasceu; não trocamos um silêncio por outro.

### Login sem seleção de ambiente

Hoje o operador loga, cai em `/selecionar-ambiente`, escolhe uma empresa, e o cookie
`portal_env` amarra toda a navegação dele àquela empresa até ele trocar
(`environments.md`). Isso existia porque **alguém precisava dizer em que empresa o pedido
entrava** — e a única pessoa disponível era o operador.

Com o fornecedor decidindo, essa pergunta deixa de existir. O passo perde a razão de ser.

**O ambiente deixa de ser um modo em que o usuário está e passa a ser uma propriedade de
cada pedido.**

| | hoje | depois |
|---|---|---|
| login | → escolher empresa → trabalhar | → trabalhar |
| caixa de entrada | os pedidos de uma empresa | todos, com selo da empresa em cada um |
| `portal_env` | **gate** de toda navegação | **filtro** opcional da listagem |
| ambiente do pedido | o que estava selecionado no commit | derivado do documento |

**O que não muda:** `environment_id` continua bind imutável em `imports`; cada empresa
continua com seu SQLite (`app_state_<slug>.db`) e seu Firebird; `active_env()` continua
envolvendo todo caminho de escrita — só que o `env` vem **do pedido**, não da sessão.
Isolamento entre empresas é o mesmo. Admin (`/admin/ambientes`) continua por empresa.

**Tamanho real:** `portal_env` e `/selecionar-ambiente` aparecem em 15 pontos de 6
arquivos — `app/web/server.py`, `auth.py`, `routes_env_select.py`,
`middleware/environment.py`, `dependencies/environment.py`, `app/persistence/context.py`.
O middleware deixa de exigir e passa a resolver por pedido; a rota de seleção vira tela
de filtro. Não é reescrita da camada de ambiente, é rebaixamento de um gate.

Isso ganha fase própria (Fase 1b) porque **vale sozinho, sem nada do lote**: mesmo que o
intercompany nunca saia, um operador que não precisa escolher empresa erra menos e
trabalha mais rápido.

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
Mostra também a fila de **pedidos retidos**: os que chegaram pelo watcher sem CNPJ de
fornecedor no documento e sem decisão lembrada. Eles não foram importados em ambiente
nenhum e estão esperando alguém escolher. É o único ponto onde a ausência de resposta
fica visível, e é de propósito que ela fique.

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
| **1** | `supplier_cnpj` nos 11 parsers + `app/routing/ambiente.py` (documento → histórico → memória → perguntar) + escolha no preview | **sim** — acaba o pedido Nasmar caindo na MM |
| **1a** | `customer_cnpj` no `DesmembramentoXlsParser` e no formato NBA — os 3 samples que hoje não têm nem cliente | **sim** — fecha os últimos 3 de 29 |
| **1b** | Fim da seleção de ambiente no login; ambiente vira selo do pedido | **sim** — vale mesmo que o lote nunca saia |
| **2** | Consolidador + lote + perna espelho + `/lotes` | **sim** |
| **3** | Perna de volta (Flow, reconciliação) pelo vínculo registrado | **sim** |

A Fase 1 tem uma ordem interna que importa: **o campo e o roteador primeiro, os 11
parsers depois, um a um.** Cada parser que passa a extrair o fornecedor tira um formato
da fila do "perguntar" — a feature funciona desde o primeiro, com os outros caindo no
fluxo de escolha manual. Não é big bang.

O degrau do histórico é uma consulta de leitura aos dois Firebird e **entra junto com o
roteador**, não depois: sem ele a Fase 1 nasce perguntando 8 vezes em 29 em vez de 3.

---

## Testes

| Alvo | Arquivo | O que cobre |
|---|---|---|
| Roteamento | `tests/test_routing_ambiente.py` | fornecedor Nasmar → `nasmar`; fornecedor MM → `mm`; **documento vence histórico e memória**; **histórico vence memória**; cliente nos dois bancos na janela → `perguntar`, nunca o de maior volume; sem nada → `perguntar`, nunca um default; divergência é gravada em `divergiu_em`; CNPJ malformado |
| Histórico | `tests/test_routing_historico.py` | janela de 12 meses (caso Beira Rio: pedidos na MM até 05/2025 ficam fora e não geram ambiguidade); cliente só num banco resolve; cliente nos dois **recusa**; cliente sem histórico devolve `None` |
| Fornecedor nos parsers | `tests/test_supplier_cnpj_samples.py` | os 21 samples que trazem CNPJ de fornecedor resolvem para o ambiente certo; **nenhum sample resolve para dois ambientes**; os 8 sem CNPJ devolvem `None`, não um palpite |
| Watcher sem humano | `tests/test_scan_environments_retencao.py` | pedido sem fornecedor no watcher fica **retido**, não é importado em ambiente default |
| Consolidador (puro) | `tests/test_consolidador_lote.py` | soma por `(produto, data, preço)`; **preços diferentes não fundem** (caso Nacional Lojas); fator aplicado; `DT_ENTREGA` = menor; `DT_ENTREGA_ITEM` por item; `OBS` formatada |
| Repos | `tests/test_decisao_ambiente_repo.py`, `tests/test_lotes_repo.py` | CRUD; chave única; lote fechado é imutável; **memória vazia é estado válido, não erro** |
| Fechamento | `tests/test_fechar_lotes.py` | janela semanal e quinzenal; **sem fator → `aguardando_fator`, não fecha**; pedido atrasado vai pro próximo lote |
| Mapper | `tests/test_erp_mapper_colunas.py` | todas as colunas de 100%; `CODFIGFISCAL` por ambiente; `UNID` do cadastro |
| Perna de volta | `tests/test_flowpcp_intercompany.py` (estender) | decisão do Flow resolve para a perna de origem via `lote_item`, não para o lote |

Os quatro testes que travam regressão perigosa: **"sem fator não fecha"**, **"preços
diferentes não fundem"**, **"sem fornecedor não roteia"** e **"documento vence memória"**.
Nenhum pode ser relaxado sem decisão comercial — os dois últimos são o que torna o caso
Centauro impossível por construção.

---

## Questões abertas

**Todas as questões comerciais foram fechadas** — as três do PDF em 08/09, as cinco do
formulário em 09/09. Estão na "Diretriz comercial", itens 5 a 11.

**A questão da lista de clientes morreu na Revisão 4.** Ela existia porque o roteamento
dependia de um cadastro curado. Com o fornecedor decidindo, não há lista para aprovar,
e a aprovação nominal que a Revisão 3 exigia deixou de ser pré-requisito de qualquer
fase. Não precisa mais perguntar ao Rafael quem compra da Nasmar.

Resta **uma**, e é bem mais barata de responder:

**A Nasmar vende alguma coisa que ela não compra da MM?** O lote consolida *tudo* que
entra no ambiente `nasmar`, porque toda venda da Nasmar implica uma compra dela na MM. Se
existir linha de produto que a Nasmar compra de outro fornecedor e revende, esses pedidos
não podem entrar no lote. É pergunta de sim ou não, e só afeta a Fase 2.

Nenhuma questão bloqueia a Fase 0 nem a Fase 1.

---

## Riscos

**INSERT direto é escrita em ERP de produção.** O caminho nunca rodou (fato 1). Fase 0 e
Fase 2 só validam em `.fdb` de cópia. Primeiro lote real sai com conferência manual antes
de faturar.

**O fator informado pode estar errado.** O desenho força a resposta a vir de um humano
identificado, mas não valida se está certa. Mitigação futura: alerta quando o fator
informado divergir do último aplicado.

**Sem documento e sem histórico é 3 de 29 hoje** (fatos 14 e 15) — e nesses o Portal
depende da escolha do operador, que pode errar. Mitigações: a escolha é explícita e registrada com
autor e data; ela **perde** para o CNPJ do documento sempre que ele aparecer; e cada
parser que passa a extrair o fornecedor reduz a superfície. O risco encolhe com o tempo
em vez de crescer, que é o oposto do cadastro curado da Revisão 3.

**A janela de 12 meses do histórico é uma escolha, não um fato** (fato 15). Cliente que
migrou de empresa há mais de um ano some da janela e some da ambiguidade — foi o que
resolveu a Beira Rio. Se ele voltar, a janela responde com a informação velha. Mitigação:
o Portal mostra o histórico completo mesmo quando a janela resolve, e a decisão nunca é
silenciosa.

**Parser que extrai o fornecedor errado rotearia errado com confiança.** É o único jeito
de o novo desenho reproduzir a classe de erro do fato 13. Por isso
`tests/test_supplier_cnpj_samples.py` roda contra os 29 samples reais e trava a
propriedade medida: nenhum documento resolve para dois ambientes.

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
- Cadastro curado de quais clientes compram da revenda (morreu na Revisão 4)
- **Percentual ou método por cliente.** Decidido em 2026-08-25: o parâmetro é global e
  não existe override. A uniformidade é o que corrige o desvio do fato 3 — uma exceção
  cadastrada reabriria exatamente o buraco que a feature fecha.
