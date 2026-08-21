# Módulo: models

## Arquivo crítico
- `app/models/order.py`

## Tipos
- `Order(header, items, source_file)`
- `OrderHeader(order_number, issue_date, customer_name, customer_cnpj)` — **`customer_*`,
  não `client_*`**. Todo campo é opcional.
- `OrderItem(description, product_code, ean, quantity, unit_price, total_price, obs,
  delivery_date, delivery_cnpj, delivery_name, delivery_ean)` — `delivery_ean` é a chave
  de split da GRADE do Sam's (ver `modules/exporters.md`).
- `ERPRow` — uma linha do XLSX. **Os campos do modelo são snake_case**
  (`pedido`, `nome_cliente`, `cnpj_cliente`, `codigo_produto`, `ean`, `descricao`,
  `quantidade`, `preco_unitario`, `valor_total`, `obs`, `data_entrega`,
  `cnpj_local_entrega`, `ean_local_entrega`). Os títulos MAIÚSCULOS que aparecem na
  planilha são a lista `HEADERS` em `app/exporters/erp_exporter.py` — 13 colunas, mesma
  ordem.

## Regra
Mudar contrato de modelo é mudança breaking — atualizar parsers, exporters, tests E este arquivo.
