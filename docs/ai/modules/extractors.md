# Módulo: extractors

Arquivos:
- `app/extractors/pdf_extractor.py` — pdfplumber (texto + tabelas).
- `app/extractors/xls_extractor.py` — openpyxl para `.xlsx` e xlrd para `.xls` legado.

Saída: `dict` com `text`, `tables`, `metadata`, consumido pelos parsers.

## Páginas com posicionamento inconsistente

Alguns geradores declaram uma fonte mais larga do que a usada para calcular o
layout — o pedido da SBF/Centauro a partir de 07/2026 declara Helvetica-Bold mas
posiciona o texto com as métricas da regular. O visual sai correto (cada trecho é
ancorado por um `Td` absoluto), mas o pdfminer deriva a posição de cada caractere
somando as larguras declaradas: dentro do trecho a escrita escorrega para a
direita e invade o vizinho. Como a extração padrão ordena por `(top, x0)`, os
caracteres se intercalam (`GrupoSaf@centauro.com.br. / Tel:` →
`GrupoSaf@centauro.com.bTre.l :/`) e o parser perde a assinatura.

`_chars_are_stacked()` detecta o caso medindo a fração de caracteres empilhados
sobre o anterior; PDFs bem formados medem 0. Só nessas páginas o texto é remontado
por `_rebuild_text_from_runs()`, que ordena **trechos** (âncora do `Td`, exata) em
vez de caracteres, preservando a adjacência rótulo↔valor de que os parsers
dependem. As tabelas usam `text_use_text_flow` — as células já são delimitadas por
posição, basta não reordenar os caracteres dentro delas.

Ordenar tudo pela ordem do content stream **não** serve como padrão: em PDFs
multi-coluna (Beira Rio, Sam's, Riachuelo e o próprio Centauro anterior a 07/2026)
a ordem do stream não é a ordem visual. Daí a decisão ser por página e baseada em
evidência.

Sem teste isolado — validação via testes de parsers que dependem da saída
(`test_centauro_all_bold_font_pdf` cobre a remontagem).
