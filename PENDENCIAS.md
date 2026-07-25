# Pendências — Portal de Pedidos

> Atualizado: 2026-07-01. Nada aqui é código do importador (esse está 100% mergeado
> na `main`, testado e empacotado). São itens que dependem do Samuel ou do lado Flow.

## ✅ Fechado nesta sessão (contexto)
- Catálogo Fase 0 (Importador): contrato + extrator + push (PR #23), shape da resposta alinhado ao Flow (PR #24).
- Bugs do importador: Magic Feet **desmembramento** coluna-total "MF" (PR #25) e Magic Feet **pedido loja-única** lia cor no lugar da qty (PR #26).
- Engine Firebird FB 5.0 restaurada em `~/firebird-5.0` (estável); `.env` corrigido.
- Pacote de deploy atual: `dist/portal-pedidos-20260701.zip` (main `d88cc65`).
- Estado: `main` limpa/sincronizada, sem PR aberto, suíte 575 verde, ruff limpo.

---

## ⬜ Pendências externas

### 1. Deploy no cliente (importador — você + Claude, ao vivo)
- [ ] Pacote pronto: `dist/portal-pedidos-20260701.zip`.
- [ ] **Gatilho:** no cliente, chamar o Claude com *"instalar pacote no cliente"*.
- [ ] Fluxo: Passo 0 inspeção (`data\app_shared.db` existe?) → backup total → extrair zip **por cima** (nunca apagar-e-extrair) → `atualizar.bat` → verificar (login + ambientes + senha Firebird decifra + histórico).
- **Depende de:** estar no local + descobrir a versão atual do cliente.

### 2. Limpeza do mock em prod (lado Flow — você)
- [ ] Apagar o pedido de teste `SMOKE-MOCK-20260630-01` (WMB Supermercados) que caiu na base de prod pelo dev server.
- IDs dos itens: `ea26e640-5bd9-4e45-ac13-08b87da4ff34`, `05cbc056-400e-40b0-91d7-7f0073ae9d36`.
- **Depende de:** resolver os gaps no Flow.

### 3. Fly prod — auth 401 (infra/Flow — você decide)
- [ ] O `IMPORTADOR_SERVICE_TOKEN` do Fly (`gestor.samflowsai.com.br`) ≠ token do MM → 401 no endpoint deployado.
- [ ] Alinhar o secret (`fly secrets set IMPORTADOR_SERVICE_TOKEN=<token do MM>`) **ou** passar o token de prod ao Claude → aí fecha o teste do Fly HTTP.
- Nota: o endpoint E O CÓDIGO já funcionam (validado no dev server local com dado de prod). Falta só a auth do deploy.

### 4. Catálogo Fase 1 (promote) + bootstrap (Flow + sua aprovação)
- [ ] Dry-run já roda: fireTotal=3421, flowTotal=827, **matchLimpo=261, fireOnly=3160, flowOnly=566**, firePkPresente=todos.
- [ ] Promote (`dryRun=false`): cria os 3160 fire-only, casa os 261.
- [ ] Bootstrap dos 566 flow-only: match assistido (descrição/EAN) — **nunca desativar flow-only**.
- **Depende de:** migration `produtos_fire_staging` no pcp-app **aprovada por você**.

---

## ✅ Fechado em 2026-07-04 (integração de PRODUTO ligada em prod)

- **#3 (auth 401) RESOLVIDO** — `IMPORTADOR_SERVICE_TOKEN` setado no `flowpcp` (`1753eebd…`) +
  `catalogoFireAtivo=true` no tenant MM. Probe dry-run em prod = **HTTP 200**.
- **#4 (Fase 1 promote) FEITO e DEPLOYADO** — motor de promote com bootstrap (pcp-app **PR #155**)
  + batelamento dos updates via upsert em lote (**PR #156**), ambos mergeados e deployados. Dry-run
  real: fire=3421, flow=3429, **match=3421, criar=0, flow_only=8** (catálogo já alinhado por código).
- **Importador (PR #28, aberto):** config tool `configurar_flowpcp.py` (`--token`/`--promover`,
  timeout 300), botões Simular/Promover na tela de ambiente, `sincronizar-catalogo.bat`, e pacote
  **1-clique** `dist/portal-pedidos-20260704.zip` (`promover-prod.bat` c/ token embutido, fora do git).
- **Aplicar no cliente:** extract-over + `atualizar.bat` → responder **S** em "Configurar PROD e
  PROMOVER produtos agora?". Caveat: se a Fire viva precisar de senha Firebird não setada, o tool
  reporta e para → rodar `configurar-integracao.bat` 1x pra setar a senha, depois repetir.

---

## ✅ Fechado em 2026-07-11 (pedido→Flow no modo xlsx + catálogo local com gate)

Decisão do Samuel: **Fire segue via XLS/manual; Flow já recebe pedido; catálogo puxa do Fire
e fica no importador, envio ao Flow é opt-in.** PR **#29** (`importar-pedidos`, aberto):
- **Pedido:** `export-xlsx` agora dispara `push_new_order` ao Flow (gated `flowpcp_enabled`;
  Flow deduplica por `externalId:idx`; audita `flowpcp_push`). Fire continua manual.
- **Produto:** tabela `catalogo_fire` (cópia local, snapshot por sync) + gate
  `flowpcp_catalogo_push` (default OFF; checkbox "Enviar catálogo ao Flow"). Sync sempre
  atualiza local; só envia com gate ON. `--promover` liga o gate explicitamente.
- Pacote final: **`dist/portal-pedidos-20260711.zip`** (PR #30). Suíte 604 verde.
- **1-clique alinhado à intenção:** `atualizar.bat` → **S** em "Ligar FlowPCP (pedido -> Flow +
  catálogo local)?" roda `ligar-flowpcp.bat` = liga pedido→Flow + sync catálogo **LOCAL** (não
  envia). Enviar catálogo ao Flow = `enviar-catalogo-flow.bat` (opt-in) OU checkbox na tela.
- Firebird indisponível **não bloqueia** o pedido→Flow (o push não usa Firebird).

## ⬜ Backlog — voltar depois (catálogo Fatia 1 §4.6 + Fatia 2)

### 5. Tela de reconciliação no Flow (Fatia 1 §4.6 — fast-follow)
- [ ] "Quem olha quando não casa": mostra o resultado do último promote (criados/atualizados/
      divergências flow-only/ambíguos/erros por item). Candidatos de rota: aba em
      `configuracoes/integracoes/` ou `produtos/` (confirmar no build, não inventar).
- [ ] RBAC: leitura `produtos.read`; ações `produtos.write`. Registrar no `screen-registry.ts`.
- O motor JÁ popula sem ela — é observabilidade, não bloqueio.

### 6. Fatia 2 — botão "Sincronizar" DENTRO do Flow (on-demand)
- [ ] Só necessária se quiser disparar o sync **de dentro do Flow** (hoje o importador força sozinho
      pelo `--promover`/botão, sem ela).
- [ ] Precisa **migration nova** `importador_comandos` (canal de comando) + `GET /comandos` (poll) +
      server action do botão. Spec: `pcp-app/docs/superpowers/specs/2026-07-04-forcar-sync-produtos-fire-design.md` §5.
- **Depende de:** sua aprovação da migration.

### 7. Limpar cadastros Flow (cruft do seed errado — Flow + sua aprovação)
- [ ] Remover os **181 `AME-` legado** do catálogo MM do Flow (`produtos`, tenant `1798c3c5…`):
      `codigo LIKE 'AME-%'`, `ativo=false`, `fonte='legado_americanense'`, `fire_produto_id=NULL`.
      São resquício do seed Americanense errado de 14/07 (banco/decisão já revertidos — produção = `.7`).
- [ ] **Como:** checar refs antes de deletar (`produto_componentes`, `pedido_items`, `ordens_producao`,
      `produto_codigos_cliente`) — mesmo padrão do FIO 2414. Alguns legado são referenciados por OPs/
      pedidos de TESTE (o DELETE atômico já falhou antes por FK) → deletar os órfãos, e p/ os referenciados
      limpar o dado de teste primeiro ou neutralizar.
- **Urgência baixa:** estão inativos e `fire_produto_id=NULL` → não afetam o match de pedido/catálogo.
- Contexto: são os "só Flow" que sobrariam num dry-run de catálogo. Ver memórias `project-fire-live-vpn`
      e `project-catalogo-meias-fire`.
