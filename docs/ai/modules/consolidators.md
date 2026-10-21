# Módulo: consolidators (lote semanal intercompany Nasmar → MM)

## Responsabilidade

Fase 2 do roteamento intercompany: a Nasmar revende o que compra da MM, e a MM
refatura para a Nasmar **uma vez por janela** (semanal por padrão). Cada pedido
que a Nasmar cadastra no Fire pelo Portal vira uma **perna de origem** do lote da
janela; no fechamento, as pernas viram **um** pedido espelho no ambiente MM, no
nome da revenda, com o preço convertido pelo fator. O espelho passa pela revisão e
pelo "Cadastrar no Fire" de sempre e, quando o Fire aceita, grava o `lote_fiscal`
do SP1 (vínculo item a item espelho ↔ origem).

Nasce desligado: sem linha em `lote_config`, ou com `ativo = 0`, nada acontece.
`PORTAL_LOTES=0` desliga a thread de fechamento.

Spec: `docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`
(Rev. 4). Plano: `docs/superpowers/plans/2026-10-17-lote-semanal-nasmar-mm.md`.
Roteamento (Fase 1, em que empresa o pedido entra): `modules/routing.md`.

## Arquivos críticos

| Arquivo | Papel |
|---|---|
| `app/consolidators/janela.py` | puro: `janela_de`, `proxima_janela`, `chave_para`, `instante_fechamento` |
| `app/consolidators/lote.py` | puro: `preco_espelho`, `consolidar` (pernas → `Consolidado`: pedido, composição, OBS) |
| `app/consolidators/integracao.py` | ganchos no envio ao Fire: anexar perna de origem; preparar/abortar/concluir o espelho |
| `app/consolidators/fechamento.py` | fechamento (`fechar_lote`, `fechar_vencidos`), materialização do espelho, varredura de recuperação, thread `loop` |
| `app/persistence/lotes_repo.py` | `lote_config`, `lote_fator`, `intercompany_lote`, `intercompany_lote_item` |
| `app/persistence/schema_shared.py` | as quatro tabelas (no `app_shared.db`) |
| `app/web/routes_lotes.py` + `static/lotes.html` | tela `/lotes` e `/api/lotes*` (ver `modules/web.md`) |
| `app/web/server.py` | ganchos em `_send_one_to_fire` e recusa em `_export_one_xlsx` |
| `app/exporters/firebird_exporter.py` | `export(..., obs=)` e `FirebirdExportResult.produtos` |
| `app/erp_eventos/lote_fiscal.py` | reserva e registro do `lote_fiscal` (ver `modules/erp_eventos.md`) |
| `tools/lote_validar_copia.py` | prova ponta a ponta contra a **cópia local** do Fire (nunca produção) |

## Pré-requisitos para ligar

- **`EXPORT_MODE=db` na origem E no MM.** Perna de origem só entra no lote quando
  vai ao Fire pelo insert direto (`_send_one_to_fire`): é dele que sai o
  `CORPO_VENDAS.CODIGO` de cada item, sem o qual não há rastro do `lote_fiscal`. O
  espelho é **recusado** no caminho XLSX (`_export_one_xlsx` → 409 `lote_espelho`)
  e, em `EXPORT_MODE=both`, não gera XLSX.
- Pedido que entra no Fire **por fora do Portal** (digitado no Fire, importador de
  Excel, `found_in_fire` da reconciliação) fica **fora do lote**: não é refaturado.
  A tela mostra esses pedidos em `fora_do_lote` para ninguém ser pego de surpresa.
- `ativo = 1` na `lote_config` e um fator informado (sem fator, o lote espera em
  `aguardando_fator` e nada é escrito).

## Decisões (do plano, mais as tomadas na execução)

1. **Fator versionado por janela.** Salvar o fator global cria linha em `lote_fator`
   com `vigente_desde` = início da **próxima** janela. O lote copia, na criação, o
   fator vigente no **início da sua janela** e congela (`fator_preco_aplicado`,
   `modo_preco_aplicado`, TEXT). Sem default em lugar nenhum. Modo padrão de tela
   `desconto` (`preço × (1 − fator)`); `divisor` (`preço / (1 + fator)`, o que o
   Fire histórico mostra) existe para lote fechado lembrar o modo que usou.
   Faixa válida `0 ≤ fator < 1` nos dois modos. Tudo em `Decimal`, 4 casas,
   `ROUND_HALF_UP`.
2. **Janela pela data de importação da perna** (`imports.imported_at`). Se o lote
   dessa janela já fechou, a perna vai para a próxima janela com lote aberto (até 8
   saltos; esgotou = erro no log, perna fora). Lote fechado **nunca reabre**.
3. **Fechamento por thread no processo web**, não cron (o worker não roda no
   Windows da MM). A cada 5 min fecha todo lote vencido (`agora ≥ fim + 1 dia às
   hora_fechamento`, hora local; padrão segunda 08:00). Servidor fora na hora →
   fecha quando voltar. Lote vencido de config **inativa** também fecha: `ativo`
   só barra perna nova.
4. **Espelho travado depois do fechamento.** Antes do insert, o espelho é
   reconsolidado das pernas e comparado com a composição gravada: quantidade,
   preço ou número de linhas diferente → bloqueio `lote_alterado` (409). Trocar o
   produto (de-para) continua permitido.
5. **Produto ausente no MM recusa o pedido inteiro.** A guarda própria do plano
   saiu depois do PR #87: o `FirebirdExporter` recusa com `PRODUCT_NOT_FOUND` e o
   espelho herda; a reserva do `lote_fiscal` é cancelada.
6. **`linhaLote` = `CORPO_VENDAS.CODIGO` da linha do espelho**; `produtoProdutor`
   = `CODPRODUTO` dessa linha (`FirebirdExportResult.produtos`).
7. **Trocar a janela (semanal ↔ quinzenal) com lote aberto que tem pernas → 409.**

Tomadas na execução:

- **Fecha antes de materializar.** `fechar_lote` grava `fechado` + composição no
  banco compartilhado (com `pernas_esperadas`: perna nova no meio → não fecha,
  tenta no próximo ciclo) e só depois cria a linha `lote-<chave>` no MM. Se a
  escrita no MM falhar, `_reparar_espelhos` refaz a cada ciclo (idempotente; não
  sobrescreve edição feita na revisão). Espelho órfão é impossível por construção.
  Lote `fechado` cujo espelho já tem `fire_codigo` no MM (a conclusão do envio não
  marcou) vira `enviado` no reparo, com `lote_fiscal_ok` = há `erp_lote_perna` da
  chave no banco do MM.
- **`erro` não é fechado.** `lotes_vencidos`, `fechar`, `informar_fator`,
  `anexar_perna` e `_fechar_lote` tratam `erro` como ainda aberto: cada ciclo tenta
  de novo; dado corrigido fecha (e limpa `erro_motivo`), mesmo problema grava o
  mesmo motivo. Perna nova ainda anexa em lote `erro`.
- **Varredura de recuperação.** Antes de fechar (no ciclo, e no "fechar agora" de
  uma pessoa para a config do lote), para cada config ativa, pernas da
  origem enviadas ao Fire pelo Portal (`sent_to_fire`, com `fire_codigo`) que não
  estão em lote nenhum são anexadas pela mesma regra (`anexar_na_janela`). Os
  `itens_fire` vêm de `erp_item_vinculo` (`origem='insert'`); sem vínculo, anexa
  com `[]` e o `lote_fiscal` dessa perna falha de forma visível.
- **Piso da varredura** = `lote_config.ativo_desde` (carimbado quando `ativo` vira
  0→1; desligar não apaga). Envio feito com o lote desligado não entra. Não é o
  máximo com a 1ª anexação: toda perna anexa depois da ativação, e um anexo que
  falhou logo após ligar ficaria abaixo do piso para sempre. Config sem o carimbo
  cai na 1ª anexação real (`MIN(intercompany_lote_item.created_at)`). Carência de 10 min em `sent_to_fire_at` para não disputar com o
  request path.
- **Espelho nunca é perna** (anexo e varredura pulam imports que são espelho) e a
  API recusa config com origem = espelho.
- **SP1 desligado conta como sem SP1.** Base sem config ou com `ativo = 0` no
  espelho → sem reserva, envio segue, lote vai a `enviado` com `lote_fiscal_ok = 0`
  e auditoria `lote_fiscal_falhou` (`sp1_desligado`).

## As tabelas (`app_shared.db`)

- **`lote_config`** — PK `env_origem_slug`: espelho, CNPJ/nome da revenda (CNPJ
  vem do ambiente de origem), `janela`, `dia_inicio`, `hora_fechamento`, `ativo`,
  `ativo_desde`.
- **`lote_fator`** — histórico: `modo_preco`, `fator_preco` (TEXT),
  `vigente_desde`, quem e quando. Vigente = maior `vigente_desde ≤ data`; empate =
  maior id.
- **`intercompany_lote`** — um por janela, `chave_lote` UNIQUE
  (`NAS-2026-S34` / `NAS-2026-Q17`, ≤ 20 caracteres, vai em `PEDIDO_CLIENTE`).
  Status `aberto → (aguardando_fator) → fechado → enviado`, ou `erro` (não
  fechado: o próximo ciclo tenta de novo). Guarda
  fator/modo congelados, `import_id_espelho` (`lote-<chave>`), `composicao_json`,
  `fire_codigo_espelho`, `lote_fiscal_ok`, `erro_motivo`.
- **`intercompany_lote_item`** — as pernas: `fire_codigo_origem`,
  `itens_fire_json` (`[(CORPO_VENDAS.CODIGO, índice em order.items)]`), cliente
  final. UNIQUE `(env_origem_slug, import_id)`: uma perna vive em um lote só.

## Fluxo

1. **Anexo** (`integracao.anexar_perna_de_origem`, no fim do sucesso de
   `_send_one_to_fire` na origem). Só age se o ambiente é origem de config ativa.
   Nunca levanta: falha vira warning e a varredura recupera depois.
2. **Fechamento** (`fechamento.fechar_vencidos` na thread, ou `POST
   /api/lotes/{id}/fechar`). Consolida: itens somados por `(product_code, ean,
   description, delivery_date, preço já convertido)` — preços diferentes **não**
   se fundem. Cabeçalho no nome da revenda, `order_number` = chave, OBS
   `LOTE <chave> | PEDIDOS <SLUG>: <CODIGO>, ...` (CODIGO da perna no Fire de
   origem, crescente). Sem fator → `aguardando_fator`; dado ruim → `erro` com
   motivo. **Não grava no Fire.**
3. **Envio** (botão "Cadastrar no Fire" no MM, `_send_one_to_fire`). Depois do
   portão de preço de sempre (`preco_trava.decidir_envio`; o espelho é
   intercompany, então em `ligado` cai na regra de hoje, `is_blocking`):
   `preparar_envio_espelho` (ambiente certo, status `fechado`, composição intacta,
   reserva do `lote_fiscal`) → insert com `obs=` → `concluir_envio_espelho`
   (registra o `lote_fiscal`, marca `enviado`). Falha antes do Fire aceitar
   (`skipped` ou exceção) cancela a reserva (`abortar_envio_espelho`,
   `guardar_reserva`). O espelho **não** vai ao `/recebimento` do Flow
   (`push_new_order` pulado): no Aponta tudo segue por pedido de origem.
   **"Sem preço no Fire" dispensado:** `PRECO_VENDA` da MM é vazio, então o
   espelho nasce (`fechamento._confirmar_sem_preco`) com `sem_preco_ack_*` de
   autor `lote:<chave>` e o audit `sem_preco_acknowledged` com `origem=lote`.
   Só cobre linha com EAN ou código; divergência de preço e preço ausente no
   pedido seguem bloqueando em `is_blocking` (que não foi tocado).

## Armadilhas

- **Thread de um processo só.** O `_LOCK` serializa thread e botão dentro do
  processo. Dois processos web no mesmo `app_shared.db` disputariam o fechamento
  (o `fechar` condicional evita fechar duas vezes, não evita trabalho dobrado).
- **Lote fechado não reabre.** Perna que chega depois vai para o próximo lote
  aberto, mesmo que a importação seja da semana anterior.
- **Espelho não vai ao `/recebimento`.** O vínculo com o Flow é só o `lote_fiscal`.
- **Snapshot de perna alterado depois do fechamento** trava o espelho
  (`lote_invalido`) sem orientação ao operador na tela.
- **Exceção em `concluir_envio_espelho` antes de `marcar_enviado`** deixa o lote
  `fechado` com o espelho já no Fire: auditado (`lote_fiscal_falhou`), correção manual.
- **`app_shared.db` de dev criado antes de `ativo_desde`** ganha a coluna pela
  `COLUMN_MIGRATIONS` de `schema_shared.py`.

## Testes

```bash
.venv/bin/pytest tests/test_consolidador_janela.py tests/test_consolidador_lote.py \
  tests/test_lotes_repo.py tests/test_lote_anexo.py tests/test_fechar_lotes.py \
  tests/test_lote_espelho_envio.py tests/test_firebird_exporter_lote.py \
  tests/test_web_lotes.py -v
```

- `test_consolidador_janela.py` — janela, chave (ano ISO), instante de fechamento.
- `test_consolidador_lote.py` — soma, preço na chave, fator/modo, OBS, Decimal.
- `test_lotes_repo.py` — vigência do fator, anexo atômico, `fechar` condicional,
  colisão de chave, troca de janela com perna, `ativo_desde`.
- `test_lote_anexo.py` — gancho da perna de origem, saltos de janela.
- `test_fechar_lotes.py` — fechamento, materialização/reparo, varredura e piso.
- `test_lote_espelho_envio.py` — guardas, reserva/cancelamento, `lote_fiscal`,
  sem `/recebimento`, recusa no XLSX.
- `test_firebird_exporter_lote.py` — `obs=` e `produtos`.
- `test_web_lotes.py` — `/api/lotes*` e a tela.

Nada disso toca Firebird de verdade. A prova contra o Fire é
`tools/lote_validar_copia.py`, só na cópia local (`tools/fire_copia_local.sh`):
`--dry-run` mostra o plano sem conectar.
