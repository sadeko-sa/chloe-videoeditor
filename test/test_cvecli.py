"""Tests of the command line version: reading the options (no FFmpeg needed) and a real export (needs FFmpeg)."""

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
