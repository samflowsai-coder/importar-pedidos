# Módulo: erp (Firebird / Fire Sistemas)

## Responsabilidade
Conectar no Firebird (embedded ou TCP) e inserir pedidos preservando o schema legado. **Idempotência por `PEDIDO_CLIENTE + CLIENTE`.**

## Arquivos críticos
- `app/erp/connection.py` — abre/fecha conexão (firebird-driver), modo embedded vs TCP.
- `app/erp/queries.py` — SQL parametrizado (CHECK_ORDER_EXISTS, INSERT_ORDER_HEADER, INSERT_ORDER_ITEM, lookup de cliente/produto).
- `app/erp/mapper.py` — mapeia `Order` → linhas do schema real (nomes de colunas).
- `app/erp/product_check.py` — checagem de existência de produto antes de inserir.
- `app/erp/exceptions.py` — exceções de domínio.
- `app/exporters/firebird_exporter.py` — orquestrador, chamado pelo pipeline com `EXPORT_MODE=db|both`.
- `tools/explore_firebird.py` — gera schema_report a partir de `.fdb` (rodar SEMPRE em cópia, nunca em produção).

## Padrões reais de produção (não inventar)
- `STATUS = 'PEDIDO'` (string, não enum).
- Flags booleanas como string: `'Sim' | 'Nao'`.
- Charset `WIN1252`.
- Idempotência: antes de inserir, `CHECK_ORDER_EXISTS` por `PEDIDO_CLIENTE + CLIENTE`.

## Variáveis de ambiente
```
EXPORT_MODE=xlsx|db|both
FB_DATABASE=/path/emp.fdb
FB_HOST=192.168.1.10  # omitir = embedded
FB_PORT=3050
FB_USER=SYSDBA
FB_PASSWORD=masterkey
```

## Testes
**Sem testes isolados.** Validar com `.fdb` de cópia + sample real, `EXPORT_MODE=both`.

## Armadilhas
- Nunca rodar `explore_firebird.py` no .fdb de produção.
- Nunca commitar `.fdb`, `jaybird.jar`, `bkp Fire/`, `bkp Fire Novo/`, `backup Fire/`.
- Charset errado quebra acentos silenciosamente.

## Cliente override (CLIENT_NOT_FOUND recovery)
Quando o CNPJ parseado não bate com `CADASTRO`, o usuário pode escolher
manualmente o cliente via picker no portal. O override é metadado sidecar
em `imports.cliente_override_*` (ver `persistence.md`); aqui mora o suporte
SQL e a integração com o exporter.

- `SEARCH_CLIENTS` — busca por razão social (`UPPER LIKE`) ou CNPJ digits-only
  (`%LIKE%`), filtrada por `RELAC_CLIENTE='Sim'`, ordenada por `RAZAO_SOCIAL`,
  `FIRST 50` baked-in (Firebird não gosta de `ROWS ?` parametrizado em todas
  as versões).
- `FIND_CLIENT_BY_CODIGO` — exact lookup por `CADASTRO.CODIGO`. Usada para
  validar o override antes do INSERT (cliente pode ter sido inativado entre
  seleção no portal e clique em "Cadastrar no Fire").
- `FirebirdExporter.export(order, *, override_client_id=None)` — kwarg
  opcional. Quando setado, pula `FIND_CLIENT_BY_CNPJ` e usa
  `_validate_client_id`. Falha com a mesma `FirebirdClientNotFoundError`
  do caminho clássico se o codigo for inválido.
- O usuário que aplicou o override é gravado em `audit_log`
  (`user_email`, `user_id`) e em `imports.cliente_override_by` (email),
  vindo de `require_user` na rota `/api/imported/{id}/override-cliente`.
