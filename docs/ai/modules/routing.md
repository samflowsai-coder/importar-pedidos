# Módulo: routing (roteamento intercompany)

## Responsabilidade

Decide em que **ambiente** (empresa) um pedido de compra entra, quando o
Portal opera mais de uma (MM Americanense, Nasmar, ...) e o mesmo cliente
pode comprar de qualquer uma delas. Atrás de um interruptor de três estados
que nasce **desligado** — mergear e deployar esta feature não muda nada no
comportamento de hoje até alguém virar a chave explicitamente em
`/admin/roteamento`.

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
- `app/routing/documento.py` — degrau 1: regex de CNPJ + `detectar_fornecedor`
  (presença, não posição) + `cnpjs_de_ambientes()`.
- `app/routing/historico.py` — degrau 2: `consultar()` (Firebird de todos os
  ambientes ativos) + `resolver()` (as duas regras acima, aplicadas).
- `app/persistence/decisao_ambiente_repo.py` — degrau 3: a memória
  (`lembrar`, `lembrada`, `marcar_divergencia`).
- `app/persistence/roteamento_repo.py` — o interruptor (`modo`, `set_modo`),
  a tabela sombra (`registrar_sombra`, `taxa`) e a fila de pendência
  (`registrar_pendencia`, `buscar_pendencia`, `limpar_pendencia`).
- `app/persistence/schema_shared.py` — as quatro tabelas (ver
  `modules/persistence.md` pro schema completo).
- `app/web/routes_roteamento.py` — `/api/roteamento/modo`, `/api/roteamento/taxa`.
- `app/web/server.py` — wiring do commit (`_decidir_ambiente`,
  `_resolver_env_alvo`, `_roteamento_para_preview`) e cross-env de
  `/api/imported` (ver `modules/web.md`, seção "Roteamento intercompany").
- `app/worker/jobs/scan_environments.py` — wiring do watcher: mesma escada,
  sem humano pra perguntar, retém em vez de chutar.
- `app/pipeline.py` — `_marcar_fornecedor`, a varredura que preenche
  `order.header.supplier_cnpj` entre o parser e o normalizer (ver
  `modules/pipeline.md`).

## A escada (`ambiente_para`, `app/routing/ambiente.py`)

Precedência **estrita** — cada degrau perde para o de cima, sem exceção:

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

`POST /api/commit`: `_decidir_ambiente(order)` → `_resolver_env_alvo(...)`.
Fora de `ligado`, `env_alvo` é sempre o ambiente do cookie (idêntico a
antes da feature). Em `ligado`: decisão resolvida > escolha do operador
(`body.environment_slug`) > 409 pedindo escolha (nunca um default). O 409
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

## As quatro tabelas (`app_shared.db`)

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
- **`decisao_ambiente`** — a memória (degrau 3), PK `cnpj_cliente`. Nasce
  vazia e isso é um estado válido; perde para documento E histórico sempre.

## Testes

```bash
.venv/bin/pytest tests/test_routing_documento.py tests/test_routing_historico.py \
  tests/test_routing_ambiente.py tests/test_decisao_ambiente_repo.py \
  tests/test_roteamento_repo.py tests/test_routing_wiring.py \
  tests/test_routing_modo.py tests/test_scan_environments_roteamento.py -v
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
