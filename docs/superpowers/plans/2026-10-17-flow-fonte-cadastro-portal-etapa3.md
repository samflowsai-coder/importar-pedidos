# Flow fonte do cadastro — Portal, Etapa 3 (fila real e gravação no Fire) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O Portal consome a fila `cadastros.v1` do Flow nos três modos (`simular`, `criar`, `criar_e_atualizar`), grava par, kit e pack no Fire com livro idempotente e devolve o resultado ao Flow seguindo as regras de ouro do contrato P4a; a ida passa a levar marcas e nota fiscal; as marcas do Flow são saneadas no Fire por ferramenta com dry-run e backup.

**Architecture:** A volta continua em `cadastro_volta.py`, chamada pela thread `flowpcp-envio`. Ela ganha quatro peças separadas: o corpo e a leitura do resultado (`cadastro_resultado.py`), o gravador no Fire (`app/erp/cadastro_fire.py`, única porta de escrita), o livro v2 (`cadastro_volta_repo`, com estados de gravação e reenvio) e a recuperação de item interrompido. Cada ciclo primeiro reenvia os resultados que ficaram sem volta, depois lê a fila e processa item a item. A escrita no Fire fica atrás de três portões: o modo, a capacidade `cadastros.v1` e o portão da P0 (`PORTAL_FLOWPCP_CADASTRO_P0`).

**Tech Stack:** Python 3.11, pydantic v2, fdb (Firebird 2.5, inclusive `fdb.services` para o backup), SQLite do ambiente, httpx via `app/http`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-16-flow-fonte-do-cadastro-design.md` (§6.0, §6.3, §6.6, §8, §9) e o contrato do Flow `pcp-app/docs/superpowers/specs/2026-10-17-p4a-contrato-portal.md` (rev. 3, commit `bc3c74d0`, branch `feat/p4a-cadastro-produto`). **Onde os dois divergem, vale o contrato P4a** (ele é posterior e foi conferido no código do Flow). Cópia das regras do contrato que o executor precisa está nas tasks; o executor lê o contrato inteiro antes da Task 1.

## Decisões que este plano assume

| # | Decisão | Por quê | Custo se estiver errada |
|---|---|---|---|
| R1 | A Etapa 3 entra em duas partes: **3a** (Tasks 1 a 12, tudo testado, gravação real só contra cópia do `.fdb`) e **3b** (Task 13: aplicar o resultado da P0 e liberar o portão por ambiente) | O que a tela do Fire grava junto com um produto ainda não é conhecido (spec §3, §9). Sem a P0 o gravador cobre as colunas conhecidas e recusa produção | Se a P0 mostrar tabela obrigatória além de `PRODUTOS`/`PRODUTOS_KIT`, a Task 13 cresce; nada do 3a se perde |
| R2 | Portão novo `PORTAL_FLOWPCP_CADASTRO_P0=<slug>[,<slug>]`: os modos `criar` e `criar_e_atualizar` recusam iniciar num ambiente fora dele | A P0 é por banco (a Confecção .4 tem cadastro diferente da Americanense .7). O ambiente DEV com cópia local entra no portão para a Task 19 do Flow | Nenhum: é variável de ambiente, sem migração |
| R3 | `cor` **não** vai para `DESCRICAO2`, na ida nem na volta | `DESCRICAO2` é a "Descrição Interna" da tela do Fire, não cor (achado de 17/10). A ida da Etapa 1 mandava `DESCRICAO2` como `cor` (interruptor desligado, nada saiu) | Kit criado pelo Flow nasce sem descrição interna no Fire. O Flow pode pedir `descricaoInterna` na rev. 4 do contrato (pergunta aberta 1) |
| R4 | **Revogada (Emenda 1):** `marca_ausente_erp` vai como erro **definitivo**, como diz o contrato §7 | Transitório não guarda o motivo no Flow e gira sem cura (só o Portal liga a marca ao código; código que sumiu do ERP não volta sozinho) | A área de cadastro precisa aprovar de novo depois de a marca existir no ERP |
| R5 | Em `simular`, linhas do livro são **descartáveis**: entrar num modo de gravação reprocessa o item | Erro decidido em `simular` nunca foi ao Flow; se o livro o tratasse como final, o item ficaria preso para sempre | Nenhum |
| R6 | Resultado pendente (`gravado_sem_volta`, `erro_a_enviar`) é reenviado em **todo** ambiente com FlowPCP habilitado, qualquer que seja o modo, inclusive `desligado` | Contrato §6: resultado nunca se descarta. Desligar a volta não pode deixar produto no Fire sem a volta registrada | Uma consulta SQLite por ambiente a cada 5 min |
| R7 | O escopo de `atualizar`/`inativar` vale para SEQ da cópia local **ou** SEQ criado pelo próprio Portal (livro) | A MM roda o catálogo com `apenas_meias`; par novo sem subgrupo MEIAS e sem pedido não entra em `catalogo_fire`, e o `atualizar` dele viraria `seq_fora_do_escopo` | Nenhum: o livro só tem o que o Portal gravou |
| R8 | O backup do saneamento usa `fdb.services` (gbak pelo servidor), sem dependência nova | `fdb` já é dependência; o gbak via services já rodou daqui (memória 25/08) | O arquivo fica no disco do servidor do Fire, não no Portal |

## Perguntas abertas para a sessão do Flow (não bloqueiam o 3a)

1. `cor` versus "Descrição Interna" (R3): o Flow quer um campo `descricaoInterna` (1..200) no item da fila?
2. Modo `criar` pede `acoes=criar`. Um `criar` que virou `atualizar` (§4 do contrato) não sai nesse modo, e o kit que depende desse par fica parado até o modo `criar_e_atualizar`. Confirmar que é aceito.
3. Par criado pelo Portal que não entra no recorte `apenas_meias` não aparece na ida. Com `fullSync`, o Flow trata como `flowOnly` sem desativar? Confirmar.

## Global Constraints

- Python `>=3.11`; nenhuma dependência nova (`fdb.services` faz parte do `fdb>=2.0`).
- Escrita no Fire **só** por `app/erp/cadastro_fire.py`, sempre parametrizada, uma transação por cadastro com `commit()`/`rollback()` explícitos.
- `simular` usa `FirebirdConnection().connect_somente_leitura` e o teste falha com qualquer `execute` que não seja SELECT.
- Nenhum dado de produção é gravado sem o ok explícito do Samuel: Fire de produção, Flow de produção, ligar modo de gravação, rodar o saneamento com `--gravar`.
- Mensagem de erro que vai ao Flow: até 500 caracteres, português claro, sem SQL, sem caminho, sem segredo, sem o valor recebido (só nome de campo e tamanho).
- `nome_existe`: a mensagem é exatamente `Já existe produto com esse nome no ERP (código N)`, sem outro número antes de `N`.
- `gravadoEm` em UTC com `Z` e milissegundos (`2026-10-17T13:05:00.000Z`).
- `erpProdutoId` enviado casa `^[1-9][0-9]{0,8}$`. `erro.codigo` nunca é `conflito_codigo`, `versao_superada` nem `cancelado_no_flow`.
- Coluna nova no SQLite do ambiente só por `_ADD_COLUMNS` em `app/persistence/schema_env.py` (aditiva, sem migração destrutiva).
- Testes: `.venv/bin/pytest`, fake do Flow e do Fire como em `tests/test_cadastro_volta.py`; a cerca `tests/conftest.py::_no_real_firebird` continua valendo.
- `ruff check app/ tests/ tools/` limpo no que for tocado.
- Doc incremental: só as seções afetadas de `docs/ai/modules/flowpcp.md` e `persistence.md`.

## Review Focus

1. **Troca de `simular` para `criar` com o livro cheio de `simulado`.** Esperado: o item é reprocessado e gravado, não pulado como "já no livro" (Task 4, teste `test_reservar_retoma_linha_simulada`; Task 7, `test_criar_reprocessa_item_simulado`).
2. **Par criado pelo Portal fora de `catalogo_fire` recebe `atualizar`.** Esperado: aceito pelo escopo do livro (Task 7, `test_atualizar_de_seq_criado_pelo_portal_esta_no_escopo`).
3. **Fire gravou, o `POST` se perdeu e o portal reiniciou com a volta `desligado`.** Esperado: o próximo ciclo reenvia o `gravado`, sem abrir o Fire (Task 7, `test_pendencia_reenviada_com_modo_desligado`).
4. **Segundo `criar` para o mesmo `produtoId` (regra 9b) enquanto o primeiro está `gravado_sem_volta`.** Esperado: reenvia o `gravado` da v1 e não manda nada para a v2 (Task 7, `test_9b_criar_repetido_reenvia_v1_e_cala_v2`).
5. **Fire lento: o lote passa do prazo.** Esperado: depois de `PRAZO_LOTE` nenhum item novo começa, e os não iniciados não recebem `POST` (Task 7, `test_prazo_do_lote_para_de_iniciar_itens`).

---

## Mapa de arquivos

| Arquivo | Papel | Task |
|---|---|---|
| `app/integrations/flowpcp/cadastro_schema.py` | item `cadastroPendenteV1` (ganha `produtoId`, `marcaErp`, regras cruzadas) | 1 |
| `app/integrations/flowpcp/cadastro_resultado.py` (novo) | corpo do `POST /gravado` e leitura da resposta em `Desfecho` | 2 |
| `app/integrations/flowpcp/client.py` | `list_cadastros_pendentes(limite, acoes, simular)` e `post_cadastro_resultado` | 3 |
| `app/persistence/schema_env.py`, `app/persistence/cadastro_volta_repo.py` | livro v2: colunas e transições de estado | 4 |
| `app/integrations/flowpcp/cadastro_volta.py` | modos, ciclo, regras de ouro | 5, 7, 8 |
| `app/erp/cadastro_fire.py` (novo) | gravador e leituras de conferência no Fire | 6 |
| `app/erp/catalog_extract.py`, `app/integrations/flowpcp/catalogo_marcas.py` (novo), `catalogo_mapper.py`, `catalogo_schema.py`, `catalogo_sync.py` | ida com `marcasErp` e `temNotaFiscal` | 9 |
| `app/integrations/flowpcp/catalogo_fiscal.py`, `cadastro_volta.py` | `cor` fora de `DESCRICAO2` | 10 |
| `app/erp/backup.py` (novo), `tools/sanear_marcas_fire.py` (novo) | saneamento das marcas | 11 |
| `app/observability/metrics.py`, `app/web/routes_environments.py`, `app/web/static/admin-ambiente-edit.html` | métricas e tela | 12 |
| `tools/perfil_cadastro_fire.py`, `tools/produto_no_fire.py` (novos), `cadastro_fire.py` | P0 | 13 |
| `docs/ai/modules/flowpcp.md`, `persistence.md`, `CHANGELOG.md` | doc | 14 |

---

### Task 1: Item da fila no contrato `cadastros.v1` rev. 3

> **Emenda 1 (revisão do Flow, 17/10).** I7: `CadastroPendente` (só o modelo de topo) usa `extra="ignore"` com `strict=True`; os modelos aninhados seguem `extra="forbid"`. Nova função `chaves_desconhecidas(bruto: dict) -> list[str]` (nomes de topo desconhecidos, ordenados; nome fora de `^[A-Za-z0-9_]{1,40}$` vira `"?"`). Testes: chave extra no topo valida e aparece em `chaves_desconhecidas`; chave extra em `grupoErp` continua `contrato_invalido`.

**Files:**
- Modify: `app/integrations/flowpcp/cadastro_schema.py`
- Test: `tests/test_cadastro_schema.py`

**Interfaces:**
- Produces: `CadastroPendente.produtoId: UUID`, `CadastroPendente.marcaErp: MarcaRef | None` (`MarcaRef.codigo: int`), `validar_item(bruto) -> tuple[CadastroPendente | None, str | None, str | None]` (assinatura igual à atual).

Regras do contrato (§2.1, §2.2) que este task aplica: `produtoId` UUID obrigatório; `marcaErp` é `{codigo: inteiro > 0}` ou `null`; `criar` exige `marcaErp` e `erpProdutoId: null`; `inativar` exige `marcaErp: null`; `atualizar` aceita os dois. `criar` de kit/pack exige lista; `atualizar` de kit/pack com `null` não mexe na composição. O código atual já cobre componentes e `erpProdutoId`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_cadastro_schema.py`, atualize o item base dos testes para ter `produtoId` e `marcaErp` (use o nome que o arquivo já usa para o dicionário base; se for `BOM`, faça `BOM = {**BOM_ANTIGO, ...}` no topo) e acrescente:

```python
PRODUTO_ID = "77777777-7777-4777-8777-777777777771"


def _item(**mudancas):
    base = {
        "cadastroId": "22222222-2222-4222-8222-222222222222",
        "produtoId": PRODUTO_ID,
        "versao": 1,
        "acao": "criar",
        "erpProdutoId": None,
        "tipo": "simples",
        "nome": "MEIA SOQUETE",
        "unidadeErp": "PAR",
        "ean": None,
        "ncm": "61159500",
        "tipoItemSped": 3,
        "grupoErp": {"codigo": "8"},
        "codigoNfe": None,
        "cor": None,
        "componentes": None,
        "marcaErp": {"codigo": 17},
        "codigoMes": None,
        "aprovadoEm": "2026-10-17T13:00:00.123Z",
    }
    base.update(mudancas)
    return base


def test_produto_id_obrigatorio():
    bruto = _item()
    del bruto["produtoId"]
    item, codigo, _ = validar_item(bruto)
    assert item is None and codigo == "contrato_invalido"


def test_produto_id_vira_uuid():
    item, codigo, _ = validar_item(_item())
    assert codigo is None
    assert str(item.produtoId) == PRODUTO_ID


@pytest.mark.parametrize("marca", [None, {"codigo": 0}, {"codigo": -1}, {"codigo": "17"}, {"codigo": True}, {}])
def test_criar_exige_marca_valida(marca):
    item, codigo, _ = validar_item(_item(marcaErp=marca))
    assert item is None and codigo == "contrato_invalido"


def test_atualizar_aceita_marca_null():
    item, codigo, _ = validar_item(_item(acao="atualizar", erpProdutoId="3990", marcaErp=None))
    assert codigo is None and item.marcaErp is None


def test_atualizar_aceita_marca_preenchida():
    item, codigo, _ = validar_item(
        _item(acao="atualizar", erpProdutoId="3990", marcaErp={"codigo": 18})
    )
    assert codigo is None and item.marcaErp.codigo == 18


def test_inativar_com_marca_e_contrato_invalido():
    item, codigo, _ = validar_item(_item(acao="inativar", erpProdutoId="3990"))
    assert item is None and codigo == "contrato_invalido"


def test_marca_acima_do_inteiro_do_fire():
    item, codigo, _ = validar_item(_item(marcaErp={"codigo": 2**31}))
    assert item is None and codigo == "contrato_invalido"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cadastro_schema.py -v`
Expected: FAIL (`produtoId` e `marcaErp` são recusados como `extra`).

- [ ] **Step 3: Implement**

Em `cadastro_schema.py`:

```python
class MarcaRef(_Estrito):
    # PRODUTOS_MARCA.CODIGO é INTEGER no Fire: cabe em 32 bits.
    codigo: int = Field(ge=1, le=2**31 - 1)
```

Em `CadastroPendente`, logo depois de `cadastroId`:

```python
    produtoId: UUID  # noqa: N815 — id estável do produto no Flow (igual em todas as versões)
```

e depois de `componentes`:

```python
    marcaErp: MarcaRef | None = None  # noqa: N815
```

O validador `_uuid` passa a valer para os dois campos:

```python
    @field_validator("cadastroId", "produtoId", mode="before")
    @classmethod
    def _uuid(cls, v: object) -> UUID:
        if not isinstance(v, str):
            raise ValueError("id deve ser texto")
        return UUID(v)
```

Em `validar_item`, depois da checagem `if item.acao != "criar" and item.erpProdutoId is None`:

```python
    if item.acao == "criar" and item.marcaErp is None:
        return None, "contrato_invalido", "criar exige marcaErp"
    if item.acao == "inativar" and item.marcaErp is not None:
        return None, "contrato_invalido", "inativar exige marcaErp null"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_schema.py tests/test_cadastro_volta.py -v`
Expected: `test_cadastro_schema.py` PASS. `test_cadastro_volta.py` pode falhar porque o item `BOM` dele não tem `produtoId`/`marcaErp`: acrescente `"produtoId": "77777777-7777-4777-8777-777777777771"` e `"marcaErp": {"codigo": 17}` no `BOM` desse arquivo e rode de novo até PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/cadastro_schema.py tests/test_cadastro_schema.py tests/test_cadastro_volta.py
git commit -m "feat(cadastro): item da fila com produtoId e marcaErp (cadastros.v1 rev. 3)"
```

---

### Task 2: Corpo do resultado e leitura da resposta do Flow

> **Emenda 1 (revisão do Flow, 17/10; vale acima do texto abaixo onde divergir).**
> (a) I1: para corpo **`gravado`**, só 200 `gravado`, `ja_registrado` e `registrado_versao_superada` dão `CONFIRMADO`. 200 `erro_registrado` ou `transitorio_registrado` com corpo `gravado` (impossível hoje no Flow) dá **`BUG_CORPO`**, nunca fecha a linha. Troque as duas linhas correspondentes da tabela de teste: `("gravado", 200, {"status": "erro_registrado"}, Desfecho.BUG_CORPO)` e `("gravado", 200, {"status": "transitorio_registrado", "tentativas": 1}, Desfecho.BUG_CORPO)`.
> (b) M1: `_RESERVADOS` ganha `fora_do_contrato` (o Flow passa a usar esse código para item que ele mesmo fecha). Acrescente `"fora_do_contrato"` ao parametrize de `test_erro_recusa_codigo_reservado`.
> (c) M4: acrescente `mensagem_para_flow(codigo: str, detalhe: str | None = None) -> str`, que traduz o código interno para a frase que a área de cadastro lê no Flow. Português claro; diga "ERP" (nunca "Fire"); sem "SEQ", "erpProdutoId", nome de campo do contrato ou índice de lista. Tabela fixa: `contrato_invalido` → "O cadastro chegou incompleto ou com um campo em formato inesperado. Confira os dados e aprove de novo."; `campo_excede_fire` → "Um texto do cadastro é maior do que o ERP aceita. Encurte e aprove de novo."; `caractere_invalido` → "O cadastro tem um caractere que o ERP não grava (por exemplo, emoji). Troque e aprove de novo."; `componente_ausente` → "Um item da composição não existe no ERP."; `componente_bloqueado` → "Um item da composição está inativo no ERP."; `marca_ausente_erp` → "A marca do cadastro não existe no ERP. Cadastre a marca no ERP e aprove de novo."; `seq_fora_do_escopo` → "O produto não está entre os produtos que o Portal pode alterar no ERP."; `seq_inexistente` → "O produto não existe mais no ERP."; `tipo_nao_suportado` → "Este tipo de cadastro ainda não é gravado no ERP pelo Portal."; `ja_criado_no_erp` → "Este produto já foi criado no ERP (código N) e não foi vinculado. Fale com a área de cadastro." (N do `detalhe`); `erp_recusou` → "O ERP recusou a gravação do cadastro."; código desconhecido → "O ERP recusou o cadastro.". `nome_existe` continua com `mensagem_nome_existe`. Teste: cada código da tabela; nenhuma saída contém "Fire", "SEQ" ou "erpProdutoId".

**Files:**
- Create: `app/integrations/flowpcp/cadastro_resultado.py`
- Test: `tests/test_cadastro_resultado.py`

**Interfaces:**
- Produces:
  - `corpo_gravado(versao: int, erp_produto_id: str, gravado_em: datetime) -> dict`
  - `corpo_erro(versao: int, tipo: Literal["transitorio","definitivo"], codigo: str, mensagem: str) -> dict`
  - `mensagem_nome_existe(seq: int | str) -> str`
  - `class Desfecho(StrEnum)`: `CONFIRMADO`, `ERRO_REGISTRADO`, `TRANSITORIO_REGISTRADO`, `ORFAO`, `SUPERADO`, `REPETIR`, `BUG_CORPO`, `CREDENCIAL`
  - `ler_resposta(resultado: Literal["gravado","erro"], status_http: int | None, corpo: dict | None) -> Desfecho`

Tabela do contrato (§5) que `ler_resposta` implementa (`None` = timeout ou queda de rede):

| HTTP / corpo | corpo enviado `gravado` | corpo enviado `erro` |
|---|---|---|
| 200 `gravado`, `ja_registrado`, `registrado_versao_superada` | `CONFIRMADO` | `ERRO_REGISTRADO` (`ja_registrado`) |
| 200 `erro_registrado` | `ERRO_REGISTRADO` | `ERRO_REGISTRADO` |
| 200 `transitorio_registrado` | `TRANSITORIO_REGISTRADO` | `TRANSITORIO_REGISTRADO` |
| 409 `versao_superada`, `conflito_codigo` | `ORFAO` | `SUPERADO` |
| 400, 404, 409 `versao_divergente`, 409 desconhecido | `BUG_CORPO` | `BUG_CORPO` |
| 401 | `CREDENCIAL` | `CREDENCIAL` |
| 429, 5xx, `None`, 200 com status desconhecido | `REPETIR` | `REPETIR` |

- [ ] **Step 1: Write the failing tests**

```python
"""Corpo do POST /cadastros/{id}/gravado e leitura da resposta (contrato P4a §5)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.integrations.flowpcp.cadastro_resultado import (
    Desfecho,
    corpo_erro,
    corpo_gravado,
    ler_resposta,
    mensagem_nome_existe,
)


def test_gravado_em_utc_com_z_e_milissegundos():
    sp = timezone(timedelta(hours=-3))
    corpo = corpo_gravado(1, "3992", datetime(2026, 10, 17, 10, 5, tzinfo=sp))
    assert corpo == {
        "versao": 1,
        "resultado": "gravado",
        "erpProdutoId": "3992",
        "gravadoEm": "2026-10-17T13:05:00.000Z",
    }


@pytest.mark.parametrize("ruim", ["0123", "0", "1234567890", "", "12a"])
def test_gravado_recusa_codigo_fora_da_regex(ruim):
    with pytest.raises(ValueError):
        corpo_gravado(1, ruim, datetime.now(UTC))


def test_gravado_recusa_instante_sem_fuso():
    with pytest.raises(ValueError):
        corpo_gravado(1, "3992", datetime(2026, 10, 17, 13, 5))


@pytest.mark.parametrize("reservado", ["conflito_codigo", "versao_superada", "cancelado_no_flow"])
def test_erro_recusa_codigo_reservado(reservado):
    with pytest.raises(ValueError):
        corpo_erro(1, "definitivo", reservado, "x")


def test_erro_recusa_mensagem_so_de_espaco():
    with pytest.raises(ValueError):
        corpo_erro(1, "definitivo", "contrato_invalido", "   ")


def test_erro_corta_mensagem_em_500():
    corpo = corpo_erro(1, "definitivo", "contrato_invalido", "a" * 900)
    assert len(corpo["erro"]["mensagem"]) == 500


def test_mensagem_nome_existe_tem_so_o_codigo_como_numero():
    msg = mensagem_nome_existe(3100)
    assert msg == "Já existe produto com esse nome no ERP (código 3100)"


@pytest.mark.parametrize(
    ("resultado", "http", "corpo", "esperado"),
    [
        ("gravado", 200, {"status": "gravado"}, Desfecho.CONFIRMADO),
        ("gravado", 200, {"status": "ja_registrado"}, Desfecho.CONFIRMADO),
        ("gravado", 200, {"status": "registrado_versao_superada", "versaoAtual": 2}, Desfecho.CONFIRMADO),
        ("erro", 200, {"status": "erro_registrado"}, Desfecho.ERRO_REGISTRADO),
        ("erro", 200, {"status": "ja_registrado"}, Desfecho.ERRO_REGISTRADO),
        ("erro", 200, {"status": "transitorio_registrado", "tentativas": 1}, Desfecho.TRANSITORIO_REGISTRADO),
        ("gravado", 409, {"error": "versao_superada", "versaoAtual": 2}, Desfecho.ORFAO),
        ("gravado", 409, {"error": "conflito_codigo"}, Desfecho.ORFAO),
        ("erro", 409, {"error": "versao_superada", "versaoAtual": 2}, Desfecho.SUPERADO),
        ("gravado", 409, {"error": "versao_divergente", "versaoAtual": 2}, Desfecho.BUG_CORPO),
        ("gravado", 404, {"error": "cadastro_nao_encontrado"}, Desfecho.BUG_CORPO),
        ("gravado", 400, {"error": "payload inválido"}, Desfecho.BUG_CORPO),
        ("gravado", 401, {"error": "unauthorized"}, Desfecho.CREDENCIAL),
        ("gravado", 429, {"error": "rate_limit_excedido"}, Desfecho.REPETIR),
        ("gravado", 503, {"error": "erro registrando o resultado"}, Desfecho.REPETIR),
        ("gravado", None, None, Desfecho.REPETIR),
        ("gravado", 200, {"status": "novidade"}, Desfecho.REPETIR),
        ("gravado", 200, None, Desfecho.REPETIR),
    ],
)
def test_ler_resposta(resultado, http, corpo, esperado):
    assert ler_resposta(resultado, http, corpo) is esperado
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_resultado.py -v`
Expected: FAIL (módulo não existe).

- [ ] **Step 3: Implement**

```python
"""Corpo do `POST /cadastros/{cadastroId}/gravado` e leitura da resposta do Flow
(contrato P4a, §5). Puro: sem rede, sem banco.

A decisão é pelo `status` da resposta, nunca pela `acao` do item (regra de ouro 5).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

MAX_MENSAGEM = 500
_ERP_ID = re.compile(r"^[1-9][0-9]{0,8}$")
_RESERVADOS = frozenset({"conflito_codigo", "versao_superada", "cancelado_no_flow"})


class Desfecho(StrEnum):
    CONFIRMADO = "confirmado"  # gravado aceito: fecha no livro
    ERRO_REGISTRADO = "erro_registrado"  # erro definitivo aceito: fecha no livro
    TRANSITORIO_REGISTRADO = "transitorio_registrado"  # reserva liberada: item volta no GET
    ORFAO = "orfao"  # produto existe no ERP sem vínculo no Flow: avisar a operação
    SUPERADO = "superado"  # erro de versão já superada: nada a fazer
    REPETIR = "repetir"  # 429, 5xx, rede: mesmo POST depois, com espera crescente
    BUG_CORPO = "bug_corpo"  # 400/404/409 versao_divergente: alerta, mantém e repete com espera longa
    CREDENCIAL = "credencial"  # 401: para os POSTs deste ciclo


def _instante(gravado_em: datetime) -> str:
    if gravado_em.tzinfo is None:
        raise ValueError("gravadoEm sem fuso")
    utc = gravado_em.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def corpo_gravado(versao: int, erp_produto_id: str, gravado_em: datetime) -> dict:
    if not _ERP_ID.fullmatch(erp_produto_id or ""):
        raise ValueError("erpProdutoId fora de ^[1-9][0-9]{0,8}$")
    return {
        "versao": versao,
        "resultado": "gravado",
        "erpProdutoId": erp_produto_id,
        "gravadoEm": _instante(gravado_em),
    }


def corpo_erro(
    versao: int, tipo: Literal["transitorio", "definitivo"], codigo: str, mensagem: str
) -> dict:
    if codigo in _RESERVADOS or not 1 <= len(codigo) <= 64:
        raise ValueError("erro.codigo reservado ou fora de 1..64")
    texto = (mensagem or "").strip()
    if not texto:
        raise ValueError("erro.mensagem vazia")
    return {
        "versao": versao,
        "resultado": "erro",
        "erro": {"tipo": tipo, "codigo": codigo, "mensagem": texto[:MAX_MENSAGEM]},
    }


def mensagem_nome_existe(seq: int | str) -> str:
    # O Flow mostra o PRIMEIRO número de 1 a 9 dígitos da mensagem: nenhum antes do código.
    return f"Já existe produto com esse nome no ERP (código {seq})"


_OK_GRAVADO = {"gravado", "ja_registrado", "registrado_versao_superada"}


def ler_resposta(
    resultado: Literal["gravado", "erro"], status_http: int | None, corpo: dict | None
) -> Desfecho:
    corpo = corpo if isinstance(corpo, dict) else {}
    if status_http == 200:
        status = corpo.get("status")
        if status == "transitorio_registrado":
            return Desfecho.TRANSITORIO_REGISTRADO
        if status == "erro_registrado":
            return Desfecho.ERRO_REGISTRADO
        if status in _OK_GRAVADO:
            return Desfecho.CONFIRMADO if resultado == "gravado" else Desfecho.ERRO_REGISTRADO
        return Desfecho.REPETIR
    if status_http == 409 and corpo.get("error") in ("versao_superada", "conflito_codigo"):
        return Desfecho.ORFAO if resultado == "gravado" else Desfecho.SUPERADO
    if status_http in (400, 404, 409):
        return Desfecho.BUG_CORPO
    if status_http == 401:
        return Desfecho.CREDENCIAL
    return Desfecho.REPETIR
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_resultado.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/cadastro_resultado.py tests/test_cadastro_resultado.py
git commit -m "feat(cadastro): corpo do resultado e leitura da resposta do Flow (P4a §5)"
```

---

> **Emenda 2 da Task 2 (revisão da Task 2, 17/10).** `ler_resposta`: `status` que não é texto → `REPETIR`; corpo `erro` só fecha com 200 `ja_registrado`/`erro_registrado` (`ERRO_REGISTRADO`) e 409 `versao_superada` (`SUPERADO`), o resto da tabela para `erro` → `BUG_CORPO`; 4xx fora da tabela → `BUG_CORPO`. `mensagem_para_flow` ganha `conexao_recusada` ("O ERP recusou a conexão.") e `erp_ocupado` ("O ERP estava ocupado; o Portal tenta de novo."); `nome_existe` delega a `mensagem_nome_existe`; `ja_criado_no_erp`/`nome_existe` exigem `detalhe` que casa `^[1-9][0-9]{0,8}$`. Textos trocados porque o Flow prefixa "O ERP recusou: ": `erp_recusou` → "Um dado do cadastro não está no formato que o ERP aceita. Fale com o suporte do Portal."; genérico → "Motivo não identificado. Fale com o suporte do Portal.".

### Task 3: Cliente HTTP — fila com `acoes`/`simular` e POST do resultado

> **Emenda 1 (revisão do Flow, 17/10; vale acima do texto abaixo onde divergir).**
> (a) M8: em `post_cadastro_resultado`, o `except` genérico não pode engolir erro de programação. Capture só falha de transporte (`httpx.HTTPError`, `OSError`, e o tipo de timeout/conexão que `app/http/client.py` levanta) e devolva `status_http=None`; `TypeError`, `KeyError` etc. sobem. Teste: um `handler` que levanta `TypeError` faz o método levantar.
> (b) O contrato rev. 4 vai aceitar `simular=1` **combinado** com `acoes`. Troque a regra "`simular=True` nunca manda `acoes`" por: `simular=True` manda `simular=1` e, se `acoes` vier, manda `acoes` também. Ajuste `test_simular_manda_simular_1_sem_acoes` para chamar sem `acoes` (espera `{"limite": "50", "simular": "1"}`) e acrescente `test_simular_com_acoes` (espera `{"limite": "50", "simular": "1", "acoes": "atualizar"}`). Quem chama com `simular=True` hoje (Task 5) não passa `acoes`.

**Files:**
- Modify: `app/integrations/flowpcp/client.py:127-143`
- Test: `tests/test_flowpcp_client_cadastros.py` (novo)

**Interfaces:**
- Consumes: `PendentesResposta` (Task 1, inalterada).
- Produces:
  - `FlowPCPClient.list_cadastros_pendentes(limite: int = 20, *, acoes: tuple[str, ...] | None = None, simular: bool = False) -> PendentesResposta`
  - `FlowPCPClient.post_cadastro_resultado(cadastro_id: str, corpo: dict) -> RespostaResultado`
  - `@dataclass(frozen=True) class RespostaResultado: status_http: int | None; corpo: dict | None; retry_after: int | None`

Regras: `simular=True` manda `simular=1` e **nunca** `acoes`. `acoes` vai como texto separado por vírgula, sem espaço, na ordem `criar,atualizar,inativar`. O POST é idempotente no Flow por `(cadastroId, versao)`: pode usar `idempotent_post_policy()`; ele **não levanta** por status HTTP: devolve o status e o corpo para `ler_resposta`. Falha de transporte (timeout, conexão) devolve `status_http=None`. `retry_after` é o header `Retry-After` em segundos inteiros (>= 1) ou `None`.

- [ ] **Step 1: Write the failing tests**

Siga o padrão de dublê HTTP já usado nos testes do cliente FlowPCP (procure em `tests/` por `OutboundClient` ou `httpx.MockTransport`: `grep -rln "MockTransport" tests/ | head`). Com `httpx.MockTransport`:

```python
import httpx
import pytest

from app.integrations.flowpcp.client import FlowPCPClient


def _cliente(handler):
    # Use o mesmo construtor que os outros testes do cliente usam para injetar transporte.
    return FlowPCPClient.para_teste(transport=httpx.MockTransport(handler))


def test_simular_manda_simular_1_sem_acoes():
    vistos = []

    def handler(req):
        vistos.append(dict(req.url.params))
        return httpx.Response(200, json={"cadastros": [], "limite": 50, "capacidades": []})

    _cliente(handler).list_cadastros_pendentes(limite=50, acoes=("criar",), simular=True)
    assert vistos == [{"limite": "50", "simular": "1"}]


def test_acoes_em_ordem_canonica():
    vistos = []

    def handler(req):
        vistos.append(dict(req.url.params))
        return httpx.Response(200, json={"cadastros": [], "limite": 20, "capacidades": []})

    _cliente(handler).list_cadastros_pendentes(limite=20, acoes=("atualizar", "criar"))
    assert vistos == [{"limite": "20", "acoes": "criar,atualizar"}]


def test_post_resultado_devolve_409_sem_levantar():
    def handler(req):
        assert req.url.path == "/api/portal-pedidos/cadastros/2222/gravado"
        return httpx.Response(409, json={"error": "conflito_codigo"})

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert (r.status_http, r.corpo, r.retry_after) == (409, {"error": "conflito_codigo"}, None)


def test_post_resultado_le_retry_after():
    def handler(req):
        return httpx.Response(429, json={"error": "rate_limit_excedido"}, headers={"Retry-After": "7"})

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert r.retry_after == 7


def test_post_resultado_sem_rede_vira_none():
    def handler(req):
        raise httpx.ConnectTimeout("x")

    r = _cliente(handler).post_cadastro_resultado("2222", {"versao": 1})
    assert r.status_http is None and r.corpo is None
```

Se não existir um construtor de teste (`para_teste`), crie-o como `@classmethod` que monta o `OutboundClient` com o `transport` dado, base `http://flow.test`, token e tenant falsos, seguindo o `__init__` atual (linhas 77-100). Não mude o `__init__`.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_flowpcp_client_cadastros.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
_ORDEM_ACOES = ("criar", "atualizar", "inativar")


@dataclass(frozen=True)
class RespostaResultado:
    status_http: int | None
    corpo: dict | None
    retry_after: int | None


def _retry_after(resp) -> int | None:
    bruto = (resp.headers.get("Retry-After") or "").strip()
    return int(bruto) if bruto.isdigit() and int(bruto) >= 1 else None
```

No `FlowPCPClient`, substitua `list_cadastros_pendentes` e acrescente o POST:

```python
    def list_cadastros_pendentes(
        self, limite: int = 20, *, acoes: tuple[str, ...] | None = None, simular: bool = False
    ) -> PendentesResposta:
        """Fila de cadastros aprovados. `simular=True` lê sem reservar (contrato §2);
        sem ele, todo GET reserva os itens por 10 minutos."""
        params: dict[str, str] = {"limite": str(limite)}
        if simular:
            params["simular"] = "1"
        elif acoes:
            params["acoes"] = ",".join(a for a in _ORDEM_ACOES if a in set(acoes))
        try:
            resp = self._client.get(_CADASTROS_PENDENTES_PATH, params=params)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"list_cadastros_pendentes falhou: {exc}",
                status_code=exc.status_code,
                body=exc.body,
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"cadastros pendentes status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return PendentesResposta.model_validate(resp.json())

    def post_cadastro_resultado(self, cadastro_id: str, corpo: dict) -> RespostaResultado:
        """Resultado de um cadastro. Nunca levanta por status HTTP: quem decide é
        `cadastro_resultado.ler_resposta`. Transporte falhou = `status_http=None`."""
        path = f"/api/portal-pedidos/cadastros/{cadastro_id}/gravado"
        try:
            resp = self._client.post_json(
                path,
                json=corpo,
                idempotency_key=f"cadastro-{cadastro_id}-{corpo.get('versao')}-{corpo.get('resultado')}",
                retry_policy=idempotent_post_policy(),
            )
        except HttpError as exc:
            corpo_erro = None
            try:
                corpo_erro = json.loads(exc.body) if exc.body else None
            except ValueError:
                corpo_erro = None
            return RespostaResultado(exc.status_code, corpo_erro, None)
        except Exception:  # noqa: BLE001 — timeout/rede: resultado incerto, o livro repete
            return RespostaResultado(None, None, None)
        try:
            dados = resp.json()
        except ValueError:
            dados = None
        return RespostaResultado(resp.status_code, dados if isinstance(dados, dict) else None, _retry_after(resp))
```

Confira em `app/http/client.py` se `post_json` levanta `HttpError` para 4xx/5xx ou devolve a resposta; ajuste o `except` para os dois caminhos (o teste do 409 e do 429 garante). Acrescente `from dataclasses import dataclass` no topo.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_flowpcp_client_cadastros.py tests/test_cadastro_volta.py -v`
Expected: PASS. Em `tests/test_cadastro_volta.py`, o `_Client.list_cadastros_pendentes` do dublê passa a aceitar `**kw` e guardar `kw` em `self.chamadas`.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/client.py tests/test_flowpcp_client_cadastros.py tests/test_cadastro_volta.py
git commit -m "feat(flowpcp): fila com acoes/simular e POST do resultado do cadastro"
```

---

### Task 4: Livro v2 — estados de gravação, reenvio e regra 9b

> **Emenda 1 (revisão do Flow, 17/10; vale acima do texto abaixo onde divergir).**
> (a) C1 (crítico): **`gravado_sem_volta` só sai para `confirmado` ou `orfao`.** `marcar_desfecho` com outro estado numa linha `gravado_sem_volta` não muda nada e devolve `False` (a função passa a devolver `bool`: `True` se mudou). Implemente no SQL: `UPDATE ... WHERE cadastro_id = ? AND versao = ? AND (estado <> 'gravado_sem_volta' OR ? IN ('confirmado', 'orfao'))`. `marcar_erro_a_enviar` também não toca linha `gravado_sem_volta` (mesmo `WHERE`). Testes: `test_gravado_sem_volta_nao_vira_transitorio` (`marcar_desfecho(..., "transitorio")` devolve `False` e o estado continua), `test_gravado_sem_volta_vira_confirmado`, `test_erro_a_enviar_nao_sobrescreve_gravado`.
> (b) I2: `criar_do_produto` passa a considerar também `incerto`, e `liberado` **com** `seq_reservado` não nulo (o INSERT pode ter comitado). A ordem de preferência quando há várias: `gravado_sem_volta`, `em_andamento`, `incerto`, `liberado` com SEQ, `orfao`, `confirmado`, e dentro do mesmo estado a mais recente. Testes: acha `incerto`; acha `liberado` com SEQ; ignora `liberado` sem SEQ.
> (c) M3: coluna nova `resposta_flow TEXT` (status ou erro devolvido pelo Flow, até 64 caracteres), gravada por `marcar_desfecho(..., resposta=...)` e por `adiar(..., resposta=...)` (parâmetro opcional nas duas).
> (d) Acrescente `seqs_em_aberto(conn) -> set[str]`: `erp_produto_id` e `seq_reservado` (como texto) das linhas em `em_andamento`, `gravado_sem_volta`, `incerto` e `liberado` com SEQ. A Task 9 usa para tirar esses SEQs da ida (I11). Teste com uma linha de cada.

**Files:**
- Modify: `app/persistence/schema_env.py` (lista `_ADD_COLUMNS`, perto da linha 366)
- Modify: `app/persistence/cadastro_volta_repo.py`
- Test: `tests/test_cadastro_volta_repo.py` (novo)

**Interfaces:**
- Produces (todas recebem `conn: sqlite3.Connection` aberta; nenhuma faz commit):
  - `ESTADOS_DESCARTAVEIS = ("simulado", "erro_definitivo", "transitorio", "liberado")` — o lease pode reaproveitar
  - `reservar(conn, *, cadastro_id, versao, produto_id, acao, tipo, modo, plano_json) -> bool`
  - `marcar_seq(conn, cadastro_id, versao, seq: int) -> None`
  - `marcar_gravado(conn, cadastro_id, versao, *, erp_produto_id: str, corpo_json: str) -> None` (estado `gravado_sem_volta`)
  - `marcar_erro_a_enviar(conn, cadastro_id, versao, *, codigo, mensagem, corpo_json) -> None`
  - `marcar_desfecho(conn, cadastro_id, versao, estado: str, *, alerta: str | None = None) -> None`
  - `adiar(conn, cadastro_id, versao, *, ate: str, alerta: str | None = None) -> None` (soma 1 em `tentativas`)
  - `pendencias_de_envio(conn, agora_iso: str, limite: int = 20) -> list[dict]`
  - `tem_pendencia(conn) -> bool`
  - `buscar(conn, cadastro_id, versao) -> dict | None`
  - `criar_do_produto(conn, produto_id: str) -> dict | None` — linha de `acao='criar'` deste produto em `em_andamento`, `gravado_sem_volta`, `confirmado` ou `orfao` (a mais recente)
  - `seq_criado_pelo_portal(conn, seq: str) -> bool` — `erp_produto_id = seq` em `gravado_sem_volta` ou `confirmado`
  - `em_andamento(conn) -> list[dict]`
- Estados (texto em `estado`): `simulado`, `erro_definitivo` (decidido em simular), `em_andamento`, `gravado_sem_volta`, `erro_a_enviar`, `confirmado`, `erro_registrado`, `transitorio`, `liberado`, `orfao`, `superado`, `incerto`.

Colunas novas (todas `TEXT`/`INTEGER` anuláveis): `produto_id TEXT`, `modo TEXT`, `erp_produto_id TEXT`, `corpo_json TEXT`, `tentativas INTEGER NOT NULL DEFAULT 0`, `proxima_tentativa_em TEXT`, `alerta TEXT`. Índice: `CREATE INDEX IF NOT EXISTS idx_cadastro_volta_produto ON cadastro_volta(produto_id)` no bloco de criação do schema (o `IF NOT EXISTS` deixa idempotente).

- [ ] **Step 1: Write the failing tests**

```python
"""Livro da volta (cadastro_volta) v2: lease, gravação, reenvio e 9b."""

import sqlite3

import pytest

from app.persistence import cadastro_volta_repo as repo
from app.persistence.schema_env import aplicar_schema_env  # use o nome real do aplicador do schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    aplicar_schema_env(c)
    yield c
    c.close()


def _reservar(conn, cid="c1", versao=1, produto="p1", acao="criar", modo="criar"):
    return repo.reservar(
        conn, cadastro_id=cid, versao=versao, produto_id=produto, acao=acao,
        tipo="simples", modo=modo, plano_json="{}",
    )


def test_reservar_uma_vez(conn):
    assert _reservar(conn) is True
    assert _reservar(conn) is False


def test_reservar_retoma_linha_simulada(conn):
    repo.registrar(conn, cadastro_id="c1", versao=1, estado="simulado", acao="criar",
                   tipo="simples", erro_codigo=None, erro_mensagem=None, plano_json="{}")
    assert _reservar(conn) is True
    assert repo.buscar(conn, "c1", 1)["estado"] == "em_andamento"


def test_reservar_retoma_transitorio_mas_nao_gravado(conn):
    _reservar(conn)
    repo.marcar_desfecho(conn, "c1", 1, "transitorio")
    assert _reservar(conn) is True
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert _reservar(conn) is False


def test_pendencias_respeitam_proxima_tentativa(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json='{"versao":1}')
    assert [p["cadastro_id"] for p in repo.pendencias_de_envio(conn, "2026-10-17T12:00:00")] == ["c1"]
    repo.adiar(conn, "c1", 1, ate="2026-10-17T13:00:00")
    assert repo.pendencias_de_envio(conn, "2026-10-17T12:30:00") == []
    assert repo.buscar(conn, "c1", 1)["tentativas"] == 1
    assert repo.tem_pendencia(conn) is True


def test_criar_do_produto_acha_gravado_de_outra_versao(conn):
    _reservar(conn, cid="v1", versao=1)
    repo.marcar_gravado(conn, "v1", 1, erp_produto_id="3992", corpo_json="{}")
    linha = repo.criar_do_produto(conn, "p1")
    assert (linha["cadastro_id"], linha["erp_produto_id"]) == ("v1", "3992")


def test_criar_do_produto_ignora_erro_e_simulado(conn):
    repo.registrar(conn, cadastro_id="v1", versao=1, estado="simulado", acao="criar",
                   tipo="simples", erro_codigo=None, erro_mensagem=None, plano_json="{}")
    assert repo.criar_do_produto(conn, "p1") is None


def test_seq_criado_pelo_portal(conn):
    _reservar(conn)
    repo.marcar_gravado(conn, "c1", 1, erp_produto_id="3992", corpo_json="{}")
    assert repo.seq_criado_pelo_portal(conn, "3992") is True
    assert repo.seq_criado_pelo_portal(conn, "3993") is False
```

Antes de escrever, descubra o nome real da função que cria o schema do ambiente: `grep -n "^def " app/persistence/schema_env.py`. Note que `registrar` (Etapa 1) não preenche `produto_id`; em `test_criar_do_produto_ignora_erro_e_simulado` isso é proposital.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_volta_repo.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Em `schema_env.py`, na lista `_ADD_COLUMNS`:

```python
    ("cadastro_volta", "produto_id", "ALTER TABLE cadastro_volta ADD COLUMN produto_id TEXT"),
    ("cadastro_volta", "modo", "ALTER TABLE cadastro_volta ADD COLUMN modo TEXT"),
    ("cadastro_volta", "erp_produto_id", "ALTER TABLE cadastro_volta ADD COLUMN erp_produto_id TEXT"),
    ("cadastro_volta", "corpo_json", "ALTER TABLE cadastro_volta ADD COLUMN corpo_json TEXT"),
    ("cadastro_volta", "tentativas", "ALTER TABLE cadastro_volta ADD COLUMN tentativas INTEGER NOT NULL DEFAULT 0"),
    ("cadastro_volta", "proxima_tentativa_em", "ALTER TABLE cadastro_volta ADD COLUMN proxima_tentativa_em TEXT"),
    ("cadastro_volta", "alerta", "ALTER TABLE cadastro_volta ADD COLUMN alerta TEXT"),
```

Confira que o aplicador de `_ADD_COLUMNS` roda **depois** dos `CREATE TABLE` e acrescente o índice no ponto onde ele roda (depois das colunas):

```python
conn.execute("CREATE INDEX IF NOT EXISTS idx_cadastro_volta_produto ON cadastro_volta(produto_id)")
```

Em `cadastro_volta_repo.py`, acrescente as colunas novas em `_COLS` e:

```python
ESTADOS_DESCARTAVEIS = ("simulado", "erro_definitivo", "transitorio", "liberado")
_PENDENTES = ("gravado_sem_volta", "erro_a_enviar")
_DESCARTAVEIS_SQL = ", ".join(f"'{e}'" for e in ESTADOS_DESCARTAVEIS)
_PENDENTES_SQL = ", ".join(f"'{e}'" for e in _PENDENTES)


def _agora() -> str:
    return "datetime('now')"


def reservar(conn, *, cadastro_id, versao, produto_id, acao, tipo, modo, plano_json) -> bool:
    """Lease de `(cadastro_id, versao)`. True = este ciclo é dono do item.
    Linha descartável (simulação, transitório, liberada) é reaproveitada."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO cadastro_volta "
        "(cadastro_id, versao, estado, acao, tipo, produto_id, modo, plano_json) "
        "VALUES (?, ?, 'em_andamento', ?, ?, ?, ?, ?)",
        (cadastro_id, versao, acao, tipo, produto_id, modo, plano_json),
    )
    if cur.rowcount == 1:
        return True
    cur = conn.execute(
        "UPDATE cadastro_volta SET estado = 'em_andamento', acao = ?, tipo = ?, produto_id = ?, "
        "modo = ?, plano_json = ?, seq_reservado = NULL, erro_codigo = NULL, erro_mensagem = NULL, "
        "corpo_json = NULL, iniciado_em = datetime('now'), atualizado_em = datetime('now') "
        f"WHERE cadastro_id = ? AND versao = ? AND estado IN ({_DESCARTAVEIS_SQL})",
        (acao, tipo, produto_id, modo, plano_json, cadastro_id, versao),
    )
    return cur.rowcount == 1


def _set(conn, cadastro_id, versao, **campos) -> None:
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(
        f"UPDATE cadastro_volta SET {sets}, atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ?",
        (*campos.values(), cadastro_id, versao),
    )


def marcar_seq(conn, cadastro_id, versao, seq: int) -> None:
    _set(conn, cadastro_id, versao, seq_reservado=seq)


def marcar_gravado(conn, cadastro_id, versao, *, erp_produto_id: str, corpo_json: str) -> None:
    _set(conn, cadastro_id, versao, estado="gravado_sem_volta", erp_produto_id=erp_produto_id,
         corpo_json=corpo_json, proxima_tentativa_em=None)


def marcar_erro_a_enviar(conn, cadastro_id, versao, *, codigo, mensagem, corpo_json) -> None:
    _set(conn, cadastro_id, versao, estado="erro_a_enviar", erro_codigo=codigo,
         erro_mensagem=mensagem, corpo_json=corpo_json, proxima_tentativa_em=None)


def marcar_desfecho(conn, cadastro_id, versao, estado: str, *, alerta: str | None = None) -> None:
    _set(conn, cadastro_id, versao, estado=estado, alerta=alerta, proxima_tentativa_em=None)


def adiar(conn, cadastro_id, versao, *, ate: str, alerta: str | None = None) -> None:
    conn.execute(
        "UPDATE cadastro_volta SET tentativas = tentativas + 1, proxima_tentativa_em = ?, "
        "alerta = COALESCE(?, alerta), atualizado_em = datetime('now') "
        "WHERE cadastro_id = ? AND versao = ?",
        (ate, alerta, cadastro_id, versao),
    )


def _dicts(cur) -> list[dict]:
    nomes = [d[0] for d in cur.description]
    return [dict(zip(nomes, r, strict=True)) for r in cur.fetchall()]


def pendencias_de_envio(conn, agora_iso: str, limite: int = 20) -> list[dict]:
    return _dicts(conn.execute(
        f"SELECT * FROM cadastro_volta WHERE estado IN ({_PENDENTES_SQL}) "
        "AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= ?) "
        "ORDER BY atualizado_em LIMIT ?",
        (agora_iso, limite),
    ))


def tem_pendencia(conn) -> bool:
    return conn.execute(
        f"SELECT 1 FROM cadastro_volta WHERE estado IN ({_PENDENTES_SQL}) LIMIT 1"
    ).fetchone() is not None


def buscar(conn, cadastro_id, versao) -> dict | None:
    linhas = _dicts(conn.execute(
        "SELECT * FROM cadastro_volta WHERE cadastro_id = ? AND versao = ?", (cadastro_id, versao)
    ))
    return linhas[0] if linhas else None


def criar_do_produto(conn, produto_id: str) -> dict | None:
    linhas = _dicts(conn.execute(
        "SELECT * FROM cadastro_volta WHERE produto_id = ? AND acao = 'criar' "
        "AND estado IN ('em_andamento', 'gravado_sem_volta', 'confirmado', 'orfao') "
        "ORDER BY iniciado_em DESC, rowid DESC LIMIT 1",
        (produto_id,),
    ))
    return linhas[0] if linhas else None


def seq_criado_pelo_portal(conn, seq: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM cadastro_volta WHERE erp_produto_id = ? "
        "AND estado IN ('gravado_sem_volta', 'confirmado') LIMIT 1",
        (seq,),
    ).fetchone() is not None


def em_andamento(conn) -> list[dict]:
    return _dicts(conn.execute("SELECT * FROM cadastro_volta WHERE estado = 'em_andamento'"))
```

Remova a função `_agora` se não for usada (ela está aqui só como lembrete de que o relógio é do SQLite; o ruff aponta). `pendencias_de_envio` compara texto ISO: grave `proxima_tentativa_em` sempre como `YYYY-MM-DDTHH:MM:SS` em UTC (Task 7 faz isso).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_volta_repo.py tests/test_cadastro_volta.py tests/test_cadastro_volta_route.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/persistence/schema_env.py app/persistence/cadastro_volta_repo.py tests/test_cadastro_volta_repo.py
git commit -m "feat(cadastro): livro da volta v2 com estados de gravação, reenvio e produtoId"
```

---

### Task 5: `simular` lê sem reservar e confere a marca

> **Emenda 1 (revisão do Flow, 17/10).** `marca_ausente_erp` é erro **definitivo** (contrato §7; R4 revogada). Em `simular` isso não muda nada (o estado no livro já é `erro_definitivo`), mas o texto do plano e os comentários não devem dizer "transitório".

**Files:**
- Modify: `app/integrations/flowpcp/cadastro_volta.py`
- Test: `tests/test_cadastro_volta.py`

**Interfaces:**
- Consumes: `list_cadastros_pendentes(limite, *, acoes, simular)` (Task 3), `MarcaRef` (Task 1).
- Produces: `_marcas_do_fire(fire_conn) -> dict[int, str]` (código → nome aparado), `_SQL_MARCAS = "SELECT CODIGO, MARCA FROM PRODUTOS_MARCA"`; `_plano(item, marcas)` passa a incluir `"MARCA": <nome>` em `campos` quando `marcaErp` vem.

Regras: `simular` chama `list_cadastros_pendentes(limite=LIMITE_FILA, simular=True)`. Item com `marcaErp.codigo` fora de `PRODUTOS_MARCA` vira, no livro, `erro_definitivo` com código `marca_ausente_erp` e mensagem `marca código N não existe no ERP` (em simulação nada vai ao Flow). Como `simular=1` não reserva, a mesma fila volta a cada ciclo: o livro continua impedindo reprocessar o que já está lá (comportamento atual).

- [ ] **Step 1: Write the failing tests**

No dublê `_Fire` de `tests/test_cadastro_volta.py`, responda `PRODUTOS_MARCA`:

```python
    def responder(self, sql, params):
        if "RDB$DATABASE" in sql:
            return [(1,)]
        if "PRODUTOS_MARCA" in sql:
            return [(c, n) for c, n in self.marcas.items()]
        ...
```

com `self.marcas = marcas if marcas is not None else {17: "Kolosh"}` no `__init__`. E os testes:

```python
def test_simular_pede_fila_sem_reserva(monkeypatch, env_slug):
    monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:simular")
    cliente = _Client([BOM])
    cv.processar_ambiente(env_slug, _client=cliente, _fire_conn=_Fire())
    assert cliente.chamadas == [{"limite": 50, "simular": True}]


def test_simular_marca_inexistente_vai_ao_livro(monkeypatch, env_slug, env_conn):
    monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:simular")
    bruto = {**BOM, "marcaErp": {"codigo": 99}}
    cv.processar_ambiente(env_slug, _client=_Client([bruto]), _fire_conn=_Fire(), _env_conn=env_conn)
    linha = cadastro_volta_repo.buscar(env_conn, BOM["cadastroId"], 1)
    assert (linha["estado"], linha["erro_codigo"]) == ("erro_definitivo", "marca_ausente_erp")


def test_simular_plano_leva_nome_da_marca(monkeypatch, env_slug, env_conn):
    monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:simular")
    cv.processar_ambiente(env_slug, _client=_Client([BOM]), _fire_conn=_Fire(), _env_conn=env_conn)
    plano = json.loads(cadastro_volta_repo.buscar(env_conn, BOM["cadastroId"], 1)["plano_json"])
    assert plano["campos"]["MARCA"] == "Kolosh"
```

Use as fixtures que o arquivo já tem para slug e conexão do ambiente (`grep -n "def \|fixture" tests/test_cadastro_volta.py`); se os nomes forem outros, adapte. O dublê `_Client.list_cadastros_pendentes(self, limite=20, **kw)` guarda `{"limite": limite, **kw}` em `self.chamadas`.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_volta.py -v -k "simular"`
Expected: FAIL.

- [ ] **Step 3: Implement**

Em `cadastro_volta.py`:

```python
_SQL_MARCAS = "SELECT CODIGO, MARCA FROM PRODUTOS_MARCA"


def _marcas_do_fire(fire_conn) -> dict[int, str]:
    """PRODUTOS_MARCA inteira (16 linhas na MM em 17/10), uma vez por ciclo."""
    return {
        int(codigo): str(nome or "").strip()
        for codigo, nome in _select(fire_conn, _SQL_MARCAS, ())
        if codigo is not None
    }
```

Em `_contra_o_fire`, receba `marcas` e cheque antes dos componentes:

```python
    if item.marcaErp is not None and item.marcaErp.codigo not in marcas:
        return "marca_ausente_erp", f"marca código {item.marcaErp.codigo} não existe no ERP"
```

Em `_plano(item, marcas)`, depois de `campos["GRUPO"]`:

```python
    if item.marcaErp is not None:
        campos["MARCA"] = marcas[item.marcaErp.codigo]
```

Em `_processar_fila`, ao lado de `nomes = _nomes_do_fire(fire_conn)`, leia `marcas = _marcas_do_fire(fire_conn)` e passe adiante (`_decidir(bruto, env_conn, fire_conn, nomes, marcas)`). Em `_processar_ambiente`, a leitura passa a ser `client.list_cadastros_pendentes(limite=LIMITE_FILA, simular=True)` quando `m == "simular"` (a Task 7 cuida dos outros modos). Atualize o docstring do módulo: "simular lê com `simular=1` (sem reservar)".

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_volta.py tests/test_cadastro_volta_route.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/cadastro_volta.py tests/test_cadastro_volta.py
git commit -m "feat(cadastro): simular lê a fila sem reservar e confere a marca no Fire"
```

---

### Task 6: Gravador no Fire (`app/erp/cadastro_fire.py`)

> **Emenda 1 (revisão do Flow, 17/10; vale acima do texto abaixo onde divergir).**
> (a) I8: em `atualizar`, valor `None` em `campos` significa **limpar** a coluna (`SET COL = ?` com parâmetro `None`). Quem decide o que vai em `campos` é a Task 7. Teste: `atualizar(fire, seq=3990, campos={"CODIGO_EAN13": None}, componentes=None)` gera `("UPDATE PRODUTOS SET CODIGO_EAN13 = ? WHERE SEQ = ?", (None, 3990))`.
> (b) I4: acrescente `classificar_erro(exc: BaseException) -> Literal["colisao", "lock", "conexao", "recusa"]`, pura, pelo texto e pelo tipo da exceção do `fdb`: `colisao` = violação de chave (a regra de `_colisao`); `lock` = "deadlock", "lock conflict", "update conflicts"; `conexao` = `FirebirdConnectionError`, "network", "connection", "Error writing data to the connection", "Unable to complete network request"; o resto = `recusa` (NOT NULL, truncamento, conversão). `_colisao` passa a usar `classificar_erro`. Teste cada classe com uma exceção montada com o texto típico do Firebird 2.5 (`"violation of PRIMARY or UNIQUE KEY constraint"`, `"deadlock\nupdate conflicts with concurrent update"`, `"Unable to complete network request to host"`, `"validation error for column DESCRICAO, value \"*** null ***\""`).
> (c) Em `criar`, exceção que não é colisão sai depois do `rollback()` **sem** tentar de novo (já é assim), e a Task 7 classifica com `classificar_erro`.

**Files:**
- Create: `app/erp/cadastro_fire.py`
- Test: `tests/test_cadastro_fire.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Gravacao: seq: int; acao: str` (resultado de uma escrita)
  - `class SeqTomadoError(Exception)` (três tentativas de MAX+1 falharam)
  - `criar(conn, *, campos: dict[str, object], componentes: list[tuple[int, float]] | None, hoje: date, ao_reservar: Callable[[int], None]) -> Gravacao`
  - `atualizar(conn, *, seq: int, campos: dict[str, object], componentes: list[tuple[int, float]] | None) -> Gravacao`
  - `inativar(conn, *, seq: int) -> Gravacao`
  - `ler_produto(conn, seq: int) -> dict | None` (colunas de conferência: `SEQ, DESCRICAO, UNIDADE, GRUPO, CODIGO_EAN13, CLASSIF_FISCAL, DATA_CAD`)
  - `ler_composicao(conn, seq: int) -> list[tuple[int, float]]` (ordenada por filho)
  - ~~`buscar_por_nome`~~: substituída por `candidatos_por_data` (Task 8 rodada 2); ver `cadastro_fire.candidatos_por_data(conn, *, de, ate)`, por `DATA_CAD` e sem `GRUPO`
  - `COLUNAS_PERMITIDAS: frozenset[str]` e `PADROES_P0: dict[str, object]` (vazio no 3a; a Task 13 preenche)

Regras:
- `campos` usa nome de **coluna** do Fire. Qualquer chave fora de `COLUNAS_PERMITIDAS` levanta `ValueError` antes de abrir cursor. `COLUNAS_PERMITIDAS = {"DESCRICAO", "UNIDADE", "CODIGO_EAN13", "CLASSIF_FISCAL", "CODITEMSPED", "CODPARANFE", "CODPROD_ALTERN", "GRUPO", "MARCA"}`. `DESCRICAO2` **não** entra (R3).
- `criar`: sob `_lock` (módulo, `threading.Lock`), em até 3 tentativas: `SELECT MAX(SEQ) FROM PRODUTOS` → `seq = max+1` → `ao_reservar(seq)` (o chamador grava `seq_reservado` no livro **antes** do INSERT) → `INSERT INTO PRODUTOS (SEQ, BLOQUEADO, DATA_CAD, <PADROES_P0>, <campos>)` com `BLOQUEADO='Nao'`; kit/pack: para cada componente, `SELECT MAX(CODIGO) FROM PRODUTOS_KIT` e `INSERT INTO PRODUTOS_KIT (CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD)`; `commit()`. Violação de chave (mensagem do driver contém `PRIMARY` ou `UNIQUE` ou `violation`) → `rollback()` e nova tentativa. Outra exceção → `rollback()` e relança. Esgotou → `SeqTomadoError`.
- `atualizar`: `UPDATE PRODUTOS SET <campos> WHERE SEQ = ?` (se `campos` vazio, não roda UPDATE); `componentes` não-`None` → `DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?` + INSERTs como no `criar`; `commit()`. Linha não achada (`rowcount == 0` no UPDATE, ou `ler_produto` vazio quando não há campos) → `rollback()` e `LookupError`.
- `inativar`: `UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?`, `commit()`; `rowcount == 0` → `LookupError`.
- Nenhum `DELETE` em `PRODUTOS`, nunca.

- [ ] **Step 1: Write the failing tests**

```python
"""Gravador do cadastro no Fire: SQL parametrizado, uma transação por cadastro."""

from datetime import date

import pytest

from app.erp import cadastro_fire as cf


class _Cur:
    def __init__(self, fire):
        self.fire, self.rows, self.rowcount = fire, [], 0

    def execute(self, sql, params=()):
        self.fire.sqls.append((" ".join(sql.split()), tuple(params)))
        self.rows, self.rowcount = self.fire.responder(sql, tuple(params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Fire:
    def __init__(self, max_seq=3991, max_kit=900, colide=0, existe=True):
        self.max_seq, self.max_kit, self.colide, self.existe = max_seq, max_kit, colide, existe
        self.sqls, self.commits, self.rollbacks = [], 0, 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def responder(self, sql, params):
        if "MAX(SEQ)" in sql:
            return [(self.max_seq,)], 1
        if "MAX(CODIGO)" in sql:
            self.max_kit += 1
            return [(self.max_kit - 1,)], 1
        if sql.lstrip().upper().startswith("INSERT INTO PRODUTOS ") and self.colide:
            self.colide -= 1
            self.max_seq += 1
            raise Exception("violation of PRIMARY or UNIQUE KEY constraint")
        if sql.lstrip().upper().startswith("UPDATE"):
            return [], 1 if self.existe else 0
        return [], 1


CAMPOS = {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8, "MARCA": "Kolosh"}


def test_criar_reserva_antes_do_insert_e_commita():
    fire, reservas = _Fire(), []
    g = cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservas.append)
    assert g == cf.Gravacao(seq=3992, acao="criar")
    assert reservas == [3992]
    insert = [s for s in fire.sqls if s[0].startswith("INSERT INTO PRODUTOS ")][0]
    assert "?" in insert[0] and 3992 in insert[1] and "MEIA SOQUETE" in insert[1]
    assert fire.commits == 1 and fire.rollbacks == 0


def test_criar_colisao_tenta_de_novo_em_transacao_nova():
    fire, reservas = _Fire(colide=1), []
    g = cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=reservas.append)
    assert reservas == [3992, 3993] and g.seq == 3993
    assert fire.rollbacks == 1 and fire.commits == 1


def test_criar_desiste_depois_de_tres_colisoes():
    fire = _Fire(colide=3)
    with pytest.raises(cf.SeqTomadoError):
        cf.criar(fire, campos=CAMPOS, componentes=None, hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    assert fire.rollbacks == 3 and fire.commits == 0


def test_criar_kit_grava_composicao_na_mesma_transacao():
    fire = _Fire()
    cf.criar(fire, campos=CAMPOS, componentes=[(3170, 3.0)], hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    kits = [s for s in fire.sqls if s[0].startswith("INSERT INTO PRODUTOS_KIT")]
    assert kits[0][1] == (900, 3992, 3170, 3.0)
    assert fire.commits == 1


def test_coluna_fora_da_lista_e_recusada_antes_do_banco():
    fire = _Fire()
    with pytest.raises(ValueError):
        cf.criar(fire, campos={**CAMPOS, "DESCRICAO2": "x"}, componentes=None,
                 hoje=date(2026, 10, 17), ao_reservar=lambda s: None)
    assert fire.sqls == []


def test_atualizar_sem_componentes_nao_toca_na_composicao():
    fire = _Fire()
    cf.atualizar(fire, seq=3990, campos={"UNIDADE": "KIT"}, componentes=None)
    assert not any("PRODUTOS_KIT" in s[0] for s in fire.sqls)
    assert fire.sqls[0] == ("UPDATE PRODUTOS SET UNIDADE = ? WHERE SEQ = ?", ("KIT", 3990))


def test_atualizar_troca_composicao_so_do_pai():
    fire = _Fire()
    cf.atualizar(fire, seq=3990, campos={}, componentes=[(3170, 2.0)])
    assert ("DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?", (3990,)) in fire.sqls


def test_atualizar_seq_inexistente():
    fire = _Fire(existe=False)
    with pytest.raises(LookupError):
        cf.atualizar(fire, seq=3990, campos={"UNIDADE": "KIT"}, componentes=None)
    assert fire.rollbacks == 1


def test_inativar_so_bloqueia():
    fire = _Fire()
    cf.inativar(fire, seq=3990)
    assert fire.sqls == [("UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?", (3990,))]


def test_nenhum_delete_em_produtos():
    import inspect
    fonte = inspect.getsource(cf)
    assert "DELETE FROM PRODUTOS " not in fonte and "DELETE FROM PRODUTOS\n" not in fonte
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_fire.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
"""Única porta de ESCRITA do cadastro de produto no Fire (volta Flow -> Fire).

Uma transação por cadastro, SQL sempre parametrizado, nunca DELETE em PRODUTOS
(só a composição do pai em PRODUTOS_KIT é trocada). `PRODUTOS.SEQ` e
`PRODUTOS_KIT.CODIGO` não têm gerador: MAX+1 sob lock, e colisão com a tela do
Fire tenta de novo em transação nova (spec 2026-10-16 §6.3).

`PADROES_P0` são as colunas que a tela do Fire grava num cadastro novo e que o
Flow não manda. Fica vazio até a P0 (diff numa cópia do .fdb); até lá os modos
de gravação só rodam em ambiente liberado por `PORTAL_FLOWPCP_CADASTRO_P0`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

COLUNAS_PERMITIDAS = frozenset(
    {"DESCRICAO", "UNIDADE", "CODIGO_EAN13", "CLASSIF_FISCAL", "CODITEMSPED",
     "CODPARANFE", "CODPROD_ALTERN", "GRUPO", "MARCA"}
)
PADROES_P0: dict[str, object] = {}
TENTATIVAS_SEQ = 3
_lock = threading.Lock()


@dataclass(frozen=True)
class Gravacao:
    seq: int
    acao: str


class SeqTomadoError(Exception):
    """MAX+1 colidiu TENTATIVAS_SEQ vezes seguidas."""


def _checa_colunas(campos: dict) -> None:
    fora = set(campos) - COLUNAS_PERMITIDAS
    if fora:
        raise ValueError(f"coluna fora da lista do gravador: {sorted(fora)}")


def _um(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    linha = cur.fetchone()
    return linha[0] if linha else None


def _colisao(exc: Exception) -> bool:
    texto = str(exc).lower()
    return "violation" in texto or "primary" in texto or "unique" in texto


def _insere_composicao(cur, pai: int, componentes: list[tuple[int, float]]) -> None:
    for filho, qtd in componentes:
        codigo = int(_um(cur, "SELECT MAX(CODIGO) FROM PRODUTOS_KIT") or 0) + 1
        cur.execute(
            "INSERT INTO PRODUTOS_KIT (CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD) VALUES (?, ?, ?, ?)",
            (codigo, pai, filho, qtd),
        )


def criar(conn, *, campos, componentes, hoje: date, ao_reservar: Callable[[int], None]) -> Gravacao:
    _checa_colunas(campos)
    linha = {**PADROES_P0, **campos}
    with _lock:
        for _ in range(TENTATIVAS_SEQ):
            cur = conn.cursor()
            try:
                seq = int(_um(cur, "SELECT MAX(SEQ) FROM PRODUTOS") or 0) + 1
                ao_reservar(seq)
                colunas = ["SEQ", "BLOQUEADO", "DATA_CAD", *linha]
                valores = (seq, "Nao", hoje, *linha.values())
                cur.execute(
                    f"INSERT INTO PRODUTOS ({', '.join(colunas)}) "
                    f"VALUES ({', '.join('?' for _ in colunas)})",
                    valores,
                )
                if componentes:
                    _insere_composicao(cur, seq, componentes)
                conn.commit()
                return Gravacao(seq=seq, acao="criar")
            except Exception as exc:
                conn.rollback()
                if not _colisao(exc):
                    raise
            finally:
                cur.close()
    raise SeqTomadoError(f"SEQ tomado {TENTATIVAS_SEQ} vezes seguidas")


def atualizar(conn, *, seq: int, campos, componentes) -> Gravacao:
    _checa_colunas(campos)
    cur = conn.cursor()
    try:
        if campos:
            sets = ", ".join(f"{c} = ?" for c in campos)
            cur.execute(f"UPDATE PRODUTOS SET {sets} WHERE SEQ = ?", (*campos.values(), seq))
            if cur.rowcount == 0:
                raise LookupError(f"SEQ {seq} não existe no Fire")
        elif _um(cur, "SELECT SEQ FROM PRODUTOS WHERE SEQ = ?", (seq,)) is None:
            raise LookupError(f"SEQ {seq} não existe no Fire")
        if componentes is not None:
            cur.execute("DELETE FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ?", (seq,))
            _insere_composicao(cur, seq, componentes)
        conn.commit()
        return Gravacao(seq=seq, acao="atualizar")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def inativar(conn, *, seq: int) -> Gravacao:
    cur = conn.cursor()
    try:
        cur.execute("UPDATE PRODUTOS SET BLOQUEADO = 'Sim' WHERE SEQ = ?", (seq,))
        if cur.rowcount == 0:
            raise LookupError(f"SEQ {seq} não existe no Fire")
        conn.commit()
        return Gravacao(seq=seq, acao="inativar")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


_CONFERENCIA = ("SEQ", "DESCRICAO", "UNIDADE", "GRUPO", "CODIGO_EAN13", "CLASSIF_FISCAL", "DATA_CAD")


def ler_produto(conn, seq: int) -> dict | None:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {', '.join(_CONFERENCIA)} FROM PRODUTOS WHERE SEQ = ?", (seq,))
        linha = cur.fetchone()
        return dict(zip(_CONFERENCIA, linha, strict=True)) if linha else None
    finally:
        cur.close()


def ler_composicao(conn, seq: int) -> list[tuple[int, float]]:
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT CODPRODUTO, QTD FROM PRODUTOS_KIT WHERE CODPRODUTO_PAI = ? ORDER BY CODPRODUTO",
            (seq,),
        )
        return [(int(f), float(q)) for f, q in cur.fetchall()]
    finally:
        cur.close()


# `buscar_por_nome`: substituída por `candidatos_por_data` (Task 8 rodada 2).
```

Em `test_criar_colisao_tenta_de_novo_em_transacao_nova`, o dublê aumenta `max_seq` na colisão para simular a tela do Fire ocupando o SEQ.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_fire.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/erp/cadastro_fire.py tests/test_cadastro_fire.py
git commit -m "feat(erp): gravador do cadastro no Fire (criar, atualizar, inativar)"
```

---

### Task 7: Modos de gravação — ciclo, regras de ouro e reenvio

> **Emenda 1 (revisão do Flow, 17/10; VALE ACIMA do fluxo abaixo onde divergir).** Ordem nova do ciclo de gravação:
> 1. **`reenviar_pendencias` primeiro, sempre**, antes da checagem da P0 (I5). Sem P0, o ciclo reenvia as pendências e só então recusa o resto.
> 2. Checa a P0 (recusa como antes).
> 3. GET (como antes). Em `criar`, o Flow (rev. 4) vai mandar também `atualizar` derivado de `criar`: **trate pela `acao` do item** — `atualizar` que chega no modo `criar` é processado como `atualizar` (mesmo escopo, mesmo gravador). `inativar` que chegue sem o portão próprio: não processa, não responde, WARNING (volta depois da reserva).
> 4. **Triagem sem o ERP, para todos os itens, antes de abrir o ERP** (C1): validação, livro (`gravado_sem_volta`/`erro_a_enviar` → só reenvia o corpo guardado; `em_andamento`/`incerto`/`orfao`/`confirmado`/... → pula), 9b, escopo. **Emenda 5 (revisão da T7):** o `reservar` sai da triagem e acontece no início da gravação de cada item (antes de qualquer comando ao ERP), para o item cortado pelo prazo do lote ficar fora do livro; `orfao_fechado` conta como `orfao` na 9b. Só os itens que saem da triagem como "a gravar" seguem.
> 5. Abre o ERP se houver item "a gravar" **ou** linha `em_andamento` (M2). Falha de conexão: `transitorio` `conexao_recusada` **só** para os itens "a gravar" desta rodada (linhas recém-reservadas, nunca uma linha que já estava gravada).
> 6. Recuperação (Task 8) e depois a gravação item a item, com o prazo do lote.
>
> Mudanças de regra:
> - **9b com `incerto` ou `liberado` com SEQ** (I2): não grava, não responde a esta versão, ERROR com o alerta (uma pessoa resolve pela ferramenta da Task 15). **9b com `orfao`**: responde a esta versão com erro **definitivo** `ja_criado_no_erp`, mensagem `mensagem_para_flow("ja_criado_no_erp", detalhe=<código do ERP>)`, e não grava (senão ela volta a cada 10 min para sempre). **9b com `confirmado`**: pula com ERROR, como antes.
> - **`nome_existe` sobre o próprio produto** (I3): se o SEQ achado pelo nome é `seq_reservado` ou `erp_produto_id` de alguma linha do livro com o mesmo `produto_id`, **não** reporta `nome_existe`: chama a recuperação dessa linha (adota se bater; senão `incerto`).
> - **Erro do ERP na gravação** (I4), depois do `rollback()` do gravador (todo `marcar_erro_a_enviar` depois de rollback do gravador passa `rollback_comprovado=True`; erro de validação antes do ERP também, pois não há SEQ; `ReservaFalhouError` e `conexao` depois do comando ficam `em_andamento`): `classificar_erro` = `lock` → `transitorio` `erp_ocupado` ("O ERP estava ocupado; o Portal tenta de novo."); `recusa` → definitivo `erp_recusou` com `mensagem_para_flow("erp_recusou")` (o detalhe técnico, sem valor, vai só para o log e o livro); `conexao` depois de enviar o comando → deixa `em_andamento` e interrompe o lote (resultado incerto, a recuperação confere). `SeqTomadoError` → `transitorio` `erp_ocupado` (não `liberado`).
> - **`marca_ausente_erp` é definitivo** (R4 revogada; contrato §7).
> - **`atualizar` limpa** (I8): `ean` e `codigoNfe` `null` no item = coluna `NULL` no ERP (`campos` leva a chave com `None`); os demais campos `null` = não mexer. `marcaErp: null` = não mexer.
> - **Chave desconhecida no item** (I7): o item valida (Task 1); loga WARNING uma vez por nome de chave com `chaves_desconhecidas`.
> - **Mensagem ao Flow** (M4): toda mensagem de erro que vai ao Flow sai de `mensagem_para_flow` (ou `mensagem_nome_existe`). O texto técnico do validador fica só no livro e no log.
> - **Dois itens com o mesmo nome no lote** (M6): depois de cada `criar` gravado, acrescente o nome normalizado ao mapa de nomes do ciclo; o segundo vira `nome_existe` com o SEQ do primeiro.
> - **Reenvio respeita a espera** (M8): `pendencias_de_envio` já filtra; o reenvio da triagem (item reentregue com `gravado_sem_volta`) também respeita `proxima_tentativa_em` (se ainda não venceu, não manda).
>
> Testes que entram (além dos do texto abaixo, ajustando o de `marca_ausente` para definitivo e o de `SeqTomado`):
> - `test_erp_fora_com_item_gravado_sem_volta_reentregue_so_reenvia_o_gravado` (C1): livro com a linha em `gravado_sem_volta` vencida, Flow reentrega o mesmo item, conexão do ERP falha → um único POST, com o corpo `gravado`; estado continua `gravado_sem_volta` ou vira `confirmado`; nenhum `transitorio`.
> - `test_bug_corpo_mantem_gravado_sem_volta` (M7).
> - `test_pendencias_antes_da_p0` (I5): sem `VAR_P0`, com pendência vencida → 1 POST e `recusado` no resumo, sem GET.
> - `test_9b_orfao_responde_ja_criado_no_erp`, `test_9b_incerto_nao_grava_nem_responde`.
> - `test_nome_existe_do_proprio_produto_vai_para_recuperacao`.
> - `test_erro_recusa_do_erp_vira_definitivo_erp_recusou`, `test_lock_do_erp_vira_transitorio`.
> - `test_atualizar_ean_null_limpa_no_erp`.
> - `test_atualizar_no_modo_criar_e_processado`.
> - `test_mesmo_nome_duas_vezes_no_lote`.
> - `test_mensagem_ao_flow_sem_jargao`: em todos os POSTs de erro dos testes deste arquivo, a mensagem não contém "Fire", "SEQ", "erpProdutoId" nem "componentes[".

**Files:**
- Modify: `app/integrations/flowpcp/cadastro_volta.py`
- Test: `tests/test_cadastro_volta_gravar.py` (novo)

**Interfaces:**
- Consumes: Tasks 1 a 6 (`validar_item`, `corpo_gravado`, `corpo_erro`, `mensagem_nome_existe`, `ler_resposta`, `Desfecho`, `list_cadastros_pendentes`, `post_cadastro_resultado`, funções do livro, `cadastro_fire.*`).
- Produces:
  - `VAR_P0 = "PORTAL_FLOWPCP_CADASTRO_P0"`, `VAR_INATIVAR = "PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR"`, `p0_ok(slug) -> bool`, `inativar_ligado(slug) -> bool`
  - `LIMITE_GRAVACAO = 20`, `PRAZO_LOTE = timedelta(minutes=8)`, `LIMITE_PENDENCIAS = 20`
  - `reenviar_pendencias(slug, env_conn, client, *, agora: datetime) -> dict` (contagens por desfecho)
  - `_processar_gravacao(slug, m, client, env_conn, fire_conn, *, relogio: Callable[[], datetime]) -> dict`
  - `processar_ambiente(slug, *, _client=None, _fire_conn=None, _env_conn=None, _relogio=None)`

Fluxo de um ciclo num modo de gravação (`criar` ou `criar_e_atualizar`):

1. Recusa iniciar (resumo `recusado`, ERROR uma vez por motivo) se o slug não está em `PORTAL_FLOWPCP_CADASTRO_P0`.
2. `reenviar_pendencias` (também roda em `simular` e `desligado`, ver passo 9).
3. `GET` com `acoes=("criar",)` em `criar`; `("criar","atualizar")` em `criar_e_atualizar`, mais `"inativar"` se `inativar_ligado(slug)`. `limite=LIMITE_GRAVACAO`. Sem `cadastros.v1` nas capacidades: nada é processado (resumo `capacidade_ok=False`), mesmo que venham itens.
4. Abre o Fire **de escrita** (`FirebirdConnection().connect_with_config`) só se houver item a processar. Falha ao conectar: para cada item válido reservado neste GET, envia `corpo_erro(versao, "transitorio", "conexao_recusada", "O ERP recusou a conexão")` e registra o desfecho; nada mais.
5. Lê nomes e marcas do Fire uma vez (`_nomes_do_fire`, `_marcas_do_fire`).
6. Para cada item, na ordem do GET, enquanto `relogio() - inicio < PRAZO_LOTE`:
   - inválido (`validar_item`) e com `cadastroId`/`versao` legíveis → `erro_a_enviar` definitivo com o código e a mensagem do validador; sem id legível → só log (não há para quem responder).
   - `tipo == "material"` → `erro_a_enviar` definitivo `tipo_nao_suportado`, mensagem `material ainda não é gravado pelo Portal` (o Flow não emite material hoje; o schema aceita).
   - já no livro: `gravado_sem_volta`/`erro_a_enviar` → reenvia o corpo guardado (regra 1), sem Fire; `em_andamento` → pula (a Task 8 resolve); `incerto`/`orfao`/`confirmado`/`erro_registrado`/`superado` → pula com WARNING.
   - `acao == "criar"` e `criar_do_produto(produto_id)` acha outra linha (9b): `gravado_sem_volta` → reenvia o corpo **dela** (a v1) e não responde à v2; `em_andamento` → pula; `confirmado`/`orfao` → pula com ERROR (o Flow não deveria mandar `criar`).
   - `atualizar`/`inativar`: escopo = `catalogo_fire` **ou** `seq_criado_pelo_portal` (R7). Fora → `erro_a_enviar` definitivo `seq_fora_do_escopo`.
   - `reservar(...)` falhou → pula (outro ciclo é dono).
   - conferência contra o Fire (nome existe, marca, componentes, como `_contra_o_fire`): falha → `erro_a_enviar`. `nome_existe` usa `mensagem_nome_existe(seq)`; `marca_ausente_erp` vai como **transitório** (R4); `componente_invalido` vira `componente_ausente` (não existe) ou `componente_bloqueado`.
   - grava: `cadastro_fire.criar(..., ao_reservar=lambda s: (marcar_seq(...), env_conn.commit()))` / `atualizar` / `inativar`. O `env_conn.commit()` dentro de `ao_reservar` é obrigatório: o `seq_reservado` tem de estar no disco antes do INSERT.
   - sucesso → `marcar_gravado(erp_produto_id=str(seq), corpo_json=json.dumps(corpo_gravado(versao, str(seq), relogio())))` + `env_conn.commit()` → POST.
   - `LookupError` (SEQ sumiu) → `erro_a_enviar` definitivo `seq_inexistente`. `SeqTomadoError` → `transitorio` `erp_ocupado` (Emenda 1, I4), com `rollback_comprovado=True`. Erro de conexão no meio → deixa `em_andamento` (resultado incerto; a Task 8 confere) e interrompe o lote.
7. Cada POST passa por `ler_resposta` e vira estado: `CONFIRMADO`→`confirmado`; `ERRO_REGISTRADO`→`erro_registrado`; `TRANSITORIO_REGISTRADO`→`transitorio`; `ORFAO`→`orfao` + ERROR com o SEQ; `SUPERADO`→`superado`; `REPETIR`→`adiar` (espera `min(2**tentativas, 60)` min; em 429 o maior entre isso e `Retry-After`); `BUG_CORPO`→`adiar` 60 min + `alerta="bug_corpo"` + ERROR; `CREDENCIAL`→`adiar` 5 min + ERROR e para os POSTs do ciclo.
8. `env_conn.commit()` depois de cada item.
9. `rodar()` passa a visitar **todo** ambiente FlowPCP habilitado que tenha `tem_pendencia` (R6), qualquer que seja o modo, e chama só `reenviar_pendencias` quando o modo é `desligado` ou `simular`. Com a variável de modo vazia e sem pendência em nenhum ambiente, continua não abrindo token.

- [ ] **Step 1: Write the failing tests**

Crie `tests/test_cadastro_volta_gravar.py` com dublês próprios (não reaproveite o `_Fire` só-leitura do outro arquivo):

```python
"""Volta Flow -> Fire nos modos de gravação. Flow e Fire falsos; SQLite do ambiente real."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.integrations.flowpcp import cadastro_volta as cv
from app.integrations.flowpcp.cadastro_schema import PendentesResposta
from app.integrations.flowpcp.client import RespostaResultado
from app.persistence import cadastro_volta_repo as repo

PROD = "77777777-7777-4777-8777-777777777771"
PAR = {
    "cadastroId": "22222222-2222-4222-8222-222222222222", "produtoId": PROD, "versao": 1,
    "acao": "criar", "erpProdutoId": None, "tipo": "simples", "nome": "MEIA SOQUETE",
    "unidadeErp": "PAR", "ean": None, "ncm": "61159500", "tipoItemSped": 3,
    "grupoErp": {"codigo": "8"}, "codigoNfe": None, "cor": None, "componentes": None,
    "marcaErp": {"codigo": 17}, "codigoMes": None, "aprovadoEm": "2026-10-17T13:00:00.123Z",
}


class Flow:
    def __init__(self, itens, caps=("cadastros.v1",), respostas=None):
        self.itens, self.caps = itens, list(caps)
        self.respostas = list(respostas or [])
        self.gets, self.posts = [], []

    def list_cadastros_pendentes(self, limite=20, **kw):
        self.gets.append({"limite": limite, **kw})
        return PendentesResposta(cadastros=self.itens, capacidades=self.caps)

    def post_cadastro_resultado(self, cadastro_id, corpo):
        self.posts.append((cadastro_id, corpo))
        if self.respostas:
            return self.respostas.pop(0)
        status = "gravado" if corpo["resultado"] == "gravado" else "erro_registrado"
        return RespostaResultado(200, {"status": status}, None)

    def close(self):
        pass


class FireGravavel:
    """Fire em memória: PRODUTOS {seq: dict}, PRODUTOS_KIT, PRODUTOS_MARCA."""

    def __init__(self, produtos=None, marcas=None, lento=timedelta(0), relogio=None):
        self.produtos = produtos if produtos is not None else {3170: {"DESCRICAO": "PAR BASE", "BLOQUEADO": "Nao"}}
        self.marcas = marcas if marcas is not None else {17: "Kolosh"}
        self.kit, self.commits, self.escritas = [], 0, []
        self.lento, self.relogio = lento, relogio

    # implemente cursor()/commit()/rollback() respondendo:
    #   SELECT SEQ, DESCRICAO FROM PRODUTOS        -> nomes
    #   SELECT CODIGO, MARCA FROM PRODUTOS_MARCA   -> marcas
    #   SELECT SEQ, BLOQUEADO FROM PRODUTOS WHERE SEQ = ?
    #   SELECT MAX(SEQ) / MAX(CODIGO)
    #   INSERT/UPDATE: guarda em self.produtos / self.kit e anota em self.escritas;
    #   se self.lento, avança self.relogio.agora em self.lento a cada INSERT.
    ...


class Relogio:
    def __init__(self):
        self.agora = datetime(2026, 10, 17, 13, 0, tzinfo=UTC)

    def __call__(self):
        return self.agora


@pytest.fixture
def modo(monkeypatch, env_slug):
    def _set(m, p0=True, inativar=False):
        monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:{m}")
        if p0:
            monkeypatch.setenv(cv.VAR_P0, env_slug)
        if inativar:
            monkeypatch.setenv(cv.VAR_INATIVAR, env_slug)
    return _set


def _rodar(env_slug, flow, fire, env_conn, relogio=None):
    return cv.processar_ambiente(env_slug, _client=flow, _fire_conn=fire, _env_conn=env_conn,
                                 _relogio=relogio or Relogio())


def test_sem_p0_recusa_iniciar(modo, env_slug, env_conn):
    modo("criar", p0=False)
    flow = Flow([PAR])
    r = _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert "recusado" in r and flow.gets == []


def test_criar_grava_e_confirma(modo, env_slug, env_conn):
    modo("criar")
    flow, fire = Flow([PAR]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.gets[0]["acoes"] == ("criar",)
    assert 3171 in fire.produtos
    cid, corpo = flow.posts[0]
    assert corpo["erpProdutoId"] == "3171" and corpo["gravadoEm"].endswith("Z")
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "confirmado"


def test_sem_capacidade_nao_grava(modo, env_slug, env_conn):
    modo("criar")
    flow, fire = Flow([PAR], caps=()), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert fire.escritas == [] and flow.posts == []


def test_criar_reprocessa_item_simulado(modo, env_slug, env_conn):
    repo.registrar(env_conn, cadastro_id=PAR["cadastroId"], versao=1, estado="simulado",
                   acao="criar", tipo="simples", erro_codigo=None, erro_mensagem=None, plano_json="{}")
    modo("criar")
    flow, fire = Flow([PAR]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert 3171 in fire.produtos


def test_post_perdido_vira_gravado_sem_volta_e_reenvia_sem_fire(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(None, None, None)])
    fire = FireGravavel()
    _rodar(env_slug, flow, fire, env_conn)
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "gravado_sem_volta"
    escritas = len(fire.escritas)
    relogio = Relogio()
    relogio.agora += timedelta(hours=2)
    flow2 = Flow([])
    _rodar(env_slug, flow2, fire, env_conn, relogio)
    assert flow2.posts[0][1]["erpProdutoId"] == "3171"
    assert len(fire.escritas) == escritas
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "confirmado"


def test_pendencia_reenviada_com_modo_desligado(modo, monkeypatch, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(503, None, None)]), FireGravavel(), env_conn)
    monkeypatch.setenv(cv.VAR_MODO, f"{env_slug}:desligado")
    relogio = Relogio()
    relogio.agora += timedelta(hours=2)
    flow2 = Flow([])
    cv.processar_ambiente(env_slug, _client=flow2, _fire_conn=None, _env_conn=env_conn, _relogio=relogio)
    assert len(flow2.posts) == 1 and flow2.gets == []


def test_9b_criar_repetido_reenvia_v1_e_cala_v2(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR], respostas=[RespostaResultado(None, None, None)]), FireGravavel(), env_conn)
    v2 = {**PAR, "cadastroId": "33333333-3333-4333-8333-333333333333", "versao": 2}
    relogio = Relogio()
    relogio.agora += timedelta(hours=2)
    flow, fire = Flow([v2]), FireGravavel()
    _rodar(env_slug, flow, fire, env_conn, relogio)
    assert [p[0] for p in flow.posts] == [PAR["cadastroId"]]
    assert fire.escritas == []
    assert repo.buscar(env_conn, v2["cadastroId"], 2) is None


def test_nome_existe_vai_com_codigo_na_mensagem(modo, env_slug, env_conn):
    modo("criar")
    fire = FireGravavel(produtos={3100: {"DESCRICAO": "meia soquete", "BLOQUEADO": "Nao"}})
    flow = Flow([PAR])
    _rodar(env_slug, flow, fire, env_conn)
    erro = flow.posts[0][1]["erro"]
    assert erro == {"tipo": "definitivo", "codigo": "nome_existe",
                    "mensagem": "Já existe produto com esse nome no ERP (código 3100)"}


def test_marca_ausente_vai_como_transitorio(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([{**PAR, "marcaErp": {"codigo": 99}}],
                respostas=[RespostaResultado(200, {"status": "transitorio_registrado", "tentativas": 1}, None)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert flow.posts[0][1]["erro"]["tipo"] == "transitorio"
    assert flow.posts[0][1]["erro"]["codigo"] == "marca_ausente_erp"
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "transitorio"


def test_orfao_em_conflito_codigo(modo, env_slug, env_conn):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(409, {"error": "conflito_codigo"}, None)])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert repo.buscar(env_conn, PAR["cadastroId"], 1)["estado"] == "orfao"


def test_atualizar_de_seq_criado_pelo_portal_esta_no_escopo(modo, env_slug, env_conn):
    modo("criar")
    _rodar(env_slug, Flow([PAR]), FireGravavel(), env_conn)
    modo("criar_e_atualizar")
    fire = FireGravavel(produtos={3171: {"DESCRICAO": "MEIA SOQUETE", "BLOQUEADO": "Nao"}})
    atu = {**PAR, "cadastroId": "44444444-4444-4444-8444-444444444444", "versao": 2,
           "acao": "atualizar", "erpProdutoId": "3171", "marcaErp": None, "unidadeErp": "PR"}
    flow = Flow([atu])
    _rodar(env_slug, flow, fire, env_conn)
    assert flow.posts[0][1]["resultado"] == "gravado"
    assert fire.produtos[3171]["UNIDADE"] == "PR"


def test_inativar_so_com_portao(modo, env_slug, env_conn):
    modo("criar_e_atualizar")
    flow = Flow([])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert flow.gets[0]["acoes"] == ("criar", "atualizar")
    modo("criar_e_atualizar", inativar=True)
    flow = Flow([])
    _rodar(env_slug, flow, FireGravavel(), env_conn)
    assert flow.gets[0]["acoes"] == ("criar", "atualizar", "inativar")


def test_prazo_do_lote_para_de_iniciar_itens(modo, env_slug, env_conn):
    modo("criar")
    relogio = Relogio()
    itens = [
        {**PAR, "cadastroId": f"5555555{i}-5555-4555-8555-555555555555",
         "produtoId": f"6666666{i}-6666-4666-8666-666666666666", "nome": f"MEIA {i}"}
        for i in range(3)
    ]
    fire = FireGravavel(lento=timedelta(minutes=5), relogio=relogio)
    flow = Flow(itens)
    _rodar(env_slug, flow, fire, env_conn, relogio)
    assert len(flow.posts) == 2  # o 3º começaria depois de 10 min do GET
    assert repo.buscar(env_conn, itens[2]["cadastroId"], 1) is None


def test_fire_fora_responde_transitorio(modo, env_slug, env_conn, monkeypatch):
    modo("criar")
    flow = Flow([PAR], respostas=[RespostaResultado(200, {"status": "transitorio_registrado", "tentativas": 1}, None)])

    def _falha(*a, **k):
        raise cv.FirebirdConnectionError("recusou")

    monkeypatch.setattr(cv, "_abrir_fire_escrita", _falha)
    cv.processar_ambiente(env_slug, _client=flow, _fire_conn=None, _env_conn=env_conn, _relogio=Relogio())
    assert flow.posts[0][1]["erro"]["codigo"] == "conexao_recusada"
```

No teste do prazo: com `lento=5 min` por INSERT, o item 1 termina em 13:05, o item 2 começa às 13:05 e termina às 13:10; o 3º veria `13:10 - 13:00 = 10 min >= 8 min` e não começa. Ajuste `lento` se o dublê avançar o relógio em outro ponto, mantendo a asserção: **2 posts e o 3º fora do livro**.

O `FireGravavel` fica no arquivo de teste e precisa ser completo (o comentário acima diz o que ele responde); escreva-o inteiro.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_volta_gravar.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Em `cadastro_volta.py`: importe `FirebirdConnectionError` de `app.erp.exceptions` (o teste do Fire fora usa `cv.FirebirdConnectionError`) e `cadastro_fire` de `app.erp`; remova `_RECUSA_ESCRITA` e o retorno antecipado `if m != "simular"`. A abertura do Fire de escrita fica num `try` em volta de `stack.enter_context(_abrir_fire_escrita(slug))`. Estruture assim (mantenha o caminho `simular` atual intacto, só com o GET da Task 5):

```python
VAR_P0 = "PORTAL_FLOWPCP_CADASTRO_P0"
VAR_INATIVAR = "PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR"
LIMITE_GRAVACAO = 20
LIMITE_PENDENCIAS = 20
PRAZO_LOTE = timedelta(minutes=8)  # a reserva do Flow dura 10 min (regra de ouro 4)
_RECUSA_P0 = f"gravação exige a P0 concluída neste ambiente ({VAR_P0})"


def _lista_var(nome: str) -> set[str]:
    return {s.strip().lower() for s in os.environ.get(nome, "").split(",") if s.strip()}


def p0_ok(slug: str) -> bool:
    return slug.lower() in _lista_var(VAR_P0)


def inativar_ligado(slug: str) -> bool:
    return slug.lower() in _lista_var(VAR_INATIVAR)


def _acoes(slug: str, m: str) -> tuple[str, ...]:
    if m == "criar":
        return ("criar",)
    return ("criar", "atualizar", "inativar") if inativar_ligado(slug) else ("criar", "atualizar")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _espera(tentativas: int, retry_after: int | None) -> timedelta:
    base = timedelta(minutes=min(2 ** max(tentativas, 0), 60))
    return max(base, timedelta(seconds=retry_after or 0))


def _abrir_fire_escrita(slug: str):
    env = environments_repo.get_by_slug(slug)
    return FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))
```

`_enviar(slug, env_conn, client, linha_ou_item, corpo, *, agora) -> Desfecho` faz o POST, chama `ler_resposta(corpo["resultado"], r.status_http, r.corpo)` e aplica a tabela do passo 7 com `repo.marcar_desfecho`/`repo.adiar` (`ate=_iso(agora + _espera(tentativas, r.retry_after))`), loga ERROR em `ORFAO`, `BUG_CORPO` e `CREDENCIAL` com `cadastro_id`, `versao` e (no órfão) o SEQ, e faz `env_conn.commit()`.

`reenviar_pendencias(slug, env_conn, client, *, agora)` percorre `repo.pendencias_de_envio(env_conn, _iso(agora), LIMITE_PENDENCIAS)`, chama `_enviar` com `json.loads(linha["corpo_json"])` e para no primeiro `CREDENCIAL`.

`_processar_gravacao` implementa os passos 3 a 8. Os erros vão por `_erro(env_conn, item, tipo, codigo, mensagem)`, que monta `corpo_erro(...)`, chama `repo.marcar_erro_a_enviar(...)` (depois de `reservar`, para ter a linha) e envia. Item inválido sem `reservar` possível (sem `produtoId` legível) usa `repo.registrar(..., estado="erro_a_enviar")` e grava `corpo_json` com `repo._set`-equivalente: acrescente ao repo `registrar_erro_a_enviar(conn, cadastro_id, versao, corpo_json, codigo, mensagem) -> bool` com `INSERT OR IGNORE` e teste-o junto (um teste em `tests/test_cadastro_volta_repo.py`).

O mapeamento wire → coluna para o gravador sai de `COLUNAS` (sem `cor`, R3) mais `GRUPO` (int) e `MARCA` (nome de `_marcas_do_fire`). Em `atualizar`, só os campos não-`None` do item; `marcaErp: None` = não mexe em `MARCA`. Componentes: `[(int(c.erpProdutoId), c.quantidade) for c in item.componentes]` ou `None`.

`processar_ambiente` ganha `_relogio` (padrão `lambda: datetime.now(UTC)`) e, depois de resolver o modo: `desligado`/`simular` com pendência → `reenviar_pendencias`; `simular` → caminho atual; gravação → `p0_ok` → `reenviar_pendencias` → `_processar_gravacao`. `rodar()` passa a montar a lista de ambientes assim: com `_entradas()` vazio, percorre `enabled_flowpcp_envs()` e só segue nos que têm `tem_pendencia` (abrindo o SQLite do ambiente, sem token); com entradas, como hoje mais os que têm pendência.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_volta_gravar.py tests/test_cadastro_volta.py tests/test_cadastro_volta_repo.py -v`
Expected: PASS. O teste antigo que esperava `recusado` para `criar` em `tests/test_cadastro_volta.py` passa a configurar `VAR_P0` ausente e continua esperando `recusado` (agora com `_RECUSA_P0`).

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/cadastro_volta.py app/persistence/cadastro_volta_repo.py tests/test_cadastro_volta_gravar.py tests/test_cadastro_volta.py tests/test_cadastro_volta_repo.py
git commit -m "feat(cadastro): modos de gravação da volta com livro idempotente e reenvio do resultado"
```

---

### Task 8: Recuperação de item interrompido e `incerto`

> **Emenda 3 (revisão da Task 4, 17/10).** Quem libera uma linha com `seq_reservado` ("existe e é diferente" ou "não existe") **zera o `seq_reservado` na mesma transição**, porque a ausência do nosso produto naquele SEQ está comprovada. O repositório recusa reaproveitar `liberado` que ainda tem SEQ, então liberar sem zerar deixaria o item parado. Use uma função do repo `liberar_comprovado(conn, cadastro_id, versao) -> bool` (de `em_andamento` para `liberado`, zerando `seq_reservado`; acrescente-a com teste). `confirmado` e `orfao` são finais no repositório.

**Files:**
- Modify: `app/integrations/flowpcp/cadastro_volta.py`
- Test: `tests/test_cadastro_volta_recuperar.py` (novo)

**Interfaces:**
- Consumes: `repo.em_andamento`, `cadastro_fire.ler_produto`, `ler_composicao`, `candidatos_por_data` (substituída por `candidatos_por_data` (Task 8 rodada 2)), `corpo_gravado`.
- Produces: `IDADE_MINIMA_RECUPERACAO = timedelta(minutes=2)`, `recuperar(slug, env_conn, fire_conn, *, agora) -> dict` (contagens `adotado`, `liberado`, `incerto`, `reaplicar`).

Regras (spec §6.3 "Recuperação"), para cada linha `em_andamento` com `iniciado_em` há mais de `IDADE_MINIMA_RECUPERACAO` (a linha recém-reservada pelo próprio ciclo não é tocada), rodando no começo de cada ciclo de gravação, depois de abrir o Fire e antes do GET:

- `acao == "criar"` com `seq_reservado`: `ler_produto(seq)`.
  - existe e `DESCRICAO`, `UNIDADE`, `GRUPO`, `CODIGO_EAN13` e `CLASSIF_FISCAL` batem com `plano_json["campos"]` (comparação aparada; `None` = vazio) e, se o plano tem componentes, `ler_composicao(seq)` bate com eles → **adota**: `marcar_gravado` com `corpo_gravado(versao, str(seq), agora)`; o envio fica para `reenviar_pendencias`.
  - existe e é diferente → **libera** (`marcar_desfecho(..., "liberado")`): outra gravação tomou o SEQ; o item volta pela fila.
  - existe, igual em `PRODUTOS` e diferente na composição → **incerto**.
  - não existe → **libera**.
- `acao == "criar"` sem `seq_reservado`: `buscar_por_nome` — substituída por `candidatos_por_data` (Task 8 rodada 2). Hoje: `candidatos_por_data` na janela da reserva, sem `GRUPO`. 0 → libera; 1 que bate → adota; qualquer outro → **incerto**.
- `acao` `atualizar` ou `inativar` → **libera** (reaplicar é idempotente; o item volta pela fila).
- `incerto`: `marcar_desfecho(..., "incerto", alerta="recuperacao")`, ERROR com `cadastro_id`, `versao` e os SEQs candidatos. Nunca grava, nunca adota no chute.

- [ ] **Step 1: Write the failing tests**

Um teste por caso: adota com `seq_reservado` (incluindo kit com composição igual), libera quando a linha é de outro produto, libera quando não existe, incerto quando a composição difere, adota por nome com 1 candidato, incerto com 2 candidatos, libera `atualizar`, e não toca linha com menos de 2 minutos. Use um dublê de Fire só-leitura com `produtos` e `kit` em dicionários e prepare o livro com `repo.reservar` + `repo.marcar_seq` e `iniciado_em` ajustado por `UPDATE cadastro_volta SET iniciado_em = ?` no teste. Exemplo do caso principal:

```python
def test_adota_seq_reservado_quando_tudo_bate(env_conn):
    plano = {"acao": "criar", "campos": {"DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR", "GRUPO": 8,
             "CLASSIF_FISCAL": "61159500"}, "componentes": None}
    repo.reservar(env_conn, cadastro_id="c1", versao=1, produto_id="p1", acao="criar",
                  tipo="simples", modo="criar", plano_json=json.dumps(plano))
    repo.marcar_seq(env_conn, "c1", 1, 3992)
    env_conn.execute("UPDATE cadastro_volta SET iniciado_em = '2026-10-17 12:00:00'")
    fire = FireLeitura(produtos={3992: {"SEQ": 3992, "DESCRICAO": "MEIA SOQUETE", "UNIDADE": "PAR",
                                        "GRUPO": 8, "CODIGO_EAN13": None, "CLASSIF_FISCAL": "61159500",
                                        "DATA_CAD": date(2026, 10, 17)}})
    r = cv.recuperar("mm", env_conn, fire, agora=datetime(2026, 10, 17, 13, 0, tzinfo=UTC))
    assert r["adotado"] == 1
    linha = repo.buscar(env_conn, "c1", 1)
    assert (linha["estado"], linha["erp_produto_id"]) == ("gravado_sem_volta", "3992")
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_volta_recuperar.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`recuperar` em `cadastro_volta.py`, chamado em `_processar_gravacao` logo depois de abrir o Fire. `iniciado_em` vem do SQLite como `YYYY-MM-DD HH:MM:SS` em UTC: converta com `datetime.fromisoformat(v).replace(tzinfo=UTC)`. A comparação de campos usa uma função pequena `_bate(lido: dict, plano: dict) -> bool` sobre as cinco colunas, com `str(x).strip() if x is not None else ""` dos dois lados e `GRUPO` comparado como `int`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_cadastro_volta_recuperar.py tests/test_cadastro_volta_gravar.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/cadastro_volta.py tests/test_cadastro_volta_recuperar.py
git commit -m "feat(cadastro): recuperação de cadastro interrompido (adota, libera ou incerto)"
```

---

### Task 9: Ida com `marcasErp` e `temNotaFiscal`

> **Emenda 1 (revisão do Flow, 17/10; vale acima do texto abaixo onde divergir).**
> (a) I10: leia a resposta. Guarde em `rep.marcas_portal` também o que o Flow devolveu em `marcas` (`vinculadas`, `jaVinculadas`, `semPar`, `conflitos`, ou `recebidas` no dry-run) e em `rep.nota_portal` o `notaFiscal` (`atualizados`). `marcas: {"erro": true}` ou `notaFiscal: {"erro": true}` → ERROR no log e `reenviar: true` no bloco correspondente do relatório (o diário tenta de novo no próximo ciclo; a rota manual mostra). `semPar` ou `conflitos` não vazios → WARNING com os nomes. Testes para cada caso.
> (b) I11: a ida tira do lote os SEQs de `cadastro_volta_repo.seqs_em_aberto` (Task 4) do ambiente, somados a `fora_do_envio`, em toda rodada (manual e diária), e reporta em `rep.fora_do_envio_volta`. Sem isso, um SEQ gravado pelo Portal e ainda sem volta entraria no lote e o Flow criaria outro produto. Teste: com uma linha `gravado_sem_volta` de SEQ 3992 no livro e o 3992 no extract, o 3992 não vai no corpo.

**Files:**
- Modify: `app/erp/catalog_extract.py` (duas leituras novas), `app/erp/queries.py` (duas constantes)
- Create: `app/integrations/flowpcp/catalogo_marcas.py`
- Modify: `app/integrations/flowpcp/catalogo_schema.py`, `catalogo_mapper.py`, `catalogo_sync.py`, `capacidades.py`
- Test: `tests/test_catalogo_marcas.py` (novo), `tests/test_catalog_extract.py`, `tests/test_catalogo_sync.py`

**Interfaces:**
- Produces:
  - `capacidades.NOTA = "produtos.nota.v1"`, `capacidades.MARCAS = "marcas.v1"`
  - `queries.LIST_PRODUTOS_MARCA = "SELECT CODIGO, MARCA FROM PRODUTOS_MARCA ORDER BY CODIGO"`, `queries.LIST_PRODUTOS_COM_NOTA = "SELECT DISTINCT CODPRODUTO FROM NOTAPROD"`
  - `extract_marcas(fire_conn) -> list[tuple[int, str | None]]`, `extract_com_nota(fire_conn) -> set[str]`
  - `catalogo_marcas.envio_ligado(slug) -> bool` (`PORTAL_FLOWPCP_CATALOGO_MARCAS`), `catalogo_marcas.nota_ligada(slug) -> bool` (`PORTAL_FLOWPCP_CATALOGO_NOTA`), `catalogo_marcas.filtrar(brutas) -> tuple[list[dict], list[dict]]` (enviadas `{codigo, nome}`, filtradas `{codigo, motivo}` com `motivo` em `codigo_invalido`, `nome_vazio`, `nome_longo`)
  - `CatalogoProdutoItem.temNotaFiscal: bool | None` (omitido quando não preenchido) e `CatalogoRequest.marcasErp: list[CatalogoMarcaErp] | None` (omitido quando `None`)
  - relatório: `rep.marcas_portal = {"enviadas": N, "filtradas": [...], "capacidade_ok": bool}`, `rep.nota_portal = {"com_nota": N, "enviados": N, "capacidade_ok": bool}`

Regras (contrato §8): `codigo` inteiro 1..999999999; `nome` aparado com 1..30 caracteres. A linha `CODIGO=1` com nome vazio da MM sai em `filtradas` com `nome_vazio`. Mais de 500 marcas → manda as 500 primeiras por código e as outras em `filtradas` com `excede_500`. `temNotaFiscal` vai em todo item enviado (`True` se o SEQ está em `extract_com_nota`, senão `False`). Sem `marcas.v1`/`produtos.nota.v1` nas capacidades da resposta → `capacidade_ok=False` e ERROR, como o fiscal faz hoje. A leitura de `NOTAPROD` que falhar não manda `temNotaFiscal` em nenhum item ("ausente = não mexer"), nunca `False` para todos.

- [ ] **Step 1: Write the failing tests**

```python
from app.integrations.flowpcp import catalogo_marcas as cm


def test_filtra_vazia_longa_e_codigo_invalido():
    enviadas, filtradas = cm.filtrar(
        [(1, ""), (2, "  Kolosh  "), (0, "Zero"), (3, "x" * 31), (4, None), (5, "Sam's Club")]
    )
    assert enviadas == [{"codigo": 2, "nome": "Kolosh"}, {"codigo": 5, "nome": "Sam's Club"}]
    assert filtradas == [
        {"codigo": 1, "motivo": "nome_vazio"},
        {"codigo": 0, "motivo": "codigo_invalido"},
        {"codigo": 3, "motivo": "nome_longo"},
        {"codigo": 4, "motivo": "nome_vazio"},
    ]


def test_limite_de_500():
    enviadas, filtradas = cm.filtrar([(i, f"M{i}") for i in range(1, 503)])
    assert len(enviadas) == 500 and [f["motivo"] for f in filtradas] == ["excede_500"] * 2
```

Em `tests/test_catalogo_sync.py`, siga o padrão dos testes fiscais existentes (`grep -n "fiscal" tests/test_catalogo_sync.py | head`) e acrescente: com `PORTAL_FLOWPCP_CATALOGO_MARCAS=<slug>`, o corpo enviado tem `marcasErp` sem a linha vazia; desligado, o corpo **não tem** a chave; com `PORTAL_FLOWPCP_CATALOGO_NOTA=<slug>`, cada item tem `temNotaFiscal` e o SEQ com nota vem `True`; leitura de `NOTAPROD` quebrada → nenhum item com a chave; resposta sem `marcas.v1` → `marcas_portal["capacidade_ok"] is False`.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_catalogo_marcas.py tests/test_catalogo_sync.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`catalogo_marcas.py`:

```python
"""Marcas do Fire (PRODUTOS_MARCA) e nota fiscal (NOTAPROD) na ida do catálogo
(contrato P4a §8). Uma marca fora das regras derruba o POST /catalogo inteiro
com 400: o Portal filtra antes e reporta."""

from __future__ import annotations

import os

MAX_MARCAS = 500
MAX_NOME = 30
MAX_CODIGO = 999_999_999


def _ligado(var: str, slug: str) -> bool:
    return slug.lower() in {s.strip().lower() for s in os.environ.get(var, "").split(",") if s.strip()}


def envio_ligado(slug: str) -> bool:
    return _ligado("PORTAL_FLOWPCP_CATALOGO_MARCAS", slug)


def nota_ligada(slug: str) -> bool:
    return _ligado("PORTAL_FLOWPCP_CATALOGO_NOTA", slug)


def filtrar(brutas) -> tuple[list[dict], list[dict]]:
    enviadas, filtradas = [], []
    for codigo, nome in brutas:
        texto = str(nome or "").strip()
        if not isinstance(codigo, int) or not 1 <= codigo <= MAX_CODIGO:
            filtradas.append({"codigo": codigo, "motivo": "codigo_invalido"})
        elif not texto:
            filtradas.append({"codigo": codigo, "motivo": "nome_vazio"})
        elif len(texto) > MAX_NOME:
            filtradas.append({"codigo": codigo, "motivo": "nome_longo"})
        elif len(enviadas) >= MAX_MARCAS:
            filtradas.append({"codigo": codigo, "motivo": "excede_500"})
        else:
            enviadas.append({"codigo": codigo, "nome": texto})
    return enviadas, filtradas
```

Em `catalogo_schema.py`: `class CatalogoMarcaErp(BaseModel): codigo: int; nome: str`; `CatalogoProdutoItem.temNotaFiscal: bool | None = None` entra na tupla de `_omite_nao_preenchidos`; `CatalogoRequest.marcasErp: list[CatalogoMarcaErp] | None = None` entra na tupla do request. `build_catalogo_request` ganha `marcas: list[dict] | None = None` e `com_nota: set[str] | None = None` (o `_item` põe `temNotaFiscal=d.fire_produto_id in com_nota` quando `com_nota is not None`). Em `run_catalogo_sync`, leia as duas coisas **dentro** do `with fire_ctx` (cada uma num `try` próprio, log de ERROR e `None` na falha) só quando o interruptor correspondente estiver ligado, e trate a resposta como o bloco fiscal (linhas 198-212).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_catalogo_marcas.py tests/test_catalogo_sync.py tests/test_catalog_extract.py tests/test_catalogo_fiscal.py -v`
Expected: PASS; com os dois interruptores desligados, os testes de "payload igual ao de antes" continuam passando.

- [ ] **Step 5: Commit**

```bash
git add app/erp/catalog_extract.py app/erp/queries.py app/integrations/flowpcp/catalogo_marcas.py app/integrations/flowpcp/catalogo_schema.py app/integrations/flowpcp/catalogo_mapper.py app/integrations/flowpcp/catalogo_sync.py app/integrations/flowpcp/capacidades.py tests/
git commit -m "feat(catalogo): ida com marcas do Fire e nota fiscal por produto (marcas.v1, produtos.nota.v1)"
```

---

### Task 10: `cor` fora de `DESCRICAO2` (R3)

**Files:**
- Modify: `app/integrations/flowpcp/catalogo_fiscal.py`, `app/integrations/flowpcp/cadastro_volta.py` (`COLUNAS`)
- Test: `tests/test_catalogo_fiscal.py`, `tests/test_cadastro_volta.py`

**Interfaces:**
- Produces: `campos_fiscais(d)` nunca devolve `cor`; `cadastro_volta.COLUNAS` sem a chave `"cor"`. O motivo `cor_longa` deixa de existir.

- [ ] **Step 1: Write the failing tests**

```python
def test_descricao_interna_nao_vai_como_cor():
    d = _dto(cor="BRANCO-BRANCO-BRANCO")  # use o construtor de DTO do arquivo
    campos, motivos = catalogo_fiscal.campos_fiscais(d)
    assert "cor" not in campos and "cor_longa" not in motivos
```

e em `tests/test_cadastro_volta.py`, no plano de um item com `cor="PRETO"`: `"DESCRICAO2" not in plano["campos"]`.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_catalogo_fiscal.py tests/test_cadastro_volta.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Em `campos_fiscais`, remova o bloco de `cor` e o motivo `cor_longa`, com um comentário de uma linha: `# DESCRICAO2 é a "Descrição Interna" da tela do Fire, não cor (17/10/2026): não vai ao Flow.` Em `COLUNAS`, remova `"cor": "DESCRICAO2"` com o mesmo comentário. Ajuste os testes antigos que esperavam `cor`/`cor_longa` (removê-los, não invertê-los). Mantenha `LIMITE_COR` só se outro módulo usar (`grep -rn LIMITE_COR app/`).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_catalogo_fiscal.py tests/test_cadastro_volta.py tests/test_catalogo_sync.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_fiscal.py app/integrations/flowpcp/cadastro_volta.py tests/
git commit -m "fix(cadastro): descrição interna do Fire (DESCRICAO2) não é cor, nem na ida nem na volta"
```

---

### Task 11: Saneamento das marcas no Fire

**Files:**
- Create: `app/erp/backup.py`, `tools/sanear_marcas_fire.py`
- Test: `tests/test_sanear_marcas.py`

**Interfaces:**
- Produces:
  - `backup.gbak(cfg: dict, destino_no_servidor: str) -> None` (via `fdb.services.connect(host, user, password)`, `svc.backup(cfg["path"], destino_no_servidor)`, `svc.wait()`; levanta se o host for vazio, porque backup embedded não é o caso da MM)
  - `tools/sanear_marcas_fire.py`: funções puras `normalizar(nome) -> str` (trim, espaços colapsados, maiúsculas, sem tirar acento: a regra do Flow), `chave_solta(nome) -> str` (`normalizar` + sem acento + sem apóstrofo/pontuação, só para avisar grafia parecida), `planejar(existentes: list[tuple[int,str|None]], desejadas: list[str]) -> Plano` com `ja_existem: list[(nome, codigo)]`, `parecidas: list[(desejada, existente, codigo)]`, `inserir: list[(codigo, nome)]` (código = MAX+1 em sequência)
  - CLI: `python tools/sanear_marcas_fire.py <slug>` (dry-run, padrão) e `python tools/sanear_marcas_fire.py <slug> --gravar --backup-servidor 'C:\Fire\backup\marcas-20261017.fbk'`

Lista das marcas (contrato §8.3, 17 ativas): `Act Vitta, Act Vitta Lisa, Artwalk, Authentic Feet, CompraMais, Kolosh, Magic Feet, Molekinha, Molekinho, Nasmar, NBA Store, Netshoes, Oxer, Riachuelo, Sam's Club, San Bovo, Studio Z`. O apóstrofo é U+0027 (confirmado pelo Flow em 17/10). Fica numa constante `MARCAS_FLOW_2026_10_17` no topo da ferramenta, com a data no nome: marca nova depois disso é cadastro manual (decisão do Samuel).

Regras do `--gravar`: (1) refaz o plano com o banco atual; (2) se `parecidas` não está vazio, para sem gravar e pede revisão; (3) roda `backup.gbak` e confere que não levantou; (4) uma transação: `INSERT INTO PRODUTOS_MARCA (CODIGO, MARCA) VALUES (?, ?)` para cada item; (5) relê `PRODUTOS_MARCA` na mesma transação e confere que cada nome está lá com o código planejado; (6) `commit()`; qualquer divergência → `rollback()` e saída com código 1. O dry-run abre a conexão por `connect_somente_leitura`. **A ferramenta só roda com `--gravar` depois do ok explícito do Samuel**; o docstring diz isso na primeira linha.

- [ ] **Step 1: Write the failing tests**

```python
from tools.sanear_marcas_fire import chave_solta, normalizar, planejar


def test_normalizar_igual_ao_flow():
    assert normalizar("  Sam's   Club ") == "SAM'S CLUB"
    assert normalizar("Têxtil") == "TÊXTIL"


def test_plano_da_mm_em_17_10():
    existentes = [(1, ""), (2, "Adatex S. A. Industrial e Come"), (16, "Zanotti S.A.")]
    plano = planejar(existentes, ["Kolosh", "Sam's Club"])
    assert plano.ja_existem == [] and plano.parecidas == []
    assert plano.inserir == [(17, "Kolosh"), (18, "Sam's Club")]


def test_avisa_grafia_parecida():
    plano = planejar([(5, "SAMS CLUB")], ["Sam's Club"])
    assert plano.parecidas == [("Sam's Club", "SAMS CLUB", 5)] and plano.inserir == []


def test_reconhece_existente_pela_regra_do_flow():
    plano = planejar([(5, "kolosh ")], ["Kolosh"])
    assert plano.ja_existem == [("Kolosh", 5)] and plano.inserir == []


def test_chave_solta_tira_acento_e_apostrofo():
    assert chave_solta("Sam's Club") == chave_solta("SAMS CLUB")
```

Para importar de `tools/`, confira se já há `tools/__init__.py` ou `pythonpath` no `pyproject.toml`; se não houver, importe com `importlib.util.spec_from_file_location` no teste (padrão usado em outros testes de ferramenta: `grep -rln "spec_from_file_location" tests/ | head -3`).

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_sanear_marcas.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Funções puras como especificado, `Plano` como `@dataclass`. `main()` usa `argparse`, resolve o ambiente por `environments_repo.get_by_slug(slug)` + `to_fb_config`, imprime o plano em texto (nome, código, situação) e, sem `--gravar`, termina com `Dry-run: nada gravado.` O `--gravar` exige `--backup-servidor`. `chave_solta` usa `unicodedata.normalize("NFKD", ...)` sem marcas combinantes e `re.sub(r"[^A-Z0-9 ]", "", ...)` depois de `normalizar`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_sanear_marcas.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/erp/backup.py tools/sanear_marcas_fire.py tests/test_sanear_marcas.py
git commit -m "feat(tools): saneamento das marcas do Flow no Fire com dry-run, backup e conferência"
```

---

### Task 12: Métricas e tela do ambiente

> **Emenda 1 (revisão do Flow, 17/10).** I9: em `criar` e em `criar_e_atualizar` sem o portão de inativar, a cada ciclo de gravação faça um `GET ?simular=1&acoes=<as ações que este modo não consome>` (`criar_e_atualizar` sem portão → `inativar`; `criar` → `atualizar,inativar`, como o exemplo do contrato rev. 4; a contagem do modo `criar` pode incluir o `atualizar` derivado de `criar`, que este modo grava, e por isso é aproximada), que não reserva, e guarde a contagem em `ultimo_ciclo` como `aguardando_modo` e na métrica `portal_flowpcp_cadastro_volta_aguardando_modo{env}`. A tela mostra "Aguardando outro modo: N" com o texto "Itens aprovados no Flow que este modo da volta não grava." Se o Flow responder 400 a `simular` com `acoes` (rev. 3), registre `aguardando_modo = None`, WARNING uma vez, e siga. Testes: contagem guardada; 400 vira `None` sem derrubar o ciclo.

**Files:**
- Modify: `app/observability/metrics.py`, `app/web/routes_environments.py` (rota `GET .../flowpcp/cadastro-volta`), `app/web/static/admin-ambiente-edit.html`
- Test: `tests/test_cadastro_volta_route.py`, `tests/test_admin_ambiente_edit_cadastro.py`

**Interfaces:**
- Produces:
  - `portal_flowpcp_cadastro_volta_total{env,resultado}` aceita também `criado`, `atualizado`, `inativado`, `adotado`, `liberado`, `confirmado`, `erro_registrado`, `transitorio`, `orfao`, `superado`, `incerto`, `repetir`, `bug_corpo`, `credencial`
  - `portal_flowpcp_cadastro_volta_sem_volta{env}` (gauge: linhas em `gravado_sem_volta` + `erro_a_enviar`)
  - a rota devolve, além do que já devolve: `p0_ok`, `inativar_ligado`, `sem_volta` (contagem), e as listas `orfaos`, `incertos`, `sem_volta_itens` (até 50 cada, com `cadastro_id`, `versao`, `acao`, `erp_produto_id`, `seq_reservado`, `tentativas`, `alerta`, `atualizado_em`; nunca `corpo_json` nem `plano_json` inteiros)

Na tela, o bloco "Volta de cadastro" ganha: o portão da P0 (liberado/não liberado), o portão de inativar, três contadores com destaque quando maiores que zero (órfãos, incertos, sem volta) e uma tabela compacta com os itens dessas três listas. Texto de cada contador:
- "Órfãos: produto gravado no Fire que o Flow não vinculou. Avise a área de cadastro com o código."
- "Incertos: cadastro interrompido sem prova do que aconteceu. Confira no Fire antes de qualquer ação."
- "Sem volta: gravado no Fire, resultado ainda não aceito pelo Flow. O Portal reenvia sozinho."

- [ ] **Step 1: Write the failing tests**

Na rota: com uma linha `orfao`, uma `incerto` e uma `gravado_sem_volta` no livro, a resposta traz as três listas com um item cada, `sem_volta == 1`, e nenhuma chave `corpo_json`. Na tela (`test_admin_ambiente_edit_cadastro.py`, mesmo padrão de busca de texto que o arquivo já usa): o HTML contém os três rótulos e os ids dos contadores.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_cadastro_volta_route.py tests/test_admin_ambiente_edit_cadastro.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Métricas: incremente `cadastro_volta_total` no ponto de cada transição (Tasks 7 e 8) e atualize o gauge `sem_volta` no fim de cada ciclo com `contar_por_estado`. Repo: `listar_por_estado(conn, estado, limite=50) -> list[dict]` com as colunas permitidas acima (teste no `test_cadastro_volta_repo.py`). Tela: siga o markup e as classes do bloco atual do mesmo arquivo; nada de cor nova fora dos tokens existentes.

- [ ] **Step 4: Run tests e validação de design**

Run: `.venv/bin/pytest tests/test_cadastro_volta_route.py tests/test_admin_ambiente_edit_cadastro.py -v`
Expected: PASS.

Depois, suba o portal por `portless` e rode a skill `hm-designer` no bloco "Volta de cadastro" de `/admin/ambientes/{id}` (estados vazio, com itens e com erro de leitura; 320/768/1280 px; foco visível; `tabular-nums` nos contadores). Corrija os críticos antes do commit.

- [ ] **Step 5: Commit**

```bash
git add app/observability/metrics.py app/web/routes_environments.py app/web/static/admin-ambiente-edit.html app/persistence/cadastro_volta_repo.py tests/
git commit -m "feat(cadastro): métricas e tela da volta com órfãos, incertos e resultados sem volta"
```

---

### Task 13: P0 — perfil do cadastro, diff da tela e liberação por ambiente

Esta task tem duas partes. A primeira é código e roda agora. A segunda depende do teste da MM na cópia do `.fdb` (pessoas da MM na tela do Fire) e do ok do Samuel.

**Files:**
- Create: `tools/perfil_cadastro_fire.py`, `tools/produto_no_fire.py`
- Modify (parte B): `app/erp/cadastro_fire.py` (`PADROES_P0`, `COLUNAS_PERMITIDAS` se preciso, tabelas extras)
- Test: `tests/test_perfil_cadastro_fire.py`, `tests/test_cadastro_fire.py`

**Interfaces:**
- Produces:
  - `perfil_cadastro_fire.py <slug> [--ultimos 30]`: só leitura (`connect_somente_leitura`). Para os últimos N SEQs de cada tipo (par = unidade `PAR`; kit = SEQ que é pai em `PRODUTOS_KIT`) do grupo 8, lista cada coluna de `PRODUTOS` (de `RDB$RELATION_FIELDS`) com: quantos têm valor não nulo e não vazio, e o valor mais comum. Função pura `perfil(colunas, linhas) -> list[dict]` testável.
  - `produto_no_fire.py <slug> --seq N`: só leitura. Lista toda tabela do banco que tem coluna com nome em `{"SEQ","CODPRODUTO","CODPRODUTO_PAI","PRODUTO","CODPROD","SEQPRODUTO","COD_PRODUTO"}` (de `RDB$RELATION_FIELDS`) e imprime as linhas onde essa coluna = N. Serve para o diff da P0: o que a tela do Fire gravou para um produto novo.

**Parte A (agora):**

- [ ] **Step 1:** Teste de `perfil` com 3 linhas falsas: coluna sempre preenchida com o mesmo valor → `{"coluna": "X", "preenchidos": 3, "mais_comum": "A"}`; coluna sempre nula → `preenchidos: 0`.
- [ ] **Step 2:** Rode e veja falhar: `.venv/bin/pytest tests/test_perfil_cadastro_fire.py -v`.
- [ ] **Step 3:** Implemente as duas ferramentas (SQL só SELECT, parametrizado onde houver valor; nomes de tabela e coluna vêm do catálogo do próprio Firebird, nunca de entrada do usuário).
- [ ] **Step 4:** Rode e veja passar.
- [ ] **Step 5:** Commit: `git commit -m "feat(tools): perfil do cadastro e rastro de um produto no Fire (P0)"`.
- [ ] **Step 6 (só leitura, com VPN):** Rode `python tools/perfil_cadastro_fire.py mm --ultimos 30` e anexe a saída ao relatório da P0 (`docs/superpowers/specs/2026-10-16-flow-fonte-do-cadastro-design.md` §11, como "perfil medido em <data>"). Não grava nada.

**Parte B (depois do teste na MM, com o ok do Samuel):**

Roteiro para a MM, numa **cópia** do `.fdb` aberta pela tela do Fire:
1. Cadastrar pela tela 1 par (grupo 8, unidade PAR), 1 kit de 3 desse par e 1 material. Anotar os três SEQs.
2. Para cada SEQ: `python tools/produto_no_fire.py <slug-da-copia> --seq N > p0-<tipo>.txt`.
3. Comparar as colunas preenchidas em `PRODUTOS` com o perfil da parte A e com o que `cadastro_fire.criar` grava.

- [ ] **Step 7:** Para cada coluna que a tela preenche e o gravador não, acrescente em `PADROES_P0` o valor que a tela grava, com um comentário citando o arquivo `p0-*.txt`. Para cada tabela além de `PRODUTOS`/`PRODUTOS_KIT` em que a tela grava, escreva o teste no `tests/test_cadastro_fire.py` (o INSERT esperado dentro da mesma transação) e implemente em `criar`.
- [ ] **Step 8:** Prova: com `PORTAL_FLOWPCP_CADASTRO_P0=<slug-da-copia>` e `PORTAL_FLOWPCP_CADASTRO_VOLTA=<slug-da-copia>:criar`, gravar 1 par e 1 kit vindos do Flow DEV na cópia e abrir os dois na tela do Fire. Registrar a prova (prints e saída de `produto_no_fire.py`) no relatório.
- [ ] **Step 9:** Commit e PR. Ligar `PORTAL_FLOWPCP_CADASTRO_P0=mm` em produção é decisão do Samuel, fora deste plano.

---

### Task 14: Documentação e prontidão para o fluxo ponta a ponta em DEV

**Files:**
- Modify: `docs/ai/modules/flowpcp.md` (§5 "Volta de cadastro", §4 "Fiscal e material"), `docs/ai/modules/persistence.md` (`cadastro_volta`), `CHANGELOG.md`, `docs/superpowers/specs/2026-10-16-flow-fonte-do-cadastro-design.md` (§6.3: tabela de respostas antiga substituída por "ver contrato P4a §5"; §6.1: `cor` fora)

- [ ] **Step 1:** Reescreva só a §5 de `flowpcp.md`: modos, portões (`PORTAL_FLOWPCP_CADASTRO_VOLTA`, `PORTAL_FLOWPCP_CADASTRO_P0`, `PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR`), regras de ouro, estados do livro e o que cada um pede da operação, reenvio, recuperação, métricas. Em §4, acrescente `PORTAL_FLOWPCP_CATALOGO_MARCAS` e `PORTAL_FLOWPCP_CATALOGO_NOTA` e a remoção de `cor`.
- [ ] **Step 2:** `persistence.md`: colunas novas de `cadastro_volta`.
- [ ] **Step 3:** Suíte completa: `.venv/bin/pytest tests/ -q` e `ruff check app/ tests/ tools/`. Anote o número de testes no CHANGELOG.
- [ ] **Step 4:** Ambiente DEV para a Task 19 do Flow: um ambiente no Portal local (subido por `portless`) apontando para uma cópia local do `.fdb` da MM (Firebird local), com `PORTAL_FLOWPCP_CADASTRO_P0=<slug-dev>` e `PORTAL_FLOWPCP_CADASTRO_VOLTA=<slug-dev>:simular`. A URL e o token de serviço do Flow DEV entram pela tela `/admin/ambientes/{id}` (cifrados pelo `secret_store`), nunca por chat nem por arquivo. Avise a sessão do Flow que o lado Portal está em DEV.
- [ ] **Step 5:** Commit: `git commit -m "docs(cadastro): volta em gravação, marcas e nota na ida (Etapa 3)"`.

---

### Task 15: Ferramenta do operador para `incerto` e `orfao` (Emenda 1, I2)

> **Emenda 4 (revisão da Task 4, 17/10).** O repositório já traz `fechar_orfao` (de `orfao` para o estado final `orfao_fechado`, não `superado`) e `liberar_comprovado`; a Task 15 só acrescenta `adotar_manual`/`liberar_manual` e a CLI, e as duas novas são **funções próprias** do repo, fora de `TRANSICOES` (que é só de `marcar_desfecho`): `adotar_manual` (de `incerto`/`liberado` para `gravado_sem_volta`, gravando `erp_produto_id` e `corpo_json`) e `liberar_manual` (de `incerto` para `liberado`, zerando o SEQ só se a ferramenta comprovou a ausência no ERP). Nunca pôr essas transições em `TRANSICOES`.

**Files:**
- Create: `tools/cadastro_volta_operar.py`
- Modify: `app/persistence/cadastro_volta_repo.py`
- Test: `tests/test_cadastro_volta_operar.py`

**Interfaces:**
- Produces (repo): `adotar_manual(conn, cadastro_id, versao, *, seq: int, operador: str, corpo_json: str) -> bool` (só de `incerto` ou `liberado` para `gravado_sem_volta`, gravando `alerta="adotado_manual:<operador>"`); `liberar_manual(conn, cadastro_id, versao, *, operador: str) -> bool` (só de `incerto` para `liberado`, e só se `seq_reservado` é nulo ou a linha do ERP comprovadamente não existe — quem confere é a ferramenta); `fechar_orfao` (já existe desde a Task 4: de `orfao` para `orfao_fechado`, nota em `nota_operador`).
- CLI: `python tools/cadastro_volta_operar.py <slug> listar`; `... adotar <cadastroId> <versao> <seq> --operador NOME`; `... liberar <cadastroId> <versao> --operador NOME`; `... fechar-orfao <cadastroId> <versao> --operador NOME --nota TEXTO`. Sem `--confirmar`, toda ação só mostra o que faria. `adotar` lê o ERP **só leitura** (`connect_somente_leitura` + `cadastro_fire.ler_produto`) e recusa se a linha não existir; `liberar` recusa se `seq_reservado` existe no ERP. Nenhuma ação escreve no ERP nem no Flow: o envio do `gravado` adotado fica para o ciclo normal (`reenviar_pendencias`).

- [ ] **Step 1:** Testes do repo: cada transição permitida e cada recusada (estado errado devolve `False` e não muda nada); `adotar_manual` grava `erp_produto_id` e `corpo_json`.
- [ ] **Step 2:** Rode e veja falhar.
- [ ] **Step 3:** Implemente repo e CLI (`argparse`; a CLI monta o `corpo_gravado` com `datetime.now(UTC)`).
- [ ] **Step 4:** Rode e veja passar.
- [ ] **Step 5:** Commit: `git commit -m "feat(tools): ferramenta do operador para cadastro incerto e órfão"`.

---

## Ordem de ligar (fora deste plano, cada passo com o ok do Samuel)

0. A Task 10 (descrição interna não vai como cor) precisa estar no pacote **antes** de ligar `PORTAL_FLOWPCP_CATALOGO_FISCAL`.
1. Deploy do pacote com tudo desligado.
2. Saneamento das marcas: dry-run → revisão → backup → `--gravar`.
3. `PORTAL_FLOWPCP_CATALOGO_MARCAS=mm` num dryRun do catálogo; conferir `marcas` (`vinculadas`, `semPar`, `conflitos`); apply.
4. `PORTAL_FLOWPCP_CADASTRO_VOLTA=mm:simular` com a fila real; conferir o livro.
5. P0 parte B na cópia e prova na tela do Fire.
6. `PORTAL_FLOWPCP_CADASTRO_P0=mm` + `mm:criar`.
7. Depois da virada da ida para conferência: `mm:criar_e_atualizar`; `inativar` por último, com o portão próprio.
