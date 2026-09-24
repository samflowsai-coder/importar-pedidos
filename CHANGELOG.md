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

1) **Pedido da Kings no modelo de kits entra com o produto certo.** Cada linha vai pro Fire com o código da cor (`KG07BR`, `KG07PR`, `KG10ST`...). Se a cor da linha vier diferente de Branco, Preto ou Sortido, o item aparece na revisão como produto não encontrado, para vincular à mão, em vez de entrar com o produto errado.

2) **Aviso de troca de produto pelo Fire.** O importador de Excel do Fire procura o código pelo começo, e às vezes grava outro produto (foi o que trocou `NB01-3G` por `NB01-3GG` no pedido 4939). Agora a tela de revisão avisa antes: o item aparece com **"⚠ vira NB01-3GG"** na coluna Fire. Item que o portal achou e o Fire vai trocar: importe normalmente e troque o produto no pedido do Fire depois. Item sem correspondência: confira se o produto escolhido pelo Fire é o certo, ou vincule antes de importar. O aviso não trava a geração da planilha.

---

## 20260921-1347

1) **Pedido NBA no modelo de kits entra com o produto certo.** Cada linha agora vai pro Fire com o código da sua cor e do seu tamanho (`NB01-1M`, `NB01-3G`, `NB03-1GG`...). Antes, todas as linhas de um mesmo modelo entravam como um produto só — foi o que aconteceu no pedido 4932.

2) **Pedido que chega sem número de ordem de compra ganha um número do portal**, que começa com `SN-` (ex.: `SN-6CF05353`). Na tela de revisão ele aparece com o selo laranja **"Gerado pelo portal"**. Esse número vai pro Fire e sai na nota fiscal no campo do pedido do cliente. Antes ia o nome da loja, a data do pedido, ou o campo ficava em branco.

3) **O nome da loja no campo Fantasia não vira mais número do pedido.** O código de loja da Authentic Feet e da Magic Feet (`AF198`, `MF048`) continua valendo como número, igual sempre foi.

4) Se aparecer o selo "Gerado pelo portal" num pedido que **tem** número no documento, não envie: avise o Samuel, porque o portal deveria ter lido aquele número.

---

## 20260912-1053

1) **Nasce desligado — esta versão não muda nada no seu dia até você ligar.** Tudo daqui pra baixo, dos itens 2 ao 7, só acontece depois que um **administrador** entra em **Configurações → Roteamento** (`/admin/roteamento`, só admin vê) e vira a chave. Instalar esta versão e não mexer em nada deixa o portal exatamente como ele é hoje.

2) A chave nova tem **três posições**, não duas. Em **Desligado** (como vem) nada acontece. Em **Observando** o portal calcula qual empresa *receberia* cada pedido e anota, **sem agir** — ele continua gravando onde você mandar, e a tela mostra em quantos pedidos ele acertaria. É pra você conferir antes de confiar. Em **Ligado** ele passa a agir pelo que calculou.

3) Com a chave **Ligada**, o portal descobre a empresa **pelo próprio pedido**, em vez de você escolher uma no login. Ele olha, nesta ordem: o **CNPJ do fornecedor impresso no pedido**; se não tiver, o **histórico do cliente no Fire** (em qual empresa esse cliente já comprou); se não tiver, o que **você respondeu na última vez** para esse mesmo cliente; e se nada disso resolver, ele **pergunta**. Ele nunca chuta.

4) Com a chave Ligada, a **caixa de entrada passa a mostrar os pedidos das duas empresas juntos**, com um selo em cada linha dizendo de qual empresa ele é. A **aba de arquivos esperando** passa a somar as pastas de entrada das duas do mesmo jeito, também com selo. E agora **dá pra agir** num pedido dessa lista sem escolher empresa nenhuma: abrir, cadastrar no Fire, exportar, cancelar, vincular produto — tudo cai na empresa certa, a do pedido.

5) Com a chave Ligada, **selecionar vários pedidos de empresas diferentes e cadastrar em lote funciona**: o portal separa por empresa e manda cada grupo para o Fire da sua. Antes ele tentaria mandar tudo para uma só. O resultado do lote diz, pedido por pedido, em qual empresa cada um entrou.

6) Com a chave Ligada, o **seletor de empresa no topo vira filtro de visualização**, e a tela de escolher empresa sai do caminho do login. Você continua podendo filtrar a lista por empresa quando quiser ver só uma.

7) Ainda com a chave Ligada: se a pasta de entrada de **alguma** das empresas estiver fora do ar (share de rede desmontado, pasta renomeada), a aba de arquivos esperando **avisa qual**. Antes, uma pasta funcionando escondia a outra que faltava, e os arquivos daquela empresa simplesmente não apareciam sem ninguém saber.

8) **Segurança, e isso vale em qualquer posição da chave:** o link direto de **download de planilha** passa a exigir que você esteja logado, e só serve arquivo de dentro das pastas de saída configuradas. Se alguém tinha um link de planilha salvo nos favoritos, ele vai pedir login. A listagem de arquivos esperando e a abertura de um pedido também passam a exigir sessão — antes, em certas situações, respondiam sem login. Junto com isso: se a sua sessão vencer com a tela aberta, o portal agora **te leva pra tela de login** em vez de mostrar uma mensagem de erro. Depois de entrar de novo você cai na tela de pedidos, onde estava.

9) **Antes de virar a chave para Ligado, três coisas precisam estar feitas:** cadastrar o **CNPJ de cada empresa** em Configurações → Ambientes (é por ele que o portal reconhece o fornecedor no pedido); cadastrar o **código de figura fiscal da Nasmar**; e conferir na Fire de verdade se o histórico do cliente traz o que a gente espera. Com a chave em **Observando** você vê a taxa de acerto sem risco nenhum — é o caminho recomendado antes de Ligar.

---

## 20260910-1400

1) Pedidos do **Sam's Club** passam a entrar no **cliente certo**. O portal vinha usando o CNPJ do clube que emite a ordem (`00.063.960/0223-31`), que muda a cada pedido e não existe no cadastro do Fire — nenhum pedido do Sam's casava com cliente. Agora ele usa o CNPJ do **local de entrega**, o centro de distribuição que recebe a mercadoria (`00.063.960/0587-94`, CD SAM'S DF), que é o que está cadastrado.

2) Junto com isso, o **nome do CD** passa a aparecer na coluna de cliente da planilha e no nome do arquivo. Antes o arquivo saía como `SEM_CLIENTE_...`, porque esse formato do Sam's não traz o nome em lugar nenhum.

3) Como o cliente agora é o próprio local de entrega, a coluna **CNPJ_LOCAL_ENTREGA** fica **em branco** nos pedidos do Sam's. Não é erro: o CNPJ está na coluna do cliente, e o código do local (EAN) continua na planilha como antes.

4) A **quantidade dos kits** do Sam's estava multiplicada pelo conteúdo do kit. Quando o pedido diz "22 por embalagem" e pede 1, o portal lançava 22 — o pedido 06839396 entraria com 22 kits no lugar de 1. Agora entra a quantidade que o Sam's realmente pediu. No formato com grade por loja o erro chegava a 36x.

5) **Atenção:** pedidos do Sam's importados antes desta versão não são corrigidos sozinhos. Se algum entrou no Fire com quantidade multiplicada ou no clube errado, precisa ser ajustado lá na mão.

---

## 20260903-1500

1) Pedidos da **Kolosh** passam a entrar com o código de produto certo. O portal estava usando o código interno da Dakota (`04145.007/9`), que não existe no cadastro da Nasmar, então **todo item do Kolosh caía sem vínculo** e precisava ser ligado na mão. Agora ele usa a referência da Nasmar (`KL403G-0003`), que já está no Fire. O código da Dakota continua no pedido, na coluna de observação, porque ele é obrigatório na nota fiscal.

2) A **data do pedido** do Kolosh estava errada. O portal gravava a data de entrega no lugar da data de emissão, então a OC 96277C entrava no Fire com 01/12/2026 em vez de 01/09/2026. A data de entrega segue normal, no item.

3) A **descrição** dos itens do Kolosh vinha cortada no meio (`(1 PTA/1`), perdendo a cor e a numeração. Agora vem inteira.

4) Pedidos do **Sam's Club** passam a mostrar o nome do centro de distribuição. Quando o Sam's manda para um CD novo, a tela mostrava só o CNPJ solto; agora aparece o nome (ex.: `CD SAM'S DF`) e o código do local vai junto na planilha.

5) O portal passa a **guardar uma cópia exata de todo arquivo que recebe**, antes de ler qualquer coisa dele — venha pela pasta de pedidos ou por upload na tela, tenha sido importado ou não. A cópia fica em `data\recebidos\<empresa>\<ano>\<mês>\`, com hora e nome original, e nunca é apagada nem sobrescrita. No pedido, o botão **Baixar arquivo original** entrega essa cópia. Motivo: no caso do AF127 (H2S4, 27/07) chegaram dois arquivos com o mesmo nome no mesmo dia, o segundo já com os produtos errados, e não havia como provar de onde veio. Pedidos anteriores a esta versão não têm a cópia.

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
