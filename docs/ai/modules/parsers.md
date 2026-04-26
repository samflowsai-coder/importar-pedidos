# Módulo: parsers

## Responsabilidade
Transformar a saída do extractor (texto + tabelas) em um `Order` (pydantic). Cascata determinística: cada parser tenta `can_parse()` e, se positivo, chama `parse()`. Para no primeiro match.

## Arquivos críticos
- `app/parsers/base_parser.py` — `BaseParser` com `_find`, `_parse_br_number`. **Sempre herde dele.**
- `app/parsers/generic_parser.py` — fallback determinístico antes do LLM.
- `app/parsers/<cliente>_parser.py` — um por formato (Mercado Eletrônico, Pedido Compras Revenda, SBF/Centauro, Beira Rio, Kolosh, Sam's Club, Kallan XLS, Desmembramento XLS).
- `app/pipeline.py` — registro da cascata na lista `_parsers`.

## Como adicionar um parser novo
1. Criar `app/parsers/<nome>_parser.py` herdando de `BaseParser`.
2. Implementar `can_parse(self, extracted: dict) -> bool` com assinatura única e estável do formato (ex: string fixa no header).
3. Implementar `parse(self, extracted: dict) -> Optional[Order]`.
4. Registrar em `app/pipeline.py` na lista `_parsers` **antes** do `GenericParser`.
5. Adicionar sample real em `samples/`.
6. Adicionar teste em `tests/test_new_parsers.py`.

## Helpers (não duplicar)
- `self._find(text, pattern)` → `Optional[str]`
- `self._parse_br_number("1.000,50")` → `1000.50`
- Kolosh: `_parse_us_number` (ponto = milhar, ex `500.000` = 500 unid.)

## Modelo de saída
`Order(header=OrderHeader, items=list[OrderItem])`. Ver `modules/models.md`.

## Testes
- `tests/test_new_parsers.py` — um teste por parser específico.
- `tests/test_generic_parser.py` — genérico.
- Comando: `.venv/bin/pytest tests/test_new_parsers.py -v`

## Armadilhas comuns
- **Ordem na cascata importa.** O específico vai antes do genérico, sempre.
- **`can_parse` precisa ser barato.** Não parseie nada lá — apenas detecte formato.
- **Datas e números brasileiros:** sempre passe por `_parse_br_number` / `OrderNormalizer`.
- **Riachuelo/ME tem footer paginado** — strip já feito, ver commit `d25d480`.

## Sam's Club: dois layouts (consolidado vs GRADE)

`SamsClubParser` cobre 2 formatos do WebEDI/Neogrid:

1. **Consolidado** — 1 só destino (CD). Usa `_parse_items()` na tabela "ITENS DO PEDIDO", aplica o `delivery_cnpj` do cabeçalho a todos os itens.
2. **GRADE** — quando o texto contém `"Cross Docking"`, ativa o caminho alternativo:
   - `_build_item_lookup(text)` lê a tabela superior e monta `{ean_produto: {pack_size, unit_price}}`.
   - `_parse_cross_docking(text, ...)` lê a seção Cross Docking. **Layout do pdfplumber quebra o CNPJ em 3 linhas visuais** (head `00.063.960 /`, linha de dados `<EAN_loja> <EAN_produto> <packs> <data>`, tail `0094-08`). `_stitch_cnpj()` junta as 2 metades pelas linhas N-1 e N+1.
   - **Quantidade na grade = embalagens, não unidades.** Multiplica por `pack_size` da tabela superior. Item 7898686879194 tem `Qtde. na Emb.=36` → 1 embalagem na grade vira 36 unidades.
   - `delivery_ean` (EAN da loja) é a chave inequívoca usada pelo exportador para split — evita ambiguidade quando o CNPJ da filial coincide com o `customer_cnpj` (caso `00.063.960/0094-08`).
   - `_warn_if_grade_diverges()` soma qty da grade por SKU e compara com a tabela superior; emite `logger.warning` se divergir.

Ambos layouts compartilham `_parse_header()` (regex `Número (?:do )?Pedido:` cobre as duas variações). Detecção case-insensitive em `can_parse`.
