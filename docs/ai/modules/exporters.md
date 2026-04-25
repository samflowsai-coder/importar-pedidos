# Módulo: exporters

## Responsabilidade
Persistir o `Order` validado no destino: XLSX (sempre disponível) ou Firebird (`EXPORT_MODE=db|both`).

## Arquivos críticos
- `app/exporters/erp_exporter.py` — XLSX. Split por loja (lógica abaixo).
- `app/exporters/firebird_exporter.py` — driver Firebird.

## Split por loja (XLSX)
| Condição | Chave | Resultado |
|---|---|---|
| `delivery_cnpj` ≠ CNPJ do cliente | CNPJ da loja | Split por CNPJ |
| `delivery_cnpj` ausente + `delivery_name` | nome da loja | Split por nome |
| Sem delivery | `""` | Arquivo único |

Naming: `{NOME_CLIENTE}_{CNPJ}_{PEDIDO}_{SUFIXO}.xlsx`

## Testes
Sem teste unitário. Validar com sample multi-loja (Riachuelo) e single (Centauro).
