#!/usr/bin/env python3
"""
cvecli.py - Chloe Videoeditor on the command line: render a project, or one video / image, without a window.
Uses the same engine as File > Export video (no Tk), so the result is the same. Options are named like HandBrakeCLI's.

    python cvecli.py -i project.cvproj -o out.mp4
    python cvecli.py -i clip.mov -o small.mp4 -Z phone_720
    python cvecli.py -i clip.mov -o out.mkv -e x265 -q 24 --encoder-preset slow -E opus -B 128
    python cvecli.py --help

chloe_videoeditor.py hands over to main() here when its command line holds an option (an argument starting with "-"),
so `python chloe_videoeditor.py -i a.mp4 -o b.mp4` does the same. Exit code: 0 done, 1 error, 2 wrong usage, 130 stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import fields, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, TextIO

from engine import APP_NAME, VERSION, effect
from engine import encode as enc
from engine import ffmpeg as ff
from engine.compiler import CompileError, compile_export
from engine.config import load_config
from engine.project import Project, ProjectError, load_project

EXIT_OK, EXIT_FAILED, EXIT_STOPPED = 0, 1, 130

PROJECT_EXT = ".cvproj"
"""Input files with this extension are projects, everything else is probed as one video or image."""


class CliError(Exception):
    """Something the user can fix; the text is printed after "error:" and the exit code is EXIT_FAILED."""


def wants_cli(argv: Sequence[str]) -> bool:
    """@return True if the command line holds an option, which only the command line version understands"""
    return any(arg.startswith("-") for arg in argv)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def _listing(values: Iterable[Any]) -> str:
    return ", ".join(str(v) for v in values if v != "")


_OPTIONS = (
    # (flags, ExportOptions field, type, help)
    (("-f", "--format"), "container", str, "container: " + _listing(enc.CONTAINERS)),
    (("-e", "--encoder"), "video", str, "video encoder: " + _listing(enc.VIDEO)),
    (("-q", "--quality"), "crf", int, "constant quality (CRF), lower = better; the range depends on the encoder"),
    (("-b", "--vb"), "bitrate", int, "average video bitrate in kb/s instead of -q"),
    (("--encoder-preset",), "speed", str, "encoder speed, e.g. slow (x264, x265) or 4 (VP9, AV1)"),
    (("--tune",), "tune", str, "encoder tune, e.g. film or animation"),
    (("--profile",), "profile", str, "encoder profile, e.g. high"),
    (("--level",), "level", str, "encoder level, e.g. 4.1"),
    (("--depth",), "bit_depth", int, "bits per color channel: " + _listing(enc.DEPTHS)),
    (("-r", "--rate"), "fps", str, "output frame rate: " + _listing(enc.FPS_CHOICES) + " (default: the project rate)"),
    (("-l", "--height"), "height", int, "output height in pixels: " + _listing(enc.HEIGHTS[1:]) + " (default: project size)"),
    (("-E", "--aencoder"), "audio", str, "audio: %s, %s (source audio as it is) or %s"
     % (_listing(enc.AUDIO), enc.COPY_AUDIO, enc.NO_AUDIO)),
    (("-B", "--ab"), "audio_bitrate", int, "audio bitrate in kb/s"),
    (("--channels",), "channels", int, "audio channels: 1 (mono) or 2 (stereo); default: as mixed"),
    (("-R", "--arate"), "sample_rate", int, "audio sample rate in Hz: " + _listing(enc.SAMPLE_RATES[1:])),
    (("--extra",), "extra", str, "extra FFmpeg output options, written like a command line"),
)

_FLAG = {field: flags[-1] for flags, field, _kind, _help in _OPTIONS}
_FLAG.update(rate="-q / -b", two_pass="--two-pass", faststart="--no-faststart")
"""Flag of each ExportOptions field, for messages."""

_NOTED = ("container", "video", "audio")
"""Fields whose change the user is told about; the rest (speed, CRF, ...) only follow from these."""


def build_parser(prog: Optional[str] = None) -> argparse.ArgumentParser:
    """@return the parser; an option the user left out is not in the result at all (see export_options())"""
    parser = argparse.ArgumentParser(
        prog=prog or os.path.basename(sys.argv[0]),
        description="%s: render a project (%s) or one video / image to a file without opening the window." % (APP_NAME, PROJECT_EXT),
        epilog="Without -Z and the options below the export uses H.264 / AAC in MP4 with the defaults of "
               "File > Export video. The container follows the extension of -o unless -f is given.")
    parser.add_argument("-i", "--input", metavar="FILE", help="project (%s), video or image" % PROJECT_EXT)
    parser.add_argument("-o", "--output", metavar="FILE", help="file to write (.mp4, .mkv or .webm)")
    parser.add_argument("-Z", "--preset", metavar="NAME", help="start from an export preset (see --list-presets)")
    parser.add_argument("--list-presets", action="store_true", help="list the presets this FFmpeg can run, then exit")
    video = parser.add_argument_group("video, audio and container")
    rate = video.add_mutually_exclusive_group()    # constant quality or average bitrate, not both
    for flags, field, kind, text in _OPTIONS:
        (rate if field in ("crf", "bitrate") else video).add_argument(
            *flags, dest=field, type=kind, default=argparse.SUPPRESS, help=text)
    video.add_argument("--two-pass", dest="two_pass", action="store_true", default=argparse.SUPPRESS,
                       help="two-pass encoding (with -b; x264, x265, VP9, libaom)")
    video.add_argument("--no-faststart", dest="faststart", action="store_false", default=argparse.SUPPRESS,
                       help="MP4: keep the index at the end of the file")
    run = parser.add_argument_group("running")
    run.add_argument("-y", "--overwrite", action="store_true", help="replace the output file if it exists")
    run.add_argument("--threads", type=int, default=0, metavar="N", help="thread limit of FFmpeg (default 0: automatic)")
    run.add_argument("--ffmpeg", metavar="PATH", help="FFmpeg file or folder (default: the setting of the window, then search)")
    run.add_argument("--dry-run", action="store_true", help="print the FFmpeg command(s) and exit")
    run.add_argument("--progress-step", type=float, default=10.0, metavar="PERCENT",
                     help="percent between two progress lines when the output is a pipe or file (default 10; 1 or 0.5 for a log you watch)")
    run.add_argument("--quiet", action="store_true", help="print nothing but errors")
    run.add_argument("--json", action="store_true",
                     help="for other programs: one JSON event per line on stdout (see JsonReporter in this file)")
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    return parser


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _container_of(path: str) -> Optional[str]:
    """@return key of enc.CONTAINERS whose extension the file name has, None if none"""
    ext = os.path.splitext(path)[1].lower()
    return next((key for key, c in enc.CONTAINERS.items() if c.ext == ext), None)


def _shown(value: Any) -> str:
    return repr(value) if value != "" else "none"


def load_input(tools: ff.Tools, path: str) -> Project:
    """
    @param path  a project file, or a video / image that becomes a project of one layer (size and rate of the file)
    @raise CliError if it cannot be read, or a media file of the project is gone
    """
    if not os.path.isfile(path):
        raise CliError("input not found: %s" % path)
    if path.lower().endswith(PROJECT_EXT):
        try:
            project = load_project(path)
        except ProjectError as exc:
            raise CliError("cannot read %s: %s" % (path, exc))
        missing = [layer.path for layer in project.media_layers() if layer.is_missing()]
        if missing:
            raise CliError("media file missing: " + ", ".join(missing))
        return project
    project = Project()
    try:
        project.add_media(ff.probe(tools, path))
    except ff.FFmpegError as exc:
        raise CliError("cannot open %s: %s" % (path, exc))
    return project


def project_export(path: str) -> Optional[Dict[str, Any]]:
    """
    @param path  input file of the command line
    @return      the export options saved in this project file, or None for anything else (not a project, no options,
                 unreadable: load_input() reports the last one)
    """
    if not path.lower().endswith(PROJECT_EXT) or not os.path.isfile(path):
        return None
    try:
        return load_project(path).export or None
    except ProjectError:
        return None


def export_options(args: argparse.Namespace, tools: ff.Tools, out_path: str,
                   notes: Optional[List[str]] = None, saved: Optional[Dict[str, Any]] = None) -> enc.ExportOptions:
    """
    The export settings, lowest to highest: the defaults, the options saved in the project, the preset (-Z), what the
    command line sets. Every value is checked against this FFmpeg, and a value the user asked for that cannot be used
    is an error, not silently another one.

    @param saved  export options of the project file (project_export()), None = none
    @param notes  gets one line for each value the user did NOT ask for that had to change to fit what they asked for
                  (for example the video encoder when -f webm is not met by x264)
    @raise CliError for an unknown preset, a requested value that is not usable, or an output name that does not fit
    """
    asked: Dict[str, Any] = {f.name: getattr(args, f.name) for f in fields(enc.ExportOptions) if hasattr(args, f.name)}
    if "bitrate" in asked:
        asked["rate"] = enc.RATE_BITRATE
    elif "crf" in asked:
        asked["rate"] = enc.RATE_QUALITY
    if "container" not in asked and _container_of(out_path):
        asked["container"] = _container_of(out_path)
    base = enc.ExportOptions.from_dict(saved, tools.encoders, tools.depths) if saved else enc.ExportOptions()
    if args.preset:
        presets = enc.available_presets(tools.encoders, tools.depths)
        if args.preset not in presets:
            raise CliError("no preset %r here (see --list-presets)" % args.preset)
        base = presets[args.preset]
    wanted = replace(base, **asked)
    final = wanted.normalized(tools.encoders, tools.depths)
    for f in fields(enc.ExportOptions):             # in declaration order: container and video decide the rest
        want, got = getattr(wanted, f.name), getattr(final, f.name)
        if want == got:
            continue
        if f.name in asked:
            raise CliError("%s %s cannot be used with %s in %s (nearest valid value: %s)"
                           % (_FLAG.get(f.name, f.name), _shown(want), enc.VIDEO[final.video].label,
                              enc.CONTAINERS[final.container].label, _shown(got)))
        if notes is not None and f.name in _NOTED:
            notes.append("%s changed from %s to %s to fit the other settings" % (f.name, _shown(want), _shown(got)))
    if _container_of(out_path) != final.container:
        raise CliError("the output name must end in %s for %s" % (enc.CONTAINERS[final.container].ext, final.container))
    try:
        final.extra_args()
    except ValueError:
        raise CliError("--extra: unbalanced quotes")
    return final


def command_text(args: Sequence[str]) -> str:
    """@return an FFmpeg command line as one string a shell of this system can run"""
    return subprocess.list2cmdline(list(args)) if ff.IS_WIN else shlex.join(args)


# --------------------------------------------------------------------------- #
# Reports: what the user, or a front end that runs this program, is told
# --------------------------------------------------------------------------- #


def _clock_text(seconds: float) -> str:
    """@return seconds as m:ss, or h:mm:ss from one hour on"""
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return "%d:%02d:%02d" % (hours, minutes, secs) if hours else "%d:%02d" % (minutes, secs)


class Reporter:
    """
    Base of the two ways to report (TextReporter for people, JsonReporter for other programs). It does the counting
    that both need; a subclass only decides how each message looks.

    The events, in the order they come: note (any number), start, progress (any number), then exactly one of
    done, stopped or error. Dry runs and the preset list send `commands` and `presets` instead.

    Progress numbers (from progress(), see stats()): percent 0..100, elapsed seconds, eta seconds (None until 1 %
    is done), speed (seconds of result made per second of work, None at the very start). A two-pass export counts
    both passes, so its speed is half of what FFmpeg shows and the eta stays right.
    """

    interval = 0.5
    """Least seconds between two progress messages (the first one always goes through)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._began = clock()
        self._duration = 0.0
        self._last: Optional[float] = None

    def stats(self, fraction: float) -> Dict[str, Any]:
        """@param fraction  0..1 of the whole export @return percent, elapsed, eta and speed as the class text says"""
        fraction = max(0.0, min(fraction, 1.0))
        elapsed = self._clock() - self._began
        eta = elapsed * (1 - fraction) / fraction if fraction >= 0.01 else None
        speed = fraction * self._duration / elapsed if fraction > 0 and elapsed > 0 else None
        return {"percent": round(fraction * 100, 1), "elapsed": round(elapsed, 1),
                "eta": None if eta is None else round(eta, 1), "speed": None if speed is None else round(speed, 2)}

    def start(self, source: str, target: str, summary: str, duration: float) -> None:
        """The export begins. @param duration  seconds of the result"""
        self._began, self._duration, self._last = self._clock(), duration, None
        self._start(source, target, summary, duration)

    def progress(self, fraction: float) -> None:
        """Export(on_progress=...): passes on at most one message per `interval`."""
        now = self._clock()
        if self._last is None or now - self._last >= self.interval:
            self._last = now
            self._progress(self.stats(fraction))

    def done(self, target: str, size: int) -> None:
        """The file is complete. @param size  bytes"""
        self._done(target, size, self.stats(1.0))

    def note(self, text: str, level: str = "note") -> None:
        """@param level  "note" (something was changed for the user) or "warning" """

    def stopped(self) -> None:
        """The export was stopped (Ctrl+C or termination); the output file was not written."""

    def error(self, text: str) -> None:
        """Nothing was written because of this."""

    def commands(self, commands: Sequence[Sequence[str]]) -> None:
        """--dry-run: the FFmpeg command(s). Always sent, also with --quiet."""

    def presets(self, presets: Dict[str, str]) -> None:
        """--list-presets: {key: summary}. Always sent, also with --quiet."""

    def _start(self, source: str, target: str, summary: str, duration: float) -> None: ...

    def _progress(self, stats: Dict[str, Any]) -> None: ...

    def _done(self, target: str, size: int, stats: Dict[str, Any]) -> None: ...


class TextReporter(Reporter):
    """
    Messages on stderr. On a terminal one line is redrawn in place:

        Encoding  42%  1.8x  elapsed 0:12  ETA 0:17

    to a pipe or file it is one line at every `step` percent (default 10), so logs stay short. quiet = errors only. Results that are the
    point of the call (commands, presets) go to stdout.
    """

    def __init__(self, quiet: bool = False, stream: Optional[TextIO] = None, clock: Callable[[], float] = time.monotonic,
                 step: float = 10.0):
        """@param step  percent between two lines when the output is not a terminal"""
        super().__init__(clock)
        self._step = max(0.1, min(step, 100.0))
        self._out = stream or sys.stderr
        self._quiet = quiet
        self._tty = self._out.isatty()
        self.interval = Reporter.interval if self._tty else 0.0
        self._shown = -1                    # percent (tty) or tenth (pipe) last written
        self._open = False                  # a redrawn line is on screen and not ended yet
        self._width = 0

    def _line(self, text: str) -> None:
        self._end_line()
        self._out.write(text + "\n")
        self._out.flush()

    def _end_line(self) -> None:
        if self._open:
            self._out.write("\n")
            self._open = False

    def _draw(self, stats: Dict[str, Any]) -> None:
        percent = stats["percent"]
        step = int(percent) if self._tty else int(percent / self._step)
        if step == self._shown and not self._tty:
            return
        self._shown = step
        parts = ["Encoding %5.1f%%" % percent if self._step < 1 and not self._tty else "Encoding %3d%%" % percent]
        if stats["speed"] is not None:
            parts.append("%.2fx" % stats["speed"])
        parts.append("elapsed " + _clock_text(stats["elapsed"]))
        if stats["eta"] is not None:
            parts.append("ETA " + _clock_text(stats["eta"]))
        text = "  ".join(parts)
        if self._tty:
            self._width = max(self._width, len(text))
            self._out.write("\r" + text.ljust(self._width))
            self._open = True
        else:
            self._out.write(text + "\n")
        self._out.flush()

    def note(self, text: str, level: str = "note") -> None:
        if not self._quiet:
            self._line("%s: %s" % (level, text))

    def _start(self, source: str, target: str, summary: str, duration: float) -> None:
        if not self._quiet:
            self._line("%s -> %s (%s)" % (os.path.basename(source), target, summary))

    def _progress(self, stats: Dict[str, Any]) -> None:
        if not self._quiet:
            self._draw(stats)

    def _done(self, target: str, size: int, stats: Dict[str, Any]) -> None:
        if not self._quiet:
            self._draw(stats)
            self._line("done: %s (%.1f MB) in %s" % (target, size / 1e6, _clock_text(stats["elapsed"])))

    def stopped(self) -> None:
        if not self._quiet:
            self._line("stopped")

    def error(self, text: str) -> None:
        self._line("error: " + text)

    def commands(self, commands: Sequence[Sequence[str]]) -> None:
        for command in commands:
            print(command_text(command))

    def presets(self, presets: Dict[str, str]) -> None:
        for key, summary in presets.items():
            print("%-12s %s" % (key, summary))


class JsonReporter(Reporter):
    """
    For programs that run this one (--json): one JSON object per line on stdout, flushed at once, and nothing else on
    stdout or stderr (a wrong command line still ends with the usage text and exit code 2, before any event).
    Every object has "event"; the other keys are:

        note      level ("note" | "warning"), text
        start     source, target, summary, duration (seconds of the result)
        progress  percent, elapsed, eta, speed (see Reporter; eta and speed can be null)
        done      target, bytes, percent (100), elapsed
        stopped
        error     text
        commands  commands: [[arg, ...], ...]
        presets   presets: {key: summary}

    The exit code says the same as the last event: 0 done (or commands / presets), 1 error, 130 stopped.
    """

    def __init__(self, stream: Optional[TextIO] = None, clock: Callable[[], float] = time.monotonic):
        super().__init__(clock)
        self._out = stream or sys.stdout

    def _emit(self, event: str, **data: Any) -> None:
        self._out.write(json.dumps(dict(event=event, **data), ensure_ascii=False) + "\n")
        self._out.flush()

    def note(self, text: str, level: str = "note") -> None:
        self._emit("note", level=level, text=text)

    def _start(self, source: str, target: str, summary: str, duration: float) -> None:
        self._emit("start", source=source, target=target, summary=summary, duration=round(duration, 3))

    def _progress(self, stats: Dict[str, Any]) -> None:
        self._emit("progress", **stats)

    def _done(self, target: str, size: int, stats: Dict[str, Any]) -> None:
        self._emit("done", target=target, bytes=size, percent=stats["percent"], elapsed=stats["elapsed"])

    def stopped(self) -> None:
        self._emit("stopped")

    def error(self, text: str) -> None:
        self._emit("error", text=text)

    def commands(self, commands: Sequence[Sequence[str]]) -> None:
        self._emit("commands", commands=[list(c) for c in commands])

    def presets(self, presets: Dict[str, str]) -> None:
        self._emit("presets", presets=presets)


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #


def _stop_on_signal(export: ff.Export) -> None:
    """Ctrl+C and termination stop the export through its own cancel(), so the temporary files are removed."""
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda _number, _frame: export.cancel())


def render(args: argparse.Namespace, tools: ff.Tools, report: Reporter) -> int:
    """
    Load the input, work out the settings and run the export.

    @return  exit code
    @raise CliError for everything the user can fix
    """
    notes: List[str] = []
    saved = project_export(args.input)
    if saved and not args.preset:
        notes.append("export settings taken from the project file")
    options = export_options(args, tools, args.output, notes, saved)       # before the (slower) probe of the input
    for note in notes:
        report.note(note)
    project = load_input(tools, args.input)
    if any(_same_file(args.output, layer.path) for layer in project.media_layers()) or _same_file(args.output, args.input):
        raise CliError("the output would replace an input file: %s" % args.output)
    if os.path.exists(args.output) and not (args.overwrite or args.dry_run):
        raise CliError("%s exists (use -y to replace it)" % args.output)
    try:
        job = compile_export(project)
    except CompileError as exc:
        raise CliError(str(exc))
    if options.audio == enc.COPY_AUDIO and not job.audio_source:
        raise CliError("-E copy needs exactly one clip with sound that is not changed")
    export = ff.Export(tools, job, options, args.output, report.progress)
    if args.dry_run:
        report.commands(export.commands(args.output, args.threads))
        return EXIT_OK
    report.start(args.input, args.output, options.summary(), job.duration)
    _stop_on_signal(export)
    error = export.run(args.threads)
    if export.cancelled:
        report.stopped()
        return EXIT_STOPPED
    if error:
        raise CliError(error)
    report.done(args.output, os.path.getsize(args.output))
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """
    @param argv  the command line without the program name; None = sys.argv[1:]
    @return      exit code (a wrong command line exits with 2 from inside argparse)
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.list_presets and not (args.input and args.output):
        parser.error("-i / --input and -o / --output are required")
    report = JsonReporter() if args.json else TextReporter(args.quiet, step=args.progress_step)
    try:
        cfg = load_config()
        effect.discover()
        tools = ff.locate(args.ffmpeg or cfg["ffmpeg"])
        if tools is None:
            raise CliError("FFmpeg not found: put it on PATH or use --ffmpeg")
        if ff.too_old(tools):
            report.note("FFmpeg %s is older than %d.%d, some effects may fail" % ((tools.version,) + ff.MIN_VERSION),
                        "warning")
        if args.list_presets:
            report.presets({k: o.summary() for k, o in enc.available_presets(tools.encoders, tools.depths).items()})
            return EXIT_OK
        return render(args, tools, report)
    except CliError as exc:
        report.error(str(exc))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
