"""
userdata.py - Where the user's own files live: config.json, the presets (preset/), the user effects (effect/), the
preview cache (cache/) and error.log (no Tk in here).

The user data folder is, by default, the folder the operating system reserves for the settings of one user of
one program:

    Windows  %APPDATA%\\ChloeVideoeditor                    (e.g. C:\\Users\\me\\AppData\\Roaming\\ChloeVideoeditor)
    macOS    ~/Library/Application Support/ChloeVideoeditor
    Linux    $XDG_CONFIG_HOME/ChloeVideoeditor, else ~/.config/ChloeVideoeditor

The user may pick another folder (Settings > General > User data folder). That choice cannot live in
config.json (config.json is inside the user data folder, so it could not be found before the folder is known).
So a tiny JSON file, `userdata.json`, ALWAYS sits in the default folder, whether or not a custom folder is
chosen. It holds the basic facts about the user data: the folder the user chose ("" = the default folder),
the folder that is in use now and the default folder. Only the first of the three is read back; the other two
are there so a person (or another tool) opening the file can see where everything is.

A program folder that already has a config.json (the layout of the versions before this one kept everything next
to the program) keeps working: the first start of this version writes that program folder as the chosen
user data folder, so no file is left behind. The user can move to the default folder in Settings.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
"""The program folder (the one with chloe_videoeditor.py). Defined here, not imported from engine/ffmpeg.py, so that
engine/effect.py can use this module without a circular import."""

APP_FOLDER = "ChloeVideoeditor"
"""Name of the program's folder inside the settings folder of the operating system."""

BOOTSTRAP_NAME = "userdata.json"
"""Name of the file in the default folder that remembers the user data folder."""

KEY_DIR = "userdata_dir"
"""Entry of userdata.json with the folder the user chose; empty = use default_dir()."""

LEGACY_MARKER = "config.json"
"""File whose presence in the program folder marks a layout of an older version (everything next to the program)."""


def default_dir() -> str:
    """
    @return the default user data folder of this operating system (it may not exist yet);
            XDG_CONFIG_HOME is only used when it is an absolute path, as the XDG specification demands
    """
    home = os.path.expanduser("~")
    if IS_WIN:
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif IS_MAC:
        base = os.path.join(home, "Library", "Application Support")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        base = xdg if os.path.isabs(xdg) else os.path.join(home, ".config")
    return os.path.join(base, APP_FOLDER)


def bootstrap_path() -> str:
    """@return full path of userdata.json (always inside default_dir(), even when a custom folder is in use)"""
    return os.path.join(default_dir(), BOOTSTRAP_NAME)


def read_bootstrap() -> Dict[str, Any]:
    """@return the content of userdata.json; {} if it is missing, unreadable or not a JSON object"""
    try:
        with open(bootstrap_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def custom_dir() -> str:
    """@return the absolute folder the user chose, "" if the user has none (then default_dir() is used)"""
    value = read_bootstrap().get(KEY_DIR, "")
    if not isinstance(value, str) or not value.strip():
        return ""
    return os.path.abspath(os.path.expanduser(value.strip()))


def userdata_dir() -> str:
    """@return the folder of config.json, preset/, effect/, cache/ and error.log (it may not exist yet)"""
    return custom_dir() or default_dir()


def write_bootstrap(custom: str) -> None:
    """
    Write userdata.json (temporary file first) with the chosen folder and the facts about it.

    @param custom  the chosen folder; "" = the default folder
    @raise OSError if the default folder or the file cannot be written
    """
    custom = os.path.abspath(os.path.expanduser(custom.strip())) if custom and custom.strip() else ""
    data = {KEY_DIR: custom, "effective_dir": custom or default_dir(), "default_dir": default_dir()}
    path = bootstrap_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(path + ".tmp", path)


def set_userdata_dir(custom: str) -> str:
    """
    Choose the user data folder: remember it in userdata.json. Nothing is moved or copied; the caller writes the
    config into the new folder (engine.config.save_config) and the program reads the other files from there.

    @param custom  the folder; "" = the default folder of the operating system
    @return        the folder that is in use now
    @raise OSError if userdata.json cannot be written
    """
    write_bootstrap(custom)
    return userdata_dir()


def init() -> str:
    """
    Make sure userdata.json exists and is current. Call once at program start, before the config is read.
    A failure to write it is ignored (a read only profile must not stop the program); the program then simply
    uses the folder it can read from.

    @return the folder that is in use
    """
    data = read_bootstrap()
    if KEY_DIR in data:
        custom = data[KEY_DIR] if isinstance(data[KEY_DIR], str) else ""
    else:
        # first start of this version: keep an old "everything next to the program" layout where it is
        custom = APP_DIR if os.path.isfile(os.path.join(APP_DIR, LEGACY_MARKER)) else ""
    wanted = {KEY_DIR: custom, "effective_dir": custom or default_dir(), "default_dir": default_dir()}
    if any(data.get(k) != v for k, v in wanted.items()):
        try:
            write_bootstrap(custom)
        except OSError:
            pass
    return userdata_dir()
