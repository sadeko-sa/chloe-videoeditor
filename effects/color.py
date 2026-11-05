"""Color and tone adjustments. All of them are neutral at their default and then add no filter."""

from engine.effect import Effect, Param, num, quote, register

GAMMA = 2.2
"""Gamma assumed for the exposure maths (a stop in linear light = 2^(1/GAMMA) in the picture values)."""


def _curve(points):
    """@return a curves filter that bends the brightness through the (input, output) points (0..1)"""
    text = " ".join("%s/%s" % (num(x), num(y)) for x, y in points)
    return "curves=master=%s:interp=pchip" % quote(text)


def _percent(key="amount", label="Amount"):
    return Param(key, label, "float", 0, -100, 100, 1)


@register
class Brightness(Effect):
    id = "brightness"
    name = "Brightness"
    params = (_percent(),)

    def filters(self, v, ctx):
        return ["eq=brightness=%s" % num(v["amount"] / 100 * 0.5)] if v["amount"] else []


@register
class Contrast(Effect):
    id = "contrast"
    name = "Contrast"
    params = (_percent(),)

    def filters(self, v, ctx):
        return ["eq=contrast=%s" % num(1 + v["amount"] / 100)] if v["amount"] else []


@register
class Exposure(Effect):
    id = "exposure"
    name = "Exposure"
    params = (Param("stops", "Stops (EV)", "float", 0, -3, 3, 0.1),)

    def filters(self, v, ctx):
        if not v["stops"]:
            return []
        gain = num(2 ** (v["stops"] / GAMMA), 5)
        expr = quote("clip(val*%s,minval,maxval)" % gain)
        return ["lutrgb=r=%s:g=%s:b=%s" % (expr, expr, expr)]


@register
class Shadows(Effect):
    id = "shadows"
    name = "Shadows"
    params = (_percent(),)

    def filters(self, v, ctx):
        if not v["amount"]:
            return []
        return [_curve([(0, 0), (0.25, 0.25 + v["amount"] / 100 * 0.15), (0.5, 0.5), (1, 1)])]


@register
class Highlights(Effect):
    id = "highlights"
    name = "Highlights"
    params = (_percent(),)

    def filters(self, v, ctx):
        if not v["amount"]:
            return []
        return [_curve([(0, 0), (0.5, 0.5), (0.75, 0.75 + v["amount"] / 100 * 0.15), (1, 1)])]


@register
class Saturation(Effect):
    id = "saturation"
    name = "Saturation"
    params = (_percent(),)

    def filters(self, v, ctx):
        return ["eq=saturation=%s" % num(1 + v["amount"] / 100)] if v["amount"] else []


@register
class Vibrance(Effect):
    id = "vibrance"
    name = "Vibrance"
    params = (_percent(),)

    def filters(self, v, ctx):
        return ["vibrance=intensity=%s" % num(v["amount"] / 100 * 2)] if v["amount"] else []


@register
class WhiteBalance(Effect):
    id = "white_balance"
    name = "White balance"
    params = (_percent("temperature", "Temperature (cool - warm)"),
              _percent("tint", "Tint (green - magenta)"))

    def filters(self, v, ctx):
        if not v["temperature"] and not v["tint"]:
            return []
        warm, tint = v["temperature"] / 100 * 0.2, v["tint"] / 100 * 0.2
        return ["colorchannelmixer=rr=%s:gg=%s:bb=%s" % (num(1 + warm), num(1 - tint), num(1 - warm))]


@register
class Hue(Effect):
    id = "hue"
    name = "Hue"
    params = (Param("degrees", "Rotate (degrees)", "float", 0, -180, 180, 1),)

    def filters(self, v, ctx):
        return ["hue=h=%s" % num(v["degrees"])] if v["degrees"] else []
