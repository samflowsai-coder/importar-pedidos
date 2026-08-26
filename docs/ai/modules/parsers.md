# Módulo: parsers

## Responsabilidade
Transformar a saída do extractor (texto + tabelas) em um `Order` (pydantic). Cascata determinística: cada parser tenta `can_parse()` e, se positivo, chama `parse()`. Para no primeiro match.

## Arquivos críticos
- `app/parsers/base_parser.py` — `BaseParser`: só os abstratos `parse` e `can_parse`.
  **Sempre herde dele.** ⚠️ `_find` e `_parse_br_number` NÃO moram aqui — cada parser
  tem a sua cópia. Dívida conhecida (ver `docs/BACKLOG.md`); ao mexer num parser,
  copie do vizinho mais próximo em vez de inventar variante.
- `app/parsers/generic_parser.py` — fallback determinístico antes do LLM.
- `app/parsers/<cliente>_parser.py` — **11 parsers específicos**: Mercado Eletrônico, Pedido Compras Revenda, SBF/Centauro, Beira Rio, Kolosh, Sam's Club, Kallan XLS, **Nasmar Template**, Daju, Desmembramento XLS, e o Generic.
  ⚠️ Um parser cobre um **formato**, não um cliente. O `NasmarTemplateParser` sozinho
  atende 4 clientes (AF, MF, Pulmão, Tennis Station) porque todos usam o template do
  próprio fornecedor. Cliente novo que chega nesse template custa **zero** parser —
  antes de escrever um novo, cheque se o formato já está coberto.
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

## SBF/Centauro: CNPJ de Faturamento, não de Cobrança

`SbfCentauroParser._extract_customer` lê o CNPJ + nome da seção **"Dados para Entrega / Faturamento"** (ex.: `06.347.409/0296-51` — CD Jarinu), **não** da seção "Informações de Cobrança" (`/0001-65` — matriz SBF). Razão: o Fire cadastra o cliente pela filial faturada, então o exporter (`FIND_CLIENT_BY_CNPJ`) precisa do CNPJ /0296-51 para achar o cadastro. Usar a matriz quebra o lookup. Texto extraído via regex (a tabela `pdfplumber` mistura essa seção com o painel "Atenção Fornecedor").

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

## Nasmar Template: a assinatura é o cabeçalho, não a marca

`NasmarTemplateParser` (`app/parsers/nasmar_template_parser.py`) cobre o template de
pedido de kits do **próprio fornecedor** (Nasmar/MM). Um template, N clientes:
Authentic Feet, Magic Feet, "Pulmão" do Grupo Afeet e Tennis Station. Samples:
`Pedido Authentic Fit.xlsx`, `Pedido Magic Feet MF048.xlsx`,
`Pedido Grupo Afeet Pulmao.xlsx`, `PEDIDO TENNIS STATION.xlsx`.

- **`_match_header(row)` é fonte única** do gate e do `col_map` — `can_parse` e
  `_find_header_row` chamam a mesma função. Antes eram duas cópias literais da mesma
  condição: idênticas, mas livres para divergir a cada edição, e qualquer divergência
  daria arquivo aprovado no gate e `ValueError` no `cells.index()` do mapeamento.
- **O match é por texto NORMALIZADO** (`_norm`: upper + espaço colapsado) do conjunto
  de 4 colunas `REF.`, `DESCRIÇÃO PRODUTO`, `TOTAL KITS`, `TOTAL R$` — **não** o nome
  da marca, e **nunca** igualdade literal. A Tennis Station digitou `TOTAL Kits` e o
  pedido inteiro escapou do parser por causa do caixa de duas letras. Pedidos Pulmão
  vêm com os campos de cliente em branco e sem nenhum texto
  `AUTHENTICFEET`/`MAGICFEET` no conteúdo (a marca só aparece no nome do arquivo).
- **A quantidade real é `TOTAL KITS`.** Sem este parser o arquivo cai no `GenericParser`,
  que lê a coluna `REF COR` (cor) como quantidade — bug real de produção, três vezes
  (Magic Feet, Pulmão, Tennis Station).
- **O preço é `CUSTO`, nunca `SUGESTÃO`.** `SUGESTÃO` é preço de venda ao consumidor
  (29,99 contra 12,18 de custo); entrar no ERP como unitário infla o pedido ~2,5x e
  passa em qualquer validador. Coberto por teste.
- **Número do pedido: `Ordem de compra` → `FANTASIA` → `DATA DO PEDIDO`.** O campo
  `Ordem de compra:` só existe no template da Tennis Station, e onde existir ganha.
  `FANTASIA` é apelido digitado livre pelo comprador e só é fallback porque o template
  antigo não tem campo de número — é de lá que vem o `AF76` vs `AF076` aberto na
  reconciliação com o Fire. `_coerce_text` evita o `'4417.0'` do float do openpyxl.
- **Números: use o valor CRU da célula** (`_raw`), não o stringificado (`_cell`, só
  para campos textuais). `_to_number` é tipo-consciente: texto só-com-ponto usa a regra
  do último grupo — 3 dígitos = milhar (`1.300` → 1300), senão decimal (`300.0` → 300.0).
  A versão ingênua lia `1.300` como `1.3`: pedido mil vezes menor, silencioso, aprovado
  pelo validador. Mesma regra do `DajuParser._parse_number`.
  ⚠️ `_raw` **não é cosmético**: um float nativo de 3 casas (`16.815`) stringifica pra
  `'16.815'`, que casa com a regra de milhar e vira `16815.0`. Trocar `_raw` por `_cell`
  nos campos numéricos reintroduz o erro de 1000x — pinado em
  `test_custo_float_nativo_com_tres_casas_nao_vira_milhar`.
- **`_next_raw` desiste depois de `_MAX_CELULAS_VAZIAS` (4) células vazias seguidas.**
  O template guarda as listas de validação dos dropdowns nas colunas remotas (39 CNPJs
  de filiais a partir da coluna X no arquivo da TS) e nada ali termina em `:`, então o
  guard de rótulo sozinho não segura. Nos 4 samples o valor nunca está a mais de
  **+2** células do rótulo; o lixo fica a **+19**.

**Lacunas conhecidas (Tennis Station, 1 sample só) — ver `docs/BACKLOG.md`:**
o sample real veio com `Ordem de compra`, `RAZÃO SOCIAL` e `DATA DO PEDIDO` em branco →
`order_number = None` → `mapper.py:64` grava `PEDIDO_CLIENTE = NULL` no Fire, sem chave
de idempotência, e a reconciliação não casa (mesmo estado do Pulmão hoje). E o CNPJ
capturado é o **primeiro de uma lista escondida de 39 CNPJs** de filiais do grupo TS nas
colunas X+ (lista de validação do dropdown) — pode ser escolha do comprador ou default
não tocado. Confirmar com um segundo pedido real antes de confiar nele.

## Daju: Ref. Forn. é o código, e a data de entrega pode vir sem o dia

`DajuParser` cobre a Ordem de Compra da Daju LTDA — um **PDF convertido pra xlsx**
(aba "Table 1", colunas vazias intercaladas). Sample: `samples/Cliente NOVO OC-70610.xlsx`.

- **`can_parse`:** `"DAJU"` + `"ORDEM DE COMPRA"` no texto.
- **Colunas mapeadas por nome do cabeçalho** (`Ref. Forn.`, `Qtd.`, ...), nunca por
  índice fixo — a conversão PDF→xlsx desloca colunas entre arquivos.
- **`product_code` = coluna "Ref. Forn."** (código Nasmar, que o Fire conhece), não a
  coluna "Código" (interno da Daju). O bloco do comprador é o primeiro `Nome\nCNPJ:` do
  arquivo; tudo a partir de "FORNECEDOR" é a Nasmar e não pode virar cliente.
- **"Entrega prevista" pode vir sem o dia** (`/09/2026` — perdido na conversão). Data
  incompleta → `delivery_date = None`. **Não há edição de data no preview** — o
  `CommitRequest` só carrega `preview_id` (`app/web/server.py:1491`). O pedido entra com
  `DT_ENTREGA_ITEM = NULL` no Fire, ajustável lá depois. Se o FlowPCP for ligado neste
  ambiente, atenção: `/recebimento` é insert-only, então `prazoSolicitado` nulo vira
  permanente sem patch manual (mesmo incidente do `tools/reprocessar_prazos_flow.py`).
- **Números: use o valor CRU da célula, não o stringificado.** `_parse_number` é
  tipo-consciente porque a conversão PDF→xlsx é instável: a mesma coluna vem como número
  nativo hoje e como texto amanhã. Texto só-com-ponto usa a regra do último grupo —
  3 dígitos = milhar (`1.300` → 1300), senão decimal (`300.0` → 300.0). A versão ingênua
  lia `1.300` como `1.3`: pedido mil vezes menor, silencioso, aprovado pelo validador.
- Sem este parser o arquivo caía no `GenericParser`, que extraía pedido `'DA'` e
  quantidades erradas — bug real observado no RED do TDD.
