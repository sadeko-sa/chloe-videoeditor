"""
naming.py - Where an export goes and what it is called: the default folder, the file name format with {tokens}
(like the auto naming of HandBrake) and what happens when the file already exists. No Tk and no processes in here.
"""

from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass
from typing import Callable, Dict

from .encode import RATE_QUALITY, ExportOptions, fps_value
from .project import Project, even

DEFAULT_FORMAT = "{source}-edit"
"""File name format used until the user changes it."""

ASK, REPLACE, NUMBER = "ask", "replace", "number"
COLLISIONS = (ASK, REPLACE, NUMBER)
"""What to do when the output file exists: ask before replacing it, replace it, or add a number to the new name."""

TOKENS = ("source", "project", "date", "time", "codec", "quality", "width", "height", "fps")
"""Tokens a format can hold, in the order the settings list them; language entry \"tip.token.<name>\" explains each."""

CODECS = {"x264": "h264", "x265": "h265", "vp9": "vp9", "svtav1": "av1", "aom": "av1"}
"""Name of each video codec (key of encode.VIDEO) in a file name."""

_TOKEN = re.compile(r"\{([a-z]+)\}")
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class NameContext:
    """
    What the tokens stand for.

    @param source   name of the first media file, without folder and extension; empty if there is none
    @param project  name of the project file, without folder and extension; empty if it was never saved
    @param now      time of the export
    @param options  the export options
    @param width    width of the project picture
    @param height   height of the project picture
    @param fps      frame rate of the project
    """
    source: str
    project: str
    now: datetime.datetime
    options: ExportOptions
    width: int
    height: int
    fps: float


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0] if path else ""


def context_for(project: Project, project_path: str, options: ExportOptions, now: datetime.datetime) -> NameContext:
    """@return the context of an export of this project (project_path None or empty = never saved)"""
    media = project.media_layers()
    return NameContext(_stem(media[0].path) if media else "", _stem(project_path or ""), now, options,
                       project.out_width, project.out_height, project.fps)


def _number(value: float) -> str:
    return "%g" % round(value, 3)


def values(ctx: NameContext) -> Dict[str, str]:
    """@return the text of every token for this context"""
    o = ctx.options
    height = o.height or ctx.height
    width = even(ctx.width * height / ctx.height) if o.height else ctx.width
    return {
        "source": ctx.source, "project": ctx.project,
        "date": ctx.now.strftime("%Y.%m.%d"), "time": ctx.now.strftime("%H.%M.%S"),
        "codec": CODECS.get(o.video, o.video),
        "quality": ("crf%d" % o.crf) if o.rate == RATE_QUALITY else "%dk" % o.bitrate,
        "width": str(width), "height": str(height), "fps": _number(fps_value(o.fps) or ctx.fps),
    }


def clean(name: str) -> str:
    """@return the name without characters a file name cannot hold (each becomes _) and without spaces or dots at the ends"""
    return _FORBIDDEN.sub("_", name).strip(" .")


def render(fmt: str, ctx: NameContext) -> str:
    """
    @param fmt  text with {tokens}; anything else (also an unknown {word}) stays as typed
    @return     the file name without extension; \"video\" if nothing is left
    """
    found = values(ctx)
    return clean(_TOKEN.sub(lambda m: found.get(m.group(1), m.group(0)), fmt)) or "video"


def append_token(fmt: str, token: str) -> str:
    """@return the format with {token} added at its end"""
    return "%s{%s}" % (fmt, token)


def first_folder(project: Project) -> str:
    """@return the folder of the first media file that is there; empty if there is none"""
    for layer in project.media_layers():
        folder = os.path.dirname(layer.path) if layer.path else ""
        if folder and os.path.isdir(folder):
            return folder
    return ""


def output_folder(configured: str, project: Project, project_path: str = "", fallback: str = "") -> str:
    """
    @param configured    the default folder of the settings; empty = the folder of the input
    @param project_path  file of the project, None or empty = never saved
    @param fallback      folder to try last (the one used last)
    @return              the configured folder if it exists, else the folder of the first media file, of the project
                         file, the fallback, the home folder
    """
    configured = os.path.expanduser(configured) if configured else ""
    if configured and os.path.isdir(configured):
        return configured
    candidates = (first_folder(project), os.path.dirname(project_path) if project_path else "", fallback)
    return next((c for c in candidates if c and os.path.isdir(c)), os.path.expanduser("~"))


def same_file(a: str, b: str) -> bool:
    """@return True if both paths name the same file (on a case-insensitive system too)"""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def is_input(path: str, project: Project) -> bool:
    """@return True if the path is a media file of the project (an export must never replace it)"""
    return any(layer.path and same_file(path, layer.path) for layer in project.media_layers())


def unique_path(path: str, taken: Callable[[str], bool] = os.path.exists) -> str:
    """
    @param path   wanted file
    @param taken  tells if a path is in use (default: the file exists)
    @return       the path if it is free, else the first of "name (2).ext", "name (3).ext", ... that is
    """
    stem, ext = os.path.splitext(path)
    found, number = path, 1
    while taken(found):
        number += 1
        found = "%s (%d)%s" % (stem, number, ext)
    return found

