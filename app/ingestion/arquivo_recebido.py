"""Guarda imutável de todo arquivo que o portal recebe, antes do parse.

Motivação real (AF127/AF017, H2S4, 27/07/2026): dois arquivos com o mesmo
nome chegaram no mesmo dia, o segundo já vinha com os códigos errados, foi
exportado e faturado — e não existia cópia nossa do segundo pra provar de
onde veio. Aqui, tudo que entra (upload ou pasta vigiada) ganha uma cópia
em `<APP_DATA_DIR>/recebidos/<ambiente>/<AAAA>/<MM>/`, nomeada por hora +
hash + nome original, que nunca é sobrescrita nem apagada pela retenção.

Escrita que falha LEVANTA — quem chama decide se bloqueia. A garantia é
"100% dos arquivos"; best-effort silencioso viraria 97% sem ninguém saber.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Windows não aceita nenhum destes num nome de arquivo; Unix aceita quase tudo,
# mas a cópia vai pro servidor do cliente (Windows). Controle 0x00–0x1f idem.
_PROIBIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SEM_AMBIENTE = "_sem-ambiente"
_MAX_STEM = 80


@dataclass(frozen=True)
class Recebido:
    path: Path
    sha256: str


def raiz_recebidos() -> Path:
    """`<APP_DATA_DIR>/recebidos` — mesma raiz dos SQLite, nunca a pasta do cliente."""
    from app.persistence.router import data_dir

    return data_dir() / "recebidos"


def nome_seguro(nome_original: str) -> str:
    """Só o nome-base, sem caracteres que o Windows rejeita, sem `..`, com
    tamanho limitado. Acento fica: é legível e NTFS aceita."""
    nome = (nome_original or "").replace("\\", "/").rsplit("/", 1)[-1]
    nome = _PROIBIDOS.sub("_", nome).replace("..", "_")
    nome = re.sub(r"\s+", " ", nome).strip(" .")
    if not nome:
        nome = "arquivo"
    p = Path(nome)
    return p.stem[:_MAX_STEM] + p.suffix.lower()


def guardar(
    raw: bytes,
    nome_original: str,
    *,
    raiz: Path,
    ambiente: str | None,
    agora: datetime | None = None,
) -> Recebido:
    """Grava `raw` em `<raiz>/<ambiente>/<AAAA>/<MM>/<AAAAMMDD-HHMMSS>_<sha12>_<nome>`.

    Nunca sobrescreve: colisão (mesmo segundo, mesmo conteúdo, mesmo nome)
    ganha sufixo `-2`, `-3`… O arquivo idêntico pingado duas vezes vira duas
    cópias — é exatamente a cronologia que faltou no caso AF127.
    """
    agora = agora or datetime.now()
    sha = hashlib.sha256(raw).hexdigest()
    pasta = Path(raiz) / (ambiente or _SEM_AMBIENTE) / f"{agora:%Y}" / f"{agora:%m}"
    pasta.mkdir(parents=True, exist_ok=True)

    base = Path(f"{agora:%Y%m%d-%H%M%S}_{sha[:12]}_{nome_seguro(nome_original)}")
    destino = pasta / base.name
    n = 1
    while True:
        try:
            with open(destino, "xb") as fh:  # 'x': cria ou falha — jamais sobrescreve
                fh.write(raw)
            return Recebido(path=destino, sha256=sha)
        except FileExistsError:
            n += 1
            destino = pasta / f"{base.stem}-{n}{base.suffix}"
