"""
ffmpeg.py - Finds FFmpeg, probes media files and runs the jobs of engine/compiler.py as processes.
Works the same on Windows, Linux and macOS; no Tk in here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

from .compiler import Job
from .encode import AUDIO, COPY_AUDIO, NO_AUDIO, VIDEO, ExportOptions, copy_fallback, copy_fits, depth_of
from .project import IMAGE_EXTS, IMAGE_SECONDS, MediaInfo
from .stamp import stamp

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIN_VERSION = (4, 4)
"""Oldest FFmpeg the filters of this program are written for."""

_SCALED = "chloe_out"
"""Label of the picture after the output scaling filter."""


class FFmpegError(Exception):
    """FFmpeg or FFprobe failed; the text is the useful end of its message."""


@dataclass(frozen=True)
class Tools:
    """The FFmpeg installation in use."""
    ffmpeg: str
    ffprobe: str
    version: str
    encoders: FrozenSet[str]
    depths: Dict[str, Tuple[int, ...]] = field(default_factory=dict)
    """Bit depths each video encoder lists in its FFmpeg help: {encoder name: (8, 10, ...)}."""


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


def _suspend(proc: subprocess.Popen, stop: bool) -> None:
    """
    Freeze or thaw a running process without ending it (SIGSTOP / SIGCONT; NtSuspendProcess / NtResumeProcess on
    Windows). Does nothing if the process is already gone.
    """
    if proc.poll() is not None:
        return
    if not IS_WIN:
        try:
            os.kill(proc.pid, signal.SIGSTOP if stop else signal.SIGCONT)
        except OSError:
            pass
        return
    import ctypes
    access = 0x0800                                 # PROCESS_SUSPEND_RESUME
    kernel, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(access, False, proc.pid)
    if handle:
        (ntdll.NtSuspendProcess if stop else ntdll.NtResumeProcess)(ctypes.c_void_p(handle))
        kernel.CloseHandle(ctypes.c_void_p(handle))


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


def _bit_depths(ffmpeg: str, encoders: FrozenSet[str]) -> Dict[str, Tuple[int, ...]]:
    """
    Ask FFmpeg which bit depths the video encoders can write: the help of an encoder lists its pixel formats.

    @param ffmpeg    path of the ffmpeg file
    @param encoders  encoders this FFmpeg has
    @return          {encoder name: bit depths of its 4:2:0 pixel formats, lowest first}; an encoder that gives no
                     answer is left out (the dialog then offers 8-bit only)
    """
    found = {}
    for codec in VIDEO.values():
        if codec.encoder not in encoders:
            continue
        try:
            text = run([ffmpeg, "-hide_banner", "-h", "encoder=" + codec.encoder], 15)
        except FFmpegError:
            continue
        formats = re.search(r"Supported pixel formats:(.*)", text)
        depths = sorted({depth_of(f) for f in formats.group(1).split()} - {0}) if formats else []
        if depths:
            found[codec.encoder] = tuple(depths)
    return found


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
    return Tools(ffmpeg, ffprobe, shown.group(1) if shown else "?", encoders, _bit_depths(ffmpeg, encoders))


def too_old(tools: Tools) -> bool:
    """@return True if the FFmpeg is older than MIN_VERSION (release builds only; git builds count as new)"""
    ver = _version("version " + tools.version)
    return ver != (0, 0) and ver < MIN_VERSION


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #


def _rate(text: str) -> float:
    try:
        value = float(Fraction(text))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return value if 0 < value < 1000 else 0.0


def audio_codec(tools: Tools, path: str) -> Optional[str]:
    """@return FFmpeg name of the codec of the first audio stream of a file (e.g. "aac"); None if it cannot be probed"""
    try:
        name = run([tools.ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                    "-of", "csv=p=0", path], 30).strip()
    except FFmpegError:
        return None
    return name or None


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


_FRAME_TIME = re.compile(r"showinfo.*?\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*(-?\d+(?:\.\d+)?)")
"""One line of the showinfo filter in the first pass of a project without sound: the time of one frame."""


class Export:
    """
    One export, run by the caller's thread (the job queue of engine/jobs.py runs it in a worker).

        export = Export(tools, graph_job, options, "out.mp4", on_progress)
        error = export.run(threads=2)           # blocks; export.cancel() from another thread stops it

    on_progress(fraction 0..1) is called from the thread that runs the export. A two-pass export reports
    the first pass as the first half. The video is written to a temporary name and renamed when it is complete.
    """

    def __init__(self, tools: Tools, job: Job, options: ExportOptions, out_path: str,
                 on_progress: Optional[Callable[[float], None]] = None):
        self.tools, self.job, self.options = tools, job, options
        self.out_path, self.on_progress = out_path, on_progress
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._paused = False
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        """@return True once cancel() was called"""
        return self._cancelled

    @property
    def paused(self) -> bool:
        """@return True between pause() and resume()"""
        return self._paused

    def pause(self) -> None:
        """Freeze the export where it is (also if its process has not started yet). Safe from any thread."""
        with self._lock:
            self._paused = True
            if self._proc:
                _suspend(self._proc, True)

    def resume(self) -> None:
        """Let a paused export go on. Safe from any thread."""
        with self._lock:
            self._paused = False
            if self._proc:
                _suspend(self._proc, False)

    def cancel(self) -> None:
        """Stop the export: kills the running process, or keeps run() from starting one. Safe from any thread."""
        with self._lock:
            self._cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()

    def reset(self) -> None:
        """Make a finished, failed or stopped export runnable again (cancel and pause flags dropped). Only call while run() is not running."""
        with self._lock:
            self._proc, self._cancelled, self._paused = None, False, False

    def commands(self, target: str, threads: int = 0) -> List[List[str]]:
        """
        @param target   file the last command writes to
        @param threads  thread limit for decoding, filtering and encoding; 0 = FFmpeg decides
        @return         the FFmpeg command lines in the order they run (two for a two-pass export, else one)
        @raise ValueError if the extra options of the export cannot be split
        """
        job, o = self.job, self.options
        picture = job.video
        # "audio copy" without any clip with sound is not an error: there is nothing to copy, so the file gets no audio
        copy = o.audio == COPY_AUDIO and bool(job.audio_source)
        # The sound chains are part of the graph only if the mixed sound is mapped into the output. With "audio copy"
        # (the source stream is mapped instead), "no audio" or the first pass, an unused graph output would make FFmpeg
        # stop with "Filter ... has an unconnected output" - so the sound chains are left out then.
        mixed = bool(job.audio) and o.audio not in (COPY_AUDIO, NO_AUDIO)
        picture_graph = job.graph_video or job.graph    # fallback: a job built without the split graphs
        if o.height:
            picture_graph += ";[%s]scale=-2:%d:flags=lanczos,setsar=1%s[%s]" % (
                picture, o.height, ",setparams=" + job.tags if job.tags else "", _SCALED)
            picture = _SCALED
        graph = picture_graph + (";" + job.graph_audio if mixed and job.graph_audio else "")
        limit = ["-threads:v", str(threads)] if threads > 0 else []
        head = _base(self.tools) + ["-progress", "pipe:1", "-nostats"]
        if threads > 0:
            head += ["-filter_complex_threads", str(threads)]
        for item in job.inputs:
            head += limit + item
        if copy:
            head += job.audio_source                    # the same file once more, only for its audio stream
        video, extra, length = o.video_args(threads), o.extra_args(), ["-t", "%.6f" % job.duration]
        if job.tags:                                    # same tags again as options (older FFmpeg reads these)
            tag = dict(item.split("=") for item in job.tags.split(":"))
            video += ["-colorspace", tag["colorspace"], "-color_primaries", tag["color_primaries"],
                      "-color_trc", tag["color_trc"], "-color_range", "tv"]
        if copy:
            # Audio copy writes the source audio without any change. If the container cannot take that codec (AAC
            # into WebM, say) FFmpeg would only fail when it writes the header, which is after the whole first pass.
            # So it is checked here, before anything runs, and then the same source audio is encoded unchanged
            # (no volume, no denoise) with the codec the container takes instead.
            codec = audio_codec(self.tools, job.audio_source[-1])
            fits = copy_fits(o.container, codec)
            if not fits:
                other = copy_fallback(o.container, self.tools.encoders or None)
                o = replace(o, audio=other, audio_bitrate=AUDIO[other].bitrate)
            audio = ["-map", "%d:a:0" % len(job.inputs)] + o.audio_args(True)
        elif mixed:
            audio = ["-map", "[%s]" % job.audio] + o.audio_args(True)
        else:
            audio = o.audio_args(False)                 # -an
        final = head + ["-filter_complex", graph, "-map", "[%s]" % picture] + video + o.container_args() + audio + extra
        if not o.two_pass:
            return [final + length + [target]]
        log = ["-passlogfile", self._pass_log(target)]
        # The first pass is the same with or without sound: the picture only, no sound at all (-an), so no sound chain is
        # in its graph and nothing is left unconnected. FFmpeg shows no time for the first pass of VP9 / AV1 (known
        # FFmpeg bug, trac.ffmpeg.org tickets 9728, 9882 and 10165), so the pictures report their position themselves:
        # the showinfo filter logs one line per real frame ("... pts_time:T", see _FRAME_TIME) and passes it on unchanged.
        counted = picture + "_pos"
        first = head + ["-loglevel", "info", "-filter_complex", "%s;[%s]showinfo[%s]" % (picture_graph, picture, counted),
                        "-map", "[%s]" % counted]
        return [first + video + extra + ["-pass", "1"] + log + ["-an"] + length + ["-f", "null", "-"],
                final + ["-pass", "2"] + log + length + [target]]

    @staticmethod
    def _pass_log(target: str) -> str:
        """@return the file name start of the first-pass statistics (next to the target, unique per export)"""
        return os.path.splitext(target)[0] + "-pass"

    def run(self, threads: int = 0) -> Optional[str]:
        """
        Render the project into the output file and wait until FFmpeg is done.

        @param threads  see commands()
        @return         None if it finished or was cancelled (see `cancelled`), else the error text
        """
        root, ext = os.path.splitext(self.out_path)
        target = root + ".chloe-tmp" + ext
        error: Optional[str] = None
        try:
            commands = self.commands(target, threads)
            for number, args in enumerate(commands):
                error = self._run_pass(args, number / len(commands), (number + 1) / len(commands))
                if error or self._cancelled:
                    break
            else:
                stamp(target)                   # writing application: this program instead of FFmpeg; a failure leaves the file as it is
                os.replace(target, self.out_path)
        except OSError as exc:
            error = str(exc)
        finally:
            self._clean_up(target)
        return error

    def _run_pass(self, args: List[str], first: float, last: float) -> Optional[str]:
        """
        Run one FFmpeg command and wait for it.

        @param first  progress of the whole export when this command starts
        @param last   progress of the whole export when it ends
        @return       None if it finished or was cancelled, else the error text
        """
        lines: List[str] = []
        with self._lock:
            if self._cancelled:
                return None
            proc = self._proc = popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      universal_newlines=True, encoding="utf-8", errors="replace")
            if self._paused:
                _suspend(proc, True)
        for line in proc.stdout:
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            f = _FRAME_TIME.search(line)
            if m or f:
                if self.on_progress:
                    seconds = int(m.group(1)) / 1e6 if m else float(f.group(1))
                    self.on_progress(first + (last - first) * max(min(seconds / self.job.duration, 1.0), 0.0))
            elif "=" not in line.split(" ")[0]:
                lines.append(line)
        proc.wait()
        if self._cancelled or proc.returncode == 0:
            return None
        return _tail("".join(lines))

    def _clean_up(self, target: str) -> None:
        """Remove the temporary file and the first-pass statistics, whatever happened."""
        folder = os.path.dirname(target) or "."
        stats = os.path.basename(self._pass_log(target))
        try:
            names = [n for n in os.listdir(folder) if n.startswith(stats)] if self.options.two_pass else []
        except OSError:
            names = []
        for path in [target] + [os.path.join(folder, n) for n in names]:
            try:
                os.remove(path)
            except OSError:
                pass
