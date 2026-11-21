"""Tests of the remaining time of a job (no FFmpeg and no real clock needed)."""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import jobs  # noqa: E402
from engine.jobs import ExportJob, JobState  # noqa: E402
from engine.remaining import RecentPace  # noqa: E402


class _Clock:
    """A clock that only moves when the test says so."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        """@return the time of this clock"""
        return self.now


@pytest.fixture
def clock(monkeypatch):
    """@return the clock that the jobs of the test run by"""
    fake = _Clock()
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=fake.monotonic))
    return fake


def _running(clock):
    """@return a job that started now"""
    job = ExportJob(1, None)
    job.state = JobState.RUNNING
    job.started = clock.now
    return job


def _render(job, clock, seconds, speed):
    """
    Renders for some seconds, a progress report every second.
    @param job      the job that renders
    @param clock    the clock the job runs by
    @param seconds  how many seconds
    @param speed    progress gained per second
    """
    for _ in range(seconds):
        clock.now += 1
        job.set_progress(job.progress + speed)


def test_nothing_is_told_before_it_can_be_known(clock):
    """Too little rendered, no progress yet or a job that does not run: no time."""
    job = _running(clock)
    assert job.remaining is None
    _render(job, clock, 2, 0.01)
    assert job.remaining is None
    _render(job, clock, 5, 0.01)
    assert job.remaining == pytest.approx(93.0)
    job.state = JobState.PAUSED
    assert job.remaining is None


def test_second_pass_at_another_speed_is_followed(clock):
    """The first pass takes 100 s, the second is five times slower: 150 s into it the time left is the slow one."""
    job = _running(clock)
    _render(job, clock, 100, 0.005)
    _render(job, clock, 150, 0.001)
    assert job.progress == pytest.approx(0.65)
    assert job.remaining == pytest.approx(350.0)


def test_a_stalled_render_gets_a_longer_time(clock):
    """Time that goes by without progress lowers the speed."""
    job = _running(clock)
    _render(job, clock, 40, 0.01)
    steady = job.remaining
    clock.now += 20
    assert job.remaining > steady


def test_reset_forgets_the_speed(clock):
    """A job that runs again measures from the start."""
    job = _running(clock)
    _render(job, clock, 10, 0.01)
    job.reset_progress()
    assert job.progress == 0.0
    assert job.remaining is None


def test_pace_measures_the_window_only():
    """Work older than the window does not count, so a speed change is followed once the window has passed it."""
    pace = RecentPace(window=10.0, min_span=2.0)
    for second in range(1, 31):
        pace.add(second, second * 0.02 if second <= 20 else 0.4 + (second - 20) * 0.01)
    assert pace.rate(30, 0.5) == pytest.approx(0.01)
    assert pace.remaining(30, 0.5) == pytest.approx(50.0)


def test_pace_is_unknown_until_it_has_data_and_progress():
    """No samples, too short a span and no progress all give None; clear forgets."""
    pace = RecentPace(window=10.0, min_span=2.0)
    assert pace.rate(0, 0.0) is None
    pace.add(0, 0.0)
    pace.add(1, 0.1)
    assert pace.rate(1, 0.1) is None
    assert pace.rate(5, 0.0) is None
    assert pace.rate(5, 0.1) == pytest.approx(0.02)
    pace.clear()
    assert pace.rate(5, 0.1) is None
