"""Tests of the command line version: reading the options (no FFmpeg needed) and a real export (needs FFmpeg)."""

import io
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cvecli as cli                                           # noqa: E402
from engine import encode as enc                                  # noqa: E402
from engine import ffmpeg as ff                                   # noqa: E402

FAKE = ff.Tools("ffmpeg", "ffprobe", "6.0",
                frozenset({"libx264", "libx265", "libvpx-vp9", "aac", "libopus"}),
                {"libx264": (8, 10), "libx265": (8, 10), "libvpx-vp9": (8, 10)})
"""An FFmpeg that can write H.264, H.265, VP9, AAC and Opus; the options are checked against this."""


def _options(line, out="out.mp4", notes=None):
    args = cli.build_parser("cvecli.py").parse_args(line.split())
    return cli.export_options(args, FAKE, out, notes)


def test_wants_cli_only_for_options():
    assert cli.wants_cli(["-i", "a.mp4"]) and cli.wants_cli(["--help"])
    assert not cli.wants_cli([]) and not cli.wants_cli(["project.cvproj"])


def test_defaults_are_the_dialog_defaults():
    assert _options("") == enc.ExportOptions().normalized(FAKE.encoders, FAKE.depths)


def test_container_follows_the_output_name():
    assert _options("", "clip.mkv").container == "mkv"
    assert _options("", "clip.webm").video == "vp9"


def test_options_are_taken_over():
    o = _options("-e x265 -q 24 --encoder-preset slow -l 720 -E opus -B 128 -r 30", "a.mkv")
    assert (o.video, o.crf, o.speed, o.height, o.audio, o.audio_bitrate, o.fps) == ("x265", 24, "slow", 720, "opus", 128, "30")


def test_bitrate_switches_the_rate_control():
    o = _options("-b 2000 --two-pass")
    assert (o.rate, o.bitrate, o.two_pass) == (enc.RATE_BITRATE, 2000, True)
    assert _options("-q 30").rate == enc.RATE_QUALITY


def test_preset_is_the_base_and_options_win():
    o = _options("-Z phone_720 -q 18")
    assert (o.height, o.crf) == (720, 18)


def _saved(line, saved, out="out.mp4"):
    args = cli.build_parser("cvecli.py").parse_args(line.split())
    return cli.export_options(args, FAKE, out, None, saved)


def test_project_options_are_the_base_preset_and_options_win():
    saved = enc.ExportOptions(crf=30, video="x265", container="mkv").to_dict()
    assert (_saved("", saved, "a.mkv").video, _saved("", saved, "a.mkv").crf) == ("x265", 30)
    assert _saved("-q 18", saved, "a.mkv").crf == 18                      # the command line wins over the project
    assert _saved("-Z phone_720", saved, "a.mp4").crf != 30               # a preset replaces the project options


def test_project_keeps_its_export_options(tmp_path):
    from engine.project import Project, load_project, save_project
    project = Project()
    path = str(tmp_path / "p.cvproj")
    save_project(project, path)
    assert "export" not in json.load(open(path)) and load_project(path).export == {}      # old files stay as they were
    project.export = enc.ExportOptions(crf=19).to_dict()
    save_project(project, path)
    assert load_project(path).export["crf"] == 19 and cli.project_export(path)["crf"] == 19
    assert cli.project_export(str(tmp_path / "clip.mp4")) is None


@pytest.mark.parametrize("line, out", [
    ("-e x264", "a.webm"),                   # the container cannot hold it
    ("--tune nonsense", "a.mp4"),
    ("-q 99", "a.mp4"),                      # beyond the range of the encoder
    ("--depth 12", "a.mp4"),                 # this FFmpeg lists no 12 bit
    ("--two-pass", "a.mp4"),                 # two-pass needs -b
    ("-e svtav1", "a.mp4"),                  # encoder this FFmpeg does not have
    ("-Z archive", "a.mkv"),                 # preset that needs FLAC / SVT-AV1 this FFmpeg lacks
    ("-Z nonsense", "a.mp4"),
    ("", "a.avi"),                           # no container for the extension
    ("-f mkv", "a.mp4"),                     # name and container disagree
    ("--extra \"x", "a.mp4"),
])
def test_unusable_requests_are_errors(line, out):
    with pytest.raises(cli.CliError):
        _options(line, out)


def test_a_change_the_user_did_not_ask_for_is_reported():
    notes = []
    o = _options("", "a.webm", notes)        # default encoder is H.264, WebM needs VP9 / AV1; audio goes to Opus too
    assert o.video == "vp9" and o.audio == "opus"
    assert any(n.startswith("video") for n in notes) and any(n.startswith("audio") for n in notes)


def test_quality_and_bitrate_exclude_each_other(capsys):
    with pytest.raises(SystemExit) as stop:
        cli.build_parser("cvecli.py").parse_args(["-q", "20", "-b", "1000"])
    assert stop.value.code == 2


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


class Clock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_progress_numbers():
    clock = Clock()
    report = cli.JsonReporter(io.StringIO(), clock)
    report.start("a.mp4", "b.mp4", "summary", 60.0)
    assert report.stats(0.0)["eta"] is None and report.stats(0.0)["speed"] is None
    clock.now += 10
    s = report.stats(0.25)                      # 15 s of result in 10 s of work, three quarters to go
    assert (s["percent"], s["elapsed"], s["speed"], s["eta"]) == (25.0, 10.0, 1.5, 30.0)
    assert report.stats(5.0)["percent"] == 100.0 and report.stats(-1)["percent"] == 0.0


def test_eta_follows_a_second_pass_that_is_much_slower():
    """The log of a Kaggle run: pass 1 took a few minutes, pass 2 is far slower, the eta must not stay at pass 1's speed."""
    clock = Clock()
    report = cli.JsonReporter(io.StringIO(), clock)
    report.start("a.mp4", "b.mp4", "summary", 1000.0)
    fraction = 0.0
    for gain, seconds in ((0.005, 100), (0.0005, 300)):         # half of the work in 100 s, then ten times slower
        for _ in range(seconds):
            clock.now += 1
            fraction += gain
            stats = report.stats(fraction)
    assert stats["percent"] == 65.0
    assert stats["eta"] == pytest.approx(700.0)                 # the average over the whole run would say 215
    assert stats["speed"] == pytest.approx(0.5)                 # 0.0005 of 1000 s of result per second


def test_no_eta_and_speed_before_a_few_seconds_are_measured_or_without_progress():
    """Too little data and no progress give None for both; they come with the first progress after a few seconds."""
    clock = Clock()
    report = cli.JsonReporter(io.StringIO(), clock)
    report.start("a.mp4", "b.mp4", "summary", 60.0)
    clock.now += 1
    s = report.stats(0.1)
    assert s["eta"] is None and s["speed"] is None
    clock.now += 10
    s = report.stats(0.0)
    assert s["eta"] is None and s["speed"] is None
    s = report.stats(0.1)
    assert s["eta"] == pytest.approx(99.0) and s["speed"] == pytest.approx(0.55)


def test_progress_is_throttled():
    clock, out = Clock(), io.StringIO()
    report = cli.JsonReporter(out, clock)
    report.start("a", "b", "s", 10.0)
    for step in range(10):                      # ten calls within one interval
        report.progress(step / 10)
        clock.now += 0.01
    clock.now += 1
    report.progress(0.9)
    assert [json.loads(l)["percent"] for l in out.getvalue().splitlines()[1:]] == [0.0, 90.0]


def test_json_events_are_one_object_per_line():
    out = io.StringIO()
    report = cli.JsonReporter(out, Clock())
    report.note("changed", "warning")
    report.start("a.mp4", "b.mp4", "MP4", 3.0)
    report.progress(0.5)
    report.done("b.mp4", 1234)
    report.error("bad")
    events = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [e["event"] for e in events] == ["note", "start", "progress", "done", "error"]
    assert events[0] == {"event": "note", "level": "warning", "text": "changed"}
    assert events[3]["bytes"] == 1234 and events[3]["percent"] == 100.0


def test_text_report_to_a_pipe_has_a_line_per_ten_percent():
    clock, out = Clock(), io.StringIO()         # StringIO is not a terminal
    report = cli.TextReporter(stream=out, clock=clock)
    report.start("a.mp4", "b.mp4", "MP4", 100.0)
    for percent in range(0, 101, 1):
        clock.now += 1
        report.progress(percent / 100)
    report.done("b.mp4", 2_000_000)
    lines = out.getvalue().splitlines()
    assert lines[0] == "a.mp4 -> b.mp4 (MP4)" and lines[-1].startswith("done: b.mp4 (2.0 MB)")
    assert len([l for l in lines if l.startswith("Encoding")]) == 11 and "ETA" in lines[2]


def test_quiet_text_report_says_only_errors():
    out = io.StringIO()
    report = cli.TextReporter(quiet=True, stream=out, clock=Clock())
    report.note("x")
    report.start("a", "b", "s", 1.0)
    report.progress(0.5)
    report.done("b", 1)
    report.error("bad")
    assert out.getvalue() == "error: bad\n"


@pytest.mark.parametrize("seconds, text", [(0, "0:00"), (59.6, "1:00"), (125, "2:05"), (3725, "1:02:05")])
def test_clock_text(seconds, text):
    assert cli._clock_text(seconds) == text


# --------------------------------------------------------------------------- #
# A real export
# --------------------------------------------------------------------------- #

needs_ffmpeg = pytest.mark.skipif(ff.locate("") is None, reason="FFmpeg not found")


@pytest.fixture
def clip(tmp_path):
    path = str(tmp_path / "in.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=1",
                    "-f", "lavfi", "-i", "sine=duration=1", "-shortest", "-c:v", "libx264", path], check=True)
    return path


@needs_ffmpeg
def test_export_and_the_guards(clip, tmp_path, capsys):
    out = str(tmp_path / "out.mkv")
    line = ["-i", clip, "-o", out, "-q", "30", "--encoder-preset", "ultrafast", "--quiet"]
    assert cli.main(line) == cli.EXIT_OK
    assert os.path.getsize(out) > 0
    assert cli.main(line) == cli.EXIT_FAILED                           # exists: needs -y
    assert cli.main(line + ["-y"]) == cli.EXIT_OK
    assert cli.main(["-i", clip, "-o", clip, "-y", "--quiet"]) == cli.EXIT_FAILED   # would replace the input
    assert sorted(os.listdir(tmp_path)) == ["in.mp4", "out.mkv"]       # no temporary files left


@needs_ffmpeg
def test_dry_run_writes_nothing(clip, tmp_path, capsys):
    out = str(tmp_path / "out.mp4")
    assert cli.main(["-i", clip, "-o", out, "--dry-run"]) == cli.EXIT_OK
    assert "libx264" in capsys.readouterr().out and not os.path.exists(out)


@needs_ffmpeg
def test_json_mode_prints_only_events(clip, tmp_path, capsys):
    out = str(tmp_path / "out.mp4")
    assert cli.main(["-i", clip, "-o", out, "--encoder-preset", "ultrafast", "--json"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    events = [json.loads(l) for l in captured.out.splitlines()]            # every line is JSON
    assert [e["event"] for e in events][0] == "start" and events[-1]["event"] == "done"
    assert captured.err == ""
    assert cli.main(["-i", clip, "-o", out, "--json"]) == cli.EXIT_FAILED   # exists
    assert json.loads(capsys.readouterr().out)["event"] == "error"
