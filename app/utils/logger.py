import sys
from pathlib import Path

from loguru import logger

from app.observability.trace import current_trace_id

Path("logs").mkdir(exist_ok=True)

# Log de leituras (app/observability/leituras.py): ancorado na raiz do app, não no CWD.
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def _inject_trace_id(record: dict) -> bool:
    """Loguru filter: stamp current_trace_id() (or '-') on every record.

    Returning True keeps the record. We mutate `record["extra"]` so the
    formatter can pick it up. Loguru ensures `extra` is always present.
    """
    record["extra"]["trace_id"] = current_trace_id() or "-"
    return True


def _so_canal(record: dict) -> bool:
    return record["extra"].get("canal") == "leituras"


def _fora_do_canal(record: dict) -> bool:
    return record["extra"].get("canal") != "leituras"


def _filtro_geral(record: dict) -> bool:
    return _fora_do_canal(record) and _inject_trace_id(record)


logger.remove()
logger.add(
    sys.stderr,
    format=(
        "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>trace={extra[trace_id]}</cyan> | {message}"
    ),
    level="INFO",
    colorize=True,
    filter=_filtro_geral,
)
logger.add(
    "logs/pipeline.log",
    rotation="10 MB",
    retention="30 days",
    level="DEBUG",
    encoding="utf-8",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "trace={extra[trace_id]} | {name}:{function}:{line} | {message}"
    ),
    filter=_filtro_geral,
)


def _configurar_sink_leituras(diretorio: Path) -> int | None:
    """Cria `diretorio` e adiciona o sink do canal `leituras` (JSONL).

    Observabilidade nunca derruba o portal: sem permissão de escrita vira warning e
    devolve None. `delay=True` adia a abertura do arquivo para a primeira linha.
    """
    try:
        diretorio.mkdir(exist_ok=True)
        return logger.add(
            diretorio / "leituras.jsonl",
            format="{message}",
            rotation="10 MB",
            retention="30 days",
            enqueue=True,
            encoding="utf-8",
            level="INFO",
            filter=_so_canal,
            delay=True,
        )
    except OSError as exc:
        logger.warning(f"Log de leituras desligado ({type(exc).__name__}): {exc}")
        return None


_ID_SINK_LEITURAS = _configurar_sink_leituras(LOG_DIR)
if _ID_SINK_LEITURAS is not None:
    logger.info(f"Log de leituras: {LOG_DIR / 'leituras.jsonl'}")

__all__ = ["logger"]
