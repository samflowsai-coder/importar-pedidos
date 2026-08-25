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

_(nada ainda)_

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
