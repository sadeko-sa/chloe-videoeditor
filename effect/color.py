"""
Color and tone adjustments. All of them are neutral at their default and then add no filter.

Every effect here uses sliders from -100 to 100 with 0 = neutral, so the same slider value
gives the same picture. Effects that only look at the channel itself use lutrgb (fast, exact); effects that need the other channels of the pixel
(highlights, saturation, vibrance, black level) use geq on RGB planes.
"""

from engine.effect import ChainEffect, Param, num, quote, register

_MAX_OF = "max(r(X,Y),max(g(X,Y),b(X,Y)))/255"
"""geq text: the brightest channel of the pixel, 0..1."""

_MIN_OF = "min(r(X,Y),min(g(X,Y),b(X,Y)))/255"
"""geq text: the darkest channel of the pixel, 0..1."""


def _percent(key="amount", label="Amount"):
    """
    @param key    parameter key in the values dict
    @param label  English slider label
    @return       a -100..100 slider that is neutral at 0
    """
    return Param(key, label, "float", 0, -100, 100, 1)


def _lut(expression):
    """
    @param expression  lutrgb expression of val / maxval, the same for every channel
    @return            a lutrgb filter that applies it to r, g and b
    """
    expr = quote(expression)
    return "lutrgb=r=%s:g=%s:b=%s" % (expr, expr, expr)


def _unit():
    """@return lutrgb text of the channel value as 0..1"""
    return "val/maxval"


def _geq(per_plane, prefix=""):
    """
    @param per_plane  function(channel letter) -> geq expression text giving the new value 0..1
    @param prefix     geq statements run before the value, each ending with ";" (st() stores), as text or
                      as function(channel letter) -> text
    @return           filters: gbrp conversion and one geq that writes the r, g and b planes
    """
    planes = ":".join("%s=%s" % (c, quote("%s255*clip(%s,0,1)" % (prefix(c) if callable(prefix) else prefix, per_plane(c))))
                      for c in "rgb")
    return ["format=gbrp", "geq=" + planes + ":interpolation=nearest"]


def _ch(c):
    """@return geq text of channel c (r, g or b) as 0..1"""
    return "(%s(X,Y)/255)" % c


@register
class Brightness(ChainEffect):
    """Brightness: positive multiplies the picture, negative subtracts a lift."""

    id = "brightness"
    name = "Brightness"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     one lutrgb filter, [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        if amount > 0:
            return [_lut("val*%s" % num(1 + amount / 100, 5))]
        offset = (1 - 1 / (0.99 + amount / 253)) * 0.6
        return [_lut("val+%s*maxval" % num(offset, 5))]


@register
class Contrast(ChainEffect):
    """Contrast: an S curve around 0.5 made of a power law on each half."""

    id = "contrast"
    name = "Contrast"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     one lutrgb filter, [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        power = 1 / (1 + abs(amount) * 0.005) if amount < 0 else 1 + amount * 0.005
        p = num(power, 6)
        x = _unit()
        return [_lut("maxval*if(lt(%s,0.5),pow(2*%s,%s)/2,1-pow(max(2-2*%s,0),%s)/2)" % (x, x, p, x, p))]


_EXPOSURE_STOPS = 0.05
"""Stops of exposure per slider unit: -100..100 gives -5..+5 stops (Photoshop's Exposure dialog allows more)."""


def _srgb_to_linear(x):
    """@param x  lutrgb text of an sRGB value 0..1  @return lutrgb text of the same value in linear light"""
    return "if(lte(%s,0.04045),%s/12.92,pow((%s+0.055)/1.055,2.4))" % (x, x, x)


def _linear_to_srgb(y):
    """@param y  lutrgb text of a linear light value >= 0  @return lutrgb text of the sRGB value (lutrgb clips above 1)"""
    return "if(lte(%s,0.0031308),%s*12.92,1.055*pow(%s,1/2.4)-0.055)" % (y, y, y)


@register
class Exposure(ChainEffect):
    """
    Exposure like Photoshop's Image > Adjustments > Exposure with Offset 0 and Gamma 1: the picture is
    turned to linear light, multiplied by 2^stops and turned back to sRGB. Slider 100 = +5 stops,
    -100 = -5 stops. Every channel is treated alone, so it is one exact lutrgb table.
    """

    id = "exposure"
    name = "Exposure"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     one lutrgb filter, [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        gain = num(2 ** (amount * _EXPOSURE_STOPS), 6)
        return [_lut("maxval*%s" % _linear_to_srgb("(%s*%s)" % (_srgb_to_linear(_unit()), gain)))]


@register
class Shadows(ChainEffect):
    """Shadows: a gamma on every channel (power 1 - 0.004*amount up, 1 - 0.006*amount down)."""

    id = "shadows"
    name = "Shadows"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     one lutrgb filter, [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        power = 1 - amount * (0.004 if amount > 0 else 0.006)
        return [_lut("maxval*pow(%s,%s)" % (_unit(), num(power, 6)))]


@register
class Highlights(ChainEffect):
    """
    Highlights. Per pixel: m = max(r, g, b); the brightest channel follows
    m' = 1 - (1 - a * m * m) * (1 - m) with a = amount / 50 (slider -100..100, so a is -2..2), and all three channels are scaled by the
    same gain m' / m = 1 + a * m * (1 - m). Black and white stay, bright pixels move the most, and hue
    and saturation are kept (a pure gain never changes the ratios between the channels).
    """

    id = "highlights"
    name = "Highlights"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     gbrp conversion and one geq filter, [] when the amount is 0
        """
        if not v["amount"]:
            return []
        gain = "(1+%s*%s*(1-%s))" % (num(v["amount"] / 50, 5), _MAX_OF, _MAX_OF)
        return _geq(lambda c: "%s*%s" % (_ch(c), gain))


@register
class BlackLevel(ChainEffect):
    """Black level: positive lifts the darkest pixels (screen blend), negative crushes them."""

    id = "black_level"
    name = "Black level"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     lutrgb (negative) or gbrp + geq (positive), [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        if amount < 0:
            k = -amount * 0.0015
            return [_lut("val*%s-%s*maxval" % (num(1 + k, 6), num(k, 6)))]
        k = "(%s*(1-%s))" % (num(amount * 0.005, 6), _MAX_OF)
        return _geq(lambda c: "1-(1-%s*%s)*(1-%s)" % (k, _ch(c), _ch(c)))


@register
class WhiteLevel(ChainEffect):
    """White level: scales the picture by 1 + 0.0035*amount (up) or 1 + 0.0025*amount (down)."""

    id = "white_level"
    name = "White level"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     one lutrgb filter, [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        return [_lut("val*%s" % num(1 + amount * (0.0035 if amount > 0 else 0.0025), 6))]


@register
class Saturation(ChainEffect):
    """
    Saturation: pushes every channel away from a luma gray. A boost is weaker on pixels
    that are already saturated; a cut applies to all colored pixels equally. Gray pixels stay.
    """

    id = "saturation"
    name = "Saturation"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     gbrp conversion and one geq filter, [] when the amount is 0
        """
        s = v["amount"] / 100
        if not s:
            return []
        gray = "(0.3*%s+0.59*%s+0.11*%s+0.00196)" % (_ch("r"), _ch("g"), _ch("b"))
        alpha = num(s, 5) if s < 0 else "%s*ld(1)/ld(0)" % num(s, 5)

        def plane(c):
            """@return geq text of the new value of channel c"""
            return "if(gt(ld(0)-ld(1),0.00001),%s+(%s-%s)*%s,%s)" % (_ch(c), _ch(c), gray, alpha, _ch(c))

        return _geq(plane, "st(0,%s);st(1,%s);" % (_MAX_OF, _MIN_OF))


@register
class Vibrance(ChainEffect):
    """
    Vibrance: moves every channel toward (negative) or away from (positive) the brightest
    channel, weighted so dull colors change more than vivid ones.
    """

    id = "vibrance"
    name = "Vibrance"
    params = (_percent(),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     gbrp conversion and one geq filter, [] when the amount is 0
        """
        amount = v["amount"]
        if not amount:
            return []
        vib = -0.01 * amount
        if vib <= 0:
            ratio = "0.75*ld(1)/ld(0)"
        else:
            ratio = "if(gt((ld(0)-ld(1))/ld(0),0.5),0.5,ld(1)/ld(0))"

        def plane(c):
            """@return geq text of the new value of channel c"""
            return ("if(gt(ld(0)-ld(1),0.00001),%s+(ld(0)-%s)*%s*(%s),%s)"
                    % (_ch(c), _ch(c), num(vib, 5), ratio, _ch(c)))

        return _geq(plane, "st(0,%s);st(1,%s);" % (_MAX_OF, _MIN_OF))


@register
class WhiteBalance(ChainEffect):
    """Temperature and tint: a gain per channel (red/blue by temperature, green/magenta by tint)."""

    id = "white_balance"
    name = "White balance"
    params = (_percent("temperature", "Temperature (cool - warm)"),
              _percent("tint", "Tint (green - magenta)"))

    def filters(self, v, ctx):
        """
        @param v    checked values (temperature, tint)
        @param ctx  picture the filters run on
        @return     one colorchannelmixer filter, [] when both are 0
        """
        if not v["temperature"] and not v["tint"]:
            return []
        temp, tint = v["temperature"] * 0.0025, v["tint"] * 0.005
        red, blue, green = 1 + tint * 0.5 + temp, 1 + tint * 0.5 - temp, 1 - tint * 0.4
        return ["colorchannelmixer=rr=%s:gg=%s:bb=%s" % (num(red, 5), num(green, 5), num(blue, 5))]


@register
class Hue(ChainEffect):
    """Hue rotation, done with the FFmpeg hue filter."""

    id = "hue"
    name = "Hue"
    params = (Param("degrees", "Rotate (degrees)", "float", 0, -180, 180, 1),)

    def filters(self, v, ctx):
        """
        @param v    checked values (degrees)
        @param ctx  picture the filters run on
        @return     one hue filter, [] when the angle is 0
        """
        return ["hue=h=%s" % num(v["degrees"])] if v["degrees"] else []
