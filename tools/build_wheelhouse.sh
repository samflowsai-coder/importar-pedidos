#!/usr/bin/env bash
# Monta o wheelhouse: os wheels das dependencias do Portal para instalar OFFLINE
# no servidor Windows do cliente (pip --no-index --find-links). O auto-updater
# roda como SYSTEM, que nao alcanca o PyPI — por isso as deps viajam empacotadas.
#
# Alvo FIXO: Windows x64 + CPython 3.11 (a build roda no Mac; baixamos os wheels
# da plataforma-alvo, nao da local). Rode isto UMA vez (ou quando pyproject.toml
# mudar); o build_package.sh embarca a pasta so com a flag --with-wheelhouse.
#
# Saida: wheelhouse/ na raiz do repo.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT="$ROOT/wheelhouse"
PLAT="win_amd64"
PYVER="3.11"
ABI="cp311"

echo "== build_wheelhouse: alvo ${PLAT} / py${PYVER} =="
rm -rf "$OUT"
mkdir -p "$OUT"

# requirements = [project].dependencies do pyproject.toml (mesma fonte que o
# manifesto usa) + firebird-driver (driver ANTIGO — necessario para o rollback
# do update de transicao rodar OFFLINE ao restaurar o pyproject antigo) +
# setuptools/wheel (o pyproject nao tem [build-system]; o pip usa o backend
# legado com build isolation, que precisa achar setuptools/wheel — no wheelhouse
# resolve offline, sem depender do setuptools do .venv).
REQ="$(mktemp)"
trap 'rm -f "$REQ"' EXIT
python3 - "$REQ" <<'PY'
import sys, tomllib
data = tomllib.load(open("pyproject.toml", "rb"))
deps = list(data["project"]["dependencies"])
deps += ["firebird-driver>=2.0", "setuptools", "wheel"]
open(sys.argv[1], "w").write("\n".join(deps) + "\n")
PY

echo "-- deps --"; cat "$REQ"

# Passo 1: baixar wheels binarios da plataforma-alvo. Cobre os compilados
# (cryptography, pillow, pydantic-core, bcrypt, greenlet...) e os puros que tem
# wheel no PyPI. --only-binary=:all: garante que nada venha como sdist (que
# precisaria compilar no Windows).
echo "== passo 1: pip download (wheels ${PLAT}/${ABI}) =="
pip download -r "$REQ" \
    --dest "$OUT" \
    --platform "$PLAT" \
    --python-version "$PYVER" \
    --implementation cp \
    --abi "$ABI" \
    --only-binary=:all:

# Passo 2: fallback para pacotes publicados SO como sdist (ex.: fdb). Como sao
# puros (py3-none-any), gerar o wheel no Mac produz um wheel valido no Windows.
# So roda para o que ficou faltando apos o passo 1.
echo "== passo 2: fallback pip wheel (sdist-only, ex.: fdb) =="
while IFS= read -r dep; do
    [ -z "$dep" ] && continue
    name="$(echo "$dep" | sed -E 's/[<>=!~; ].*$//' | tr '[:upper:]' '[:lower:]' | tr '-' '_')"
    if ! ls "$OUT"/${name}-*.whl >/dev/null 2>&1; then
        echo "   faltou wheel de '$dep' — gerando com pip wheel --no-deps"
        pip wheel "$dep" --no-deps --wheel-dir "$OUT"
    fi
done < "$REQ"

echo "== wheelhouse pronto: $(ls "$OUT" | wc -l | tr -d ' ') wheels, $(du -sh "$OUT" | cut -f1) =="
# Garante que os criticos estao la (falha alto se faltar algum).
for must in fdb firebird_driver setuptools wheel cryptography; do
    if ! ls "$OUT"/${must}-*.whl >/dev/null 2>&1; then
        echo "ERRO: wheel obrigatorio ausente no wheelhouse: $must" >&2
        exit 1
    fi
done
echo "OK — fdb, firebird_driver, setuptools, wheel, cryptography presentes."
