# O ambiente é propriedade do pedido — design

**Data:** 2026-09-11 · **Revisão 1**
**Status:** aprovado para implementar. Fecha a lacuna que a Fase 1b deixou aberta.
**Domínios:** `web`, `persistence`, `environments`

---

## O problema

A Fase 1b entregou menos do que prometeu, e a omissão é do plano, não do código.

Com `roteamento_modo = 'ligado'` e nenhuma empresa selecionada, a caixa de entrada soma as
duas empresas e mostra um selo por linha. Navegar funciona. **Agir não.** Medido na revisão
final da branch anterior: as rotas por-pedido devolvem 412 porque o handler toca
`db.connect()`, que levanta `NoActiveEnvironmentError` sem ambiente no contextvar.

```
ligado, SEM cookie — o que a UI chama ao clicar numa linha da caixa somada:
  GET  /api/imported/{id}/preview          -> 412
  GET  /api/imported/{id}                  -> 412
  POST /api/imported/{id}/send-to-fire     -> 412
  POST /api/batch/send-to-fire             -> 412
  (controle) COM cookie: GET /api/imported/{id} -> 200
```

A caixa somada é navegável e **cada linha é um beco sem saída**, com um 412 que aponta para
uma tela que a própria fase removeu do fluxo.

A spec da fase anterior diz: *"o ambiente deixa de ser um modo em que o usuário está e passa
a ser uma propriedade de cada pedido"*. Ela é propriedade — `imports.environment_id` é bind
imutável — mas nada a **lê** de volta. Este documento fecha isso.

---

## O que muda

| | hoje (`ligado`, sem cookie) | depois |
|---|---|---|
| abrir um pedido da caixa somada | 412 | abre, na empresa dele |
| cadastrar no Fire / exportar / cancelar | 412 | executa, na empresa dele |
| lote com pedidos de duas empresas | 412 | agrupa por empresa e processa cada grupo |
| aba de arquivos esperando | 412 | soma as pastas das duas empresas, com selo |
| cookie `portal_env` | filtro da listagem | filtro da listagem (inalterado) |

**O que NÃO muda:** `environment_id` continua bind imutável; cada empresa continua com seu
SQLite e seu Firebird; `active_env()` continua envolvendo todo caminho de escrita. Em
`desligado` e `observando` **nada** disto acontece — o cookie segue sendo a única fonte e
um pedido sem cookie continua dando 412, exatamente como hoje.

---

## Arquitetura

### O resolver

Responde uma pergunta só: de qual empresa é este pedido?

```python
def env_do_import_id(import_id: str) -> dict | None:
    """Empresa ATIVA que contém este pedido, ou None.

    `imports.id` é PRIMARY KEY em cada `app_state_<slug>.db`, então é acerto de
    índice por empresa. Com duas empresas isso é uma consulta trivial.
    """
    for env in environments_repo.list_active():
        with router.env_connect(env["slug"]) as conn:
            if conn.execute(
                "SELECT 1 FROM imports WHERE id = ? LIMIT 1", (import_id,)
            ).fetchone():
                return env
    return None
```

**`list_active()`, não `list_all()`.** Pedido de empresa desativada fica inalcançável (404).
É coerente com o resto: `list_imports_all_envs` já usa `list_active()`, então esse pedido nem
aparece na caixa; e o middleware já recusa cookie apontando para empresa inativa. Desativar
uma empresa tem que parar a atividade nela, não só escondê-la da UI.

**Sem cache, por ora.** O mapeamento `import_id → empresa` é **imutável por construção**,
porque `environment_id` é bind imutável — então cachear seria seguro aqui, ao contrário da
tabela de roteamento, onde o cache foi recusado porque ela muda. Não se faz agora por volume
(~108 pedidos, 2 empresas). Fica registrado como opção barata se a latência doer.

### O gate

```python
async def env_do_pedido(import_id: str):
    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        yield None                  # cookie manda; comportamento de hoje intacto
        return
    env = env_do_import_id(import_id)
    if env is None:
        raise HTTPException(404, "Pedido não encontrado em nenhuma empresa ativa")
    with env_context.active_env(env["id"], env["slug"]):
        yield env
```

Em `desligado` e `observando` a dependency devolve `None` na hora e **não consulta banco
nenhum**. É a mesma forma de gate dos dois wirings da fase anterior, e é o que permite
mergear e deployar sem mudar nada para a equipe.

**Em `ligado`, o pedido decide e o cookie não opina.** O cookie filtra a listagem e nada
mais. É mais simples de afirmar, mais simples de testar, e resolve o link direto: abrir um
pedido da Nasmar com "MM" no filtro funciona em vez de dar 404.

### `async def` é requisito, não estilo

**Medido em 2026-09-11 neste repo**, com `TestClient` real:

```
dependency def (sync) + yield + contextvar  -> ValueError: Token was created in a different Context
dependency async def + yield + contextvar   -> handler sync  vê o valor  ✓
                                            -> handler async vê o valor  ✓
```

O FastAPI roda dependency síncrona com `yield` via `contextmanager_in_threadpool`, então o
`__enter__` e o `__exit__` acontecem em contextos diferentes e o `reset` do token estoura.
A variante `async def` roda no mesmo contexto do handler.

Isto vai no docstring da dependency com o erro literal. Sem essa nota, alguém "simplifica" o
`async` daqui a seis meses e quebra todas as rotas por-pedido de uma vez.

---

## As três superfícies

### 1. Rotas por-pedido (10 rotas)

Cada uma ganha `env = Depends(env_do_pedido)` na assinatura. Uma linha, sem reindentar corpo.
A resolução aparece na **assinatura**, então quem lê a rota sabe que ela é env-aware —
diferente de resolver por middleware, que seria invisível no ponto de uso.

`GET /api/imported/{id}` · `/arquivo-original` · `/preview` · `POST /send-to-fire` ·
`/export-xlsx` · `/post-to-gestor` · `/cancel` · `/override-cliente` · `/vincular-produto` ·
`/ack-sem-preco`

### 2. Lote (2 rotas)

O lote hoje resolve o ambiente **uma vez** do cookie e passa o mesmo para todos os ids do
loop. Isso era garantido pelo portão do cookie e deixa de ser quando a caixa soma empresas.

Em `ligado`, o lote **agrupa por empresa** e processa grupo a grupo, cada um dentro do
`active_env` da sua empresa:

```python
grupos = defaultdict(list)
for import_id in body.ids:
    env = env_do_import_id(import_id)
    grupos[env["slug"] if env else None].append(import_id)

for slug, ids in grupos.items():
    if slug is None:                     # ids que não existem em empresa ativa
        results += [{"id": i, "ok": False, "reason": "nao_encontrado"} for i in ids]
        continue
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        for import_id in ids:
            results.append(_processa(import_id, env))
```

Uma conexão por Firebird em vez de conexões intercaladas, e a tolerância a falha parcial que
o lote já tem continua valendo. A resposta ganha `env_slug` e `env_name` por item, para a UI
poder dizer onde cada coisa caiu.

**Por que agrupar e não recusar seleção mista:** recusar reintroduz "pense na empresa antes
de agir", que é exatamente o passo que esta entrega remove. Agrupar mantém a promessa e é
mais seguro que intercalar conexões.

### 3. Fluxo da pasta de entrada (4 rotas)

Aqui o identificador é diferente: **`import_id` é único globalmente, nome de arquivo não é.**
Dois arquivos com o mesmo nome podem existir nas pastas das duas empresas.

Então a resolução não é derivada, é **declarada**:

- `GET /api/pending` em `ligado` sem cookie soma as pastas das empresas ativas, e cada item
  passa a carregar `env_slug` e `env_name`.
- `POST /api/import`, `/reimport` e `/preview-pending` aceitam `env_slug` no corpo. Em
  `ligado` ele é obrigatório quando não há cookie; nos outros modos é ignorado e o cookie
  manda, como hoje.
- Um `env_slug` que não corresponda a empresa ativa devolve 404, nunca um default.

A UI da aba de pendentes ganha o mesmo selo de empresa que a caixa de entrada já tem, e
passa o `env_slug` do item de volta ao agir sobre ele.

---

## Erros

| situação | resposta |
|---|---|
| `import_id` não existe em nenhuma empresa ativa | 404 "Pedido não encontrado em nenhuma empresa ativa" |
| `import_id` existe em empresa desativada | 404, mesma mensagem — deliberado |
| lote com ids de empresas diferentes | 200, agrupado, com resultado por item incluindo a empresa |
| lote com id inexistente no meio | esse item falha com `nao_encontrado`, os outros seguem |
| `env_slug` inválido no fluxo de pasta | 404 |
| `ligado` sem cookie, `env_slug` ausente onde é obrigatório | 400 com mensagem dizendo qual campo falta |
| `desligado`/`observando` sem cookie | 412, exatamente como hoje |

Nenhum caminho devolve um ambiente default. É a mesma regra da escada de roteamento: sem
resposta é resultado válido, palpite não é.

---

## Testes

| Alvo | O que trava |
|---|---|
| `desligado` é inerte | a dependency devolve `None` sem consultar banco; espião confirma zero chamadas ao resolver; sem cookie continua 412 |
| `observando` é inerte | idem — são dois modos, não um |
| resolver | acha na empresa certa; devolve `None` para id inexistente; **ignora empresa desativada** |
| pedido decide, cookie não opina | cookie da MM + pedido da Nasmar em `ligado` → opera na Nasmar |
| lote agrupado | ids das duas empresas → cada um na sua DB, conferido abrindo os dois `app_state_*.db` separadamente |
| lote com id órfão | falha só aquele item |
| pasta somada | `/api/pending` lista as duas, cada item com `env_slug` |
| `env_slug` obrigatório | ausente em `ligado` sem cookie → 400; inválido → 404 |
| `async def` da dependency | um teste que falharia se alguém trocasse por `def` |

O teste que protege a adoção continua sendo **`desligado` não muda nada**. Enquanto a chave
não virar, o Portal se comporta exatamente como hoje, e isso é verificado, não presumido.

---

## Fora de escopo

- Cache do mapeamento `import_id → empresa` (seguro, mas desnecessário neste volume)
- Permissão por empresa: hoje qualquer usuário autenticado pode agir em qualquer empresa
  selecionando-a; esta mudança não amplia isso, só remove o passo de selecionar
- Os três gates manuais de Firebird que já estão abertos da fase anterior
