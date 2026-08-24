#!/usr/bin/env python3
"""Extrai as notas de uma versão do CHANGELOG.md para o `manifest.json` do pacote.

Chamado por `tools/build_package.sh`. Imprime o texto já como string JSON, pronto
para ser interpolado no manifest.

Ordem de resolução:

1. A seção `## <versao>` com o nome EXATO da versão que está sendo buildada.
2. Se não houver, a seção do topo — na prática `## Não publicado`, que é onde o
   texto é escrito enquanto o trabalho anda.
3. Se o CHANGELOG não existir ou estiver vazio, o legado `RELEASE_NOTES.txt`.

O legado era um arquivo solto na raiz, sobrescrito a cada build: as notas de uma
versão desapareciam quando a próxima era escrita. O CHANGELOG mantém o histórico
versionado, e é a mesma fonte que alimenta o corpo do GitHub Release.

Uso:
    python3 tools/extract_notes.py CHANGELOG.md 20260824-1530 RELEASE_NOTES.txt
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Cap alinhado com o que a tela `/admin/atualizacao` mostra sem virar parede de
# texto. O manifest não é lugar para changelog inteiro.
_MAX_CHARS = 4000

_HEADING = re.compile(r"^## +(.+?) *$", re.MULTILINE)
# `---` separando seções é diagramação, não conteúdo.
_TRAILING_RULE = re.compile(r"\n+-{3,}\s*$")


def parse_sections(texto: str) -> list[tuple[str, str]]:
    """Devolve `(titulo, corpo)` de cada `## ...`, na ordem do arquivo."""
    partes = _HEADING.split(texto)
    # partes = [preâmbulo, titulo1, corpo1, titulo2, corpo2, ...]
    return [
        (partes[i].strip(), _TRAILING_RULE.sub("", partes[i + 1]).strip())
        for i in range(1, len(partes) - 1, 2)
    ]


def notes_for(changelog: Path, version: str, legacy: Path) -> str:
    if changelog.exists():
        secoes = parse_sections(changelog.read_text(encoding="utf-8"))
        for titulo, corpo in secoes:
            if titulo == version and corpo:
                return corpo
        if secoes and secoes[0][1]:
            return secoes[0][1]

    if legacy.exists():
        return legacy.read_text(encoding="utf-8").strip()

    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "uso: extract_notes.py <CHANGELOG.md> <versao> <RELEASE_NOTES.txt>",
            file=sys.stderr,
        )
        return 2
    texto = notes_for(Path(argv[1]), argv[2], Path(argv[3]))
    print(json.dumps(texto[:_MAX_CHARS]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
