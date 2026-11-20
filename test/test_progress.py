"""Tests of the progress an Export reports while FFmpeg runs (a fake FFmpeg process, no FFmpeg needed)."""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ffmpeg as ff  # noqa: E402

DURATION = 100.0
"""Seconds of the result in these tests."""


class _FakeProcess:
    """A stand-in for the FFmpeg process: its output is the given lines."""

    def __init__(self, lines):
        """@param lines  what FFmpeg writes, one string per line"""
        self.stdout = iter(lines)
        self.returncode = 0

    def wait(self):
        """Does nothing, the process is already done."""
        return 0


def _block(us, ms=None):
    """
    @param us  value of the out_time_us line, in microseconds
    @param ms  value of the out_time_ms line; the same as us if None
    @return    the lines of one -progress block
    """
    ms = us if ms is None else ms
    return ["frame=1\n", "out_time_us=%d\n" % us, "out_time_ms=%d\n" % ms, "progress=continue\n"]


def _run(monkeypatch, lines, first=0.0, last=1.0):
    """
    Runs one pass over the fake output.

    @param monkeypatch  pytest fixture that replaces the process start
    @param lines        what the fake FFmpeg writes
    @param first        progress of the whole export when the pass starts
    @param last         progress of the whole export when the pass ends
    @return             the fractions that were reported, in order
    """
    reported = []
    monkeypatch.setattr(ff, "popen", lambda *_args, **_kwargs: _FakeProcess(lines))
    export = ff.Export(None, SimpleNamespace(duration=DURATION), None, "out.mkv", reported.append)
    assert export._run_pass(["ffmpeg"], first, last) is None
    return reported


def test_equal_time_lines_report_once_per_block(monkeypatch):
    """The two time lines of a block are one position, not two."""
    assert _run(monkeypatch, _block(10_000_000) + _block(20_000_000)) == pytest.approx([0.1, 0.2])


def test_lower_second_time_line_does_not_make_the_percent_jump(monkeypatch):
    """The log that showed 1 % and 0 % in turns: the block holds one right and one far too low time."""
    lines = _block(1_010_000, 1_010) + _block(1_050_000, 1_050) + _block(1_100_000, 1_100)
    assert _run(monkeypatch, lines) == pytest.approx([0.0101, 0.0105, 0.011])


def test_progress_never_goes_back(monkeypatch):
    """A block that is behind the one before is dropped."""
    assert _run(monkeypatch, _block(30_000_000) + _block(20_000_000) + _block(40_000_000)) == pytest.approx([0.3, 0.4])


def test_unknown_time_is_ignored(monkeypatch):
    """FFmpeg writes N/A until it has a position; that is no progress."""
    lines = ["out_time_us=N/A\n", "out_time_ms=N/A\n", "progress=continue\n"] + _block(5_000_000)
    assert _run(monkeypatch, lines) == pytest.approx([0.05])


def test_second_pass_maps_into_its_half(monkeypatch):
    """The progress of a pass is placed between where the pass starts and where it ends."""
    assert _run(monkeypatch, _block(50_000_000), 0.5, 1.0) == pytest.approx([0.75])


def test_showinfo_frame_times_still_report(monkeypatch):
    """The first pass of VP9 and AV1 has no time from FFmpeg, its frames tell the position."""
    line = "[Parsed_showinfo_3 @ 0x1] n:   5 pts:    250 pts_time:25      duration:1\n"
    assert _run(monkeypatch, [line], 0.0, 0.5) == pytest.approx([0.125])
