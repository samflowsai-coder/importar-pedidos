from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_CONFIG_FILE = Path(__file__).parent.parent / "config.json"
_DEFAULTS = {
    "watch_dir": str(Path.cwd() / "input"),
    "output_dir": str(Path.cwd() / "output"),
}


def load() -> dict:
    cfg = dict(_DEFAULTS)
    if os.environ.get("INPUT_DIR"):
        cfg["watch_dir"] = str(Path(os.environ["INPUT_DIR"]).expanduser().resolve())
    if os.environ.get("OUTPUT_DIR"):
        cfg["output_dir"] = str(Path(os.environ["OUTPUT_DIR"]).expanduser().resolve())
    if _CONFIG_FILE.exists():
        try:
            saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            for key in ("watch_dir", "output_dir"):
                if saved.get(key):
                    cfg[key] = saved[key]
        except Exception:
            pass
    return cfg


def save(watch_dir: Optional[str] = None, output_dir: Optional[str] = None) -> dict:
    cfg = load()
    if watch_dir is not None:
        cfg["watch_dir"] = watch_dir
    if output_dir is not None:
        cfg["output_dir"] = output_dir
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return cfg


def imported_dir(cfg: dict) -> Path:
    return Path(cfg["watch_dir"]) / "Pedidos importados"
