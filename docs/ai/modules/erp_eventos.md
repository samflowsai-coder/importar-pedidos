# Módulo: erp_eventos (SP1 — pedidos do ERP para o Flow)

## Responsabilidade

Ler os pedidos **abertos** de cada base do ERP (só leitura), transformar cada mudança
num evento do contrato `pedido.evento_erp.v1` e entregar ao Flow em
`POST /api/portal-pedidos/eventos`; à noite, uma foto completa por base em
`POST /api/portal-pedidos/snapshot-erp`. Modo sombra: o Flow só registra.

Spec: `pcp-app/docs/superpowers/specs/2026-10-03-sp1-transporte-pedidos-fire-design.md`
(v6.1, §3, §3A, §4). Plano: `docs/superpowers/plans/2026-10-09-sp1-portal-transporte-pedidos-erp.md`.

**Nomes pelo papel (P-PROD-08):** tudo novo é `erp_*`, `base`, `produtora`,
`intermediaria`. Os valores de base (`mm`, `nasmar`) são configuração em
`/admin/ambientes`, nunca código. Colunas antigas (`imports.fire_codigo`) ficam.

## Arquivos críticos

| Arquivo | Papel |
|---|---|
| `app/erp_eventos/canonico.py` | serialização canônica e `id` (SHA-256) — ver "id" abaixo |
| `app/erp_eventos/contrato.py` | espelho pydantic estrito do Zod do Flow; nada fora dele entra na fila |
| `app/erp_eventos/modelo.py` | `PedidoErp`/`ItemErp`, ida e volta exata com o estado |
| `app/erp_eventos/mudancas.py` | diff puro estado × leitura, em ordem de aplicação |
| `app/erp_eventos/eventos.py` | mudança → evento, `seq`, `unidade` de envio |
| `app/erp_eventos/leitura.py` | `LeitorFirebird` (SELECT em `queries.py`, bloco SP1) |
| `app/erp_eventos/vigia.py` | ciclo de uma base: lê, enriquece, grava estado+seq+fila numa transação |
| `app/erp_eventos/lotes.py` / `drenagem.py` / `envio.py` | corte de lote, resposta por id, HTTP, ritmo |
| `app/erp_eventos/foto.py` | foto noturna paginada |
| `app/erp_eventos/vinculo.py` | vínculo exato item do ERP ↔ `order.items` (insert e xlsx) |
| `app/erp_eventos/produtor.py` | `produtoProdutor` dos itens da base intermediária |
| `app/erp_eventos/eco.py` | `correlacaoId` da data que o próprio Portal aplicou |
| `app/erp_eventos/lote_fiscal.py` | emissor do `lote_fiscal` (produtor = envio do espelho do lote, `app/consolidators/integracao.py`) |
| `app/erp_eventos/runner.py` | thread do processo web, trava por base |
| `app/erp_eventos/consulta.py` | consulta por pedido: rótulos, frases, `texto_bloqueio`, `rotulo_lote` |
| `app/persistence/erp_envio_lote_repo.py` | registro de cada lote (`erp_envio_lote`): origem janela/foto/manual, quem, `envio_id` |
| `app/persistence/erp_bases_repo.py` | config + credencial cifrada por (ambiente, base) — `app_shared.db` |
| `app/persistence/erp_estado_repo.py` / `erp_vinculo_repo.py` / `erp_foto_repo.py` | tabelas `erp_*` do banco do ambiente |
| `tools/erp_medir.py` | medições SÓ LEITURA no ERP |

## Fluxo

`runner.loop` (thread `erp-eventos`, a cada 30 s) → para cada base ativa, sob trava:

1. **vigia** a cada 15 min: `LIST_PEDIDOS_ABERTOS_ERP` + releitura por `CODIGO` de quem
   estava aberto e sumiu da lista (saída de aberto, exclusão) → enriquece (cliente real,
   `produtoProdutor`, vínculo xlsx dos novos) → **uma transação**: estado + marca do
   `seq` + eventos. Lê tudo de novo a cada ciclo (medido: 323 pedidos em 0,3 s na cópia);
   não depende de `ULT_ALT_DTHR`, que não está provado mudar com item ou nota.
2. **drenagem**: só nas **janelas de envio** (8h, 11h, 14h, 17h, seg-sáb, Brasília;
   `app/utils/janelas_envio.py`, `PORTAL_FLOW_JANELAS`); fora delas a vigia segue
   enfileirando. Lotes em série. **Pelo menos um lote por janela**: sem 202 desde o
   início da janela, o runner pede o lote vazio (`_garantir_lote_na_janela`), mesmo com
   o ERP fora — o Flow usa o 202 como sinal de vida e alerta janela sem nada. A foto
   não segue as janelas; antes da primeira página, a fila observada até o `corteEm`
   sai (única exceção às janelas), para a foto nunca chegar antes dos eventos dela.
3. **foto**: no `foto_hora` local, uma por base por dia; páginas congeladas. Pedido fora
   do contrato → a foto do dia fica `nao_criada` (com o motivo, visível na tela) e não é
   tentada de novo até amanhã — uma foto sem ele diria ao Flow que saiu de aberto.
4. **retenção**: uma vez por dia, eventos `aceito`/`duplicado` com mais de 30 dias saem
   da fila; `rejeitado` fica (é o rastro). `erp_pedido_estado` não é podado: sem o estado
   de um pedido encerrado, a reabertura viraria `pedido_visto` em vez de `status_alterado`.

**Pedido manual ("Enviar agora"):** `runner.pedir_envio_manual` só grava o pedido em
memória, sob `_MANUAL_GUARD`. A thread atende no tick logo depois de `_lotes_da_janela`:
`drenar` se houver pendentes, depois vigia forçada (respeitando `_FALHA_ERP`), depois
`drenar`. Substitui a vigia normal daquele tick. O resultado fica em memória por 1 h
(`envio-manual/{envio_id}`). `drenar(origem, quem, envio_id)` grava uma linha em
`erp_envio_lote` por lote; a retenção de 30 dias apaga também `erp_envio_lote`.
`erp_bases_repo.base_atendida` é o predicado único de "a thread atende esta base":
`listar_ativas` e a rota filtram por ele.

**Etapas isoladas:** vigia, foto, drenagem e envio da foto têm `try` próprios — ERP fora
do ar ou foto inviável nunca param a drenagem nem o sinal de vida. Vigia que falha só
tenta de novo depois de 5 min (`fdb` não tem timeout).

Leitura do ERP sempre em `FirebirdConnection.connect_somente_leitura` (READ ONLY +
SNAPSHOT): o Firebird recusa escrita nessa transação.

## Contrato — o que o Portal garante

### id

`id = sha256(tenant ␟ base ␟ tipo ␟ erpCodigo ␟ erpItemCodigo|0 ␟ campo ␟ antes ␟ depois ␟ observadoEm)`,
separador `\x1f`, UTF-8. Número: `Decimal` normalizado sem expoente (`1200`, `3.15`, `0`);
`None` → vazio; data `YYYY-MM-DD`; `observadoEm` sempre UTC com milissegundos
(`2026-10-09T13:00:00.000+00:00`). `lote_fiscal`: `erpCodigo = erpCodigoEspelho`,
item 0, `campo = chaveLote`. Vetores fixos em `tests/test_erp_eventos_canonico.py` —
**mudar um deles é quebra de contrato**.

### seq e ordem

- `seq` contíguo por (base, `erpCodigo`), marca em `erp_seq_marca`, separada de
  `erp_pedido_estado`: apagar o estado não reaproveita `seq`.
- O `seq` só avança junto com o enfileiramento, na mesma transação; pedido que viola o
  contrato não consome `seq` e fica de fora até a próxima vigia (log `fora do contrato`).
- `pedido_visto` + `item_incluido` são uma `unidade`: o lote nunca a parte.
- Reabertura: `status_alterado` primeiro; encerramento: `status_alterado` por último.

### resposta

| HTTP | Portal |
|---|---|
| 202 | casa por `id`; id sem resultado segue pendente |
| 413 | divide sem partir unidade |
| 429 | espera `Retry-After` (mínimo: espera crescente 30 s → 15 min) |
| 5xx / rede | espera crescente |
| 401 | **bloqueia a base** até salvar credencial nova |
| 400 / outro 4xx | **bloqueia a base** até "Retomar envio" na tela |

Rejeitado é terminal na fila (não reenvia em laço) e fica contado na tela.

### Acordos com o Flow (2026-10-09)

- `codProduto` pode ir `null` (item sem produto no ERP); o pedido nunca fica fora por isso.
- `faturamento_detectado` de estorno sai com `codNf: null`.

## Vínculo exato

`indiceOriginal` = posição do item em `order.items` = posição no array `itens` do
`/recebimento` (`mapper.py`), e `importId` = `externalId` = `import_id`.

- **insert** (`EXPORT_MODE=db|both`): `FirebirdExporter` devolve
  `(CORPO_VENDAS.CODIGO, posição)` por identidade do item — o INSERT segue os grupos
  por local de entrega, não a ordem de `order.items`.
- **xlsx** (produção hoje): o Portal guarda em `erp_xlsx_linha` o que cada planilha
  levou; na primeira vez que vê o pedido, casa por posição (contagem, quantidade,
  cliente, número; preço só com `COMPARAR_PRECO`) e confere contra o payload do
  `/recebimento` no outbox. Ambíguo ou divergente = sem vínculo (o Flow usa a heurística).
- Log `vínculo origem=insert|xlsx` para medir quanto cai na heurística.

## Configuração (`/admin/ambientes` → "Pedidos do ERP para o Flow")

Por ambiente: base, papel, endereço e tenant do Flow, credencial `fpe_…` (gerada no
Flow por `scripts/portal-credencial-gerar.mjs`, cifrada aqui), hora da foto (a mesma
`fotoHoraSp` do Flow), ligar/desligar. Intermediário fiscal = o `intercompany_cnpj`
do ambiente produtor (já existente).

`PORTAL_ERP_VIGIA=0` desliga a thread inteira sem redeploy (os testes desligam).

## Medições pendentes (só leitura, via `tools/erp_medir.py`)

- **M1** — o importador de Excel do ERP grava os itens na ordem da planilha, com
  `CORPO_VENDAS.CODIGO` crescente? Grava o preço da planilha? (liga `COMPARAR_PRECO`)
- **M2** — editar um item no ERP preserva `CORPO_VENDAS.CODIGO`? (`HISTORICO` mostra)
- **M5** — cobertura do `produtoProdutor` (EAN/código do documento no catálogo da produtora).

## Armadilhas

- **Um processo só.** A trava por base é de processo (`runner._LOCKS`), como a da
  reconciliação. `ui.py` sobe um processo; com `PORTAL_RELOAD=true` ou mais de um worker
  do uvicorn haveria duas vigias da mesma base.
- **STATUS vazio no ERP** sai como `(SEM STATUS)` (o contrato exige texto) e conta como
  aberto; a comparação de status é sem caixa, no SQL e no modelo.
- **Vínculo xlsx não casa quando o cliente do ERP não é o CNPJ da planilha** (ex.: Sam's
  Club, cadastrado pelo CD de entrega). É o "ambíguo = sem vínculo" funcionando: o Flow
  usa a heurística. Contar isso antes de ler a medição de quanto cai na heurística.

- **Só o `ui.py` roda no Windows da MM.** O outbox do flowpcp e o `poll_decisoes` vivem
  no worker — se for isso em produção, nem o `/recebimento` sai nem há eco. Confirmar
  antes de ligar (não verificado).
- `fdb` no Mac não fala com a fbclient do Firebird 5: para ler a cópia local, use
  `firebirdsql` (`tools/erp_medir.py`).
- `lote_fiscal` tem produtor: o envio do espelho do lote intercompany
  (`app/consolidators/integracao.py`). `preparar_envio_espelho` chama
  `cancelar_reserva` (sobra de tentativa interrompida) + `reservar_lote` antes do
  INSERT; `concluir_envio_espelho` chama `registrar_lote_fiscal` numa transação
  só; insert que não acontece cancela a reserva. Base SP1 do espelho ausente ou
  com `ativo = 0` = sem reserva e sem `lote_fiscal` (`lote_fiscal_ok = 0`,
  auditado). Sem reserva, a vigia não segura nada. Ver `modules/consolidators.md`.

## Testes

```bash
.venv/bin/python -m pytest tests/test_erp_eventos_*.py -v
```
