"""
ui/curve_editor.py - The editor of tone curves: a square drawing area that shows the curve of one channel at a time, a
list to choose the channel and a button that resets the shown curve. Click on empty space to add a point, drag a
point to move it, right click a point to remove it (the two ends stay). All changes of the points are made by
engine/curve.py; this module only turns mouse positions into curve inputs and outputs and draws the result.
"""

import tkinter as tk
from tkinter import ttk

from lang import t
from theme import resolve_color

from engine import curve

SIZE = 200
"""Side of the square area in pixels that stands for the input and output range 0..1."""

_PAD = 10
"""Space in pixels around the square, so points on its edge can be grabbed."""

_HIT = 9
"""Largest distance in pixels between a click and a point for the click to take that point."""

_HANDLE = 4
"""Half the side of the square that marks a point."""

_GRID = 4
"""Number of cells of the grid on each side."""


def _mix(widget, first, second, share):
    """
    @param widget  any widget (it knows the colors)
    @param first   color name or #rrggbb
    @param second  color name or #rrggbb
    @param share   how much of `second` there is in the result, 0..1
    @return        the mixed color as #rrggbb; `first` if a color is unknown to Tk
    """
    try:
        a, b = widget.winfo_rgb(first), widget.winfo_rgb(second)
    except tk.TclError:
        return first
    return "#%02x%02x%02x" % tuple(int((x + (y - x) * share) / 257) for x, y in zip(a, b))


class CurveEditor(ttk.Frame):
    """Editor for the curves of one effect; `send(key, points)` is called after every change."""

    def __init__(self, parent, channels, send):
        """
        @param parent    parent widget
        @param channels  one (key, label, points) per curve, in the order of the channel list
        @param send      function(key, points) called after every change; it gets a new list of points each time
        """
        super().__init__(parent)
        self._send = send
        self._keys = [key for key, _, _ in channels]
        self._points = {key: curve.normalize(points) for key, _, points in channels}
        self._active = 0
        self._drag = None
        style = ttk.Style(self)
        back = resolve_color(self, "palette", "field") or style.lookup("TEntry", "fieldbackground") or "#232323"
        front = resolve_color(self, "palette", "fg") or style.lookup("TLabel", "foreground") or "#d0d0d0"
        self._line = front
        self._grid_color = resolve_color(self, "palette", "border") or _mix(self, back, front, 0.2)
        self._diagonal = _mix(self, back, front, 0.45)
        self._accent = resolve_color(self, "palette", "select_bg") or "#4b6eaf"
        top = ttk.Frame(self)
        top.pack(fill="x")
        self._box = ttk.Combobox(top, state="readonly", width=10, values=[label for _, label, _ in channels])
        self._box.current(0)
        self._box.bind("<<ComboboxSelected>>", self._choose)
        if len(channels) > 1:
            self._box.pack(side="left")
        ttk.Button(top, text=t("btn.curve_reset"), command=self._reset).pack(side="right")
        extent = SIZE + 2 * _PAD
        self._canvas = tk.Canvas(self, width=extent, height=extent, highlightthickness=0, background=back)
        self._canvas.pack(pady=4)
        self._readout = ttk.Label(self, text="")
        self._readout.pack(anchor="w")
        self._canvas.bind("<Button-1>", self._press)
        self._canvas.bind("<B1-Motion>", self._dragged)
        self._canvas.bind("<ButtonRelease-1>", self._release)
        self._canvas.bind("<Motion>", self._hover)
        self._canvas.bind("<Leave>", self._leave)
        for sequence in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self._canvas.bind(sequence, self._remove)
        self._draw()

    def _key(self):
        """@return key of the curve that is shown"""
        return self._keys[self._active]

    def _to_canvas(self, point):
        """@return x and y of a curve point on the canvas"""
        return _PAD + point[0] * SIZE, _PAD + (1 - point[1]) * SIZE

    def _from_canvas(self, x, y):
        """@return input and output (0..1) of a position on the canvas"""
        return (x - _PAD) / SIZE, 1 - (y - _PAD) / SIZE

    def _hit(self, event):
        """@return index of the point of the shown curve closest to the mouse within _HIT pixels, None if none"""
        best, found = _HIT, None
        for index, point in enumerate(self._points[self._key()]):
            x, y = self._to_canvas(point)
            distance = max(abs(x - event.x), abs(y - event.y))
            if distance <= best:
                best, found = distance, index
        return found

    def _show_point(self, index):
        """Show the input and output of a point (8 bit levels) under the drawing, nothing for None."""
        if index is None:
            self._readout.configure(text="")
            return
        x, y = self._points[self._key()][index]
        self._readout.configure(text=t("curve.readout", x=round(x * 255), y=round(y * 255)))

    def _change(self, points, index):
        """
        Take a new curve for the shown channel, draw it and send it.
        @param points  the new curve
        @param index   index of the point whose values to show under the drawing, None for no point
        """
        key = self._key()
        self._points[key] = points
        self._draw()
        self._show_point(index)
        self._send(key, [list(point) for point in points])

    def _press(self, event):
        """Take the point under the mouse, or add one there and take it."""
        index = self._hit(event)
        if index is None:
            grown = curve.insert(self._points[self._key()], *self._from_canvas(event.x, event.y))
            if grown is None:
                return
            self._change(*grown)
            index = grown[1]
        self._drag = index
        self._show_point(index)
        self._draw()

    def _dragged(self, event):
        """Move the taken point to the mouse."""
        if self._drag is None:
            return
        x, y = self._from_canvas(event.x, event.y)
        self._change(curve.move(self._points[self._key()], self._drag, x, y), self._drag)

    def _release(self, _event):
        """Let go of the taken point."""
        self._drag = None
        self._draw()

    def _leave(self, _event):
        """Clear the values under the drawing when the mouse leaves it (unless a point is being dragged)."""
        if self._drag is None:
            self._show_point(None)

    def _hover(self, event):
        """Show the values of the point under the mouse when no point is being dragged."""
        if self._drag is None:
            self._show_point(self._hit(event))

    def _remove(self, event):
        """Remove the point under the mouse (not the two ends)."""
        index = self._hit(event)
        if index is None:
            return
        shorter = curve.remove(self._points[self._key()], index)
        if len(shorter) < len(self._points[self._key()]):
            self._drag = None
            self._change(shorter, None)

    def _choose(self, _event):
        """Show the curve picked in the channel list."""
        self._active = max(self._box.current(), 0)
        self._drag = None
        self._show_point(None)
        self._draw()

    def _reset(self):
        """Set the shown curve back to the straight line."""
        if self._points[self._key()] != curve.identity():
            self._drag = None
            self._change(curve.identity(), None)

    def _draw(self):
        """Draw the grid, the diagonal, the shown curve and its points."""
        canvas = self._canvas
        canvas.delete("all")
        for step in range(_GRID + 1):
            offset = _PAD + step * SIZE / _GRID
            canvas.create_line(_PAD, offset, _PAD + SIZE, offset, fill=self._grid_color)
            canvas.create_line(offset, _PAD, offset, _PAD + SIZE, fill=self._grid_color)
        canvas.create_line(_PAD, _PAD + SIZE, _PAD + SIZE, _PAD, fill=self._diagonal, dash=(3, 3))
        points = self._points[self._key()]
        line = []
        for level, output in enumerate(curve.table(points)):
            line.extend(self._to_canvas((level / (curve.LEVELS - 1), output)))
        canvas.create_line(*line, fill=self._line, width=2)
        for index, point in enumerate(points):
            x, y = self._to_canvas(point)
            canvas.create_rectangle(x - _HANDLE, y - _HANDLE, x + _HANDLE, y + _HANDLE, outline=self._line,
                                    fill=self._accent if index == self._drag else self._line)
