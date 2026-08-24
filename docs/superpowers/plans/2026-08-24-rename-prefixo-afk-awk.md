# Rename de prefixo de produto AFK→AUTH / AWK→WALK — Plano de Execução

> **Para quem executa:** este é um plano de **escrita em banco de produção**, não de código.
> O código já está pronto, testado e com dry-run rodado contra os dois bancos vivos.
> Os passos abaixo são para rodar em sequência, conferindo a saída de cada um antes do próximo.
> Steps usam checkbox (`- [ ]`) para acompanhamento.

**Goal:** trocar o prefixo do código dos 65 produtos de kit de meia nas duas Fire de
produção — `AFK*` passa a `AUTH*` e `AWK*` passa a `WALK*` — sem perder rastreabilidade
e com rollback pronto.

**Arquitetura:** um script único (`tools/rename_prefixo_produto.py`) que coleta o estado
atual, grava um snapshot em disco (que É o rollback), aplica UPDATEs otimistas por chave
primária dentro de uma transação, confere que sobrou zero prefixo antigo e só então dá
commit. `firebirdsql` não tem autocommit — fechar a conexão sem `commit()` reverte tudo.

**Tech Stack:** Python 3.11 · `firebirdsql` 1.4.6 (driver puro, wire protocol — a lib C do
FB 2.5 não existe pra arm64) · Firebird **2.5.7** nos dois servidores · acesso TCP 3050 via VPN.

**Spec:** a lista validada e o levantamento que originou este plano estão em
https://claude.ai/code/artifact/e9a3951a-6bcb-4cf1-a48c-943be04b586d

## Restrições globais

- **Janela:** executar **depois das 17h** (decisão do Samuel, 2026-08-24).
- **Regra da troca:** substituir só as 3 primeiras letras. `AFK3S-A-100-3338` → `AUTH3S-A-100-3338`.
  O resto do código não muda. Nunca truncar: código novo tem 17 chars, coluna é `VARCHAR(30)`.
- **Escopo confirmado pelo Samuel:** os **dois** bancos, e **`PRODUTOS_TERCEIROS` entra**.
- **`NOTAPROD.CODPARANFE` NÃO É TOCADO** — 3.465 linhas na Nasmar e 4.884 na Americanense
  são histórico de NF já emitida. Documento fiscal fechado.
- **Senha nunca em arquivo.** `FB_PASSWORD` vai por variável de ambiente na hora.
- **VPN precisa estar de pé** nos dois hosts (`192.168.15.4` e `192.168.15.7`, porta 3050).

## Os dois bancos (identidade verificada em `CONFIG`, não pelo nome do arquivo)

| Alvo | Host | Arquivo | Empresa registrada | Linhas a alterar |
|---|---|---|---|---|
| `nasmar` | 192.168.15.4 | `C:\FireAdmMM\MM_CONFECCAO.FDB` | NASMAR COMÉRCIO DE ROUPAS (34.513.679/0001-34) | **187** |
| `americanense` | 192.168.15.7 | `C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB` | M.M. AMERICANENSE (35.394.871/0001-11) | **133** |

> ⚠️ O arquivo `.4` se chama `MM_CONFECCAO.FDB` mas a empresa dentro dele é a **Nasmar**.
> Não existe um terceiro banco "MM Confecção". Ver `memory/project_fire_live_vpn.md`.

**320 linhas no total.** Por banco: 65 em `PRODUTOS.CODPROD_ALTERN` + 65 em
`PRODUTOS.CODPARANFE` + 57 (Nasmar) / 3 (Americanense) em `PRODUTOS_TERCEIROS.CODPROD_TERCEIRO`.

## Arquivos

- Criado: `tools/rename_prefixo_produto.py` — coleta, snapshot, apply, rollback
- Criado: `tests/test_rename_prefixo_produto.py` — 21 testes (transformação + guardas)
- Gerado em runtime: `output/rename_prefixo/snapshot_<banco>_<timestamp>.json` (gitignored)

---

## Task 1: Pré-voo

**Objetivo:** provar que a VPN está de pé e que o estado do banco ainda é o que o
levantamento viu. Se qualquer número divergir, **pare** — alguém mexeu no cadastro.

- [ ] **Step 1: Confirmar a hora**

Não executar antes das 17h.

- [ ] **Step 2: Testar a rota até os dois servidores**

```bash
for h in 192.168.15.4 192.168.15.7; do nc -z -G 3 $h 3050 && echo "$h OK"; done
```

Esperado: as duas linhas `OK`. Se falhar, reconectar a VPN antes de seguir.

- [ ] **Step 3: Exportar a senha na sessão**

```bash
export FB_PASSWORD='masterkey'
```

- [ ] **Step 4: Rodar os testes do script**

```bash
cd "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos"
.venv/bin/pytest tests/test_rename_prefixo_produto.py -q
```

Esperado: `21 passed`.

- [ ] **Step 5: Dry-run nos dois bancos**

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python tools/rename_prefixo_produto.py --banco todos
```

Esperado, exatamente:

```
NASMAR ........  PRODUTOS.CODPARANFE 65 · PRODUTOS.CODPROD_ALTERN 65 · PRODUTOS_TERCEIROS 57 · TOTAL 187
AMERICANENSE ..  PRODUTOS.CODPARANFE 65 · PRODUTOS.CODPROD_ALTERN 65 · PRODUTOS_TERCEIROS  3 · TOTAL 133
```

Se qualquer contagem divergir de 187 / 133, **parar e investigar** — o cadastro mudou
desde o levantamento de 24/08.

- [ ] **Step 6: Guardar o caminho dos dois snapshots**

O dry-run imprime `snapshot: output/rename_prefixo/snapshot_<banco>_<timestamp>.json`.
Anotar os dois. **São eles que revertem a alteração.** O apply gera snapshots novos —
use sempre o gerado pelo próprio apply para rollback.

---

## Task 2: Aplicar na Nasmar (`.4`)

**Objetivo:** escrever as 187 linhas. Banco menor primeiro — se algo der errado, o
estrago é menor e a Americanense continua intacta.

- [ ] **Step 1: Aplicar**

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python tools/rename_prefixo_produto.py \
    --banco nasmar --apply
```

Esperado na última linha: `APLICADO e commitado: 187 linhas.`

O script já garante, dentro da mesma transação e **antes** do commit:
1. cada UPDATE casou exatamente 1 linha (`WHERE SEQ=? AND TRIM(col)=?`);
2. sobrou zero código com prefixo `AFK`/`AWK` em `PRODUTOS` e `PRODUTOS_TERCEIROS`.

Qualquer falha levanta exceção → `conn.close()` sem commit → **rollback automático**.

- [ ] **Step 2: Conferir na fonte**

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python - <<'PY'
import os, firebirdsql
c = firebirdsql.connect(host="192.168.15.4", port=3050,
                        database=r"C:\FireAdmMM\MM_CONFECCAO.FDB",
                        user="SYSDBA", password=os.environ["FB_PASSWORD"], charset="WIN1252")
cur = c.cursor()
for rotulo, pref in (("antigo (esperado 0)", ("AFK", "AWK")), ("novo (esperado 65)", ("AUTH", "WALK"))):
    cur.execute("SELECT COUNT(*) FROM PRODUTOS WHERE UPPER(CODPROD_ALTERN) STARTING WITH ? "
                "OR UPPER(CODPROD_ALTERN) STARTING WITH ?", pref)
    print(f"  PRODUTOS {rotulo}: {cur.fetchone()[0]}")
cur.execute("SELECT FIRST 3 SEQ, CODPROD_ALTERN, CODPARANFE, DESCRICAO FROM PRODUTOS "
            "WHERE UPPER(CODPROD_ALTERN) STARTING WITH 'AUTH' ORDER BY CODPROD_ALTERN")
for r in cur.fetchall():
    print("   ", r[0], str(r[1]).strip(), "| NF:", str(r[2]).strip(), "|", str(r[3]).strip()[:40])
cur.execute("SELECT COUNT(*) FROM NOTAPROD WHERE UPPER(CODPARANFE) STARTING WITH 'AFK' "
            "OR UPPER(CODPARANFE) STARTING WITH 'AWK'")
print("  NOTAPROD intocado (esperado 3465):", cur.fetchone()[0])
c.close()
PY
```

Esperado: antigo `0` · novo `65` · `CODPARANFE` acompanhando o código novo ·
`NOTAPROD` ainda em **3465** (o histórico fiscal não pode ter mudado).

- [ ] **Step 3: Checar na tela do Fire**

Abrir o cadastro de produtos no Fire da Nasmar e buscar `AUTH3S-A-100-3338`.
Tem que achar. Quem estiver com o Fire aberto desde antes precisa reabrir a tela —
o app cacheia a lista.

---

## Task 3: Aplicar na Americanense (`.7`)

**Objetivo:** as 133 linhas do segundo banco. Só rodar depois da Task 2 conferida.

- [ ] **Step 1: Aplicar**

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python tools/rename_prefixo_produto.py \
    --banco americanense --apply
```

Esperado: `APLICADO e commitado: 133 linhas.`

- [ ] **Step 2: Conferir na fonte**

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python - <<'PY'
import os, firebirdsql
c = firebirdsql.connect(host="192.168.15.7", port=3050,
                        database=r"C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB",
                        user="SYSDBA", password=os.environ["FB_PASSWORD"], charset="WIN1252")
cur = c.cursor()
for rotulo, pref in (("antigo (esperado 0)", ("AFK", "AWK")), ("novo (esperado 65)", ("AUTH", "WALK"))):
    cur.execute("SELECT COUNT(*) FROM PRODUTOS WHERE UPPER(CODPROD_ALTERN) STARTING WITH ? "
                "OR UPPER(CODPROD_ALTERN) STARTING WITH ?", pref)
    print(f"  PRODUTOS {rotulo}: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM NOTAPROD WHERE UPPER(CODPARANFE) STARTING WITH 'AFK' "
            "OR UPPER(CODPARANFE) STARTING WITH 'AWK'")
print("  NOTAPROD intocado (esperado 4884):", cur.fetchone()[0])
c.close()
PY
```

Esperado: antigo `0` · novo `65` · `NOTAPROD` ainda em **4884**.

- [ ] **Step 3: Conferir que o subgrupo MEIAS não foi afetado**

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python - <<'PY'
import os, firebirdsql
c = firebirdsql.connect(host="192.168.15.7", port=3050,
                        database=r"C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB",
                        user="SYSDBA", password=os.environ["FB_PASSWORD"], charset="WIN1252")
cur = c.cursor()
cur.execute("SELECT COUNT(*) FROM PRODUTOS WHERE CODSUBGRUPO = 1")
print("  subgrupo MEIAS (esperado 748):", cur.fetchone()[0])
c.close()
PY
```

Esperado: **748**. O rename mexe em código, não em subgrupo — se esse número mudou,
alguma coisa saiu do escopo.

---

## Task 4: Fechamento

- [ ] **Step 1: Guardar os snapshots do apply**

Os dois `output/rename_prefixo/snapshot_*.json` gerados pelo `--apply` são o rollback.
`output/` é gitignored, então copiar pra um lugar que sobrevive à máquina.

- [ ] **Step 2: Conferir o de-para de produto do Portal na produção**

O SQLite por ambiente tem a tabela `produto_depara`, que guarda `fire_codigo`. Se algum
de-para salvo apontar pra um código `AFK*`/`AWK*`, ele fica órfão depois do rename.
No servidor do cliente:

```sql
SELECT client_key, chave_valor, fire_codigo, fire_nome
FROM produto_depara
WHERE fire_codigo LIKE 'AFK%' OR fire_codigo LIKE 'AWK%';
```

Se voltar vazio, nada a fazer. Se voltar linhas, atualizar `fire_codigo` para o código
novo (o `fire_produto_id` continua correto — a chave durável é o SEQ, que não muda).

- [ ] **Step 3: Avisar o time da MM**

O código mudou no cadastro e na NF-e. Notas já emitidas continuam com o código antigo —
isso é correto e proposital.

---

## Rollback

Vale para qualquer momento depois do apply. Usa o snapshot gerado **pelo próprio apply**:

```bash
FB_PASSWORD="$FB_PASSWORD" .venv/bin/python tools/rename_prefixo_produto.py \
    --banco nasmar --rollback output/rename_prefixo/snapshot_nasmar_<timestamp>.json
```

O rollback é otimista igual ao apply: escreve o valor antigo de volta só onde o valor
atual ainda é o novo. Se alguém já editou o código à mão depois do rename, aquela linha
não casa e o rollback aborta inteiro sem gravar — aí é caso a caso.

## O que este plano NÃO cobre

- **Backup completo dos `.fdb`.** Os servidores rodam Firebird 2.5.7 e não tenho `gbak`
  2.5 nesta máquina (só o framework 5.0, incompatível). O que existe é o snapshot linha a
  linha, que reverte exatamente estas 320 linhas — mesmo procedimento aceito na criação do
  subgrupo MEIAS em julho. Se você quiser um backup de arquivo inteiro antes, tem que sair
  da rotina do próprio Fire, no servidor Windows.
- **Mudança de descrição, marca ou qualquer outro campo.** Só o código muda.
- **Lado Flow / Gestor.** O catálogo casa por `fire_produto_id` (o SEQ), que não muda —
  o rename é invisível pra lá. Confirmado em `app/integrations/flowpcp/catalogo_schema.py:13`.
