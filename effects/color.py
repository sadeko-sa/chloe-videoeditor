"""
Color and tone adjustments. All of them are neutral at their default and then add no filter.

Every effect here uses sliders from -100 to 100 with 0 = neutral, so the same slider value
gives the same picture. Effects that only look at the channel itself use lutrgb (fast, exact); effects that need the other channels of the pixel
(highlights, saturation, vibrance, black level) use geq on RGB planes.
"""

from engine.effect import Effect, Param, num, quote, register

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
class Brightness(Effect):
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
class Contrast(Effect):
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


@register
class Exposure(Effect):
    """Exposure: negative scales by 2^(amount*0.02353); positive lifts the dark parts more."""

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
        if amount < 0:
            return [_lut("val*%s" % num(2 ** (amount * 0.02353), 6))]
        x = _unit()
        return [_lut("maxval*%s*(1+pow(6,-%s)*%s)" % (x, x, num(amount * 0.034, 5)))]


@register
class Shadows(Effect):
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
class Highlights(Effect):
    """
    Highlights. Per pixel: k = amount / 100 * max(r, g, b); out = 1 - (1 - k * c) * (1 - c)
    for every channel c. Black and white stay, bright pixels move the most, hue is kept.
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
        k = "(%s*%s)" % (num(v["amount"] / 100, 5), _MAX_OF)
        return _geq(lambda c: "1-(1-%s*%s)*(1-%s)" % (k, _ch(c), _ch(c)))


@register
class BlackLevel(Effect):
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
class WhiteLevel(Effect):
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
class Saturation(Effect):
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
class Vibrance(Effect):
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


_HSL_BANDS = (("red", "Red", 0.3), ("orange", "Orange", 0.2), ("yellow", "Yellow", 0.2), ("green", "Green", 0.4),
              ("magenta", "Magenta", 0.4), ("purple", "Purple", 0.4), ("blue", "Blue", 0.4), ("aqua", "Aqua", 0.4))
"""HSL bands in processing order: key, English name, degrees of hue shift per slider unit."""

_HSL_RANGES = {"red": (315, 45), "orange": (0, 60), "yellow": (15, 105), "green": (75, 165), "magenta": (255, 345),
               "purple": (240, 300), "blue": (195, 285), "aqua": (135, 225)}
"""Hue range (start, end) in degrees of every band; red wraps around 360."""


def _rgb_to_hsl_statements():
    """
    @return geq statements that turn the pixel into hue (degrees, slot 0), saturation (slot 1) and
            lightness (slot 2) with the HSL model
    """
    d = "max(ld(3)-ld(4),0.000001)"
    r, g, b = _ch("r"), _ch("g"), _ch("b")
    hh = "if(eq(%s,ld(3)),(%s-%s)/%s,if(eq(%s,ld(3)),2+(%s-%s)/%s,4+(%s-%s)/%s))" % (r, g, b, d, g, b, r, d, r, g, d)
    return ("st(3,max(%s,max(%s,%s)));st(4,min(%s,min(%s,%s)));st(2,(ld(3)+ld(4))/2);"
            "st(1,if(eq(ld(3),ld(4)),0,if(lt(ld(2),0.5),(ld(3)-ld(4))/(ld(3)+ld(4)),(ld(3)-ld(4))/(2-ld(3)-ld(4)))));"
            "st(5,%s);st(0,if(eq(ld(3),ld(4)),240,if(lt(ld(5),0),ld(5)*60+360,ld(5)*60)));"
            % (r, g, b, r, g, b, hh))


def _hsl_band_statements(key, hue, sat, lum):
    """
    One band of the HSL adjustment as geq statements working on slots 0 (hue), 1 (saturation)
    and 2 (lightness). Pixels outside the band's hue range are left as they are.

    @param key  band key (red, orange, ...)
    @param hue  hue shift in degrees (already scaled)
    @param sat  saturation factor (already scaled)
    @param lum  lightness factor (already scaled)
    @return     geq statements, each ending with ";"
    """
    lo, hi = _HSL_RANGES[key]
    h = "ld(0)"
    if key == "red":
        cond = "gt(gte(%s,315)+lte(%s,45),0)" % (h, h)
        upper = "lte(%s,45)" % h
        dist = "if(%s,%s/45,1-(360-%s)/45)" % (upper, h, h)
        raw = "if(%s,%s+(45-%s)*ld(5),315+(%s+45)*ld(5))" % (upper, num(hue, 5), num(hue, 5), num(hue, 5))
        weight = "if(%s,1-ld(5),ld(5))" % upper
    else:
        mid = (lo + hi) / 2
        half = (hi - lo) / 2
        center = mid + hue
        upper = "lte(%s,%s)" % (h, num(mid, 4))
        cond = "gte(%s,%d)*lte(%s,%d)" % (h, lo, h, hi)
        dist = "if(%s,(%s-%d)/%s,(%s-%s)/%s)" % (upper, h, lo, num(half, 4), h, num(mid, 4), num(half, 4))
        raw = "if(%s,%d+%s*ld(5),%s+%s*ld(5))" % (upper, lo, num(center - lo, 5), num(center, 5), num(hi - center, 5))
        weight = "if(%s,ld(5),1-ld(5))" % upper
    tone = "if(lt(ld(2),0.5),2*ld(2),2-2*ld(2))"
    return ("st(8,%s);st(5,%s);st(6,%s);st(5,%s);"
            "st(7,clip(ld(2)*(1+%s*%s*ld(5)*ld(1)),0,1));"
            "st(1,if(ld(8),clip(ld(1)*(1+%s*ld(5)),0,1),ld(1)));st(2,if(ld(8),ld(7),ld(2)));"
            "st(0,if(ld(8),if(lt(ld(6),0),ld(6)+360,if(gt(ld(6),360),ld(6)-360,ld(6))),ld(0)));"
            % (cond, dist, raw, weight, tone, num(lum, 6), num(sat, 6)))


def _hsl_back_statements(c):
    """
    @param c  channel letter (r, g or b)
    @return   geq statements that prepare the HSL -> RGB step for channel c: slot 3 and 4 get the upper
              and lower helper value, slot 5 the hue position of that channel
    """
    shift = {"r": "st(5,ld(0)/360+1/3);st(5,if(gt(ld(5),1),ld(5)-1,ld(5)));",
             "g": "st(5,ld(0)/360);",
             "b": "st(5,ld(0)/360-1/3);st(5,if(lt(ld(5),0),ld(5)+1,ld(5)));"}[c]
    return ("st(3,if(lt(ld(2),0.5),ld(2)*(1+ld(1)),ld(2)+ld(1)-ld(2)*ld(1)));st(4,2*ld(2)-ld(3));" + shift)


@register
class HSL(Effect):
    """
    HSL: hue, saturation and brightness for each of eight color bands (red, orange, yellow,
    green, magenta, purple, blue, aqua), all -100..100. A band affects pixels whose hue lies in its
    range and fades out toward the range edges; saturated pixels change most.
    """

    id = "hsl"
    name = "HSL"
    params = tuple(Param("%s_%s" % (key, part), "%s %s" % (label, text), "float", 0, -100, 100, 1)
                   for key, label, _ in _HSL_BANDS
                   for part, text in (("hue", "hue"), ("sat", "saturation"), ("lum", "brightness")))

    def filters(self, v, ctx):
        """
        @param v    checked values (<band>_hue, <band>_sat, <band>_lum for every band)
        @param ctx  picture the filters run on
        @return     gbrp conversion and one geq filter, [] when every slider is 0
        """
        steps = []
        for key, _, degrees in _HSL_BANDS:
            hue, sat, lum = v[key + "_hue"], v[key + "_sat"], v[key + "_lum"]
            if hue or sat or lum:
                steps.append(_hsl_band_statements(key, hue * degrees, sat * 0.01, lum * 0.004))
        if not steps:
            return []
        shared = _rgb_to_hsl_statements() + "".join(steps)

        def rebuild(c):
            """@return geq text of the new value of channel c"""
            return "if(eq(ld(1),0),ld(2),%s)" % _hsl_value()

        return _geq(rebuild, lambda c: shared + _hsl_back_statements(c))


def _hsl_value():
    """@return geq text of one channel from the helper values in slots 3 (upper), 4 (lower) and 5 (hue position)"""
    t = "ld(5)"
    return ("if(lt(%s,1/6),ld(4)+(ld(3)-ld(4))*6*%s,if(lt(%s,0.5),ld(3),"
            "if(lt(%s,2/3),ld(4)+(ld(3)-ld(4))*(2/3-%s)*6,ld(4))))" % (t, t, t, t, t))



@register
class WhiteBalance(Effect):
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
class Hue(Effect):
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
