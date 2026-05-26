# Portal de Pedidos — Instalação no Servidor

Guia passo a passo para instalar e manter o Portal de Pedidos em um servidor
Windows. O app roda **somente no servidor**; as outras máquinas (estações de
trabalho) **não instalam nada** — acessam pelo navegador.

---

## 1. Pré-requisitos (servidor)

- Windows 10 / 11 ou Windows Server (2016+).
- Conexão com a internet **durante a instalação** (para baixar o Python e as
  dependências). Depois de instalado, funciona offline na rede local.
- Recomendado: **IP fixo** no servidor (IP estático no Windows ou reserva de
  DHCP no roteador), para que o endereço de acesso nunca mude.

> Não precisa instalar Python manualmente. O instalador detecta e, se faltar,
> instala o Python 3.11 automaticamente (via winget).

---

## 2. Instalação (primeira vez)

1. Copie o arquivo `portal-pedidos-AAAAMMDD.zip` para o servidor (ex.:
   `C:\PortalPedidos\`).
2. Clique com o botão direito no `.zip` → **Extrair tudo**.
3. Entre na pasta extraída e dê **duplo-clique em `instalar.bat`**.
4. Responda as perguntas:
   - **OPENROUTER_API_KEY** — chave para o processamento de PDFs complexos.
   - **Modo de exportação** — `1` (xlsx) é o recomendado para começar.
   - **Porta** — Enter para usar `3636`.
   - **Acesso pela rede** — escolha **`1` (Rede local)** para que as outras
     máquinas acessem pelo IP. (Mesmo assim o Portal **não fica exposto na
     internet** — a liberação no firewall é só para redes Particular/Domínio.)
   - **Usuário admin** — e-mail e senha de acesso ao Portal.
5. No final, anote o endereço mostrado, algo como:
   `http://192.168.x.x:3636`.

---

## 3. Deixar no ar automaticamente (recomendado para servidor)

Para o Portal subir sozinho sempre que o servidor ligar/reiniciar, **sem
precisar de ninguém logado**:

1. Dê **duplo-clique em `setup-service.bat`**.
2. Clique **Sim** no aviso de Administrador (UAC).

Isso registra uma Tarefa Agendada que:
- inicia no boot do Windows;
- roda invisível, em segundo plano;
- reinicia sozinha se cair.

> Com o serviço ativo, você **não precisa** abrir o `iniciar.bat`. Ele só serve
> para subir manualmente em testes.

---

## 4. Acesso pelas estações de trabalho

Em qualquer PC ou celular **na mesma rede**, abra o navegador em:

```
http://<IP-DO-SERVIDOR>:3636
```

(ex.: `http://192.168.1.50:3636`). Faça login com o usuário admin criado.

---

## 5. Atualização (enviar uma nova versão)

Quando você receber um novo pacote `portal-pedidos-AAAAMMDD.zip`:

1. **Extraia o novo `.zip` por cima da pasta atual**, substituindo os arquivos.
   - O `.env` (configurações/senhas) e os dados (usuários, histórico) **não
     são tocados** — eles não vêm no pacote.
2. Dê **duplo-clique em `atualizar.bat`**.
   - Ele para o serviço, atualiza as dependências e reinicia sozinho.

Nada de reconfigurar: usuários, chave e configurações permanecem.

---

## 6. Operação do dia a dia

| Ação | Como |
|------|------|
| Ver se o serviço está rodando | PowerShell: `Get-ScheduledTask -TaskName PortalPedidos \| Select State` |
| Parar / iniciar manualmente | `Stop-ScheduledTask` / `Start-ScheduledTask -TaskName PortalPedidos` |
| Subir na mão (sem serviço) | duplo-clique em `iniciar.bat` |
| Remover o auto-start | `desinstalar.bat` (como Administrador) |

---

## 7. Solução de problemas

- **Outra máquina não acessa pelo IP** — confirme que escolheu "Rede local" na
  instalação (no `.env` deve constar `PORTAL_HOST=0.0.0.0`). Rode `instalar.bat`
  de novo se precisar recriar a regra de firewall.
- **O endereço mudou** — o servidor está com IP dinâmico. Configure um IP fixo
  (estático ou reserva de DHCP).
- **Porta 3636 ocupada** — edite `PORTAL_PORT` no `.env` para outra porta e rode
  `atualizar.bat` (ou reinicie o serviço).
- **Esqueci a senha do admin / criar outro usuário** — no servidor, dentro da
  pasta:
  `\.venv\Scripts\python.exe tools\create_user.py email@exemplo.com --role admin`

---

## Resumo rápido

```
Servidor (1 vez):   instalar.bat  →  setup-service.bat (Admin)
Estações:           navegador → http://<IP-do-servidor>:3636
Atualizar:          extrair novo zip por cima  →  atualizar.bat
```
