"""
curve.py - Tone curves as data and as numbers. No FFmpeg and no Tk.
A curve is a list of [input, output] points, both 0..1, sorted by input, with at least two points (the ends).
The ends may move: a first point with input 0.2 and output 0 clips everything below 0.2 to black.
FFmpeg's curves filter draws a natural cubic spline through the points and clips it to 0..1; table() computes the
same line (FFmpeg's own table differs by one 8 bit level, and by up to three where the curve bends very sharply), so
the curve editor shows what the preview and the export will do.
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

Point = List[float]
"""One point of a curve: [input, output], both 0..1."""

MIN_POINTS = 2
"""Fewest points of a curve: its two ends."""

MAX_POINTS = 16
"""Most points of a curve (the same limit as Photoshop's curves)."""

MIN_GAP = 0.004
"""
Least distance between the inputs of two neighbouring points: about one 8 bit level.
FFmpeg refuses points that are closer.
"""

LEVELS = 256
"""Number of entries of table(): one per 8 bit input level."""

_SAME = 0.0001
"""Largest distance between input and output of a point that still counts as on the diagonal."""


def identity() -> List[Point]:
    """@return the neutral curve: a straight line from black to white"""
    return [[0.0, 0.0], [1.0, 1.0]]


def _is_number(value: Any) -> bool:
    """@return True for a finite int or float (a bool is not a number here)"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _unit(value: float) -> float:
    """@return value limited to 0..1 and rounded to 4 decimals, so equal curves are saved as equal text"""
    return round(min(1.0, max(0.0, float(value))), 4)


def normalize(value: Any, fallback: Optional[Sequence[Sequence[float]]] = None) -> List[Point]:
    """
    Make a curve out of anything that was read from a project file or a preset.
    @param value     the stored value
    @param fallback  curve to return when `value` has fewer than MIN_POINTS usable points (None = the neutral curve)
    @return          a new list of points: values limited to 0..1, sorted by input, a point closer than MIN_GAP to the
                     one before it dropped, at most MAX_POINTS (the last point is always kept)
    """
    points: List[Point] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2 and _is_number(item[0]) and _is_number(item[1]):
                points.append([_unit(item[0]), _unit(item[1])])
    points.sort(key=lambda point: point[0])
    kept: List[Point] = []
    for point in points:
        if kept and point[0] - kept[-1][0] < MIN_GAP - _SAME / 1000:
            continue
        kept.append(point)
    if len(kept) > MAX_POINTS:
        kept = kept[:MAX_POINTS - 1] + kept[-1:]
    if len(kept) < MIN_POINTS:
        return [list(point) for point in fallback] if fallback is not None else identity()
    return kept


def is_identity(points: Sequence[Sequence[float]]) -> bool:
    """@return True if every point lies on the diagonal, so the curve changes nothing"""
    return all(abs(x - y) <= _SAME for x, y in points)


def limits(points: Sequence[Sequence[float]], index: int) -> Tuple[float, float]:
    """
    @param points  curve
    @param index   index of one of its points
    @return        lowest and highest input the point may be moved to: MIN_GAP away from its neighbours, the first
                   point not below 0 and the last not above 1 (both are multiples of 0.0001, like all inputs)
    """
    low = 0.0 if index == 0 else round(points[index - 1][0] + MIN_GAP, 4)
    high = 1.0 if index == len(points) - 1 else round(points[index + 1][0] - MIN_GAP, 4)
    return low, high


def move(points: Sequence[Sequence[float]], index: int, x: float, y: float) -> List[Point]:
    """
    @param points  curve
    @param index   index of the point to move
    @param x       wanted input; it is kept inside limits()
    @param y       wanted output; it is kept inside 0..1
    @return        a new curve with the point moved
    """
    low, high = limits(points, index)
    moved = [list(point) for point in points]
    moved[index] = [round(min(high, max(low, x)), 4), _unit(y)]
    return moved


def insert(points: Sequence[Sequence[float]], x: float, y: float) -> Optional[Tuple[List[Point], int]]:
    """
    @param points  curve
    @param x       input of the new point
    @param y       output of the new point
    @return        the new curve and the index of the new point; None if the curve already has MAX_POINTS points or
                   another point is closer than MIN_GAP to x
    """
    x = _unit(x)
    if len(points) >= MAX_POINTS or any(abs(point[0] - x) < MIN_GAP - _SAME / 1000 for point in points):
        return None
    grown = [list(point) for point in points]
    index = sum(1 for point in grown if point[0] < x)
    grown.insert(index, [x, _unit(y)])
    return grown, index


def remove(points: Sequence[Sequence[float]], index: int) -> List[Point]:
    """
    @param points  curve
    @param index   index of the point to remove
    @return        a new curve without that point; the two ends cannot be removed, so they come back unchanged
    """
    kept = [list(point) for point in points]
    if 0 < index < len(kept) - 1:
        del kept[index]
    return kept


def _second_derivatives(xs: Sequence[float], ys: Sequence[float]) -> List[float]:
    """
    Solve the tridiagonal system of a natural cubic spline (second derivative 0 at both ends).
    @param xs  inputs of the points, strictly increasing
    @param ys  outputs of the points
    @return    the second derivative of the spline at every point
    """
    count = len(xs)
    second = [0.0] * count
    if count < 3:
        return second
    upper = [0.0] * count
    rhs = [0.0] * count
    for i in range(1, count - 1):
        before, after = xs[i] - xs[i - 1], xs[i + 1] - xs[i]
        diag = 2.0 * (before + after)
        value = 6.0 * ((ys[i + 1] - ys[i]) / after - (ys[i] - ys[i - 1]) / before)
        if i > 1:
            diag -= before * upper[i - 1]
            value -= before * rhs[i - 1]
        upper[i] = after / diag
        rhs[i] = value / diag
    for i in range(count - 2, 0, -1):
        second[i] = rhs[i] - upper[i] * second[i + 1]
    return second


def table(points: Sequence[Sequence[float]]) -> List[float]:
    """
    The curve as a lookup table, like the one FFmpeg builds.
    @param points  curve (see normalize)
    @return        LEVELS outputs 0..1 for the inputs 0, 1/255, ... 1; below the first and above the last point the
                   output stays at that point's output
    """
    pts = normalize(points)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    second = _second_derivatives(xs, ys)
    out: List[float] = []
    seg = 0
    for level in range(LEVELS):
        x = level / (LEVELS - 1)
        if x <= xs[0]:
            y = ys[0]
        elif x >= xs[-1]:
            y = ys[-1]
        else:
            while xs[seg + 1] < x:
                seg += 1
            width = xs[seg + 1] - xs[seg]
            a, b = (xs[seg + 1] - x) / width, (x - xs[seg]) / width
            y = (a * ys[seg] + b * ys[seg + 1]
                 + ((a ** 3 - a) * second[seg] + (b ** 3 - b) * second[seg + 1]) * width * width / 6.0)
        out.append(min(1.0, max(0.0, y)))
    return out
