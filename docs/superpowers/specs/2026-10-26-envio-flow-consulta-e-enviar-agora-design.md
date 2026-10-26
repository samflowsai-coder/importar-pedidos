# Envio ao Flow: consulta por pedido e "Enviar agora"

**Data:** 2026-10-26 · **Domínio:** `erp_eventos` (SP1) + `web` · **Status:** rascunho para aprovação
**Rascunho da tela:** aprovado pelo Samuel em 26/10 (mockup no Chrome, 4 estados).

## Por que

No teste da virada (26/10, pedido 5131 `TESTE-VIRADA-1026` gravado direto na `.7` e
cancelado em seguida) não houve como responder "o Flow já tem esse pedido?":

- a fila do SP1 só sai nas janelas 8h/11h/14h/17h (seg-sáb), então o pedido gravado às
  18h34 só chegaria ao Flow às 8h do dia seguinte;
- o Flow está em modo sombra para o SP1: o pedido não aparece em `/pedidos`;
- o portal só mostra um resumo agregado da fila (em `/admin/ambientes/<id>`), sem
  nada por pedido.

Com a virada para `EXPORT_MODE=db`, todo pedido passa a nascer pelo portal no Fire e
chega ao Flow só pelo SP1. Saber onde um pedido está nesse caminho, e poder empurrá-lo
quando preciso, passa a ser operação do dia a dia, não depuração.

## Objetivos

1. **Consulta por pedido:** dado o número do pedido do cliente ou o código do Fire,
   mostrar cada evento SP1 daquele pedido, a situação (na fila, enviado, aceito,
   duplicado, recusado), quando saiu, por qual caminho (janela, foto ou manual, e quem)
   e o que o Flow respondeu.
2. **Enviar agora:** um admin manda a fila de uma base para o Flow fora das janelas,
   com as mesmas regras do envio da janela (ordem, lotes, recuo, bloqueio).
3. **Últimos envios:** lista dos lotes recentes com origem, quantidade e resultado.

## Não objetivos

- Reenviar evento recusado. A regra atual continua: recusado é terminal na fila e a
  próxima mudança no Fire gera evento novo.
- Mudar as janelas, o sinal de vida ou a foto noturna.
- Qualquer mudança no Flow. Conferido no pcp-app (commit `11314141`, 25/10): o Flow
  aceita lote a qualquer hora (`/api/portal-pedidos/eventos` não olha janela), alerta
  só por **ausência** de lote (`api/cron/saude-eventos-erp`) e limita o ERP a
  30 lotes/min (`rate-limit-portal.ts`).
- Acesso de operadora. A tela é de admin (é ela que dispara envio).
- Ler o Fire na consulta. A consulta mostra o que o portal já leu (com a hora da
  leitura); quem precisa do agora usa "Enviar agora", que lê antes de mandar.

## Anti-objetivos (falha mesmo que "funcione")

- "Enviar agora" passar por fora da drenagem (outra ordem, outro corte de lote, ignorar
  bloqueio ou `proxima_tentativa_em`). Tem que ser **o mesmo** `drenagem.drenar`,
  chamado pela mesma thread.
- Dois envios da mesma base ao mesmo tempo (o botão e a thread).
- Request HTTP pendurado: `fdb` não tem timeout; a leitura do Fire não pode rodar dentro
  do request.
- Mostrar "No Flow" para algo que o Flow não confirmou: só `aceito`/`duplicado` com
  resposta registrada contam.

## Como funciona

```
                 ┌──────── thread erp-eventos (a cada 30 s) ────────┐
Fire ──vigia──▶ erp_eventos_saida ──drenar (janela)──▶ Flow          │
                 ▲            │                                      │
                 │            └──drenar (pedido manual) ──▶ Flow     │
  Enviar agora ──┴─ grava o PEDIDO; a própria thread o atende no próximo tick
                    (≤ 30 s): drenar → vigia → drenar, sob a trava de sempre
                    cada lote ──▶ erp_envio_lote (origem, quem, contagens)
Consulta ◀── erp_pedido_estado + erp_eventos_saida + erp_envio_lote + imports
```

### 1. Consulta por pedido

`GET /api/admin/environments/{env_id}/erp-base/pedidos?q=<texto>` (admin; base do
ambiente, padrão `mm`/`nasmar` conforme `erp_bases`).

- `q` só dígitos → procura `erp_pedido_estado.erp_codigo = q` **e** pedido do cliente
  igual a `q` (UY44549110 é dígito também). Caso contrário, só pelo pedido do cliente
  (`json_extract(estado_json, '$.pedido_cliente')`, comparação com `TRIM` e sem caixa).
- `PEDIDO_CLIENTE` não é único no Fire (memória NBA/xPed): mais de um resultado vira
  uma lista para escolher (código do Fire, cliente, data, situação); um resultado abre
  direto.
- Resposta por pedido:
  - cabeçalho: `erp_codigo`, `pedido_cliente`, cliente (código + CNPJ; nome quando o
    portal o tem no `imports` daquele pedido), nº de itens, `status`, `visto_em` e a
    hora da leitura que vale para ele: pedido **aberto** é relido a cada vigia, então
    mostra `erp_envio_estado.ultima_vigia_em` da base; pedido **encerrado** (faturado,
    cancelado, excluído) não é mais relido (`vigia.py:66-71`), então mostra
    "última mudança vista em `erp_pedido_estado.atualizado_em`; pedido encerrado não é
    mais lido". (`atualizado_em` só anda quando algo muda, `vigia.py:186-191`: não é a
    hora da última leitura.)
  - origem: "Portal" se existe `imports.fire_codigo = erp_codigo` no banco do ambiente,
    senão "Digitado no Fire";
  - eventos de `erp_eventos_saida` daquele `erp_codigo`, em `ordem`: tipo em português
    (tabela abaixo), detalhe curto tirado do `payload_json`, `observado_em`, `situacao`,
    `enviado_em`, origem do lote (join `erp_envio_lote` por `lote_id`), `respondido_em`,
    `motivo`;
  - resumo em uma frase (ver "Frases").
- Nada encontrado → mensagem do mockup: o portal só lê pedidos abertos; pode ter sido
  faturado/cancelado antes da primeira leitura, ter menos de 15 min, ou ser de outra
  empresa; mostra a hora da última leitura.
- Retenção: eventos `aceito`/`duplicado` com mais de 30 dias saem da fila
  (`runner._reter`). A consulta mostra "eventos com mais de 30 dias já foram limpos"
  quando o estado existe e a fila não tem nada dele.

Tipos (rótulo da tela): `pedido_visto` Pedido novo · `pedido_excluido` Pedido excluído ·
`status_alterado` Status mudou · `item_incluido` Item incluído · `item_removido` Item
removido · `quantidade_alterada` Quantidade mudou · `preco_alterado` Preço mudou ·
`data_entrega_alterada` Data de entrega mudou · `faturamento_detectado` Faturado ·
`lote_fiscal` Lote fiscal (lista conferida em `app/erp_eventos/contrato.py`, 26/10). Tipo novo sem
rótulo aparece com o nome cru (nunca quebra a tela).

Frases do resumo:
- tudo `aceito`/`duplicado` → "O Flow aceitou todos os eventos deste pedido. Último aceite: <data>, <origem>."
- algum `pendente` ou `enviado` sem resposta → "O Flow ainda não recebeu N evento(s). Saem na janela de <próxima> ou agora pelo Enviar agora."
- algum `rejeitado` → "O Flow recusou N evento(s). Recusa não é reenviada: corrija a causa no Fire; a próxima mudança gera um evento novo."
- base bloqueada → a frase diz que o envio está parado e por quê (mesmos textos de `BLOQUEIOS` da tela de ambiente).

### 2. Enviar agora

`POST /api/admin/environments/{env_id}/erp-base/enviar-agora` (admin). Proteção igual
às demais rotas de admin: sessão + cookie `SameSite=Strict` (`auth.py`), sem token
CSRF (o portal não tem; não é escopo desta mudança).

**O request não lê o Fire nem fala com o Flow: só registra o pedido.** Quem atende é a
própria thread `erp-eventos`, no próximo tick (até 30 s), sob a trava que ela já usa.
Assim não existe segundo caminho de envio nem segunda disputa de trava, e um Fire
travado (`fdb` sem timeout) prende só o que já prenderia hoje: a thread, nunca o
request.

1. Recusa com 409 e mensagem se: SP1 desligado (`PORTAL_ERP_VIGIA=0`) ou base inativa;
   base bloqueada (`bloqueio`); recuo ativo (`proxima_tentativa_em` no futuro, diz até
   quando); já existe pedido manual pendente ou atendido há menos de 60 s.
2. Aceito: guarda o pedido em memória do processo (`runner.pedir_envio_manual(cfg,
   quem) → envio_id`; `ui.py` é processo único, `erp_eventos.md` §runner) e responde
   202 com o `envio_id`.
3. No tick, `processar_base` vê o pedido e, nesta ordem:
   1. `drenar(origem="manual")` do que já está na fila (mesma razão de
      `runner.py:144-151`: mandar antes de ler, para um Fire travado não segurar o que
      já existe);
   2. vigia, **forçada** mesmo sem os 15 min, mas respeitando a pausa de 5 min de
      `_FALHA_ERP` (Fire que acabou de falhar não é relido; o resultado diz "Fire fora,
      mandei só o que estava na fila");
   3. `drenar(origem="manual")` de novo, para levar o que a vigia acabou de enfileirar.
   O resultado (vigia ok/pulada/falhou, lotes, aceitos, recusados, `parou_por`) fica em
   memória por 1 h, chaveado por `envio_id`, e vai para o log.
4. A tela acompanha por `GET .../erp-base/envio-manual/{envio_id}` (polling de 2 s até
   `terminado`) e então recarrega os números, os últimos envios e a consulta aberta.

**Fila vazia:** o clique usa o `drenar` de sempre. Como toda vigia liga `sinal_pendente`
(`erp_estado_repo.py:209`), sair sem eventos normalmente manda o lote vazio de sinal de
vida (`drenagem.py:66`). Isso é aceito: é um 202 legítimo, aparece em "Últimos envios"
como "sinal de vida" e a tela diz "nada novo para enviar; o Flow confirmou que o portal
está vivo". Nenhum parâmetro novo no `drenar` além de `origem`/`quem`/`envio_id`.

**Decisão minha (não perguntada): "Enviar agora" lê o Fire antes de mandar.** O uso que
motivou é "acabei de gravar e quero ver no Flow"; mandar só a fila deixaria esse pedido
de fora até a próxima vigia (até 15 min). O diálogo de confirmação diz isso:
"Lê o Fire agora e manda tudo o que estiver na fila (N eventos já esperando)".

O sinal de vida não muda: lote manual com 202 conta como `ultimo_202_em`, então uma
janela que já teve envio manual não precisa do lote vazio (comportamento de
`_garantir_lote_na_janela`, que compara com o início da janela).

### 3. Registro de lotes: `erp_envio_lote` (schema novo, pede ok)

Hoje o lote só existe como `lote_id` nos eventos; lote vazio (sinal de vida) e a origem
não ficam em lugar nenhum. Tabela nova no banco do ambiente, aditiva
(`CREATE TABLE IF NOT EXISTS` em `schema_env.py`, como as demais `erp_*`):

```sql
CREATE TABLE IF NOT EXISTS erp_envio_lote (
    lote_id      TEXT PRIMARY KEY,
    base         TEXT NOT NULL,
    origem       TEXT NOT NULL,          -- 'janela' | 'foto' | 'manual'
    quem         TEXT,                   -- e-mail do admin quando manual
    envio_id     TEXT,                   -- agrupa os lotes de um "Enviar agora"
    enviado_em   TEXT NOT NULL,
    eventos      INTEGER NOT NULL,
    http         INTEGER,
    aceitos      INTEGER NOT NULL DEFAULT 0,
    duplicados   INTEGER NOT NULL DEFAULT 0,
    rejeitados   INTEGER NOT NULL DEFAULT 0,
    respondido_em TEXT
);
CREATE INDEX IF NOT EXISTS ix_erp_envio_lote_base_em ON erp_envio_lote (base, enviado_em);
```

- `drenagem.drenar` ganha `origem`, `quem` e `envio_id` (padrão `janela`; a drenagem
  antes da foto em `_enviar_foto` passa `foto`), grava a linha antes do POST e completa
  com o resultado depois. Uma linha por lote de eventos, inclusive o vazio.
- As **páginas da foto** (`foto.enviar`) não entram aqui: a foto já tem registro e
  situação próprios (`erp_foto`/`erp_foto_pagina`). "Últimos envios" mostra uma linha
  extra "Foto do dia: <situação> (<n> páginas)" lida de `erp_foto_repo.foto_do_dia`.
- Retenção: mesma rotina diária de `_reter`, 30 dias.
- Os lotes de um "Enviar agora" levam o mesmo `envio_id` (o desfecho do clique fica em
  memória, ver §2).

### 4. A tela

`/admin/envio-flow` (estática, mesmo shell e `tokens.css`), entrada no menu de
administração. Fiel ao mockup aprovado:

- faixa de situação: última leitura do Fire, na fila, próxima saída (de
  `janelas_envio`), último lote aceito, recusados em 30 dias, botão **Enviar agora**
  (desabilitado com o motivo quando a rota recusaria: bloqueada, recuo, SP1 desligado);
- "Consultar um pedido" com o resultado logo abaixo;
- "Últimos envios": 20 lotes mais recentes de `erp_envio_lote` (quando, origem, quem,
  eventos, resultado), lote vazio marcado "sinal de vida";
- diálogo de confirmação do Enviar agora com a contagem real.

A tela de edição do ambiente mantém o resumo de hoje e ganha um link "ver envio por
pedido".

## Restrições

- Somente leitura no Fire (a vigia já é READ ONLY + SNAPSHOT).
- Nada de rede no request path além do SQLite local; leitura do Fire e POST ao Flow só na
  thread.
- Multi-ambiente: tudo filtra pela base/ambiente do `env_id` da rota; o usuário precisa
  ser admin.
- Sem dependência nova.

## Testes (mínimo)

- consulta: código do Fire, pedido do cliente com dígitos, pedido do cliente repetido
  (lista), inexistente, estado sem eventos (retenção), tipo desconhecido;
- frases do resumo para cada combinação de situação;
- enviar agora: cada recusa 409 (desligado, bloqueada, recuo, pedido pendente, 60 s);
  o request não abre conexão com o Fire nem com o Flow; o tick atende o pedido na ordem
  drenar → vigia forçada → drenar, grava `erp_envio_lote` com `origem='manual'`, `quem`
  e `envio_id`; vigia falhando ou em pausa de `_FALHA_ERP` ainda drena e o resultado
  diz isso; fila vazia manda o lote de sinal de vida;
- drenar: janela grava `origem='janela'`, antes da foto grava `foto`, lote vazio grava
  linha com `eventos=0`;
- o pedido manual é atendido uma vez só (dois ticks seguidos não drenam duas vezes);
- rota não-admin → 403; outro ambiente → 404.

## Perguntas para o Samuel

1. **Schema novo `erp_envio_lote`** (aditivo, só no SQLite do portal). Ok?

## Decisões minhas (confira ao ler)

- "Enviar agora" lê o Fire antes de mandar (drenar → vigia → drenar).
- O clique não roda nada no request: vira pedido atendido pela thread de sempre em até
  30 s (sem segundo caminho de envio; Fire travado não prende o request).
- Fila vazia no clique manda o lote de sinal de vida, como o `drenar` já faz.
- Páginas da foto fora de `erp_envio_lote`; a foto aparece numa linha própria.
- Consulta não lê o Fire: mostra a última leitura com a hora.
- Intervalo mínimo de 60 s entre envios manuais da mesma base (o Flow aceita 30
  lotes/min; o intervalo evita clique repetido, não o limite).
- Origem "Portal × Digitado no Fire" pelo `imports.fire_codigo`, sem ler
  `ULT_INS_USER` (o SELECT do SP1 não traz e não vale mudar o contrato por isso).
- Tela própria `/admin/envio-flow`, só admin.
