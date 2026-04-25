# Módulo: models

## Arquivo crítico
- `app/models/order.py`

## Tipos
- `Order(header, items, source_file)`
- `OrderHeader(order_number, client_cnpj, client_name, ...)`
- `OrderItem(description, product_code, ean, quantity, unit_price, total_price, obs, delivery_date, delivery_cnpj, delivery_name)`
- `ERPRow` — colunas do output XLSX (`PEDIDO`, `NOME_CLIENTE`, `CNPJ_CLIENTE`, `CODIGO_PRODUTO`, `EAN`, `DESCRICAO`, `QUANTIDADE`, `PRECO_UNITARIO`, `VALOR_TOTAL`, `OBS`, `DATA_ENTREGA`, `CNPJ_LOCAL_ENTREGA`)

## Regra
Mudar contrato de modelo é mudança breaking — atualizar parsers, exporters, tests E este arquivo.
