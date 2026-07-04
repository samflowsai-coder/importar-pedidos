#!/usr/bin/env bash
#
# Monta o pacote de distribuicao do Portal de Pedidos (zip) para instalar em
# servidor Windows. Funciona em macOS e Linux. Usa lista de inclusao explicita
# (allowlist) para nunca empacotar segredos, dados ou artefatos de dev.
#
# Uso:  ./tools/build_package.sh
# Saida: dist/portal-pedidos-AAAAMMDD.zip

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

NAME="portal-pedidos"
STAMP="$(date +%Y%m%d)"
TMP="$(mktemp -d)"
STAGE="$TMP/$NAME"
mkdir -p "$STAGE"

# ── Conteudo do pacote (allowlist) ───────────────────────────────────────────
cp -R app scripts tools "$STAGE/"
cp ui.py main.py pyproject.toml .env.example "$STAGE/"
cp instalar.bat iniciar.bat atualizar.bat setup-service.bat desinstalar.bat "$STAGE/"
cp configurar-integracao.bat sincronizar-catalogo.bat "$STAGE/"
# promover-prod.bat carrega o token de prod embutido — específico do cliente,
# fora do git (.gitignore). Só entra no pacote se existir no working tree.
[ -f promover-prod.bat ] && cp promover-prod.bat "$STAGE/"
cp README.md INSTALACAO-SERVIDOR.md "$STAGE/"

# Nao empacotar o proprio script de build
rm -f "$STAGE/tools/build_package.sh"

# ── Limpeza: artefatos de dev e qualquer segredo/dado que tenha vazado ───────
find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$STAGE" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.DS_Store' \) -delete
find "$STAGE" -type f \( \
    -name '.env' -o -name '.env.*' -o -name 'config.json' -o \
    -name 'firebird.json' -o -name '.secret.key' -o \
    -name '*.fdb' -o -name '*.fbk' -o -name '*.gbk' -o \
    -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' \
    \) ! -name '.env.example' -delete

# ── Quebras de linha CRLF para os arquivos executados no Windows ─────────────
# .bat exige CRLF (LF puro pode quebrar goto/labels); .ps1 e o template .env
# tambem ficam CRLF por seguranca.
while IFS= read -r -d '' f; do
    perl -i -pe 's/\r?\n/\r\n/' "$f"
done < <(find "$STAGE" -type f \( -name '*.bat' -o -name '*.ps1' \) -print0)
perl -i -pe 's/\r?\n/\r\n/' "$STAGE/.env.example"

# ── Empacotar ────────────────────────────────────────────────────────────────
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/${NAME}-${STAMP}.zip"
rm -f "$OUT"
( cd "$TMP" && zip -rq "$OUT" "$NAME" )
rm -rf "$TMP"

echo "Pacote criado: $OUT"
unzip -l "$OUT" | tail -n 1
