# Módulo: routing (roteamento intercompany)

## Responsabilidade

Decide em que **ambiente** (empresa) um pedido de compra entra, quando o
Portal opera mais de uma (MM Americanense, Nasmar, ...) e o mesmo cliente
pode comprar de qualquer uma delas. Atrás de um interruptor de três estados
que nasce **desligado** — mergear e deployar esta feature não muda nada no
comportamento de hoje até alguém virar a chave explicitamente em
`/admin/roteamento`.

Por cima da escada original há a **regra comercial de faturamento** (degrau
`regra`): rede fixa por raiz/CNPJ ou UF do cliente faturado decidem MM x
Nasmar antes de qualquer outro degrau. Nasce **desligada** e só existe quando
a política está ativa.

Duas regras não se negociam em lugar nenhum deste domínio:

1. **Ambíguo não responde.** Cliente com pedido em dois ambientes na mesma
   janela, dois CNPJs de ambiente no mesmo documento, degrau que empata —
   tudo isso devolve "não sei", nunca um palpite pelo caminho mais provável.
2. **Erro não vira ausência.** Firebird de um ambiente fora do ar entra
   marcado "indisponível", nunca como "zero pedidos". Contar "não sei" como
   "não tem" transformaria uma VPN caída em roteamento errado com confiança.

## Arquivos críticos

- `app/routing/ambiente.py` — a escada (`ambiente_para`), o wiring comum
  (`decidir`) e `Deps`/`Decisao`. Módulo **puro**: só faz I/O através de
  `Deps`, nenhum import de persistência no topo do arquivo — é o que permite
  testar a escada inteira sem banco.
- `app/routing/regra.py` — degrau `regra` (puro): `Politica`, `RedeFixa`,
  `UfLida`, `avaliar`, `ler_uf` (UF de `CADASTRO.UF` lida no Fire, só-leitura,
  via `connect_somente_leitura`, `CPF_CNPJ IN (mascarado, dígitos)`) e
  `ler_uf_com_cache` (TTL 600 s; `indisponivel` fica 60 s).
- `app/routing/documento.py` — degrau 1: regex de CNPJ + `detectar_fornecedor`
  (presença, não posição) + `cnpjs_de_ambientes()`.
- `app/routing/historico.py` — degrau 2: `consultar()` (Firebird de todos os
  ambientes ativos) + `resolver()` (as duas regras acima, aplicadas).
- `app/persistence/decisao_ambiente_repo.py` — degrau 3: a memória
  (`lembrar`, `lembrada`, `marcar_divergencia`).
- `app/persistence/roteamento_repo.py` — o interruptor (`modo`, `set_modo`),
  a tabela sombra (`registrar_sombra`, `taxa`) e a fila de pendência
  (`registrar_pendencia`, `buscar_pendencia`, `limpar_pendencia`).
- `app/persistence/regra_faturamento_repo.py` — política e redes
  (`config`, `set_config`, `listar_redes`, `salvar_rede`, `remover_rede`,
  `politica`).
- `app/persistence/roteamento_decisao_repo.py` — o rastro (`registrar`,
  `listar_por_import`, `ultima`); `registrar` nunca levanta.
- `app/persistence/mover_pedido.py` — mover pedido em revisão de empresa
  (`mover_import`, `motivo_nao_move`, `trava_do_pedido`, `PedidoNaoMovivel`,
  `PedidoOcupado`).
- `app/persistence/schema_shared.py` — as tabelas (ver
  `modules/persistence.md` pro schema completo).
- `app/web/routes_roteamento.py` — `/api/roteamento/modo`, `/api/roteamento/taxa`,
  `/api/roteamento/regra*`, `/api/roteamento/decisoes`.
- `app/web/server.py` — wiring do commit (`_decidir_ambiente`,
  `_resolver_env_alvo`, `_roteamento_para_preview`) e cross-env de
  `/api/imported` (ver `modules/web.md`, seção "Roteamento intercompany").
- `app/worker/jobs/scan_environments.py` — wiring do watcher: mesma escada,
  sem humano pra perguntar, retém em vez de chutar.
- `app/pipeline.py` — `_marcar_fornecedor`, a varredura que preenche
  `order.header.supplier_cnpj` entre o parser e o normalizer (ver
  `modules/pipeline.md`).

## A escada (`ambiente_para`, `app/routing/ambiente.py`)

Precedência **estrita** — cada degrau perde para o de cima, sem exceção.
São 5 degraus; `regra` fica no topo e só existe com a política ativa:

0. **`regra`** — regra comercial de faturamento (`app/routing/regra.py`).
   Rede fixa (raiz de 8 dígitos ou CNPJ de 14) vence a UF; sem rede fixa, a
   UF do CNPJ faturado (`CADASTRO.UF`, lida só-leitura nas duas empresas da
   política) manda: SP → `env_sp`, fora → `env_fora_sp`. **Nunca chuta UF**:
   Fire mudo, UF divergente entre as bases, linha sem UF, UF fora das 27 +
   `EX` ou cliente inexistente = `None`, e a escada segue para o degrau 1.
   O CNPJ é o do cabeçalho (`customer_cnpj`); lojas só contam para achar a
   rede fixa. Rede cuja empresa não é ambiente ativo não decide (nem cai na
   UF): `None` com aviso no log. Quando decide e o
   documento apontaria outra empresa, `Decisao.contraria` carrega o slug do
   documento. `deps_padrao` só monta a regra (`_regra_padrao`) com política
   ativa e as duas empresas ativas; fora disso `Deps.regra is None` e zero
   I/O novo.

1. **`documento`** — `supplier_cnpj` do pedido casa com `environments.cnpj`.
   Detecção é por **presença**, não posição: `detectar_fornecedor` acha o
   único CNPJ de ambiente presente no texto do documento, sem saber (nem
   precisar saber) se ele apareceu no papel de fornecedor, cliente ou
   transportadora. Dois CNPJs de ambiente no mesmo documento → `None`, o
   degrau seguinte decide. Medido em 29 samples reais: 21 trazem o CNPJ de
   uma das duas empresas, nenhum traz as duas.
2. **`historico`** — quantos pedidos este cliente tem em cada ambiente
   ativo, no Firebird, na janela de 12 meses — só responde se for
   **inequívoco** (`app/routing/historico.py::resolver`): exatamente um
   ambiente com `pedidos > 0`, e nenhum ambiente `indisponivel`. Cliente em
   dois ambientes, ou um Firebird mudo, e o degrau não resolve — sem
   exceção pelas regras do topo do arquivo. Enriquecimento opcional
   (`Deps.historico_amplo`, janela de 24 meses): só anexa uma frase de
   alerta na explicação quando existe ("...também já comprou de X"); nunca
   influencia `env_slug` e nunca causa I/O quando `None` (é o que mantém
   `observando` barato).
3. **`memoria`** — a decisão que um humano já tomou pra este cliente
   (`decisao_ambiente_repo.lembrada`), chaveada por
   `cnpjs_do_pedido(order)[0]` — no desmembramento sem CNPJ no cabeçalho,
   isso é o CNPJ da PRIMEIRA loja, não um "CNPJ do cliente" canônico; quem
   grava a decisão tem que usar exatamente essa mesma chave. Filtra decisão
   apontando pra ambiente desativado (cai pra "perguntar" em vez de travar
   um commit em `ligado` num 412 sem seletor na tela).
4. **`perguntar`** — nada resolveu, ou o histórico foi ambíguo/indisponível.
   `env_slug=None`. Resultado, não falha — `_motivo_historico_nao_resolveu`
   distingue pro operador três casos que pedem ações diferentes: pedido sem
   CNPJ nenhum, Firebird mudo, ou cliente novo sem histórico.

`ambiente_para` **nunca devolve um ambiente default**. `env_slug is None`
com `degrau == 'perguntar'` é resposta legítima, e quem chama tem que saber
tratar — não existe fallback silencioso pra "o de sempre".

Contratos que o wiring (quem monta `Deps` e renderiza `Decisao`) precisa
conhecer porque não dá pra descobrir sozinho em produção — ver docstring de
`app/routing/ambiente.py` para o texto completo:
- Exceção de `env_por_cnpj`, `historico` ou `memoria` **propaga** — só
  `historico_amplo` (enriquecimento) é blindado dentro do módulo puro.
- `explicacao` mistura slug (degraus documento e memória, só têm isso à
  mão) e nome legível (degrau histórico, via `HistoricoAmbiente`). Resolver
  slug → nome pro operador é trabalho de quem renderiza, não deste módulo.

## O interruptor de três estados (`roteamento_repo.MODOS`)

| Modo | O roteador roda? | Quem decide o ambiente do commit/import |
|---|---|---|
| `desligado` (default, nasce assim) | Não é chamado | O de sempre — cookie no commit, pasta varrida no watcher. Nenhuma sombra, nenhuma pendência. |
| `observando` | Roda e grava o que TERIA feito | A escolha do operador (commit) / a pasta varrida (watcher) — a decisão do roteador vira **sombra**, nunca ação. |
| `ligado` | Roda e a decisão **vale** | O ambiente decidido. Sem resposta, não cai pro cookie/pasta em silêncio — pergunta (commit, 409) ou retém (watcher, pendência). |

Por que três e não dois: o problema é de sequência — não dá pra validar o
que não está rodando, nem pra ligar o que não foi validado. `observando` é
o degrau que resolve isso, e a taxa de acerto (`roteamento_repo.taxa`,
exposta em `GET /api/roteamento/taxa`) é o material que autoriza virar a
chave pra `ligado`.

Trocar o modo é imediato — sem deploy, `PUT /api/roteamento/modo`
(admin-only; leitura é `require_user`). A branch inteira depende de
`desligado` ser **bit-a-bit** o comportamento de hoje — é a garantia que
permite mergear antes de o roteador estar validado.

### Reversão imediata (não relaxar)

A promessa central dos três estados é "volta a qualquer momento, sem
deploy, se aparecer surpresa". Duas armadilhas já quebraram essa promessa
na revisão final de branch e foram fechadas:

- **Pendência não pode sobreviver à troca de modo.** Uma linha em
  `roteamento_pendencia` só é criada em `ligado` (watcher sem resposta,
  arquivo retido). O ramo de throttle (reavalia no máximo 1x/hora, ver
  `_PENDENCIA_REAVALIACAO_S` em `scan_environments.py`) só pode gatear
  quando a varredura ATUAL está em `ligado` — gatear incondicionalmente
  fazia um rollback pra `desligado`/`observando` continuar retendo o
  arquivo até o throttle expirar sozinho, em vez de imediatamente.
- **A dedupe global do watcher é a ÚNICA coisa que muda comportamento em
  `desligado`.** `_already_imported` varre TODOS os ambientes (não só o
  varrido) fora de `desligado` — necessário porque o roteamento pode mandar
  o arquivo pra outra empresa. Em `desligado` isso não pode acontecer nunca,
  então a checagem é só dentro do próprio ambiente (`(environment_id,
  sha256)`, a semântica de sempre); sem esse gate, o mesmo arquivo
  aparecendo nas pastas de DUAS empresas perdia o segundo silenciosamente
  (`scan.skip_duplicate` + move pra `Pedidos importados/`) mesmo com o
  roteador nem sendo chamado.

## Os dois pontos de wiring

`app/routing/ambiente.py::decidir(order, *, origem)` é o núcleo comum — os
dois chamadores eram cópias idênticas exceto o prefixo do log:

- **`desligado`**: `decidir` nem chama a escada. Devolve `(modo, None)`.
- **`observando`**: exceção da escada vira `logger.warning` + `(modo,
  None)` — do ponto de vista de quem chama é como se fosse `desligado`
  PARA ESTE PEDIDO. Evidência é importante, o pedido é mais.
- **`ligado`**: exceção vira um degrau `'perguntar'` explicado — NUNCA cai
  pro ambiente "de sempre" em silêncio. Por contrato, em `ligado` o retorno
  nunca é `(modo, None)`: só varia entre `Decisao` resolvida e não
  resolvida.

### Web (`app/web/server.py`, humano na tela)

`POST /api/commit`: a decisão que o preview guardou no `PreviewEntry`
(`roteamento_modo`/`roteamento_decisao`, a que o operador viu; sem I/O no Fire
de novo) → `_resolver_env_alvo(...)`. Modo mudou entre preview e commit → 409
pedindo nova conferência, antes de consumir. Preview sem decisão guardada →
`_decidir_ambiente(order)`.
Fora de `ligado`, `env_alvo` é sempre o ambiente do cookie (idêntico a
antes da feature). Em `ligado`: escolha do operador
(`body.environment_slug`) > decisão resolvida (a sugestão do preview) > 409
pedindo escolha (nunca um default). A escolha vence porque o operador pode
trocar qualquer sugestão; a troca fica no rastro. Quando o destino difere da
empresa que conferiu o preview (`PreviewEntry.check_env_slug`), o commit refaz
a conferência de produtos no Fire do destino. O 409
roda ANTES de `get_cache().consume()` — não queima o preview se o
roteador falhar exatamente no commit. Em `observando`, grava
`roteamento_sombra`; se a decisão divergiu da memória, marca
`divergiu_de`; em `ligado` sem resposta, a escolha do operador vira memória
nova (`decisao_ambiente_repo.lembrar`).

O audit trail (`imported_to_portal`) carrega `{degrau, env_slug,
explicacao}` sob a chave `roteamento` sempre que `decisao is not None`
(`None` só em `desligado`, ou em `observando` com falha do roteador) — é o
registro de POR QUE o pedido foi pra aquela empresa, não só o
`environment_id` (o resultado). Sem isso, um pedido roteado pelo degrau
`historico`/`memoria` em `ligado` não deixava rastro nenhum da razão, e o
histórico do Firebird de 12 meses atrás não é reconstruível depois.

### Watcher (`app/worker/jobs/scan_environments.py`, sem humano)

Mesma escada, sem ninguém pra perguntar: em `ligado`, pedido que não
resolve (degrau `perguntar`, ou ambiente resolvido que não existe mais)
**não é importado** — fica retido na pasta e vira uma linha em
`roteamento_pendencia`, reavaliada no máximo 1x/hora. `env_alvo` é o
ambiente DECIDIDO (pode divergir do varrido); a linha em `imports` nasce
sob `env_context.active_env(env_alvo)`, mas `_move_to_imported` continua
usando a pasta VARRIDA — é de lá que o arquivo veio e é lá que fica o
histórico de entrada.

Mesmo audit trail que o commit: `append_audit(import_id,
"imported_to_portal", {..., "roteamento": {degrau, env_slug, explicacao}})`
dentro do mesmo `active_env` do insert, e o log estruturado
`scan.imported` carrega o `degrau` também. O watcher não chamava
`append_audit` nenhuma vez antes da revisão final de branch — a única
evidência de um pedido roteado automaticamente era o `environment_id`.

Nunca grava sombra em `ligado` — sombra é "o que o roteador TERIA feito" e
alimenta a taxa que autoriza virar a chave; misturar "o que ele fez"
distorceria a métrica.

### Rastro (`roteamento_decisao`) e mover pedido

Todo commit web e todo import do watcher grava uma linha em
`roteamento_decisao` com `acao` em: `sugestao_aceita`, `troca_no_preview`,
`escolha_sem_sugestao`, `movido_apos_importar`, `automatico`, `observando`.
`decidido_por` = e-mail do usuário, ou `sistema:<degrau>` / `sistema:pasta`
sem humano. Só INSERT.

`POST /api/imported/{id}/mover-empresa` (fora de `desligado`) leva um pedido
em revisão (`portal_status='parsed'`) para a outra empresa
(`mover_pedido.mover_import`): trava por pedido compartilhada com o envio ao
Fire/XLSX, lock global de mover, recusa (409) pedido já no Fire, exportado em
XLSX, com rastro no Flow/Gestor ou com revisão de preço enviada ao Flow;
também 409 com o pedido em duas origens e com o banco ocupado ("database is
locked"). `mover_import` devolve `movido` | `concluido` (terminou um mover
interrompido) | `ja_movido`; rastro e memória saem uma vez por mover. Em
`desligado` a rota só CONCLUI um mover interrompido (pedido no destino e na
origem). No destino recalcula o check de produto e preço; em `ligado` a
memória aprende a nova empresa. O detalhe do pedido ganha `env_slug`,
`roteamento_modo`, `pode_mover` e `motivo_nao_move` (nada disso é lido em
`desligado`).

Em todos os modos o envio ao Fire/XLSX segura `trava_do_pedido` com teto de
120 s (`_ESPERA_ENVIO_S`, depois 409 `pedido_ocupado`) e recusa (409
`mover_inacabado`) pedido que também está em outra empresa ativa — mover
interrompido deixa as duas cópias em revisão.

## As tabelas (`app_shared.db`)

Schema completo em [`modules/persistence.md`](persistence.md). Resumo do
papel de cada uma:

- **`environments.cnpj`** (+ índice único parcial `is_active=1`) — não é
  uma tabela nova, mas é a chave do degrau 1. Documentado em
  [`modules/environments.md`](environments.md).
- **`roteamento_modo`** — o interruptor. Linha única, sem linha = `desligado`.
- **`roteamento_sombra`** — o que o roteador TERIA feito vs. o que
  aconteceu, só em `observando`. Alimenta `taxa()` e a lista de
  divergências (`GET /api/roteamento/taxa`).
- **`roteamento_pendencia`** — arquivo que o watcher não soube rotear em
  `ligado`. PK `sha256`, idempotente (re-varredura não infla a fila).
- **`regra_faturamento_config`** — política única (`id=1`): `env_sp`,
  `env_fora_sp`, `ativa` (nasce 0). Sem linha = regra desligada.
- **`regra_faturamento_rede`** / **`regra_faturamento_chave`** — redes fixas e
  suas chaves (`raiz`|`cnpj`). Semente: Centauro, Riachuelo, Kallan (por
  raiz) e Sam's Club (12 CNPJs de CD), todas em `mm` — só enquanto a política
  nunca foi salva e a tabela de redes está vazia. Ligar exige toda rede com
  chave e em empresa ativa; remover rede exige a regra desligada.
- **`roteamento_decisao`** — o rastro de cada decisão (ver acima).
- **`decisao_ambiente`** — a memória (degrau 3), PK `cnpj_cliente`. Nasce
  vazia e isso é um estado válido; perde para documento E histórico sempre.

## Testes

```bash
.venv/bin/pytest tests/test_routing_documento.py tests/test_routing_historico.py \
  tests/test_routing_ambiente.py tests/test_decisao_ambiente_repo.py \
  tests/test_roteamento_repo.py tests/test_routing_wiring.py \
  tests/test_routing_modo.py tests/test_scan_environments_roteamento.py \
  tests/test_routing_regra.py tests/test_regra_faturamento_repo.py \
  tests/test_roteamento_decisao_repo.py tests/test_mover_pedido.py \
  tests/test_routes_regra_faturamento.py tests/test_queries_regra_uf.py \
  tests/test_web_static_regra_faturamento.py -v
```

- `test_routing_documento.py` — regex de CNPJ, presença vs. ambiguidade.
- `test_routing_historico.py` — janela de 12/24 meses, ambíguo não
  responde, erro não vira ausência.
- `test_routing_ambiente.py` — a escada pura, com `Deps` fake (sem banco).
- `test_decisao_ambiente_repo.py` — a memória, `marcar_divergencia`.
- `test_roteamento_repo.py` — o interruptor, `taxa()` (inclusive o clamp de
  `dias` em `[1, 3650]`), a fila de pendência.
- `test_routing_wiring.py` — `/api/commit` nos três modos, o teste que
  protege a adoção (`test_desligado_nao_muda_nada`).
- `test_routing_modo.py` — `/api/roteamento/*`, `/api/env/clear`, gate de
  `/` e cross-env de `/api/imported`.
- `test_scan_environments_roteamento.py` — o watcher nos três modos,
  retenção de slug fantasma, throttle de pendência e sua reversão
  imediata ao trocar de modo, dedupe global vs. por-ambiente conforme o
  modo, e o audit trail em `ligado`.
- `test_routing_regra.py` — `avaliar` (rede fixa, UF, nunca chuta), cache de UF.
- `test_regra_faturamento_repo.py` / `test_roteamento_decisao_repo.py` —
  política, redes e rastro.
- `test_mover_pedido.py` — mover, recusas, travas e a rota.
- `test_routes_regra_faturamento.py` — `/api/roteamento/regra*` e `/decisoes`.
- `test_queries_regra_uf.py`, `test_web_static_regra_faturamento.py` — SQL de
  UF só-leitura e a UI do admin.
