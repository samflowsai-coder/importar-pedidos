# Log de leituras — design (entrega 1)

Data: 2026-10-05. Branch: `feat/log-leituras` (empilhada em `hotfix/beira-rio-grade-unica`).
Aprovado pelo Samuel em 05/10: **sem tela**; log em arquivo que ele baixa do servidor do
cliente (VPN) e traz para investigar. Revisão de desenho por Fable em 05/10; achados
incorporados abaixo.

## Problema

05/10/2026: 9 planilhas Authentic Feet foram lidas errado (coluna `QUAN.` em vez de
`TOTAL KITS` → `GenericParser` → "CNPJ:" como item, sem cliente). O pedido passou pelo
validador sem bloqueio e só se soube pela reclamação da operadora. Hoje o portal não
deixa rastro consultável de **o que leu, com qual parser, o que extraiu e por que**:
o `pipeline.log` só tem "Processando X" e o resultado; o `imports` só existe depois do
commit; rejeições e exceções antes/dentro do parse não deixam linha nenhuma.

## Objetivo

Só com `logs/leituras.jsonl`, o Samuel decide com assertividade se um problema foi:
(a) leitura errada do parser, (b) formato não reconhecido, (c) cadastro no Fire
(cliente/produto) ou Fire fora do ar, (f) arquivo rejeitado antes do parse, (g) pedido
duplicado — e consegue reproduzir (sha256 + cópia em `recebidos/`).

Fora desta entrega (entrega 2, spec própria): eventos pós-leitura no mesmo arquivo
(commit, export, envio ao Fire), campo de roteamento, `sha_guardado` vs lido.
Bug separado, vai para o BACKLOG: o `NasmarTemplateParser` ignora `DATA DE FATURAMENTO`
(mês de entrega), então pedidos NOVEMBRO/DEZEMBRO da mesma loja saem idênticos.

## Decisões

1. **Observabilidade, nunca portão.** Falha ao gravar a linha vira `logger.warning` e o
   fluxo segue. (O arquivo recebido, que é garantia, continua levantando.)
2. **Pipeline continua stateless.** `process_detailed(file, diag)` preenche um
   `Diagnostico` que o CHAMADOR possui; assim, se der exceção no meio, o que já foi
   coletado (extração, parsers pulados) não se perde. `process(file)` vira wrapper e
   nenhum chamador/teste existente muda.
3. **Um registrador só**, `leituras.registrar(...)`, context manager que escreve a linha
   no `__exit__` — inclusive com exceção — e re-levanta. As rotas ganham ~4 linhas cada.
4. **Uma linha JSON por arquivo lido**, mais uma linha de `etapa="rejeitado"` por arquivo
   barrado antes do parse. Formato cru (`{message}`), não `serialize=True` do loguru.
5. **Sem conteúdo livre da planilha.** CNPJ e razão social entram; texto/rows inteiros,
   e-mail, telefone, IE, `env`/senhas, nunca. Do cabeçalho da tabela entra só UMA linha.
6. **`trace_id` mintado ANTES do parse** nas rotas de preview e guardado em
   `PreviewEntry.trace_id`; o commit reusa (`with_trace_id(entry.trace_id)`). Só assim a
   linha de leitura casa com `imports.trace_id`/`pipeline.log`/`audit_log`.

## Arquivo e sink

- `<raiz do app>/logs/leituras.jsonl`, raiz ancorada em `Path(__file__)` (não no CWD).
  Rotação 10 MB, retenção 30 dias, `enqueue=True`, `encoding="utf-8"`.
- Sink próprio com `filter` por `extra["canal"] == "leituras"`. Os sinks existentes
  (stderr e `pipeline.log`) passam a **excluir** esse canal.
- No startup, `logger.info` com o caminho absoluto do arquivo.

## Esquema (v1)

Uma linha, chaves sempre presentes (valor `null` quando não se aplica):

```json
{"v":1,"ts":"2026-10-05T11:24:03.117-03:00",
 "portal":{"versao":"20261005-1136","git_commit":"8ee8059"},
 "trace_id":"…","origem":"upload|preview-pending|lote|reimport|process-legado|worker",
 "ambiente":"mm|null","usuario":"email|null",
 "arquivo":{"nome":"…","ext":".xlsx","tamanho":69481,"sha256":"…","copia":"…|null",
            "sha_ja_visto":{"import_id":"…","quando":"…"}},
 "etapa":"rejeitado|parseado|erro",
 "rejeicao":{"motivo":"extensao|tamanho|nao_encontrado|guarda_falhou","detalhe":"…"},
 "extracao":{"fmt":"xlsx","linhas":13,"abas":[{"nome":"NOV","linhas":13}],
             "cabecalho_candidato":{"linha":9,"celulas":["IMAGEM","REF.","…","QUAN.","TOTAL R$"]}},
 "parser":{"usado":"NasmarTemplateParser","ms":41,
           "pulados":[{"nome":"KoloshParser","motivo":"can_parse"}],
           "llm":{"tentado":false,"motivo":"nao_necessario"}},
 "pedido":{"numero":"AF072","numero_gerado":false,"emissao":"02/10/2026",
           "cliente":"…","cnpj":"…","supplier_cnpj":null,"itens":4,"qtd_total":170,
           "valor_total":2033.2,"itens_sem_preco":0,"itens_sem_codigo":0,
           "pedido_ja_importado":{"import_id":"…","sha256":"…","quando":"…"}},
 "validacao":{"ok":true,"avisos":[]},
 "suspeito":{"sinais":["parser_generico","sem_cnpj"],"forte":true},
 "fire_check":{"available":true,"reason":null,"cliente":{"match":true,"fire_id":123,"razao":"…"},
               "itens":{"total":4,"match":4,"missing":0},"nao_encontrados":["…"],
               "preco":{"match":4,"mismatch":0,"sem_preco_fire":0,"sem_preco_pedido":0},
               "troca_no_fire":0},
 "resultado":"preview|importado|retido|pendencia_throttle|skip_duplicate|422|400|404|413|erro",
 "excecao":{"tipo":"…","msg":"…","onde":"arquivo.py:linha"},
 "ms_total":187}
```

Regras de serialização: `json.dumps(default=str, ensure_ascii=False)`; listas com no
máximo 20 elementos; linha com no máximo 16 KB (se estourar, derruba `nao_encontrados`,
`avisos` e `celulas` e marca `"truncado":true`).

## Componentes

### `app/observability/leituras.py` (novo)

- `Diagnostico` (dataclass): `extracao`, `parser_usado`, `parsers_pulados`, `llm`,
  `parser_ms`, `validacao_ok`, `avisos`, `fmt`.
- `sinais_suspeitos(order, diag, cnpj_ambiente=None) -> list[str]` (pura). Sinais:
  `parser_generico`, `llm_usado`, `sem_cnpj` (cabeçalho E todos os itens sem CNPJ de
  entrega), `sem_cliente`, `numero_gerado`, `numero_parece_rotulo`
  (`^(OBSERVA|CNPJ|TOTAL|DATA)`, case-insensitive), `item_parece_rotulo` (descrição
  termina em `:` ou tem menos de 4 caracteres), `qtd_fracionada`, `qtd_max_alta`
  (>10000), `preco_ausente_em_todos`, `codigos_repetidos` (mesma tupla código +
  descrição + loja de entrega + data; código sozinho repetido é normal com várias
  lojas/datas), `validacao_reprovada`, `cliente_igual_ao_ambiente` (CNPJ do cliente ==
  CNPJ do ambiente DA LINHA; sem ambiente, sem sinal). Removidos por falso positivo:
  `qtd_todas_iguais` (kits legítimos) e `valor_total_zero_com_qtd` (redundante com
  `preco_ausente_em_todos`).
- `suspeito.forte` = `true` se algum de `parser_generico`, `llm_usado`,
  `item_parece_rotulo`, `numero_parece_rotulo`, `validacao_reprovada`,
  `cliente_igual_ao_ambiente` está nos sinais; os demais são informativos. A linha base
  (`rejeitado`) leva `{"sinais":[],"forte":false}`.
- `resumo_fire_check(check) -> dict` (pura): `available`, `reason`, cliente, contagens,
  `nao_encontrados` (≤20 códigos), `preco`, `troca_no_fire`. Nunca copia `env`.
- `cabecalho_candidato(rows) -> dict|None` (pura): a linha de `rows[:30]` com MAIS
  rótulos de texto sem dígito nem `@`, de 3 a 40 caracteres (≥3 rótulos; empate: a primeira). A primeira
  linha com texto é o bloco de cadastro, não o cabeçalho. Só essa linha.
- `versao_portal() -> dict`: lê `applied_update.json` (cache de módulo invalidado pela mtime);
  falha → `{"versao":"desconhecida","git_commit":null}`.
- `sha_ja_visto(sha)` / `pedido_ja_importado(numero, cnpj, sha)`: consultas ao `imports`
  do ambiente ativo; qualquer erro → `None`. `pedido_ja_importado` olha 90 dias e exige
  sha diferente.
- `escrever(linha: dict) -> None`: nunca levanta.
- `registrar(origem, nome, raw=None, usuario=None, ambiente=None, copia=None)` →
  context manager com `.diag`, `.processar(loaded) -> Order|None`, `.falha(exc)` (exceção
  que o chamador engole), `.check = …`, o sha e o `sha_ja_visto` saem do `__enter__`,
  `.resultado = …`. No `__exit__`: monta e escreve a linha; mapeia exceção com
  `status_code` (HTTPException, por duck typing) para `resultado=str(status_code)` e as
  demais para `etapa="erro"` + `excecao`; re-levanta.
- `rejeitado(origem, nome, motivo, detalhe=None, tamanho=None, usuario=None, ambiente=None)`.

### `app/pipeline.py`

`process_detailed(file, diag=None) -> Order|None` com o corpo atual de `process`,
preenchendo `diag`: formato, extração (`linhas`, `abas`, `cabecalho_candidato`), parsers
pulados com motivo (`can_parse` = `can_parse()` falso; `none` = `parse()` devolveu None),
parser usado, `llm.tentado`, `parser_ms`, resultado de `validate_detail`.
`process(file)` = `process_detailed(file)`.

### `app/extractors/xls_extractor.py`

Passa a incluir `sheets: [{"name", "rows"}]` no dict de saída (campo novo, aditivo).
O PDF não muda (`abas=[]`).

### `app/validators/order_validator.py`

`validate_detail(order) -> list[str]` (as mensagens atuais); `validate` vira wrapper com o
mesmo comportamento e os mesmos logs.

### `app/utils/logger.py`

Sink `leituras` e exclusão do canal nos dois sinks existentes.

### Rotas (`app/web/server.py`) e worker

- `/api/preview`, `/api/preview-pending`: `registrar` + `trace_id` no `PreviewEntry`;
  commit usa `with_trace_id(entry.trace_id)`.
- `_process_file` (lote `/api/import` e `/api/reimport`): `registrar(origem=…)`; novo
  kwarg `origem`.
- `/api/process` (legado): `registrar`.
- Guards de extensão/tamanho/não encontrado/guarda: `rejeitado(...)`.
- `scan_environments._process_file`: `registrar(origem="worker")`; os `return` de
  `skip_duplicate` e pendência viram linha de `rejeitado`/`resultado`
  (`skip_duplicate`, `pendencia_throttle`). Extensão ignorada no scan: 1 linha por
  arquivo (cache em memória do nome); `pendencia_throttle`: 1 linha por sha (set de
  módulo), para não repetir a cada 30 s. O worker marca `importado` DEPOIS do insert.
- CLI (`main.py`) fica de fora.

## Erros e limites

- `logs/` sem permissão de escrita → `_configurar_sink_leituras` devolve None com warning
  e o resto funciona (o import de `logger.py` não levanta).
- `applied_update.json` ilegível → versão "desconhecida".
- Banco do ambiente indisponível → `sha_ja_visto`/`pedido_ja_importado` = `null`.
- Fire fora do ar: o `fire_check.available=false` com `reason` (`FB_DATABASE_NOT_SET`,
  `CHECK_FAILED: <Tipo>`) distingue "Fire offline" de "cliente não cadastrado".

## Testes

- `tests/test_leituras_sinais.py`: um caso por sinal; a planilha `QUAN.` lida pelo
  `GenericParser` dispara ≥5 sinais; AF093 (modelo antigo) dispara nenhum; todos os
  arquivos de `samples/` têm `forte=false`, exceto o modelo em branco do Kings.
- `tests/test_leituras_registrar.py`: linha em sucesso, em `HTTPException`, em exceção
  genérica (re-levanta, linha gravada com `excecao`), em rejeição; falha de escrita não
  quebra; serialização de `datetime`/`float`; truncamento de 16 KB; sem `env` na linha.
- `tests/test_pipeline_detailed.py`: `process` ≡ `process_detailed(...).` ordem;
  `pulados`/`usado`/`cabecalho_candidato` corretos para AF072 (`QUAN.`) e AF093.
- `tests/test_validator_detail.py`: `validate` mantém comportamento e logs.
- `tests/test_web_leituras.py`: preview grava 1 linha com o `trace_id`; commit do mesmo
  preview usa o mesmo `trace_id` do `imports`; 422 grava linha; extensão inválida grava
  `rejeitado`.
- `tests/test_scan_environments_leituras.py`: worker grava `origem="worker"`,
  `skip_duplicate`.
- Suíte completa antes do commit; lint em `app/` e `tests/`.
