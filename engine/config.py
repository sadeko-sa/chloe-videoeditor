"""
config.py - The program settings as one JSON file next to the program (no Tk in here).
Nothing is ever written to AppData, ~/.config or similar.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .ffmpeg import APP_DIR, QUALITIES

CONFIG_NAME = "chloe_videoeditor.json"

DEFAULTS: Dict[str, Any] = {
    "language": "", "theme": "", "geometry": "1280x800", "zoomed": False,
    "ffmpeg": "", "preview_max": 1280, "last_dir": "",
    "export_format": "h264", "export_quality": "medium",
}
"""Config entries and defaults. "ffmpeg" = path of ffmpeg or its folder (empty = search);
"preview_max" = longest side in pixels of the preview picture."""

PREVIEW_RANGE = (320, 3840)


def config_path() -> str:
    """@return full path of the config file (in the program folder)"""
    return os.path.join(APP_DIR, CONFIG_NAME)


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def load_config() -> Dict[str, Any]:
    """@return the config: defaults overridden by the saved file (values checked)"""
    cfg = dict(DEFAULTS)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    cfg["preview_max"] = _clamp(cfg["preview_max"], *PREVIEW_RANGE, DEFAULTS["preview_max"])
    for key in ("language", "theme", "geometry", "ffmpeg", "last_dir", "export_format"):
        if not isinstance(cfg[key], str):
            cfg[key] = DEFAULTS[key]
    if cfg["export_quality"] not in QUALITIES:
        cfg["export_quality"] = DEFAULTS["export_quality"]
    cfg["zoomed"] = bool(cfg["zoomed"])
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """Write the config (temporary file first). @raise OSError if it cannot be written"""
    path = config_path()
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(path + ".tmp", path)
