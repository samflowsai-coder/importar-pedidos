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
# Passo 1: baixar POR PACOTE (nao `-r $REQ` em bloco). Sob `set -e`, o bloco
# aborta TUDO se UM pacote perder wheel Windows, deixando o fallback abaixo como
# codigo morto. Por-pacote e tolerante: a falha vai pro fallback, os demais ja
# baixaram. A arvore transitiva vem junto em cada `pip download` (redundancia de
# wheels no wheelhouse e inofensiva -- o pip resolve o set final no install).
echo "== passo 1: pip download por-pacote (wheels ${PLAT}/${ABI}) =="
MISSING=()
while IFS= read -r dep; do
    [ -z "$dep" ] && continue
    if pip download "$dep" \
            --dest "$OUT" --platform "$PLAT" --python-version "$PYVER" \
            --implementation cp --abi "$ABI" --only-binary=:all: >/dev/null 2>&1; then
        echo "   ok: $dep"
    else
        echo "   sem wheel ${PLAT} para '$dep' -> fallback"
        MISSING+=("$dep")
    fi
done < "$REQ"

# Passo 2: fallback para pacotes publicados SO como sdist (ex.: fdb). Geramos o
# wheel no Mac; so serve se for PURO (py3-none-any / py2.py3-none-any). Se sair
# um wheel de PLATAFORMA (macosx_*, compilado), rejeitamos -- nao rodaria no
# Windows; esse pacote precisa de um wheel win_amd64 vindo do PyPI.
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "== passo 2: fallback pip wheel (${#MISSING[@]} pacote(s)) =="
    for dep in "${MISSING[@]}"; do
        tmpw="$(mktemp -d)"
        pip wheel "$dep" --no-deps --wheel-dir "$tmpw"
        for whl in "$tmpw"/*.whl; do
            case "$(basename "$whl")" in
                *-none-any.whl)
                    cp "$whl" "$OUT/"; echo "   fallback ok (puro): $(basename "$whl")" ;;
                *)
                    echo "ERRO: '$dep' gerou wheel de PLATAFORMA ($(basename "$whl")) -- nao serve" >&2
                    echo "      offline no Windows. Esse pacote precisa de wheel win_amd64 no PyPI." >&2
                    rm -rf "$tmpw"; exit 1 ;;
            esac
        done
        rm -rf "$tmpw"
    done
fi

echo "== wheelhouse pronto: $(ls "$OUT" | wc -l | tr -d ' ') wheels, $(du -sh "$OUT" | cut -f1) =="
# Garante que os criticos estao la (falha alto se faltar algum).
for must in fdb firebird_driver setuptools wheel cryptography; do
    if ! ls "$OUT"/${must}-*.whl >/dev/null 2>&1; then
        echo "ERRO: wheel obrigatorio ausente no wheelhouse: $must" >&2
        exit 1
    fi
done
echo "OK — fdb, firebird_driver, setuptools, wheel, cryptography presentes."
