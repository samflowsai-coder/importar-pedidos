import sys
from pathlib import Path

from loguru import logger

from app.observability.trace import current_trace_id

Path("logs").mkdir(exist_ok=True)


def _inject_trace_id(record: dict) -> bool:
    """Loguru filter: stamp current_trace_id() (or '-') on every record.

    Returning True keeps the record. We mutate `record["extra"]` so the
    formatter can pick it up. Loguru ensures `extra` is always present.
    """
    record["extra"]["trace_id"] = current_trace_id() or "-"
    return True


logger.remove()
logger.add(
    sys.stderr,
    format=(
        "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
        "<cyan>trace={extra[trace_id]}</cyan> | {message}"
    ),
    level="INFO",
    colorize=True,
    filter=_inject_trace_id,
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
    filter=_inject_trace_id,
)

__all__ = ["logger"]
