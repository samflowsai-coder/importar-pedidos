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

## Número gerado (entre a varredura e o normalizer)

`_numerar_se_ausente(order, file.raw)` roda logo depois de `_marcar_fornecedor`.
Se o `Order` chegou sem `order_number`, preenche `SN-` + 8 hex maiúsculos do
sha256 dos bytes do arquivo e marca `order_number_gerado = True`. Parser nunca
inventa número: quem gera é o pipeline, e o preview avisa ("Gerado pelo portal").

Por que existe — medido na Fire viva em 21/09/2026:
- `order_number` vira `CAB_VENDAS.PEDIDO_CLIENTE`, que o Fire copia para
  `NOTAPROD.PEDCLIENTE` ao faturar (o xPed da NF-e, cortado em **15**
  caracteres). O token tem 11 e chega inteiro na nota.
- Em modo xlsx é o **único** campo que liga o pedido do portal à linha do Fire
  (o importador de Excel do Fire não leva OBS). Sem número, o pedido fica órfão.

Propriedades:
- **Determinístico:** o mesmo arquivo dá o mesmo número (reimport, preview de
  novo, worker). É o prefixo de `imports.file_sha256` — do número impresso na
  nota o suporte chega no import.
- **Não é chave única:** outro arquivo do mesmo pedido (PDF + XLS, reenvio
  corrigido) gera outro token. Dedup de arquivo é outra camada.
- **Sobrevive à reconciliação:** o hífen seguido de 8 caracteres nunca casa o
  corte de sufixo de loja (`app/erp/numero_pedido.py`), nem com os 8 hex todos
  dígitos. Pinado em `tests/test_numero_pedido_gerado.py`.

## Quando alterar
- Adicionar parser novo → inserir antes do `GenericParser`.
- Alterar normalizer/validator → revisar todos os parsers (saída deles entra aqui).
