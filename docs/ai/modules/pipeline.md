# Módulo: pipeline (orquestrador)

## Responsabilidade
Dado um `LoadedFile`, executa: classify → extract → cascata de parsers → LLM
fallback → **varredura do fornecedor** → normalize → validate. Retorna
`Order` ou `None`.

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
8. NasmarTemplateParser
9. DesmembramentoXlsParser
10. GenericParser
11. LLMFallbackParser (só se 1–10 retornarem `None`)

## Varredura do fornecedor (entre parser e normalizer)

`_marcar_fornecedor(order, extracted)` roda logo depois que um parser (ou o
LLM fallback) devolveu um `Order`, e antes do `OrderNormalizer`. Preenche
`order.header.supplier_cnpj` só se ainda estiver vazio (um parser que já leu
o rótulo "FORNECEDOR" no documento ganha) — via
`app.routing.documento.detectar_fornecedor`, que devolve o CNPJ de ambiente
que aparece no texto, por **presença**, não por posição (ver
[`modules/routing.md`](routing.md), degrau 1). `documento.cnpjs_de_ambientes()`
lê `environments_repo.list_active()` — import local, porque o pipeline
também roda no CLI e nos testes de parser, que não devem pagar o custo de
importar persistência quando não há ambiente algum.

Nunca levanta: qualquer erro (banco compartilhado ausente no CLI, nenhum
ambiente com CNPJ cadastrado) vira `logger.debug` e o pedido segue sem
`supplier_cnpj` — o roteamento cai pro degrau seguinte (histórico), que é
exatamente o desenho. Parsing não pode quebrar por causa de roteamento.

## Quando alterar
- Adicionar parser novo → inserir antes do `GenericParser`.
- Alterar normalizer/validator → revisar todos os parsers (saída deles entra aqui).
