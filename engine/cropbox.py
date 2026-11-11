"""
cropbox.py - The geometry behind the crop assist (ui/crop_assist.py): a box inside a frame that is moved and resized
by its corners and edges, can be locked to an aspect ratio
and turned between portrait and landscape, like the crop of a phone gallery. All numbers are pixels of the project frame; there is no Tk in here, so it can be tested alone.

    box = CropBox(1920, 1080)
    box.set_ratio(16 / 9.0)               # lock to 16:9 (the largest box of that shape inside the box now, same centre)
    box.begin("se", 1900, 1000)           # take the bottom right corner, the mouse is at 1900, 1000
    box.drag_to(1500, 900)                # the mouse moved: the box follows and keeps its ratio
    left, right, top, bottom = box.cuts() # the pixels cut from each edge of the frame
"""

from __future__ import annotations

from typing import Optional, Tuple

MIN_SIDE = 16
"""Smallest side of the box in pixels (less on a tiny frame: never more than a quarter of it)."""

ZONES = ("move", "n", "s", "e", "w", "nw", "ne", "sw", "se")
"""What a grab can take: the inside of the box, one of its four edges or one of its four corners."""


class CropBox:
    """
    The box (x0, y0, x1, y1) in a frame of width x height pixels. It is always inside the frame and never smaller
    than min_side. Numbers are floats while a corner is dragged; cuts() rounds them.
    """

    def __init__(self, width: int, height: int):
        """@param width, height  size of the frame in pixels; the box starts as big as the frame"""
        self.width, self.height = int(width), int(height)
        self.min_side = float(min(MIN_SIDE, self.width // 4, self.height // 4)) or 1.0
        self.x0, self.y0, self.x1, self.y1 = 0.0, 0.0, float(self.width), float(self.height)
        self.landscape = self.width >= self.height
        """True = the wide form of a locked ratio, False = the tall form (also follows the box while it is free)."""
        self._base: Optional[float] = None
        self._grab: Optional[Tuple[str, Tuple[float, float, float, float], float, float]] = None

    # ---------------------------------------------------------------- reading

    @property
    def w(self) -> float:
        """@return width of the box"""
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        """@return height of the box"""
        return self.y1 - self.y0

    @property
    def locked(self) -> bool:
        """@return True if the box keeps an aspect ratio"""
        return self._base is not None

    def ratio(self) -> Optional[float]:
        """@return width / height the box keeps (in the chosen orientation); None while it is free"""
        if self._base is None:
            return None
        long_form = max(self._base, 1.0 / self._base)
        return long_form if self.landscape else 1.0 / long_form

    def cuts(self) -> Tuple[int, int, int, int]:
        """@return (left, right, top, bottom): whole pixels cut from each edge of the frame"""
        left, top = int(round(self.x0)), int(round(self.y0))
        right, bottom = int(round(self.width - self.x1)), int(round(self.height - self.y1))
        return max(left, 0), max(right, 0), max(top, 0), max(bottom, 0)

    # ---------------------------------------------------------------- setting

    def set_cuts(self, left: float, right: float, top: float, bottom: float) -> None:
        """
        Take the box from the cuts of the effect. An axis whose cuts leave less than min_side is taken as not cut.
        The ratio becomes free.
        """
        self._base = None
        self.x0, self.x1 = float(left), self.width - float(right)
        self.y0, self.y1 = float(top), self.height - float(bottom)
        if self.x0 < 0 or self.x1 > self.width or self.w < self.min_side:
            self.x0, self.x1 = 0.0, float(self.width)
        if self.y0 < 0 or self.y1 > self.height or self.h < self.min_side:
            self.y0, self.y1 = 0.0, float(self.height)
        self._sync()

    def reset(self) -> None:
        """The box covers the whole frame again. The ratio is not touched: free stays free, a locked ratio stays
        locked (in its orientation) and the box becomes the largest of that shape."""
        self.x0, self.y0, self.x1, self.y1 = 0.0, 0.0, float(self.width), float(self.height)
        if self._base is None:
            self._sync()
        else:
            self._fit_ratio(float(self.width), float(self.height))

    def set_ratio(self, base: Optional[float]) -> None:
        """
        @param base  width / height of the shape to keep (either form, 16/9 and 9/16 are the same shape: the
                     orientation is chosen with flip()); None = free (the box stays as it is). The box stays where
                     it is: it becomes the largest of that shape that fits inside the box it is now, around the same
                     centre. A box that already has the shape (within 1.5 px) is only locked, not changed.
        """
        self._base = base if base and base > 0 else None
        if self._base is None:
            return
        if abs(self.w - self.h * self.ratio()) > 1.5:
            self._fit_ratio(self.w, self.h)

    def flip(self) -> None:
        """
        Portrait <-> landscape. A locked box takes the other form of its ratio; a free box is turned by 90 degrees
        around its centre (made smaller if it would not fit the frame, but not thinner than min_side).
        """
        self.landscape = not self.landscape
        if self._base is not None:                                         # the box turned by 90 degrees, kept inside the frame
            shrink = min(1.0, self.width / self.h, self.height / self.w)
            self._fit_ratio(self.h * shrink, self.w * shrink)
            return
        width, height = self.h, self.w
        shrink = min(1.0, self.width / width, self.height / height)
        self._place((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0,
                    max(width * shrink, self.min_side), max(height * shrink, self.min_side))
        self._sync()

    def nudge(self, dx: float, dy: float) -> None:
        """Move the box by dx, dy pixels, as far as the frame allows."""
        self._place((self.x0 + self.x1) / 2.0 + dx, (self.y0 + self.y1) / 2.0 + dy, self.w, self.h)

    # ---------------------------------------------------------------- dragging

    def begin(self, zone: str, x: float, y: float) -> None:
        """
        Take a part of the box.

        @param zone  one of ZONES
        @param x     where the mouse is, in frame pixels
        @param y     same
        """
        if zone not in ZONES:
            raise ValueError("unknown zone: %r" % zone)
        self._grab = (zone, (self.x0, self.y0, self.x1, self.y1), x, y)

    def end(self) -> None:
        """Let go of what begin() took."""
        self._grab = None

    @property
    def dragging(self) -> bool:
        """@return True between begin() and end()"""
        return self._grab is not None

    def drag_to(self, x: float, y: float) -> None:
        """
        The mouse moved while a part is taken: move or resize the box from the way it was when taken. Nothing
        happens when no part is taken.

        @param x  where the mouse is now, in frame pixels (may be outside the frame)
        @param y  same
        """
        if self._grab is None:
            return
        zone, start, gx, gy = self._grab
        dx, dy = x - gx, y - gy
        if zone == "move":
            self._place((start[0] + start[2]) / 2.0 + dx, (start[1] + start[3]) / 2.0 + dy,
                        start[2] - start[0], start[3] - start[1])
        elif self._base is None:
            self._resize_free(zone, start, dx, dy)
        elif len(zone) == 2:
            self._resize_corner(zone, start, dx, dy)
        else:
            self._resize_edge(zone, start, dx, dy)
        self._sync()

    # ---------------------------------------------------------------- inside

    def _sync(self) -> None:
        """A free box is landscape when it is wider than high (a locked one only changes with flip())."""
        if self._base is None:
            self.landscape = self.w >= self.h

    def _place(self, cx: float, cy: float, w: float, h: float) -> None:
        """Put a box of w x h around (cx, cy), moved as far as needed to lie inside the frame."""
        self.x0 = min(max(cx - w / 2.0, 0.0), self.width - w)
        self.y0 = min(max(cy - h / 2.0, 0.0), self.height - h)
        self.x1, self.y1 = self.x0 + w, self.y0 + h

    def _fit_ratio(self, room_w: float, room_h: float) -> None:
        """The largest box of the ratio that fits room_w x room_h (and the frame), around the centre of the box now."""
        ratio = self.ratio()
        fit = min(room_w, room_h * ratio)
        w = self._limit_width(fit, fit, ratio)
        self._place((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0, w, w / ratio)

    def _resize_free(self, zone: str, start, dx: float, dy: float) -> None:
        """Move the grabbed edges (a corner is two) on their own; each stays inside the frame and min_side from the other."""
        x0, y0, x1, y1 = start
        low = self.min_side
        if "w" in zone:
            x0 = min(max(x0 + dx, 0.0), x1 - low)
        if "e" in zone:
            x1 = max(min(x1 + dx, float(self.width)), x0 + low)
        if "n" in zone:
            y0 = min(max(y0 + dy, 0.0), y1 - low)
        if "s" in zone:
            y1 = max(min(y1 + dy, float(self.height)), y0 + low)
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    def _resize_corner(self, zone: str, start, dx: float, dy: float) -> None:
        """
        A corner of a locked box: the opposite corner stays, the box grows or shrinks towards the mouse in the
        direction in which the mouse asks for more, limited by the frame and by min_side.
        """
        ratio = self.ratio()
        west, north = "w" in zone, "n" in zone
        anchor_x, anchor_y = (start[2] if west else start[0]), (start[3] if north else start[1])
        corner_x, corner_y = (start[0] if west else start[2]) + dx, (start[1] if north else start[3]) + dy
        sign_x, sign_y = (-1.0 if west else 1.0), (-1.0 if north else 1.0)
        want_w, want_h = max(0.0, (corner_x - anchor_x) * sign_x), max(0.0, (corner_y - anchor_y) * sign_y)
        room_x = anchor_x if west else self.width - anchor_x
        room_y = anchor_y if north else self.height - anchor_y
        w = self._limit_width(max(want_w, want_h * ratio), min(room_x, room_y * ratio), ratio)
        h = w / ratio
        x_edge, y_edge = anchor_x + sign_x * w, anchor_y + sign_y * h
        self.x0, self.x1 = min(anchor_x, x_edge), max(anchor_x, x_edge)
        self.y0, self.y1 = min(anchor_y, y_edge), max(anchor_y, y_edge)

    def _resize_edge(self, zone: str, start, dx: float, dy: float) -> None:
        """
        An edge of a locked box: the opposite edge stays and the box keeps its centre on the other axis, so the other
        side follows the ratio; the box is moved only as far as needed to stay inside the frame.
        """
        ratio = self.ratio()
        x0, y0, x1, y1 = start
        if zone in ("e", "w"):
            anchor = x0 if zone == "e" else x1
            want = (x1 + dx - x0) if zone == "e" else (x1 - (x0 + dx))
            room = (self.width - anchor) if zone == "e" else anchor
            w = self._limit_width(max(want, 0.0), min(room, self.height * ratio), ratio)
            h = w / ratio
            left = anchor if zone == "e" else anchor - w
            top = min(max((y0 + y1) / 2.0 - h / 2.0, 0.0), self.height - h)
            self.x0, self.y0, self.x1, self.y1 = left, top, left + w, top + h
        else:
            anchor = y0 if zone == "s" else y1
            want = (y1 + dy - y0) if zone == "s" else (y1 - (y0 + dy))
            room = (self.height - anchor) if zone == "s" else anchor
            h = max(self._limit_width(max(want, 0.0) * ratio, min(room * ratio, self.width), ratio) / ratio, 0.0)
            w = h * ratio
            top = anchor if zone == "s" else anchor - h
            left = min(max((x0 + x1) / 2.0 - w / 2.0, 0.0), self.width - w)
            self.x0, self.y0, self.x1, self.y1 = left, top, left + w, top + h

    def _limit_width(self, width: float, largest: float, ratio: float) -> float:
        """@return width limited to `largest`, to the frame and to the least that keeps both sides at min_side"""
        least = self.min_side * max(1.0, ratio)
        return min(max(width, least), max(min(largest, float(self.width), self.height * ratio), least))
