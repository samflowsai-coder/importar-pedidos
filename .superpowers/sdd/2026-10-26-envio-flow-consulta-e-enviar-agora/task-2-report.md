# Task 2 report
Status: DONE
- drenagem.drenar ganhou origem/quem/envio_id; abrir na transação de marcar_enviados (antes do POST); fechar nas transações de aplicar, registrar_espera (esperar e 202 sem resultados), bloquear e dividir (413). Runner passa origem="foto".
- RED: 8 falhas (7 drenagem + 1 runner) antes da implementação. GREEN: 38 passed (drenagem+runner).
- ruff check/format --check: ok nos 4 arquivos.
- Arquivos: app/erp_eventos/drenagem.py, app/erp_eventos/runner.py, tests/test_erp_eventos_drenagem.py, tests/test_erp_eventos_runner.py.
- Testes extras: 413 fecha o lote recusado; origem inválida falha antes do POST.
Concerns: nenhum. Não rodei a suíte completa.
