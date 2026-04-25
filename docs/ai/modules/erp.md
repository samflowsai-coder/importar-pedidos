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
