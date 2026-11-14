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
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import fields, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, TextIO

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
    run.add_argument("--quiet", action="store_true", help="print nothing but errors")
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


def export_options(args: argparse.Namespace, tools: ff.Tools, out_path: str,
                   notes: Optional[List[str]] = None) -> enc.ExportOptions:
    """
    The export settings: the preset (or the defaults) with what the command line sets on top. Every value is checked
    against this FFmpeg, and a value the user asked for that cannot be used is an error, not silently another one.

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
    base = enc.ExportOptions()
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


class Progress:
    """
    Progress for Export(on_progress=...): a percent that counts up on one line when the output is a terminal,
    a line at every 10 % when it is a pipe or a file (so logs stay short).
    """

    def __init__(self, stream: Optional[TextIO] = None):
        self._out = stream or sys.stderr
        self._tty = self._out.isatty()
        self._shown = -1

    def __call__(self, fraction: float) -> None:
        percent = int(max(0.0, min(fraction, 1.0)) * 100)
        step = percent if self._tty else percent // 10 * 10
        if step != self._shown:
            self._shown = step
            self._out.write("\rEncoding %3d%%" % step if self._tty else "Encoding %d%%\n" % step)
            self._out.flush()

    def close(self) -> None:
        """End the line of the terminal version."""
        if self._tty and self._shown >= 0:
            self._out.write("\n")
            self._out.flush()


def _stop_on_signal(export: ff.Export) -> None:
    """Ctrl+C and termination stop the export through its own cancel(), so the temporary files are removed."""
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, lambda _number, _frame: export.cancel())


def render(args: argparse.Namespace, tools: ff.Tools, say) -> int:
    """
    Load the input, work out the settings and run the export.

    @param say  prints a message line to stderr (does nothing with --quiet)
    @return     exit code
    @raise CliError for everything the user can fix
    """
    notes: List[str] = []
    options = export_options(args, tools, args.output, notes)       # before the (slower) probe of the input
    for note in notes:
        say("note: " + note)
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
    progress = None if args.quiet else Progress()
    export = ff.Export(tools, job, options, args.output, progress)
    if args.dry_run:
        for command in export.commands(args.output, args.threads):
            print(command_text(command))
        return EXIT_OK
    say("%s -> %s (%s)" % (os.path.basename(args.input), args.output, options.summary()))
    _stop_on_signal(export)
    error = export.run(args.threads)
    if progress:
        if not error and not export.cancelled:
            progress(1.0)
        progress.close()
    if export.cancelled:
        say("stopped")
        return EXIT_STOPPED
    if error:
        raise CliError(error)
    say("done: %s (%.1f MB)" % (args.output, os.path.getsize(args.output) / 1e6))
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

    def say(text: str) -> None:
        if not args.quiet:
            print(text, file=sys.stderr)

    try:
        cfg = load_config()
        effect.discover()
        tools = ff.locate(args.ffmpeg or cfg["ffmpeg"])
        if tools is None:
            raise CliError("FFmpeg not found: put it on PATH or use --ffmpeg")
        if ff.too_old(tools):
            say("warning: FFmpeg %s is older than %d.%d, some effects may fail" % ((tools.version,) + ff.MIN_VERSION))
        if args.list_presets:
            for key, options in enc.available_presets(tools.encoders, tools.depths).items():
                print("%-12s %s" % (key, options.summary()))
            return EXIT_OK
        return render(args, tools, say)
    except CliError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
