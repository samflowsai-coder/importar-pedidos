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

## Sanitização de célula (`_clean_cell`)

Todo valor escrito no XLSX passa por `_clean_cell`: strings têm caracteres de
controle ilegais removidos via `ILLEGAL_CHARACTERS_RE` do openpyxl (0x00–0x08,
0x0B, 0x0C, 0x0E–0x1F). `\t`/`\n`/`\r` são válidos e preservados. Não-strings
(números, datas, None) passam intactos.

**Por quê:** texto parseado de PDF/XLS sujo pode trazer um char invisível; sem
o strip, `openpyxl` levanta `IllegalCharacterError` no `wb.save()` → HTTP 500 na
rota `/api/imported/{id}/export-xlsx` (regressão real: pedido AF185/H2S4,
2026-07-22, um único item afetado).

## Testes
- `tests/test_exporter_split.py` — split por loja, schema, qty bate por arquivo, regressão consolidado.
- `tests/test_smoke_exporter.py` — contrato público + sanitização de char de controle (`test_export_strips_illegal_control_chars`, `test_export_preserves_legit_whitespace`).
- Validações implícitas em `tests/test_new_parsers.py` (NBA: 21 arquivos; Magic Feet: 9 arquivos; Sam's GRADE: 3 arquivos).
