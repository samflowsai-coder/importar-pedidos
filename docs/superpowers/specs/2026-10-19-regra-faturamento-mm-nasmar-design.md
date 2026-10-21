# Regra comercial de faturamento MM × Nasmar no roteamento

> Status: **rascunho para aprovação** (2026-10-19). Domínio: `routing` (+ `web`, `persistence`).
> Regra de negócio validada pela Bianca (comercial MM) e passada pelo Samuel em 19/10.

## 1. Intenção

O roteamento intercompany já está em `main` (`app/routing/ambiente.py`, escada
documento → histórico → memória → perguntar, atrás do interruptor de três
estados, hoje **desligado** em produção). Ele decide a empresa pelo que o papel
diz (o CNPJ de ambiente impresso) e pelo passado no Fire.

O comercial da MM tem uma regra escrita e explícita para decidir **quem fatura**:

| Clientes | Faturamento |
|---|---|
| Centauro, Riachuelo, Kallan, Sam's Club | **sempre MM** |
| Os outros (Artwalk, Authentic Feet, Beira Rio, Beira Rio Infantil, Daju, Kings, Kolosh, Magic Feet, NBA, Studio Z, Tennis Station) | **MM se o CNPJ faturado é de SP, Nasmar se é fora de SP** |

Essa regra passa a ser a **fonte da sugestão**. É sugestão, não ordem: o operador
vê o motivo e pode trocar a empresa no portal antes do pedido ir para o Fire, e
cada decisão fica registrada com quem decidiu.

**Por quê:** a regra do comercial é a verdade do negócio; o papel e o histórico
são indícios. Quando divergem, quem está certo é a regra. E quem errar precisa
deixar rastro auditável (quem, quando, o que o portal sugeria).

## 2. Objetivos

1. Um degrau novo no topo da escada, **`regra`**, que aplica a tabela acima.
2. A UF vem do **cadastro do cliente faturado no Fire** (decisão do Samuel: "o CNPJ
   que será faturado"), nunca do endereço de entrega impresso.
3. O operador vê no preview: empresa sugerida + motivo legível ("Kolosh, CNPJ de
   MG → Nasmar pela regra comercial") e pode trocar.
4. Para pedido que entrou pela pasta (watcher), existe ação de **mover para a
   outra empresa** enquanto o pedido não foi gravado no Fire.
5. **Rastro por pedido**: sugerido, escolhido, quem escolheu, quando, degrau e
   motivo. Não só a última decisão por cliente (que é o que `decisao_ambiente` guarda).
6. A lista das redes "sempre MM" é **dado editável no admin**, não código — a regra
   é do comercial e vai mudar sem deploy.

## 3. Não-objetivos

- Não grava nada no Fire além do que o fluxo de commit já grava.
- Não troca empresa de pedido **já gravado no Fire** (isso é cancelar lá; fora de escopo).
- Não mexe no lote semanal Nasmar→MM (Fase 2, outra branch).
- Não muda o interruptor de três estados: a regra entra pela mesma chave
  (`desligado` continua bit-a-bit o comportamento de hoje).
- Não cadastra rede nova no Fire nem corrige UF no cadastro.

## 4. Anti-objetivos (falha mesmo que "funcione")

- **Chutar a UF.** CNPJ não está no Fire, está sem UF, ou o mesmo CNPJ tem UFs
  diferentes nas duas bases → a regra **não responde** e a escada segue. Nunca
  inferir UF do DDD, do endereço impresso ou do prefixo do CNPJ.
- **Dividir um pedido entre as duas empresas em silêncio.** Pedido com lojas de
  CNPJs em UFs diferentes, fora do grupo "sempre MM", não é dividido: a regra não
  responde (vai para a escada e, em `ligado` sem resposta, para a pergunta).
- **Troca sem dono.** Toda escolha diferente da sugestão grava o usuário. Sem
  usuário identificado (watcher), o dono é `sistema:regra`/`sistema:<degrau>`.
- **Firebird mudo virar "fora de SP".** Erro de leitura da UF é "não sei", nunca
  ausência (regra 2 do módulo routing). O mesmo vale para UF que não é uma das
  27 nem `EX`: lixo no cadastro não vira "fora de SP".
- **UF das lojas decidir.** A UF que vale é a do CNPJ faturado do cabeçalho
  (`customer_cnpj`, §10.3); CNPJs de loja só servem para achar a rede fixa.

## 5. Fatos que o desenho assume (confirmados com o Samuel / Bianca)

1. Os casos com entrega em várias lojas (Centauro, Riachuelo, Kallan, Sam's)
   faturam num endereço único e são sempre MM. As demais redes não mandam pedido
   para várias lojas.
2. A UF que vale é a do CNPJ faturado.
3. Rede fora da lista: **minha decisão** — trata como "as outras" (SP/fora). É só
   sugestão; o operador corrige. A Bianca não respondeu esse ponto.

## 6. Desenho

### 6.1 Onde entra na escada

Precedência nova, estrita:

```
0. regra      → política comercial (sempre MM / por UF do CNPJ faturado)
1. documento  → CNPJ de ambiente impresso
2. historico  → Fire, 12 meses, inequívoco
3. memoria    → decisão lembrada
4. perguntar
```

`regra` só resolve quando tem certeza. Quando não tem (§4), devolve `None` e o
degrau `documento` decide como hoje. Se `regra` resolveu e o `documento` (ou a
memória) apontaria outra empresa, a divergência viaja em `Decisao.divergiu_de`
e aparece no preview ("o papel traz o CNPJ da Nasmar") — informação, não veto.

`ambiente_para` continua puro: a regra entra por um campo novo em `Deps`
(`regra: Callable[[Order], ResultadoRegra | None] | None = None`). `None` = degrau
desligado e zero I/O, o que preserva os testes atuais da escada.

### 6.2 A política (dado, não código)

Tabela nova em `app_shared.db`, editada em `/admin/roteamento`:

- `regra_faturamento_config` (linha única): `env_sp` (slug da MM), `env_fora_sp`
  (slug da Nasmar), `ativa` (0/1).
- `regra_faturamento_rede`: `nome`, `env_slug` (a empresa em que a rede fatura
  sempre), e as **chaves** em `regra_faturamento_chave` — raiz de CNPJ (8
  dígitos) ou CNPJ completo (14), mesmo formato que `preco_rede_chave_v1` já
  usa (`raiz`/`cnpj`, cnpj vence raiz). Rede cujo `env_slug` não é ambiente
  ativo não decide (nem cai na UF) e barra ligar a regra.

Só as exceções precisam de linha (as 4 redes "sempre MM"). Toda rede sem linha cai
na política padrão "por UF" — por isso rede nova funciona sem cadastro (fato 3).

Nasce com as 4 redes semeadas e `ativa = 0`. A semente só roda enquanto a
política nunca foi salva e a tabela de redes está vazia — depois que o admin
configurou, nada mais é semeado. As raízes de CNPJ das 4 redes
saem dos samples reais e do Fire (Centauro = SBF, faturamento `/0296-51` já
conhecido; Sam's = CD do local de entrega, ver memória do Sam's) e são conferidas
pelo Samuel na tela antes de ligar.

**Por que tabela própria e não `preco_rede_v1`:** a cópia de redes da tabela de
preço é do Flow, mora no SQLite de um ambiente e só existe se a sync de preço
rodou. Roteamento roda **antes** de saber o ambiente e não pode depender de uma
sync de outro domínio estar em dia.

### 6.3 Qual CNPJ é "o faturado"

`cnpj_da_rede(order, customer_cnpj)` (`app/erp/preco_avaliacao.py`) já resolve isso
para a tabela de preço: CNPJ do cliente no cabeçalho; senão, se todas as lojas têm
CNPJ válido com a mesma raiz, o menor. Reusar a mesma função (mover para um lugar
neutro se o import cruzar domínio). Para a regra:

1. Rede identificada pela raiz → se `sempre:<slug>`, resolve **sem olhar UF**.
2. Senão, CNPJ faturado = `customer_cnpj` normalizado. Sem ele, não responde
   (ver §10.3).

### 6.4 Leitura da UF no Fire

Consulta nova em `app/erp/queries.py`: UF do `CADASTRO` por CNPJ (cliente ativo),
**nas duas bases ativas**. Resultado:

- achou em uma ou nas duas com a mesma UF → usa;
- achou com UFs diferentes → não responde;
- não achou → não responde;
- uma base não respondeu e a outra não achou → não responde (erro não vira ausência).

Coluna cravada na Task 0 (Fire viva, só SELECT): `CADASTRO.UF`, cliente com
`RELAC_CLIENTE = 'Sim'`, casado por `CPF_CNPJ`. O Fire grava o CNPJ mascarado
(`NN.NNN.NNN/NNNN-NN`), então a consulta é `C.CPF_CNPJ IN (?, ?)` com a forma
mascarada e a de só dígitos — a coluna crua usa o índice (um `REPLACE` na
coluna varreria o `CADASTRO` inteiro). Cadastro em formato fora desses dois não
acha UF e a regra não responde (nunca chuta). UF válida = as 27 + `EX`.

Custo: 1 SELECT indexado por CNPJ por base, por pedido, com cache em memória
por CNPJ: leitura boa vale 600 s; base indisponível fica 60 s em cache (Fire
fora do ar não cobra um timeout por pedido e volta a ser tentado em 1 minuto).

### 6.5 Preview e troca pelo operador

- O banner de roteamento do preview ganha o degrau `regra` com texto do comercial:
  "Regra comercial: Centauro fatura sempre pela MM" / "Regra comercial: Kolosh, CNPJ
  faturado em MG → Nasmar".
- Seletor de empresa visível sempre que o modo é `ligado` ou `observando`, com a
  sugestão pré-selecionada. Trocar exige confirmar (a sugestão divergente fica no
  registro).
- **Pedido que entrou pela pasta**: na lista/detalhe, ação "Mover para <outra
  empresa>" enquanto o pedido não tem gravação no Fire. Mover = recriar a linha em
  `imports` no `app_state_<slug>.db` de destino dentro de
  `env_context.active_env(destino)` (armadilha conhecida do módulo) e apagar na
  origem, na mesma operação lógica, com o audit dos dois lados. Bloqueado se já
  houver número do Fire.

### 6.6 Rastro por pedido

Tabela nova `roteamento_decisao` em `app_shared.db` (append-only):

`import_id`, `decidido_em`, `degrau`, `env_sugerido`, `env_escolhido`,
`decidido_por` (login do usuário ou `sistema:<degrau>`), `motivo` (texto exibido),
`acao` (`sugestao_aceita` | `troca_no_preview` | `movido_apos_importar` | `automatico`).

Grava em `observando` e `ligado`. A `roteamento_sombra` continua existindo e
alimentando a taxa (não muda contrato). A memória (`decisao_ambiente`) continua
aprendendo da escolha do operador como hoje.

Tela: no detalhe do pedido, linha do tempo "Sugerido Nasmar pela regra (MG) →
trocado para MM por fulano em 19/10 14:02".

## 7. Mudanças de schema (precisa do ok do Samuel na hora da migration)

- `regra_faturamento_config`, `regra_faturamento_rede` (+ chaves), `roteamento_decisao`
  — todas novas, `CREATE TABLE IF NOT EXISTS` em `schema_shared.py`. Nenhuma
  alteração de tabela existente.

## 8. Ligar em produção

1. Deploy com `regra_faturamento_config.ativa = 0` → nada muda.
2. Samuel confere as 4 redes + raízes no admin e liga a regra.
3. Interruptor em `observando` por uma semana: a taxa mostra regra × escolha do
   operador. Divergência alta = conversa com o comercial antes de `ligado`.
4. `ligado`.

## 9. Testes

- Escada pura: regra resolve (sempre MM; SP; fora de SP); regra não responde (sem
  CNPJ no cabeçalho, CNPJ fora do Fire, UF vazia, UF inválida, UFs divergentes
  entre bases, base muda, rede de empresa inativa); regra × documento
  divergindo; `Deps.regra=None` mantém os testes atuais verdes.
- Wiring commit: troca no preview grava `roteamento_decisao` com o usuário.
- Wiring watcher: `automatico` com `sistema:regra`; mover pedido entre empresas
  (linha some na origem, nasce no destino, audit nos dois, bloqueado com número Fire).
- Query da UF contra a cópia do Fire (marcador de integração, igual aos existentes).

## 10. Decisões tomadas por mim na revisão (Samuel confere)

Saíram da leitura de um revisor como construtor. Nenhuma muda o que o comercial pediu.

1. **Em `ligado`, o operador pode trocar qualquer decisão resolvida**, não só a da
   `regra` (pedido do Samuel: "possibilidade de ajustar no portal"). Muda
   `_resolver_env_alvo` (`app/web/server.py`): hoje a decisão resolvida vence
   `body.environment_slug`; passa a ser sugestão + escolha explícita do operador,
   que vence quando vem e fica em `roteamento_decisao`. A memória aprende com a
   troca (como já aprende quando o roteador não resolve).
2. **`observando` continua sem mudar nada.** O commit vai para o ambiente do cookie,
   e a sugestão aparece só como informação, sem seletor pré-marcado. Assim a taxa
   mede o operador de forma independente. O seletor com sugestão aparece só em `ligado`.
3. **Sem `customer_cnpj`, a regra só responde para as redes "sempre MM"** (identificadas
   pela raiz das lojas). Para as outras, não responde e a escada segue. Corrige o
   §6.3, passo 2 (a "UF das lojas" sai).
4. **Mover pedido entre empresas** vale só com o modo diferente de `desligado` e só
   para pedido que ainda não foi enviado (sem número do Fire **e** sem XLSX exportado).
   O id do pedido (`imports.id`) é **mantido**, então o rastro não se parte. O arquivo
   original fica onde está. De-para, check de produto e trava de preço são
   **recalculados no destino** (o preview roda de novo); nada é copiado da origem.
5. **A UF é buscada só em `env_sp` e `env_fora_sp`.** Uma terceira empresa fora do ar
   não bloqueia a regra.
6. **A divergência da regra com o documento ganha um campo próprio na `Decisao`**
   (`contraria: str | None`). `divergiu_de` continua significando só "divergiu da
   memória", e `marcar_divergencia` e a sombra não se distorcem.
7. **Gate da regra:** `ativa=1` **e** modo diferente de `desligado`. Com `ativa=0`,
   `Deps.regra=None` (o degrau some e não faz I/O).
8. **Sam's Club é semeado por CNPJ completo** (os CDs de faturamento), não pela raiz,
   porque a raiz pode ser compartilhada com outras bandeiras do grupo. As outras 3
   redes vão pela raiz, conferida no admin antes de ligar.

## 11. O que fica para quem constrói

- Formato exato das telas (seguindo o banner de roteamento que já existe).
- Onde `cnpj_da_rede` passa a morar se o import cruzar `erp`→`routing`.
- Estratégia de cache da UF dentro do ciclo.
