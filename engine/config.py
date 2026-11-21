"""
config.py - The program settings as one JSON file (config.json) in the user data folder (no Tk in here).
The user data folder is the settings folder of the operating system for this program, or the folder the user chose
(see engine/userdata.py, which also keeps the small userdata.json that remembers the choice). No other file name is read.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from . import userdata
from .encode import ExportOptions
from .jobs import THREADS_RANGE, WORKERS_RANGE
from .naming import COLLISIONS, DEFAULT_FORMAT

CONFIG_NAME = "config.json"
"""File name of the config in the user data folder (no fallback to any older name)."""

DEFAULTS: Dict[str, Any] = {
    "language": "", "theme": "", "geometry": "1280x800", "zoomed": False,
    "ffmpeg": "", "preview_max": 1280, "last_dir": "",
    "export": {}, "output_dir": "", "name_format": DEFAULT_FORMAT, "on_exists": "ask",
    "cache_dir": "", "recent": [], "effect_override": False,
    "workers": 1, "worker_threads": 0, "goto_field": "time", "crop_ratio": "free",
}
"""Config entries and defaults. "ffmpeg" = path of ffmpeg or its folder (empty = search);
"preview_max" = longest side in pixels of the preview picture;
"export" = the export options last used (see engine/encode.py ExportOptions; {} = defaults);
"output_dir" = default folder of exports (empty = the folder of the first video of the project);
"name_format" = file name of an export with {tokens} (see engine/naming.py);
"on_exists" = what to do when the output file exists: "ask", "replace" or "number" (see engine/naming.py COLLISIONS);
"cache_dir" = folder that holds the preview cache (empty = cache/ in the user data folder);
"effect_override" = True lets an effect of the user's effect/ folder replace a built-in effect that has the same id (False = such a file is rejected);
"recent" = project files opened or saved last, newest first (at most RECENT_MAX);
"workers" = number of job queue workers (jobs rendered at the same time); "worker_threads" = thread limit of each job (0 = auto);
"goto_field" = field of the Go to time / frame dialog that had the focus when it was closed ("time" or "frame");
"crop_ratio" = aspect ratio last chosen in the crop assist ("free", "original" or a shape like "16:9")."""

RECENT_MAX = 10
"""Most entries kept in the recent projects list."""

PREVIEW_RANGE = (320, 3840)


def config_path() -> str:
    """@return full path of the config file (in the user data folder, see engine.userdata.userdata_dir())"""
    return os.path.join(userdata.userdata_dir(), CONFIG_NAME)


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
    for key in ("language", "theme", "geometry", "ffmpeg", "last_dir", "cache_dir", "crop_ratio", "output_dir", "name_format"):
        if not isinstance(cfg[key], str):
            cfg[key] = DEFAULTS[key]
    # recent: only non-empty text entries, each file once, newest first; always a list of its own
    seen, recent = set(), []
    for item in cfg["recent"] if isinstance(cfg["recent"], list) else []:
        if isinstance(item, str) and item and os.path.normcase(item) not in seen:
            seen.add(os.path.normcase(item))
            recent.append(item)
    cfg["recent"] = recent[:RECENT_MAX]
    cfg["name_format"] = cfg["name_format"].strip() or DEFAULT_FORMAT
    if cfg["on_exists"] not in COLLISIONS:
        cfg["on_exists"] = DEFAULTS["on_exists"]
    cfg["export"] = ExportOptions.from_dict(cfg["export"]).to_dict()
    cfg["zoomed"] = bool(cfg["zoomed"])
    cfg["effect_override"] = cfg["effect_override"] is True
    if cfg["goto_field"] not in ("time", "frame"):
        cfg["goto_field"] = DEFAULTS["goto_field"]
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    """Write the config (temporary file first); the user data folder is made if it is missing.
    @raise OSError if it cannot be written"""
    path = config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(path + ".tmp", path)
