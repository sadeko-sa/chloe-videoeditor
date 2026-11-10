"""
Curves like Photoshop's Image > Adjustments > Curves: one tone curve for the whole picture (RGB) and one for each
of the red, green and blue channels. A curve maps the input level of a pixel (x) to its output level (y); the
straight diagonal changes nothing. Done by FFmpeg's curves filter, which is a lookup table and costs no more than a
copy of the picture. The curves are edited in the effect panel (ui/curve_editor.py).
"""

from typing import List, Mapping

from engine import curve
from engine.effect import ChainEffect, Param, RenderContext, num, quote, register

_CHANNELS = (("master", "RGB"), ("red", "Red"), ("green", "Green"), ("blue", "Blue"))
"""Curve parameters: key (also the name of the option of the curves filter), English label."""


def _points(points) -> str:
    """@return the points of a curve as the text of a curves option: 'x/y x/y ...' (inputs and outputs 0..1)"""
    return " ".join("%s/%s" % (num(x, 4), num(y, 4)) for x, y in points)


@register
class Curves(ChainEffect):
    """Curves: one tone curve for all channels together and one for each of red, green and blue."""

    id = "curves"
    name = "Curves"
    params = tuple(Param(key, label, "curve") for key, label in _CHANNELS)

    def filters(self, v: Mapping[str, list], ctx: RenderContext) -> List[str]:
        """
        @param v    checked values (master, red, green, blue: lists of points)
        @param ctx  picture the filters run on
        @return     one curves filter with an option for every curve that is not the diagonal, [] when all are
        """
        options = ["%s=%s" % (key, quote(_points(v[key]))) for key, _ in _CHANNELS if not curve.is_identity(v[key])]
        return ["curves=" + ":".join(options)] if options else []
