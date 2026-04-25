from __future__ import annotations

import json
import os
from pathlib import Path

_CONFIG_FILE = Path(__file__).parent.parent / "config.json"
_DEFAULTS = {
    "watch_dir": str(Path.cwd() / "input"),
    "output_dir": str(Path.cwd() / "output"),
    "export_mode": "xlsx",
}

_VALID_EXPORT_MODES = {"xlsx", "db", "both"}


def load() -> dict:
    cfg = dict(_DEFAULTS)
    if os.environ.get("INPUT_DIR"):
        cfg["watch_dir"] = str(Path(os.environ["INPUT_DIR"]).expanduser().resolve())
    if os.environ.get("OUTPUT_DIR"):
        cfg["output_dir"] = str(Path(os.environ["OUTPUT_DIR"]).expanduser().resolve())
    raw_mode = os.environ.get("EXPORT_MODE", "").lower()
    if raw_mode in _VALID_EXPORT_MODES:
        cfg["export_mode"] = raw_mode

    if _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            for key in ("watch_dir", "output_dir"):
                if saved.get(key):
                    cfg[key] = saved[key]
            if saved.get("export_mode") in _VALID_EXPORT_MODES:
                cfg["export_mode"] = saved["export_mode"]
        except Exception:
            pass
    return cfg


def save(
    watch_dir: str | None = None,
    output_dir: str | None = None,
    export_mode: str | None = None,
) -> dict:
    cfg = load()
    if watch_dir is not None:
        cfg["watch_dir"] = watch_dir
    if output_dir is not None:
        cfg["output_dir"] = output_dir
    if export_mode is not None and export_mode in _VALID_EXPORT_MODES:
        cfg["export_mode"] = export_mode
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def imported_dir(cfg: dict) -> Path:
    return Path(cfg["watch_dir"]) / "Pedidos importados"
