from __future__ import annotations

import io
from collections import defaultdict
from collections.abc import Iterator

import pdfplumber

from app.ingestion.file_loader import LoadedFile
from app.utils.logger import logger

# Fração de caracteres empilhados a partir da qual o posicionamento da página é
# considerado inconfiável. PDFs bem formados medem 0 — dois glifos ocupando o
# mesmo espaço seriam ilegíveis. A folga cobre geradores que desenham acento e
# letra base como glifos sobrepostos.
_STACKED_CHAR_THRESHOLD = 0.02
_MIN_PAIRS_TO_JUDGE = 20

# Tolerância para considerar que um caractere continua o run anterior em vez de
# iniciar um novo. Precisa ser maior que o word spacing (operador Tw, 0.10–0.15
# nestes PDFs), que o pdfminer aplica à posição mas não devolve em `adv`; sem a
# folga todo espaço viraria uma fronteira falsa, e o fragmento seguinte herdaria
# uma âncora já contaminada pelo drift. Saltos reais de Td são ordens de grandeza
# maiores; os que caem dentro da folga pousam onde o texto fluiria de qualquer
# forma, então tratá-los como continuação não muda a ordenação.
_RUN_BREAK_TOLERANCE = 0.6
_LINE_TOLERANCE = 3.0


def _chars_are_stacked(page: pdfplumber.page.Page) -> bool:
    """Detecta páginas cujo posicionamento de caracteres é internamente inconsistente.

    Alguns geradores declaram uma fonte mais larga do que a usada para calcular o
    layout — o pedido da SBF/Centauro a partir de 07/2026 declara Helvetica-Bold
    (com o Widths do bold) mas posiciona o texto com as métricas da regular. O
    visual sai correto, porque cada trecho é ancorado por um Td absoluto, mas o
    pdfminer deriva a posição de cada caractere somando as larguras declaradas:
    dentro de um trecho a escrita escorrega para a direita e invade o trecho
    vizinho. Como a extração padrão ordena por (top, x0), os caracteres se
    intercalam — "GrupoSaf@centauro.com.br. / Tel:" vira
    "GrupoSaf@centauro.com.bTre.l :/" e o parser da Centauro deixa de reconhecer
    o pedido.
    """
    lines: dict[int, list] = defaultdict(list)
    for char in page.chars:
        lines[round(char["top"])].append(char)

    pairs = stacked = 0
    for chars in lines.values():
        chars.sort(key=lambda c: c["x0"])
        for left, right in zip(chars, chars[1:]):
            pairs += 1
            if right["x0"] < left["x0"] + (left["x1"] - left["x0"]) * 0.5:
                stacked += 1

    if pairs < _MIN_PAIRS_TO_JUDGE:
        return False
    return stacked / pairs > _STACKED_CHAR_THRESHOLD


def _iter_runs(page: pdfplumber.page.Page) -> Iterator[list]:
    """Agrupa os caracteres nos trechos em que o PDF os desenhou.

    Cada `Tj` do content stream é ancorado por um `Td` absoluto e confiável; só as
    larguras dentro do trecho estão erradas. O pdfminer posiciona cada caractere
    somando `adv` ao anterior, então a identidade `x0 == x0_anterior + adv_anterior`
    vale dentro de um trecho e quebra exatamente onde houve um Td novo.
    """
    run: list = []
    previous = None
    for char in page.chars:
        if previous is not None and (
            abs(char["top"] - previous["top"]) > _RUN_BREAK_TOLERANCE
            or abs(char["x0"] - (previous["x0"] + previous["adv"])) > _RUN_BREAK_TOLERANCE
        ):
            yield run
            run = []
        run.append(char)
        previous = char
    if run:
        yield run


def _rebuild_text_from_runs(page: pdfplumber.page.Page) -> str:
    """Remonta o texto da página ordenando trechos, não caracteres.

    A âncora de cada trecho (x0 do primeiro caractere) vem do Td e é exata, então
    ordenar por ela reconstrói o layout visual — inclusive a adjacência entre
    rótulo e valor ("CNPJ: 06.347.409/0296-51") que os parsers dependem. Dentro do
    trecho a ordem do content stream já é a correta.
    """
    runs = [
        {
            "text": "".join(char["text"] for char in chars).strip(),
            "x0": chars[0]["x0"],
            "top": chars[0]["top"],
        }
        for chars in _iter_runs(page)
    ]
    runs = [run for run in runs if run["text"]]
    runs.sort(key=lambda run: run["top"])

    lines: list[list[dict]] = []
    for run in runs:
        if lines and abs(run["top"] - lines[-1][0]["top"]) <= _LINE_TOLERANCE:
            lines[-1].append(run)
        else:
            lines.append([run])

    return "\n".join(
        " ".join(run["text"] for run in sorted(line, key=lambda run: run["x0"])) for line in lines
    )


class PDFExtractor:
    def extract(self, file: LoadedFile) -> dict:
        text_pages = []
        tables = []
        with pdfplumber.open(io.BytesIO(file.raw)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                stacked = _chars_are_stacked(page)
                if stacked:
                    logger.warning(
                        f"{file.path.name} p.{number}: fonte declarada mais larga que o "
                        f"layout; remontando o texto pelos trechos do content stream"
                    )
                    text = _rebuild_text_from_runs(page)
                else:
                    text = page.extract_text()
                if text:
                    text_pages.append(text)
                # As células já são delimitadas por posição, então basta não reordenar
                # os caracteres dentro delas.
                page_tables = page.extract_tables({"text_use_text_flow": True} if stacked else {})
                if page_tables:
                    tables.extend(page_tables)
        return {
            "text": "\n".join(text_pages),
            "tables": tables,
        }
