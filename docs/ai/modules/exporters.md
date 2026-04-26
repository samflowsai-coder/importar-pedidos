# Módulo: exporters

## Responsabilidade
Persistir o `Order` validado no destino: XLSX (sempre disponível) ou Firebird (`EXPORT_MODE=db|both`).

## Arquivos críticos
- `app/exporters/erp_exporter.py` — XLSX. Split por loja (lógica abaixo).
- `app/exporters/firebird_exporter.py` — driver Firebird.

## Split por loja (XLSX)

Prioridade da chave de agrupamento (`_delivery_key`):

| Prioridade | Condição | Chave | Caso de uso |
|---|---|---|---|
| 1 | `delivery_ean` presente | `ean:<EAN>` | **Sam's GRADE** — EAN é único por loja, evita ambiguidade quando CNPJ da filial == customer_cnpj |
| 2 | `delivery_cnpj` ≠ `customer_cnpj` | CNPJ da loja | Riachuelo (cada loja com CNPJ próprio) |
| 3 | só `delivery_name` | nome da loja | NBA (lojas identificadas só por nome) |
| 4 | sem nada | `""` | Arquivo único |

### Sufixo do nome do arquivo (`_suffix_for_group`)

- Sam's (com `delivery_ean`) → `SAMS_LOJA_<filial>` onde `<filial>` são os 4+2 últimos dígitos do CNPJ (ex: `SAMS_LOJA_0094_08`). Sem CNPJ, fallback para últimos 4 do EAN.
- NBA (só `delivery_name`) → nome sanitizado, primeiros 30 chars.
- Default → índice numérico `1`, `2`, `3`.

Naming: `{NOME_CLIENTE}_{CNPJ}_Pedido_{ORDER_NUMBER}_{SUFIXO}.xlsx`

## Schema XLSX (13 colunas)

`PEDIDO | NOME_CLIENTE | CNPJ_CLIENTE | CODIGO_PRODUTO | EAN | DESCRICAO | QUANTIDADE | PRECO_UNITARIO | VALOR_TOTAL | OBS | DATA_ENTREGA | CNPJ_LOCAL_ENTREGA | EAN_LOCAL_ENTREGA`

`EAN_LOCAL_ENTREGA` é populado quando `OrderItem.delivery_ean` está setado (Sam's GRADE). O ERP Firebird usa esse EAN para mapear a loja de destino.

## Testes
- `tests/test_exporter_split.py` — split por loja, schema, qty bate por arquivo, regressão consolidado.
- Validações implícitas em `tests/test_new_parsers.py` (NBA: 21 arquivos; Magic Feet: 9 arquivos; Sam's GRADE: 3 arquivos).
