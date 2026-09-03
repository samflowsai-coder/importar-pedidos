# Changelog

Notas de versão do Portal de Pedidos, escritas **para quem opera**, não para quem
programa. O que muda na tela, o que passa a funcionar, o que exige atenção.

Como funciona:

- A seção do topo é a próxima versão. Escreva nela conforme o trabalho avança.
- **A nota viaja no PR da feature, nunca antes dele.** Nota de algo que ainda não
  está na `main` vira promessa falsa se alguém cortar uma tag no meio.
- Ao publicar, renomeie o título para a versão da tag (ex.: `## 20260824-1530`) e
  abra uma nova `## Não publicado` acima.
- `tools/build_package.sh` procura a seção com o nome exato da versão; se não
  achar, usa a do topo. O texto vai pro `manifest.json`, aparece na tela
  `/admin/atualizacao` do cliente e no corpo do GitHub Release.

Uma frase por item, numerada, sem jargão. Quem lê é a operação, não o dev.

Versões anteriores a 20260725-1634 não têm notas aqui: até então elas viviam
em `RELEASE_NOTES.txt`, sobrescrito a cada build. O histórico delas está no git.

---

## Não publicado

1) Pedidos da **Kolosh** passam a entrar com o código de produto certo. O portal estava usando o código interno da Dakota (`04145.007/9`), que não existe no cadastro da Nasmar, então **todo item do Kolosh caía sem vínculo** e precisava ser ligado na mão. Agora ele usa a referência da Nasmar (`KL403G-0003`), que já está no Fire. O código da Dakota continua no pedido, na coluna de observação, porque ele é obrigatório na nota fiscal.

2) A **data do pedido** do Kolosh estava errada. O portal gravava a data de entrega no lugar da data de emissão, então a OC 96277C entrava no Fire com 01/12/2026 em vez de 01/09/2026. A data de entrega segue normal, no item.

3) A **descrição** dos itens do Kolosh vinha cortada no meio (`(1 PTA/1`), perdendo a cor e a numeração. Agora vem inteira.

4) Pedidos do **Sam's Club** passam a mostrar o nome do centro de distribuição. Quando o Sam's manda para um CD novo, a tela mostrava só o CNPJ solto; agora aparece o nome (ex.: `CD SAM'S DF`) e o código do local vai junto na planilha.

---

## 20260826-1925

1) Pedidos da **Tennis Station** passam a ser lidos corretamente. O arquivo dela é o mesmo formulário do Authentic Feet e do Magic Feet, só com uma diferença de maiúsculas no cabeçalho — e por causa disso o portal estava lendo 12 unidades e R$ 0 no lugar de 8.100 kits e R$ 120.882. No primeiro pedido dela, confira quantidade, valor e o nome do cliente antes de confirmar.

2) Atenção no formulário da Tennis Station: se o comprador deixar o campo **"Ordem de compra" em branco**, o pedido entra sem número, e sem número o portal não consegue casar o pedido com o Fire depois. Peça pro comprador preencher.

---

## 20260825-1010

1) Correção importante na verificação com o Fire: quando um cliente **reusa o número do pedido** (a Authentic Feet e a Xambre fazem isso todo mês), o portal estava mostrando o número de um pedido antigo do Fire no lugar do atual. Agora ele escolhe a linha do Fire com a data mais próxima do pedido.

2) Pedidos que já tinham sido marcados com o vínculo errado são **corrigidos sozinhos** na próxima verificação. O botão "Verificar no Fire" avisa quantos foram corrigidos.

3) Mais pedidos passam a ser reconhecidos: números como `AF049-6` e `AF090 - 3` (com o traço e um número curto no fim) agora casam com o `AF049` e o `AF090` do Fire. Eram 43 pedidos parados em "Em revisão" sem motivo.

---

## 20260824-2109

1) A lista de pedidos agora separa o que ainda falta fazer do que já está no Fire. O portal consulta o Fire e marca sozinho os pedidos que foram cadastrados lá na mão — eles saem de "Em revisão" e passam para "No Fire".

2) A tela abre em "Em revisão", que é o trabalho pendente. **Na primeira vez a lista vai encolher bastante** — é esperado: são os pedidos antigos que já estavam no Fire. Nenhum pedido foi apagado.

3) Cada filtro agora mostra quantos pedidos tem. Se "Em revisão" cair de 308 para 12, o número em "No Fire" sobe na mesma medida — dá para ver exatamente para onde os pedidos foram.

4) A verificação acontece sozinha ao entrar no ambiente e mais três vezes por dia (7h, 12h e 18h). O botão "Verificar no Fire" consulta na hora, quando você quiser.

5) O pedido marcado mostra o número dele no Fire e a situação lá — por exemplo "Cadastrado no Fire (PEDIDO)" ou "(CANCELADO)". O badge também diferencia quem cadastrou: o portal ou uma pessoa.

6) Pedido que já consta no Fire não pode mais ser cancelado nem reexportado pelo portal. Ele já está no ERP, e refazer convidaria pedido duplicado.

7) Se o Fire estiver fora do ar, o portal diz isso com todas as letras. Ele nunca responde "nenhum pedido encontrado" quando na verdade não conseguiu consultar.

8) Para marcar um pedido, o portal exige duas confirmações: o número do pedido **e** a identidade do cliente. Pedido dividido em várias lojas só é marcado quando **todas** as lojas aparecem no Fire — faltando uma, ele continua em revisão. Na dúvida, o portal prefere deixar em revisão a marcar errado.

---

## 20260824-1408

1) O portal agora entende a Ordem de Compra da Daju (cliente novo). É só subir o arquivo da OC que o pedido sai completo no preview: número da OC, CNPJ da Daju, os itens com o código do fornecedor (Ref. Forn.), EAN, quantidade e preço.

2) Atenção na Daju: quando a OC chega com a data de entrega incompleta (sem o dia), o pedido entra **sem data de entrega**. O preview mostra o campo vazio e não dá pra preencher por lá — o ajuste é no Fire, depois de importar.

3) O pacote de instalação passa a trazer três atalhos de diagnóstico para o servidor: `REINICIAR-APP.bat` (quando o portal não responde), `DIAGNOSTICO-APP.bat` (quando reiniciar não resolve) e `DIAGNOSTICO-PIP.bat` (quando uma atualização falha ao instalar). São só leitura, nenhum altera dados.

---

## 20260725-1634

1) Depois de aplicar uma atualização, as mudanças de tela aparecem sozinhas, sem precisar recarregar a página na mão.

2) Quando você vincula um produto, a contagem de "sem match" na lista de pedidos atualiza na hora, sem precisar dar refresh.

3) O botão "Descartar" na tela de atualização agora remove de verdade um pacote enviado. Antes ele só limpava a tela e o pacote voltava a aparecer ao recarregar a página.
