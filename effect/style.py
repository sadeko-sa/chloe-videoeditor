"""Style: vignette and flip."""

import math

from engine.effect import Effect, Param, num, quote, register


def _mask(width, height, size, feather, roundness):
    """
    Builds the vignette mask (1 inside the clear ellipse, 0 outside the feather band, a
    cosine ramp between) as geq text that stores dx in slot 0, dy in slot 1 and the mask in slot 2.

    @param width      picture width in pixels
    @param height     picture height in pixels
    @param size       size ratio 0..1 (1 = ellipse touches the picture edges)
    @param feather    feather ratio 0..1
    @param roundness  roundness ratio 0..1 (1 = circle)
    @return           geq statements, each ending with ";"
    """
    cx, cy = width / 2, height / 2
    a = max(0.2 * cx * (1 + 4 * size), 0.001)
    b = max(0.2 * cy * (1 + 4 * size), 0.001)
    diff = abs(a - b) * roundness
    if a > b:
        a -= diff
    elif b > a:
        b -= diff
    band = feather * (max(a, b) / 2) + 3
    ea, eb = a + band, b + band
    k = "pow(ld(1)/(ld(0)+0.000001),2)"
    outer = "sqrt((1+%s)/(%s/%s+1/%s))" % (k, k, num(eb * eb, 4), num(ea * ea, 4))
    ramp = "0.5*(1+cos(%s*(%s-(%s-sqrt(pow(ld(0),2)+pow(ld(1),2))))))" % (num(math.pi / band, 8), num(band, 4), outer)
    inner = "pow(ld(0)/%s,2)+pow(ld(1)/%s,2)-1" % (num(a, 4), num(b, 4))
    rim = "pow(ld(0)/%s,2)+pow(ld(1)/%s,2)-1" % (num(ea, 4), num(eb, 4))
    return ("st(0,abs(X+0.5-%s));st(1,abs(Y+0.5-%s));st(2,if(lte(%s,0),1,if(gte(%s,0),0,%s)));"
            % (num(cx, 3), num(cy, 3), inner, rim, ramp))


@register
class Vignette(Effect):
    """
    Vignette. The picture inside the ellipse is scaled by the exposure factor, the outside
    by 1 + amount (negative = darker edges), with a feathered cosine transition; in the transition
    the highlights slider pulls the tones toward white or black.
    """

    id = "vignette"
    name = "Vignette"
    category = "style"
    params = (Param("amount", "Amount", "float", 0, -100, 100, 1),
              Param("feather", "Feather", "float", 50, 0, 100, 1),
              Param("highlights", "Highlights", "float", 50, 0, 100, 1),
              Param("size", "Size", "float", 100, 0, 100, 1),
              Param("roundness", "Roundness", "float", 0, 0, 100, 1),
              Param("exposure", "Exposure", "float", 0, -100, 100, 1))

    def filters(self, v, ctx):
        """
        @param v    checked values (amount, feather, highlights, size, roundness, exposure)
        @param ctx  picture the filters run on (its size places the ellipse)
        @return     gbrp conversion and one geq filter, [] when amount and exposure are 0
        """
        if not v["amount"] and not v["exposure"]:
            return []
        amount = v["amount"] / 100
        gain = 2 ** (v["exposure"] / 100)
        strength = (2 * v["highlights"] / 100 - 1) * abs(amount)
        prefix = _mask(ctx.width, ctx.height, v["size"] / 100, v["feather"] / 100, v["roundness"] / 100)
        prefix += "st(3,ld(2)*%s+%s*(1-ld(2)));" % (num(gain, 6), num(1 + amount, 6))
        mean = "((r(X,Y)+g(X,Y)+b(X,Y))/765*ld(3))"
        lift = "pow(1-ld(2),2)*%s*(1-%s)" % (num(strength, 6), mean)
        planes = []
        for c in "rgb":
            val = "(%s(X,Y)/255*ld(3))" % c
            planes.append("%s=%s" % (c, quote("%s255*clip(1-(1-%s*%s)*(1-%s),0,1)" % (prefix, lift, val, val))))
        return ["format=gbrp", "geq=" + ":".join(planes) + ":interpolation=nearest"]


@register
class Flip(Effect):
    """Mirrors the picture horizontally, vertically or both."""

    id = "flip"
    name = "Flip"
    category = "style"
    params = (Param("mode", "Direction", "choice", "none", choices=("none", "horizontal", "vertical", "both")),)

    def filters(self, v, ctx):
        """
        @param v    checked values (mode)
        @param ctx  picture the filters run on
        @return     the flip filters of the chosen direction, [] for none
        """
        return {"none": [], "horizontal": ["hflip"], "vertical": ["vflip"],
                "both": ["hflip", "vflip"]}[v["mode"]]
