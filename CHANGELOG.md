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

1) **A simulação do catálogo mostra o impacto na fila de produção, os kits que o Flow segurou e por quê, e os códigos do Apontaê que não existem lá.** O impacto já vinha do Flow, mas a tela procurava no lugar errado e não mostrava.
2) **Kit cuja quantidade no nome não bate com a composição do Fire fica congelado no Flow.** Ele continua no catálogo, mas sem composição: o Flow não muda o que tem até o Fire ser corrigido (a composição ou o nome), e aí o kit sobe sozinho no envio seguinte. A tela lista esses kits como congelados.

---

## 20261016-0942

1) **O catálogo pode informar ao Flow do que cada kit é feito e o código de cada par no Apontaê.** Fica desligado até o Flow estar pronto para receber, e liga por empresa. Kit que não tem composição no Fire não apaga a que o PCP cadastrou no Flow. Se o portal não conseguir ler a composição no Fire, ele não manda nada sobre composição, e o Flow mantém a que tem. A tela do ambiente no admin agora mostra o que foi enviado, o que o Flow mudaria, e traz o botão "Gravar liberando a trava" quando o Flow está segurando uma mudança em massa.
2) **Produto com cadastro errado no Fire pode ficar fora de todo envio de catálogo**, inclusive do envio diário, até ser corrigido.
3) **Gravar o catálogo no Flow não repete o envio quando o Flow demora**, e a tela avisa quando o Flow já está gravando.
4) **Há um botão "Reiniciar o portal" em Atualização do sistema**, para o portal passar a valer uma mudança na configuração do servidor sem precisar de acesso remoto; ele só fica liberado quando não há atualização em andamento nem pacote esperando.

---

## 20261015-2000

1) **O envio manual do catálogo ao Flow pode deixar produtos de fora.** Quando um cadastro do Fire tem problema (por exemplo, o mesmo par cadastrado duas vezes), dá para mandar o catálogo sem ele, em vez de segurar o catálogo inteiro até o Fire ser corrigido. Vale só para aquele envio: o envio diário continua mandando todos, por isso ele fica só em conferência enquanto o Fire não for corrigido. O catálogo do portal continua com todos os produtos. Se um código pedido para ficar de fora não existir no catálogo, nada é enviado e o portal avisa qual é.

---

## 20261015-1810

1) **Kits e packs chegam ao Flow como kit e pack, nunca como par.** O catálogo só marcava como kit o produto que tinha a composição cadastrada no Fire; os kits sem composição (como os Kings e os POLO MATCH) iam como se fossem um par, e o Flow poderia mandar produzir o kit inteiro como se fosse uma meia. Agora todo produto com unidade KIT ou PACK, ou com KIT ou PACK no nome, vai como kit ou pack. Kit sem composição fica parado no Flow com o aviso "sem composição cadastrada" até a composição ser cadastrada no Fire.

2) **Os pares que formam os kits passam a ir no catálogo.** O par não é vendido sozinho, então ficava fora do catálogo enviado, e os pares que a fábrica produz existiam só no Flow, sem ligação com o Fire. Agora vão junto todos os produtos que compõem um kit ou pack do catálogo, em qualquer nível (pack, kit, par).

---

## 20261015-1308

1) **O catálogo de produtos do Fire vai para o Flow sozinho, todo dia às 5h, por enquanto só como conferência.** Antes ele só ia quando alguém mandava à mão (a última vez foi em julho), e todo produto cadastrado depois disso chegava no Flow como "produto desconhecido". Agora, nas empresas com "Enviar catálogo ao Flow" ligado, o catálogo vai toda madrugada para o Flow comparar com o dele, sem gravar nada. A gravação diária só é ligada depois que a primeira gravação do catálogo for conferida e aprovada.

2) **O catálogo enviado passa a ter todas as meias que a fábrica vende.** A opção "só meias" mandava apenas os produtos marcados como MEIAS no Fire e deixava de fora meias infantis e kits novos sem essa marcação (45 dos produtos dos pedidos abertos da MM). Agora ela manda as meias e também toda meia que aparece num pedido aberto ou num pedido dos últimos 12 meses, mesmo sem a marcação. Cuecas e kits de cueca, tecido e cadastros que nunca venderam continuam de fora.

---

## 20261015-0846

1) **"Verificar no Fire" volta a funcionar.** Desde 27/08 a conferência com o Fire falhava em toda rodada, na MM e na Nasmar, porque havia na lista um pedido com número de cliente maior do que o Fire aceita (mais de 20 caracteres), e o Fire recusava a consulta inteira por causa dele. Agora esse número fica de fora (ele não existe no Fire) e os outros pedidos voltam a ser conferidos: os que já estão cadastrados no Fire saem de "em revisão" sozinhos e os pedidos antigos guardados voltam a seguir para o Flow.

---

## 20261014-2242

1) **A foto completa da madrugada só sai na hora dela.** Antes, ao ligar o envio de pedidos à noite, o portal tirava a foto do dia na hora e mandava junto todos os pedidos, fora dos horários de envio (aconteceu em 14/10 às 22h20). Agora a foto só sai dentro da hora configurada (às 5h, das 5:00 às 5:59).

2) **Tela "Pedidos do ERP para o Flow" mais clara.** O campo Base já vem preenchido com o identificador do ambiente (antes o "mm" era só um exemplo cinza e o salvar dava erro), os exemplos dos campos deixam de parecer valores de verdade, e os erros aparecem em português dizendo qual campo corrigir.

---

## 20261014-2158

1) **Tudo que vai para o Flow sai em 4 horários por dia, não mais na hora.** Os pedidos importados e as mudanças dos pedidos do Fire (quando o bloco "Pedidos do ERP para o Flow" estiver ligado) saem às 8h, 11h, 14h e 17h, de segunda a sábado. Entre um horário e outro tudo fica só aguardando; nada se perde. Em cada horário o portal manda pelo menos um aviso de que está vivo, mesmo sem pedido novo. A foto completa da madrugada continua no mesmo horário. Cada horário deixa registrado quantos pedidos foram enviados, recusados pelo Flow, descartados (já faturados, cancelados ou apagados no Fire) e guardados.

2) **A foto completa da madrugada só vai para o Flow depois das mudanças que vieram antes dela.** Como as mudanças dos pedidos esperam os horários de envio, um pedido faturado à noite ficaria na foto das 5h antes de o Flow saber do faturamento. Agora, na hora da foto, o portal manda primeiro as mudanças que estavam esperando e só então a foto. É a única exceção aos horários de envio.

---

## 20261014-2136

1) **Tudo que vai para o Flow sai em 4 horários por dia, não mais na hora.** Os pedidos importados e as mudanças dos pedidos do Fire (quando o bloco "Pedidos do ERP para o Flow" estiver ligado) saem às 8h, 11h, 14h e 17h, de segunda a sábado. Entre um horário e outro tudo fica só aguardando; nada se perde. Em cada horário o portal manda pelo menos um aviso de que está vivo, mesmo sem pedido novo. A foto completa da madrugada continua no mesmo horário. Cada horário deixa registrado quantos pedidos foram enviados, recusados pelo Flow, descartados (já faturados, cancelados ou apagados no Fire) e guardados.

---

## 20261014-1940

1) **Os pedidos importados voltam a chegar ao Flow.** Desde 24/07 o envio estava parado: ele dependia de um programa auxiliar que não roda no servidor da MM. Agora o próprio portal entrega cada pedido ao Flow. Antes de enviar, o portal confere no Fire: pedido já faturado, cancelado ou apagado não vai ao Flow e o motivo fica registrado no histórico do pedido. Pedido parado que o portal ainda não achou no Fire fica guardado e segue sozinho quando a conferência com o Fire o encontrar.

2) **A data de entrega decidida no Flow ainda NÃO volta para o Fire.** Essa parte continua desligada de propósito e só será ligada depois de conferir com o Flow as decisões que ficaram pendentes desde julho.

3) **Novo bloco "Pedidos do ERP para o Flow" em Ambientes.** O portal passa a poder enviar ao Flow todos os pedidos abertos no Fire e as mudanças neles (quantidade, data, preço, itens, nota, status), mais uma foto completa por noite. Vem desligado: só começa depois de cadastrar o endereço, o tenant e a credencial do Flow e marcar "ligado".

---

## 20261014-0949

1) **Pedido do Sam's com PACK agora avisa na observação quantos kits separar.** Quando o Sam's pede um pack (ex.: `PACK C/36 KITS`), o relatório "Pedido p/ Separação" do Fire somava só os kits e o pack sumia da conta (pedido 06858299-0000: aparecia 1.872 kits, o certo são 1.908). Agora a observação do pedido sai assim: `06858299-0000 | CONTEM PACK: 1 PACK C/36 = 36 KITS | TOTAL A SEPARAR: 1.908 KITS (1.872 AVULSOS + 36 DENTRO DE PACK)`. O pedido continua entrando com o código e a quantidade do pack, como o Sam's pediu. Na revisão, a linha do pack mostra `pack c/ 36 · 36 kits` embaixo da quantidade. Pedido sem pack não muda.

---

## 20261005-1812

1) **O portal passa a registrar cada arquivo que lê, num arquivo de log para o suporte.** Nada muda na tela: o arquivo `logs\leituras.jsonl` (na pasta do portal) guarda, para cada planilha ou PDF recebido, qual leitor o interpretou, o que ele extraiu, se o cliente e os produtos foram achados no Fire, e se o arquivo parece ter sido lido errado ou já tinha sido importado. Quando uma operadora reclamar de um arquivo, o Samuel baixa esse arquivo pela VPN e descobre o motivo sem precisar pedir a planilha de novo.

---

## 20261005-1136

1) **Planilha da Authentic Feet com a coluna `QUAN.` ou `QUANT` volta a abrir certa.** O lote de 05/10 (AF072, AF082, AF108 e AF119) trazia o título da coluna de quantidade diferente do habitual (`TOTAL KITS`), e o portal lia o pedido errado: sem cliente nem CNPJ, com um item "CNPJ:" e quantidades trocadas. Agora ele lê cliente, CNPJ, quantidade e preço. As planilhas com `TOTAL KITS` continuam saindo iguais.

---

## 20261005-0858

1) **Planilha para o Fire sempre sai com o total de cada linha.** Quando o pedido do cliente não traz o valor por linha (caso da Beira Rio), o portal agora calcula quantidade × preço. Antes a coluna ia vazia e o importador do Fire podia parar com o aviso "is not a valid floating point value", deixando o pedido só com o primeiro item. Pedidos que já trazem o total continuam com o valor do documento.

---

## 20261002-1430

1) **Pedido da Beira Rio com uma só faixa de tamanho volta a abrir na revisão.** O pedido 14330456 (meias infantil e bebê, tamanhos 23/27 e 28/33) aparecia como "Formato não reconhecido ou pedido sem itens" porque o portal só entendia o layout antigo, com duas faixas por cor (33/38 e 39/44). Agora ele lê a faixa que está impressa no pedido e continua aceitando o layout antigo. A observação de cada linha traz a faixa (ex.: `23/27`).

2) **Item da Beira Rio em PAR não perde mais o preço.** O portal só lia o preço quando a unidade era KIT; item em PAR entraria no Fire sem preço. Agora lê o preço em qualquer unidade.

---

## 20260925-1405

1) **Pedido da Kings não leva mais "Produto atoalhado" (nem "sem toalha com silicone") para a observação do pedido no Fire.** Esse texto vinha da planilha da Kings e o Fire colocava só um deles na observação, confundindo a separação quando o pedido mistura os dois tipos. Nos outros clientes a observação continua igual (o "KIT 3" da Authentic Feet e da Magic Feet segue indo).

---

## 20260924-1149

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
