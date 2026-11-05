"""
ffmpeg.py - Finds FFmpeg, probes media files and runs the jobs of engine/compiler.py as processes.
Works the same on Windows, Linux and macOS; no Tk in here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Dict, FrozenSet, List, Optional

from .compiler import Job
from .project import IMAGE_EXTS, IMAGE_SECONDS, MediaInfo

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIN_VERSION = (4, 4)
"""Oldest FFmpeg the filters of this program are written for."""


class FFmpegError(Exception):
    """FFmpeg or FFprobe failed; the text is the useful end of its message."""


@dataclass(frozen=True)
class Tools:
    """The FFmpeg installation in use."""
    ffmpeg: str
    ffprobe: str
    version: str
    encoders: FrozenSet[str]


@dataclass(frozen=True)
class Preset:
    """One export format: container, video encoder, quality (CRF per level), extra options, audio encoder."""
    label: str
    ext: str
    encoder: str
    crf: Dict[str, int]
    video: tuple
    audio: tuple


PRESETS: Dict[str, Preset] = {
    "h264": Preset("H.264 (MP4)", ".mp4", "libx264", {"high": 17, "medium": 21, "low": 26},
                   ("-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart"),
                   ("-c:a", "aac", "-b:a", "192k")),
    "h265": Preset("H.265 (MP4)", ".mp4", "libx265", {"high": 21, "medium": 25, "low": 30},
                   ("-preset", "medium", "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-movflags", "+faststart"),
                   ("-c:a", "aac", "-b:a", "192k")),
    "vp9": Preset("VP9 (WebM)", ".webm", "libvpx-vp9", {"high": 24, "medium": 31, "low": 38},
                  ("-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p"),
                  ("-c:a", "libopus", "-b:a", "160k")),
}
QUALITIES = ("high", "medium", "low")


# --------------------------------------------------------------------------- #
# Processes
# --------------------------------------------------------------------------- #


def popen(args: List[str], **kwargs) -> subprocess.Popen:
    """Start a process without a console window (Windows) and without reading the keyboard."""
    if IS_WIN:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, **kwargs)


def run(args: List[str], timeout: float = 60) -> str:
    """@return stdout of a finished process @raise FFmpegError if it fails or takes too long"""
    try:
        proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise FFmpegError("%s did not finish in time" % os.path.basename(args[0]))
    except OSError as exc:
        raise FFmpegError(str(exc))
    if proc.returncode != 0:
        raise FFmpegError(_tail(err.decode("utf-8", "replace")))
    return out.decode("utf-8", "replace")


def _tail(text: str, lines: int = 8) -> str:
    """@return the last non-empty lines of an FFmpeg message"""
    rows = [r.rstrip() for r in text.splitlines() if r.strip()]
    return "\n".join(rows[-lines:]) or "unknown error"


# --------------------------------------------------------------------------- #
# Finding FFmpeg
# --------------------------------------------------------------------------- #


def _exe(name: str) -> str:
    return name + ".exe" if IS_WIN else name


def _search_dirs() -> List[str]:
    """@return folders to look in besides PATH (a program started from Finder / Explorer has a short PATH)"""
    dirs = [os.path.join(APP_DIR, "ffmpeg"), os.path.join(APP_DIR, "ffmpeg", "bin"), os.path.join(APP_DIR, "bin")]
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA", "")
        dirs += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin"),
                 r"C:\ffmpeg\bin", os.path.join(local, "Microsoft", "WinGet", "Links"),
                 os.path.join(os.path.expanduser("~"), "scoop", "shims")]
    elif IS_MAC:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    else:
        dirs += ["/usr/local/bin", "/usr/bin", "/snap/bin", os.path.expanduser("~/.local/bin")]
    return dirs


def _find(name: str, configured: str) -> Optional[str]:
    """@return full path of a tool: next to the configured FFmpeg, in the program folder, on PATH, in the usual places"""
    candidates = []
    if configured:
        folder = configured if os.path.isdir(configured) else os.path.dirname(configured)
        candidates.append(os.path.join(folder, _exe(name)))
    found = shutil.which(name)
    if found:
        candidates.append(found)
    candidates += [os.path.join(d, _exe(name)) for d in _search_dirs()]
    # the program folder first, a configured path before everything
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _version(text: str) -> tuple:
    m = re.search(r"version\D*(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def locate(configured: str = "") -> Optional[Tools]:
    """
    Find a working FFmpeg + FFprobe.

    @param configured  path from the settings (the ffmpeg file or its folder); empty = search
    @return            Tools, or None if FFmpeg is not found or does not run
    """
    ffmpeg, ffprobe = _find("ffmpeg", configured), _find("ffprobe", configured)
    if not ffmpeg or not ffprobe:
        return None
    try:
        first = run([ffmpeg, "-hide_banner", "-version"], 15).splitlines()[0]
        listing = run([ffmpeg, "-hide_banner", "-encoders"], 15)
    except (FFmpegError, IndexError):
        return None
    encoders = frozenset(m.group(1) for m in re.finditer(r"^\s[VAS][\w.]{5}\s+(\S+)", listing, re.M))
    shown = re.search(r"version\s+(\S+)", first)
    return Tools(ffmpeg, ffprobe, shown.group(1) if shown else "?", encoders)


def too_old(tools: Tools) -> bool:
    """@return True if the FFmpeg is older than MIN_VERSION (release builds only; git builds count as new)"""
    ver = _version("version " + tools.version)
    return ver != (0, 0) and ver < MIN_VERSION


def available_presets(tools: Tools) -> Dict[str, Preset]:
    """@return the export formats whose encoder this FFmpeg has"""
    return {k: p for k, p in PRESETS.items() if p.encoder in tools.encoders}


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


def _rate(text: str) -> float:
    try:
        value = float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return value if 0 < value < 1000 else 0.0


def probe(tools: Tools, path: str) -> MediaInfo:
    """
    Read size, frame rate, length and sound of a media file.

    @raise FFmpegError if the file is not readable media with a picture
    """
    data = json.loads(run([tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                           "-show_streams", "-i", path]) or "{}")
    streams = data.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    videos.sort(key=lambda s: (s.get("disposition") or {}).get("attached_pic", 0))
    if not videos:
        raise FFmpegError("no picture in this file")
    v = videos[0]
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    turn = 0
    for item in v.get("side_data_list") or []:
        if "rotation" in item:
            turn = int(float(item["rotation"]))
    turn = turn or int(float((v.get("tags") or {}).get("rotate") or 0))
    if abs(turn) % 180 == 90:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise FFmpegError("no picture size found")
    is_image = os.path.splitext(path)[1].lower() in IMAGE_EXTS
    try:
        seconds = float(v.get("duration") or (data.get("format") or {}).get("duration") or 0)
    except ValueError:
        seconds = 0.0
    if is_image:
        seconds = 0.0
    elif seconds <= 0:
        raise FFmpegError("length of the file is unknown")
    fps = _rate(v.get("avg_frame_rate", "")) or _rate(v.get("r_frame_rate", "")) or 30.0
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return MediaInfo(path, width, height, fps, seconds, has_audio and not is_image, is_image)


# --------------------------------------------------------------------------- #
# Running jobs
# --------------------------------------------------------------------------- #


def _base(tools: Tools) -> List[str]:
    return [tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]


def render_frame(tools: Tools, job: Job, final: str, prefix: Optional[str] = None,
                 on_start: Optional[Callable[[subprocess.Popen], None]] = None) -> None:
    """
    Render the single picture of a frame job (and its prefix picture) into image files.

    @param final     output file for Job.video (format by extension: .png, .jpg)
    @param prefix    output file for Job.prefix, None if the job has none
    @param on_start  called with the process right after it started (so it can be killed)
    @raise FFmpegError if FFmpeg fails or the files do not appear
    """
    args = _base(tools) + job.input_args() + ["-filter_complex", job.graph]
    outputs = [(job.video, final)] + ([(job.prefix, prefix)] if job.prefix and prefix else [])
    for label, path in outputs:
        args += ["-map", "[%s]" % label, "-an", "-frames:v", "1", "-update", "1", "-q:v", "2", path]
    try:
        proc = popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise FFmpegError(str(exc))
    if on_start:
        on_start(proc)
    err = proc.communicate()[1].decode("utf-8", "replace")
    if proc.returncode != 0 or not all(os.path.exists(p) and os.path.getsize(p) > 0 for _l, p in outputs):
        raise FFmpegError(_tail(err))


class Export:
    """
    One export running in a thread.

        job = Export(tools, graph_job, preset, "medium", "out.mp4", fps, on_progress, on_done)
        job.start();  ... job.cancel()

    on_progress(fraction 0..1) and on_done(error or None, cancelled) are called from the thread.
    The video is written to a temporary name and renamed when it is complete.
    """

    def __init__(self, tools: Tools, job: Job, preset: Preset, quality: str, out_path: str,
                 on_progress: Callable[[float], None], on_done: Callable[[Optional[str], bool], None]):
        self.tools, self.job, self.preset, self.quality = tools, job, preset, quality
        self.out_path, self.on_progress, self.on_done = out_path, on_progress, on_done
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def command(self, target: str) -> List[str]:
        """@return the full FFmpeg command line writing to `target`"""
        job, p = self.job, self.preset
        args = _base(self.tools) + ["-progress", "pipe:1", "-nostats"] + job.input_args()
        args += ["-filter_complex", job.graph, "-map", "[%s]" % job.video]
        args += ["-c:v", p.encoder, "-crf", str(p.crf[self.quality])] + list(p.video)
        if job.audio:
            args += ["-map", "[%s]" % job.audio] + list(p.audio)
        else:
            args += ["-an"]
        return args + ["-t", "%.6f" % job.duration, target]

    def _run(self) -> None:
        root, ext = os.path.splitext(self.out_path)
        target = root + ".chloe-tmp" + ext
        error: Optional[str] = None
        lines: List[str] = []
        try:
            with self._lock:
                if self._cancelled:
                    return self.on_done(None, True)
                self._proc = popen(self.command(target), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True, encoding="utf-8", errors="replace")
            for line in self._proc.stdout:
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m:
                    self.on_progress(min(int(m.group(1)) / 1e6 / self.job.duration, 1.0))
                elif "=" not in line.split(" ")[0]:
                    lines.append(line)
            self._proc.wait()
            if self._cancelled:
                error = None
            elif self._proc.returncode != 0:
                error = _tail("".join(lines))
            else:
                os.replace(target, self.out_path)
        except OSError as exc:
            error = str(exc)
        finally:
            if os.path.exists(target):
                try:
                    os.remove(target)
                except OSError:
                    pass
        self.on_done(error, self._cancelled)
