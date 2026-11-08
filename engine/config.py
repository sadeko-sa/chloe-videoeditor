"""
config.py - The program settings as one JSON file (config.json) next to the program (no Tk in here).
Nothing is ever written to AppData, ~/.config or similar. No other file name is read.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from .ffmpeg import APP_DIR, QUALITIES
from .jobs import THREADS_RANGE, WORKERS_RANGE

CONFIG_NAME = "config.json"
"""File name of the config in the program folder (no fallback to any older name)."""

DEFAULTS: Dict[str, Any] = {
    "language": "", "theme": "", "geometry": "1280x800", "zoomed": False,
    "ffmpeg": "", "preview_max": 1280, "last_dir": "",
    "export_format": "h264", "export_quality": "medium",
    "cache_dir": "", "recent": [],
    "workers": 1, "worker_threads": 0,
}
"""Config entries and defaults. "ffmpeg" = path of ffmpeg or its folder (empty = search);
"preview_max" = longest side in pixels of the preview picture;
"cache_dir" = folder that holds the preview cache (empty = cache/ in the program folder);
"recent" = project files opened or saved last, newest first (at most RECENT_MAX);
"workers" = number of job queue workers (jobs rendered at the same time); "worker_threads" = thread limit of each job (0 = auto)."""

RECENT_MAX = 10
"""Most entries kept in the recent projects list."""

PREVIEW_RANGE = (320, 3840)


def config_path() -> str:
    """@return full path of the config file (in the program folder)"""
    return os.path.join(APP_DIR, CONFIG_NAME)


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def push_recent(recent: List[str], path: str, limit: int = RECENT_MAX) -> List[str]:
    """
    Put a project file at the top of the recent list.

    @param recent  the list so far, newest first (not changed)
    @param path    project file to put first
    @param limit   most entries kept
    @return        a new list: path first, an earlier entry of the same file removed, cut to `limit`
    """
    path = os.path.abspath(path)
    key = os.path.normcase(path)
    rest = [p for p in recent if os.path.normcase(os.path.abspath(p)) != key]
    return ([path] + rest)[:limit]


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
    cfg["workers"] = _clamp(cfg["workers"], *WORKERS_RANGE, DEFAULTS["workers"])
    cfg["worker_threads"] = _clamp(cfg["worker_threads"], *THREADS_RANGE, DEFAULTS["worker_threads"])
    for key in ("language", "theme", "geometry", "ffmpeg", "last_dir", "export_format", "cache_dir"):
        if not isinstance(cfg[key], str):
            cfg[key] = DEFAULTS[key]
    # recent: only non-empty text entries, each file once, newest first; always a list of its own
    seen, recent = set(), []
    for item in cfg["recent"] if isinstance(cfg["recent"], list) else []:
        if isinstance(item, str) and item and os.path.normcase(item) not in seen:
            seen.add(os.path.normcase(item))
            recent.append(item)
    cfg["recent"] = recent[:RECENT_MAX]
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
