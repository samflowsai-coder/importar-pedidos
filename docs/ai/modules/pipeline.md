# Módulo: pipeline (orquestrador)

## Responsabilidade
Dado um `LoadedFile`, executa: classify → extract → cascata de parsers → LLM fallback → normalize → validate. Retorna `Order` ou `None`.

## Arquivo crítico
- `app/pipeline.py` — instâncias singleton dos componentes + função `process(file)`.

## Ordem da cascata (não embaralhar sem motivo)
1. MercadoEletronicoParser
2. PedidoComprasRevendaParser
3. SbfCentauroParser
4. BeiranRioParser
5. KoloshParser
6. SamsClubParser
7. KallanXlsParser
8. DesmembramentoXlsParser
9. GenericParser
10. LLMFallbackParser (só se 1–9 retornarem `None`)

## Quando alterar
- Adicionar parser novo → inserir antes do `GenericParser`.
- Alterar normalizer/validator → revisar todos os parsers (saída deles entra aqui).
