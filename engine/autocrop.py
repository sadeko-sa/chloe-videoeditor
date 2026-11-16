"""
autocrop.py - Finds the black bars of a project, like the automatic crop of HandBrake (no Tk in here).

HandBrake looks at a number of pictures spread over the video and keeps the LEAST it may cut on every side, so a dark
scene never cuts into the picture. This does the same with the pictures the project really shows: it renders a few
frames of the whole project (all layers, all effects, without the project crop) and lets FFmpeg's cropdetect measure the
bars on each. A frame that is mostly black (a fade, a dark scene) finds almost nothing and is ignored.

    cuts = detect(tools, project)        # (left, right, top, bottom) in pixels of the project frame, even
    project.set_crop(*cuts)
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import tempfile
from typing import Callable, List, Optional, Tuple

from .compiler import compile_frame
from .ffmpeg import FFmpegError, Tools, _tail, popen, render_frame
from .preview import active_layers
from .project import Project, even, normalize_cuts

SAMPLES = 8
"""Pictures looked at, spread evenly over the project (the middle of eight equal parts)."""

MAX_SIDE = 1920
"""Longest side in pixels of the sampled pictures; a bigger project is looked at smaller and the result scaled back."""

LIMIT = 24
"""cropdetect: brightest value (0..255) still counted as black."""

MIN_KEPT = 0.1
"""A picture whose bars leave less than this part of its area is ignored (it is a dark scene, not a letterbox)."""

DETECT_SECONDS = 60
"""Time one cropdetect run may take."""

_CROP = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
"""What cropdetect prints: crop=width:height:x:y."""


def _measure(tools: Tools, picture: str) -> Optional[Tuple[int, int, int, int]]:
    """
    Ask cropdetect where the picture really begins and ends.

    @param tools    the FFmpeg programs
    @param picture  an image file
    @return         (width, height, x, y) of the area inside the bars, None if cropdetect said nothing
    @raise FFmpegError if FFmpeg cannot run
    """
    # the image is repeated for a second: cropdetect skips its first frames and then reports each one
    args = [tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-loop", "1", "-framerate", "10", "-t", "1",
            "-i", picture, "-vf", "format=yuv420p,cropdetect=limit=%d:round=2" % LIMIT, "-f", "null", "-"]
    try:
        proc = popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        err = proc.communicate(timeout=DETECT_SECONDS)[1].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise FFmpegError("cropdetect did not finish in time")
    except OSError as exc:
        raise FFmpegError(str(exc))
    if proc.returncode != 0:
        raise FFmpegError(_tail(err))
    found = _CROP.findall(err)
    return tuple(int(v) for v in found[-1]) if found else None      # the last report is the settled one


def sample_times(duration: float, samples: int = SAMPLES) -> List[float]:
    """@return `samples` seconds spread over the duration, each in the middle of an equal part"""
    return [duration * (i + 0.5) / samples for i in range(max(samples, 1))]


def detect(tools: Tools, project: Project, samples: int = SAMPLES,
           on_start: Optional[Callable[[subprocess.Popen], None]] = None) -> Tuple[int, int, int, int]:
    """
    Find the black bars of the project.

    @param tools     the FFmpeg programs
    @param project   the project; its own crop is left out (the bars are looked for in the whole frame) and it is not changed
    @param samples   how many pictures to look at
    @param on_start  given to render_frame for each picture (so a caller can kill the process); None = not killable
    @return          (left, right, top, bottom): the least cut on each side over all sampled pictures, in pixels of the
                     project frame, even; (0, 0, 0, 0) when no picture shows bars or the project has nothing to show
    @raise FFmpegError if a picture cannot be made
    """
    plain = copy.deepcopy(project)
    plain.crop = (0, 0, 0, 0)
    total = plain.duration()
    if total <= 0:
        return 0, 0, 0, 0
    scale = min(1.0, MAX_SIDE / float(max(plain.width, plain.height)))
    size = (even(plain.width * scale), even(plain.height * scale))
    folder = tempfile.mkdtemp(prefix="chloe_crop_")
    least: Optional[List[int]] = None
    try:
        for number, t in enumerate(sample_times(total, samples)):
            layers = active_layers(plain, t)
            if not layers:
                continue
            picture = os.path.join(folder, "%d.png" % number)
            render_frame(tools, compile_frame(plain, layers, t, size), picture, None, on_start)
            box = _measure(tools, picture)
            if box is None:
                continue
            width, height, x, y = box
            if width * height < MIN_KEPT * size[0] * size[1]:
                continue                                            # a dark scene: not a reason to cut
            sides = [x, size[0] - x - width, y, size[1] - y - height]
            least = sides if least is None else [min(a, b) for a, b in zip(least, sides)]
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if least is None:
        return 0, 0, 0, 0
    left, right, top, bottom = (int(round(v / scale)) for v in least)       # back to pixels of the project frame
    return normalize_cuts((left, right, top, bottom), plain.width, plain.height)
