"""remaining.py - The time a job still needs, from how fast it went lately (no Tk, no FFmpeg)."""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, Optional, Tuple

WINDOW = 120.0
"""Seconds of work the speed is measured over. Short enough to follow a pass that is faster or slower than the one before."""

MIN_SPAN = 3.0
"""Seconds of work needed before a speed (and so a remaining time) is given."""


class RecentPace:
    """
    The speed of a job over its last WINDOW seconds. The average over the whole job is wrong as soon as the speed
    changes, for example in a two-pass export whose second pass is much slower than the first. A job that stalls
    gets a longer remaining time, as no progress is gained while time goes by. Safe to use from several threads
    (the one that renders adds, another one reads).
        pace = RecentPace()
        pace.add(elapsed, fraction)             # at every progress report
        pace.remaining(elapsed, fraction)       # whenever a text is drawn
    """

    def __init__(self, window: float = WINDOW, min_span: float = MIN_SPAN):
        """
        @param window    seconds of work the speed is measured over
        @param min_span  seconds of work needed before a speed is given
        """
        self._window, self._min_span = window, min_span
        self._samples: Deque[Tuple[float, float]] = deque()      # (elapsed, fraction), oldest first
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Forget everything, for a job that starts again."""
        with self._lock:
            self._samples.clear()

    def add(self, elapsed: float, fraction: float) -> None:
        """
        Remember how far the job was at a time. Samples older than the window are dropped, but one at or before the
        window's start stays, so the speed is measured over the whole window.
        @param elapsed   seconds of work so far (time paused not counted)
        @param fraction  0..1
        """
        with self._lock:
            self._samples.append((elapsed, fraction))
            while len(self._samples) > 1 and self._samples[1][0] <= elapsed - self._window:
                self._samples.popleft()

    def rate(self, elapsed: float, fraction: float) -> Optional[float]:
        """
        @param elapsed   seconds of work so far
        @param fraction  0..1, the latest progress
        @return          progress gained per second over the window up to `elapsed`; None until MIN_SPAN seconds
                         were measured or while no progress was gained
        """
        with self._lock:
            if not self._samples:
                return None
            then, before = self._samples[0]
        if elapsed - then < self._min_span or fraction <= before:
            return None
        return (fraction - before) / (elapsed - then)

    def remaining(self, elapsed: float, fraction: float) -> Optional[float]:
        """
        @param elapsed   seconds of work so far
        @param fraction  0..1, the latest progress
        @return          seconds left at the speed of the window; None where `rate` is None
        """
        rate = self.rate(elapsed, fraction)
        return None if rate is None else (1.0 - fraction) / rate
