"""Crop: cuts away the edges of a picture, in pixels."""

from typing import List, Tuple

from engine.effect import ChainEffect, Param, quote, register
from engine.project import even

MAX_CUT = 95.0
"""Most that may be cut from one axis in total (percent of the frame side), so that a usable picture is always left."""

MAX_PX = 16384
"""Largest cut of one edge in pixels (the largest frame side a project may have, see engine/project.py)."""

CUT_KEYS = ("left_px", "right_px", "top_px", "bottom_px")
"""Keys of the four cuts, in the order left, right, top, bottom (also used by the crop assist)."""


def _limit(first: float, second: float, size: float) -> Tuple[float, float]:
    """
    Keeps the two cuts of one axis (left / right or top / bottom) inside MAX_CUT percent of the side.

    @param first   cut from the first edge
    @param second  cut from the opposite edge, in the same unit
    @param size    side of the frame on this axis, in the same unit
    @return        both cuts; scaled down together (same ratio) if their sum is above MAX_CUT percent of size
    """
    room = size * MAX_CUT / 100.0
    total = first + second
    if total <= room:
        return first, second
    factor = room / total
    return first * factor, second * factor


def _skips(first: int, second: int, frame: int, size: str) -> Tuple[str, str]:
    """
    How much of a picture one axis loses at each edge. The cuts are counted from the edges of the FRAME. A picture
    that does not fill the frame (a clip fitted into it is centred) has an empty margin there, and the margin counts as
    already cut: cutting 100 px from a frame in which the picture starts 60 px in removes 40 px of the picture.

    @param first   cut from the first edge of the frame, pixels of this picture
    @param second  cut from the opposite edge
    @param frame   side of the frame on this axis, pixels of this picture
    @param size    FFmpeg name of the side of the picture itself on this axis ("iw" / "ih", "w" / "h" inside enable)
    @return        FFmpeg expressions for the pixels to remove at the first and at the second edge of the picture
    """
    margin = "(%d-%s)/2" % (frame, size)
    return "max(0,%d-%s)" % (first, margin), "max(0,%d-%s)" % (second, margin)


def _mask(left: int, right: int, top: int, bottom: int, width: int, height: int) -> List[str]:
    """
    Paints the cut edges black and keeps the picture size.

    @param left    pixels to cover on the left of the frame (0 = none)
    @param right   pixels to cover on the right
    @param top     pixels to cover at the top
    @param bottom  pixels to cover at the bottom
    @param width   width of the frame
    @param height  height of the frame
    @return        one drawbox filter per edge that is cut; a box is only drawn if the cut reaches into the picture
    """
    across = _skips(left, right, width, "iw")
    along = _skips(top, bottom, height, "ih")
    ready = _skips(left, right, width, "w") + _skips(top, bottom, height, "h")      # inside enable: w, h
    boxes = []
    if left:
        boxes.append(("0", "0", "ceil(%s)" % across[0], "ih", ready[0]))
    if right:
        boxes.append(("iw-ceil(%s)" % across[1], "0", "ceil(%s)" % across[1], "ih", ready[1]))
    if top:
        boxes.append(("0", "0", "iw", "ceil(%s)" % along[0], ready[2]))
    if bottom:
        boxes.append(("0", "ih-ceil(%s)" % along[1], "iw", "ceil(%s)" % along[1], ready[3]))
    return ["drawbox=x=%s:y=%s:w=%s:h=%s:color=black:t=fill:enable=%s" % (
        quote(x), quote(y), quote(w), quote(h), quote("gt(%s,0)" % used)) for x, y, w, h, used in boxes]


def _cut(left: int, right: int, top: int, bottom: int, width: int, height: int) -> str:
    """
    @param left    pixels to cut on the left of the frame
    @param right   pixels to cut on the right
    @param top     pixels to cut at the top
    @param bottom  pixels to cut at the bottom
    @param width   width of the frame
    @param height  height of the frame
    @return        the crop filter; exact, so no side is rounded to the chroma grid; at least 2 px are left
    """
    skip_left, skip_right = _skips(left, right, width, "iw")
    skip_top, skip_bottom = _skips(top, bottom, height, "ih")
    return "crop=w=%s:h=%s:x=%s:y=%s:exact=1" % (
        quote("max(2,iw-%s-%s)" % (skip_left, skip_right)), quote("max(2,ih-%s-%s)" % (skip_top, skip_bottom)),
        quote("min(%s,iw-ow)" % skip_left), quote("min(%s,ih-oh)" % skip_top))


@register
class Crop(ChainEffect):
    """
    Cuts a number of pixels from each edge, like the crop of HandBrake. The pixels are those of the project frame and
    are counted from its edges. In the preview, which is smaller than the frame, they are scaled, so the result is the same in the preview and in the export. The picture that is left is then
    placed in the frame as the "mode" says. If the two cuts of one axis add up to more than MAX_CUT percent of the frame
    they are reduced together, so something is always left. The crop assist (the button under the values) sets
    the cuts with the mouse.
    """

    id = "crop"
    name = "Crop"
    category = "transform"
    assist = "crop"
    params = (Param("left_px", "Left", "int", 0, 0, MAX_PX, 1, extent="width"),
              Param("right_px", "Right", "int", 0, 0, MAX_PX, 1, extent="width"),
              Param("top_px", "Top", "int", 0, 0, MAX_PX, 1, extent="height"),
              Param("bottom_px", "Bottom", "int", 0, 0, MAX_PX, 1, extent="height"),
              Param("mode", "Mode", "choice", "fit",
                    choices=("fit", "fill", "stretch", "mask")))

    def filters(self, v, ctx):
        """
        @param v    checked values (the four cuts in pixels, mode)
        @param ctx  picture the filters run on (its size is the frame the result is placed in)
        @return     [] when nothing is cut, else the filters of the chosen mode:
                    fit      whole cut picture inside the frame, black bars where the shapes differ
                    fill     cut picture covers the frame; what sticks out is trimmed from the middle
                    stretch  cut picture is stretched to the frame (the shape may be distorted)
                    mask     nothing is moved: the cut edges are painted black
        """
        width, height = even(ctx.width), even(ctx.height)
        left, right = _limit(ctx.px(v["left_px"]), ctx.px(v["right_px"]), width)
        top, bottom = _limit(ctx.px(v["top_px"]), ctx.px(v["bottom_px"]), height)
        left, right, top, bottom = (int(round(x)) for x in (left, right, top, bottom))
        if not (left or right or top or bottom):
            return []
        if v["mode"] == "mask":
            return _mask(left, right, top, bottom, width, height)
        cut = [_cut(left, right, top, bottom, width, height)]
        if v["mode"] == "fit":
            return cut + ["scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos" % (width, height),
                          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (width, height), "setsar=1"]
        if v["mode"] == "fill":
            return cut + ["scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos" % (width, height),
                          "crop=%d:%d" % (width, height), "setsar=1"]
        return cut + ["scale=%d:%d:flags=lanczos" % (width, height), "setsar=1"]

