# Log de leituras — plano de implementação (entrega 1)

Spec: `docs/superpowers/specs/2026-10-05-log-de-leituras-design.md`. Branch `feat/log-leituras`.
Regras: TDD (teste falha → código → passa), diff mínimo, `.venv/bin/pytest <arquivo>` direcionado
por tarefa, `ruff check` no que tocou, **sem commit** dentro das tarefas (o commit é do loop
principal depois da revisão). Reusar helpers existentes; não reformatar código alheio.

Roteamento (skill `roteando-modelo-e-esforco`): implementação = agente `executor`
(sonnet · medium); revisão da branch = `revisor` (opus · high), agente novo; spec/plano,
integração e commit = loop principal.

## T1 — Pipeline detalhado e validador com detalhe  (executor · sonnet · medium)

Arquivos: `app/observability/leituras.py` (só o dataclass `Diagnostico` por ora),
`app/pipeline.py`, `app/validators/order_validator.py`, `app/extractors/xls_extractor.py`,
testes `tests/test_pipeline_detailed.py`, `tests/test_validator_detail.py`.

1. `Diagnostico` (dataclass, todos com default): `fmt: str|None`, `extracao: dict`,
   `parser_usado: str|None`, `parsers_pulados: list[dict]`, `llm: dict`, `parser_ms: int`,
   `validacao_ok: bool|None`, `avisos: list[str]`.
2. `OrderValidator.validate_detail(order) -> list[str]` devolve as mensagens atuais;
   `validate` chama `validate_detail`, loga igual e devolve `not errors`.
3. `XLSExtractor` ganha `"sheets": [{"name": str, "rows": int}]` (aditivo; `.xlsx` e `.xls`).
4. `pipeline.process_detailed(file, diag: Diagnostico | None = None) -> Order | None` = corpo
   atual de `process` preenchendo `diag`: `fmt`; `extracao` = `{"linhas": len(rows), "abas":
   sheets, "cabecalho_candidato": cabecalho_candidato(rows)}` (função pura em
   `leituras.py`: 1ª linha de `rows[:30]` com ≥3 células `str` não vazias sem dígito →
   `{"linha": idx, "celulas": [str, ...≤20]}`, senão `None`); por parser: se `can_parse`
   falso → `pulados += {"nome", "motivo": "can_parse"}`; se `parse()` devolve `None` →
   `{"motivo": "none"}`; `parser_usado`; `llm = {"tentado": bool, "motivo":
   "nao_necessario|ok|sem_resultado"}`; `parser_ms`; `avisos = validator.validate_detail(order)`
   e `validacao_ok = not avisos`. `process(file)` devolve `process_detailed(file)`.
   O retorno do validador deixa de ser descartado só no sentido de ficar em `diag`; **não
   bloqueia nada** (comportamento idêntico).

Testes: `process` e `process_detailed` devolvem a mesma ordem; AF072 `QUAN.` (sample
`Pedido Authentic Feet AF072 QUAN.xlsx`) → `parser_usado == "NasmarTemplateParser"` e o
`cabecalho_candidato.celulas` contém `"QUAN."`; AF093 idem com `"TOTAL KITS"`; parser
pulado por `can_parse` aparece; um arquivo que cai no Generic (monte um xlsx mínimo em
`tmp_path` com openpyxl) → `parser_usado == "GenericParser"`; `validate` mantém retorno
bool e logs (test existente do validador continua verde); `.xls` traz `sheets`.

## T2a — Funções puras do registro  (executor · sonnet · medium)

Arquivo: `app/observability/leituras.py` + `tests/test_leituras_sinais.py`.

`sinais_suspeitos(order, diag, cnpjs_ambiente: set[str] | None = None) -> list[str]`,
`resumo_fire_check(check: dict | None) -> dict | None`, `montar_pedido(order) -> dict | None`
(campos do esquema: `numero`, `numero_gerado`, `emissao`, `cliente`, `cnpj`,
`supplier_cnpj`, `itens`, `qtd_total`, `valor_total` (soma de `total_price`, ou
qtd×preço quando ausente), `itens_sem_preco`, `itens_sem_codigo`), `limitar(linha: dict) ->
dict` (teto de 16 KB e listas ≤20; marca `"truncado": True`), `serializar(linha) -> str`.
Sinais e definições exatos na spec. Testes: um caso por sinal, o caso `QUAN.`→Generic
(monte `Order` com item "CNPJ:" e qtd 100/1, sem cnpj) dispara ≥5 sinais, AF093 e AF072
(pós-fix) disparam nenhum; `resumo_fire_check` com `available=False/reason`, sem vazar
chaves fora do esquema; `limitar` e `serializar` com `datetime`.

## T2b — Sink, registrador e consultas de duplicidade  (executor · sonnet · medium)

Arquivos: `app/observability/leituras.py`, `app/utils/logger.py`,
`tests/test_leituras_registrar.py`.

1. `logger.py`: `LOG_DIR = Path(__file__).resolve().parents[2] / "logs"`; `mkdir`; sink
   `LOG_DIR/"leituras.jsonl"` (`format="{message}"`, `rotation="10 MB"`,
   `retention="30 days"`, `enqueue=True`, `encoding="utf-8"`, `level="INFO"`,
   `filter=lambda r: r["extra"].get("canal") == "leituras"`); os sinks de stderr e
   `pipeline.log` passam a rejeitar `canal == "leituras"`. Não mudar mais nada ali (o
   `Path("logs")` relativo atual do `pipeline.log` fica; ancorar só o novo). `logger.info`
   com o caminho absoluto no import.
2. `escrever(linha)` → `logger.bind(canal="leituras").info(serializar(limitar(linha)))` em
   `try/except Exception` → `logger.warning`.
3. `versao_portal()` com cache de módulo; lê `app.web.routes_update._applied_info()`
   (import local) e devolve `{"versao","git_commit"}`.
4. `sha_ja_visto(sha)` / `pedido_ja_importado(numero, cnpj, sha)`: `with
   db.connect()` do ambiente ativo (padrão de `app/persistence/repo.py`) e `SELECT id,
   imported_at FROM imports WHERE file_sha256 = ?` / `WHERE order_number = ? AND
   customer_cnpj = ? AND file_sha256 IS NOT ? AND imported_at >= date('now','-90 day')`;
   qualquer exceção → `None`. Sem contexto de ambiente → `None`.
5. `registrar(...)` conforme a spec (classe com `__enter__/__exit__`), `.processar(loaded)`
   usa `pipeline.process_detailed(loaded, self.diag)` e guarda `self.order`; `.check`,
   `.resultado`; `__exit__` monta e escreve a linha e **retorna `False`** (re-levanta).
   Mapeia `status_code` por duck typing; `ms_total` por `perf_counter`; `trace_id` =
   `current_trace_id()`.
6. `rejeitado(...)`.

Testes: linha de sucesso (lê o arquivo com um sink temporário: use `logger.add` num
`tmp_path` com o mesmo filtro, ou monkeypatch de `escrever` capturando o dict — preferir
capturar o dict e testar `escrever` à parte com arquivo real); `HTTPException(422)` →
`resultado="422"` e re-levanta; `ValueError` → `etapa="erro"`, `excecao.tipo`, re-levanta;
`escrever` com disco falhando (monkeypatch) não levanta; `rejeitado`; linha sem `env`.

## T3 — Preview e `trace_id` fim a fim  (executor · sonnet · medium)

Arquivos: `app/web/preview_cache.py` (`PreviewEntry.trace_id: str | None = None` e `put(...,
trace_id=None)`), `app/web/server.py` (`/api/preview`, `/api/preview-pending`,
`commit_preview`), `tests/test_web_leituras.py`.

- Em ambas as rotas: `with with_trace_id() as trace_id, leituras.registrar(origem=..., nome=...,
  raw=raw, usuario=_user.email, ambiente=<slug do request_env ou None>, copia=recebido.path)
  as L:` em volta de `process` + `check_order`; `order = L.processar(loaded)`;
  `L.check = check`; `L.resultado = "preview"`; `put(..., trace_id=trace_id)`. Guards
  (extensão/tamanho/404) chamam `leituras.rejeitado(...)` antes do `raise`. `finally` do
  tmp_path preservado.
- `commit_preview`: trocar `with with_trace_id() as trace_id:` por
  `with with_trace_id(entry.trace_id) as trace_id:` (gera um novo se `None`).
- Testes (TestClient, padrão de `tests/test_web_server.py`): upload do sample AF072 →
  1 linha com `origem="upload"`, `parser.usado="NasmarTemplateParser"` e `trace_id` igual
  ao do `imports` após o commit; arquivo `.txt` → linha `rejeitado`/`extensao`; arquivo
  que dá 422 → linha com `resultado="422"`.

## T4 — Lote, reimport, legado  (executor · sonnet · medium)

Arquivos: `app/web/server.py` (`_process_file`, `/api/import`, `/api/reimport`,
`/api/process`), `tests/test_web_leituras.py` (acrescentar).

`_process_file(src, output_path, env=None, origem="lote")`: `registrar` em volta do
`process`; `/api/reimport` passa `origem="reimport"`. `/api/process` (legado):
`registrar(origem="process-legado")`. Guards de extensão/tamanho/404 → `rejeitado`.
Nunca mudar o contrato de resposta nem o tratamento de erro existente. Testes: um por
origem (`lote`, `reimport`, `process-legado`) e um de rejeição por extensão no lote.

## T5 — Worker  (executor · sonnet · medium)

Arquivos: `app/worker/jobs/scan_environments.py`, `tests/test_scan_environments_leituras.py`.

`_process_file(env, p)`: `skip_duplicate` e `pendencia_throttle` viram linha
(`leituras.rejeitado`-estilo com `resultado`), o parse passa por `registrar(origem="worker",
ambiente=env["slug"], raw=raw)` dentro do `with_trace_id(trace_id)` que já existe; extensão
ignorada no scan → 1 linha por nome (set em memória do módulo). Não mudar a ordem dos
gates nem o roteamento. Testes: padrão de `tests/test_scan_environments_roteamento.py`.

## T6 — Docs, BACKLOG, CHANGELOG  (loop principal)

`docs/ai/modules/observability.md` (seção "Log de leituras": caminho, esquema, como ler:
`jq`/`grep` por sha e trace), `docs/ai/00-index.md` (linha de tarefa + teste),
`docs/BACKLOG.md` (item novo: `DATA DE FATURAMENTO` ignorada no template de kits; item:
pedido sem CNPJ não é barrado; entrega 2 do log), `CHANGELOG.md` (nota para quem opera:
nenhuma mudança na tela; o suporte passa a ter o arquivo `leituras.jsonl`).

## T7 — Verificação e revisão  (loop principal + `revisor` · opus · high)

Suíte completa + `ruff check app/ tests/`; revisão independente da branch inteira (agente
novo, não o que escreveu), foco: nenhum caminho do upload pode falhar por causa do log,
vazamento de dado sensível, `trace_id`, concorrência/Windows, regressão de comportamento
das rotas. Achados corrigidos por `executor` (sonnet · medium). Só então commit.
