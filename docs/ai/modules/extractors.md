# Módulo: extractors

Arquivos:
- `app/extractors/pdf_extractor.py` — pdfplumber (texto + tabelas).
- `app/extractors/xls_extractor.py` — openpyxl para `.xlsx` e xlrd para `.xls` legado.

Saída: `dict` com `text`, `tables`, `metadata`, consumido pelos parsers.

Sem teste isolado — validação via testes de parsers que dependem da saída.
