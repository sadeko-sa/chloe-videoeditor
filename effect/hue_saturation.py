"""
Hue/Saturation like Photoshop's Image > Adjustments > Hue/Saturation: a master set of sliders and one set for each of
six color ranges (reds, yellows, greens, cyans, blues, magentas). Hue turns the color around the color wheel in degrees,
saturation and lightness are -100..100 with 0 = neutral.
The rule runs in the HSL model, once per color on a lattice (see LatticeEffect), not once per pixel of every frame.
"""

from typing import List, Mapping

from engine.effect import LatticeEffect, Param, RenderContext, num, quote, register

_BANDS = (("red", "Reds", 0), ("yellow", "Yellows", 60), ("green", "Greens", 120), ("cyan", "Cyans", 180),
          ("blue", "Blues", 240), ("magenta", "Magentas", 300))
"""Color ranges in the order of Photoshop's list: key, English name, hue of the center in degrees."""

_CORE = 15
"""Half width of a range in degrees where it has its full effect."""

_FADE = 30
"""
Width in degrees of the fade out on both sides of the core. Two neighbouring ranges fade over the same degrees in
opposite directions, so their weights always add up to 1.
"""

_PARTS = (("hue", "Hue", 180), ("sat", "Saturation", 100), ("lum", "Lightness", 100))
"""The three sliders of a set: key part, English label, largest absolute value."""


def _set(key: str, name: str) -> tuple:
    """
    @param key   key of the set (master or a range key)
    @param name  English name of the set, the group the three sliders are shown in
    @return      the three Param of the set: hue (degrees), saturation and lightness (percent)
    """
    return tuple(Param("%s_%s" % (key, part), text, "float", 0, -limit, limit, 1, group=name)
                 for part, text, limit in _PARTS)


def _wrap(text: str) -> str:
    """
    @param text  geq text of an angle in degrees (it must not be negative)
    @return      geq text of the same angle limited to 0..360
    """
    return "((%s)-360*floor((%s)/360))" % (text, text)


def _channel(c: str) -> str:
    """@return geq text of channel c (r, g or b) as 0..1"""
    return "(%s(X,Y)/255)" % c


def _to_hsl() -> str:
    """
    @return geq statements that turn the pixel into hue (degrees, slot 0), saturation (slot 1) and lightness
            (slot 2); slots 3, 4 and 5 hold the largest and smallest channel and the chroma while they run
    """
    r, g, b = _channel("r"), _channel("g"), _channel("b")
    sector = "if(eq(%s,ld(3)),(%s-%s)/ld(5),if(eq(%s,ld(3)),2+(%s-%s)/ld(5),4+(%s-%s)/ld(5)))" % (r, g, b, g, b, r, r, g)
    return ("st(3,max(%s,max(%s,%s)));st(4,min(%s,min(%s,%s)));st(2,(ld(3)+ld(4))/2);st(5,max(ld(3)-ld(4),0.000001));"
            "st(1,if(eq(ld(3),ld(4)),0,if(lt(ld(2),0.5),ld(5)/(ld(3)+ld(4)),ld(5)/(2-ld(3)-ld(4)))));"
            "st(0,if(eq(ld(3),ld(4)),0,%s));" % (r, g, b, r, g, b, _wrap("60*%s+360" % sector)))


def _adjust(v: Mapping[str, float]) -> str:
    """
    The adjustment itself, on hue / saturation / lightness in slots 0 / 1 / 2. The sliders of the master and of every
    range are added up with the weight of each range at the hue of the pixel (1 in the core, fading to 0 over _FADE
    degrees, so the ranges together never change the total). Hue is added as degrees. Saturation is multiplied by
    1 + amount (-100 gives gray, +100 doubles it, never above full). Lightness moves toward black (negative) or
    white (positive) by that share of the way. A gray pixel has no hue and stays gray. Slots 6, 7 and 8 hold the
    summed hue, saturation and lightness amounts.
    @param v  checked values of all parameters
    @return   geq statements, each ending with ";"
    """
    steps = ["st(6,%s);st(7,%s);st(8,%s);" % (num(v["master_hue"], 4), num(v["master_sat"] / 100, 5),
                                                num(v["master_lum"] / 100, 5))]
    for key, _, center in _BANDS:
        hue, sat, lum = v[key + "_hue"], v[key + "_sat"] / 100, v[key + "_lum"] / 100
        if not (hue or sat or lum):
            continue
        steps.append("st(5,clip((%d-abs(%s-180))/%d,0,1));" % (_CORE + _FADE, _wrap("ld(0)-%d+540" % center), _FADE))
        for slot, amount in ((6, hue), (7, sat), (8, lum)):
            if amount:
                steps.append("st(%d,ld(%d)+ld(5)*%s);" % (slot, slot, num(amount, 5)))
    steps.append("st(0,%s);st(1,clip(ld(1)*(1+clip(ld(7),-1,1)),0,1));st(8,clip(ld(8),-1,1));"
                 "st(2,if(lt(ld(8),0),ld(2)*(1+ld(8)),ld(2)+(1-ld(2))*ld(8)));" % _wrap("ld(0)+ld(6)+720"))
    return "".join(steps)


def _from_hsl(c: str) -> str:
    """
    @param c  channel letter (r, g or b)
    @return   geq statements that prepare the HSL to RGB step for channel c: slot 3 gets the upper and slot 4 the
              lower helper value, slot 5 the position of that channel on the hue circle
    """
    shift = {"r": "st(5,ld(0)/360+1/3);st(5,if(gt(ld(5),1),ld(5)-1,ld(5)));",
             "g": "st(5,ld(0)/360);",
             "b": "st(5,ld(0)/360-1/3);st(5,if(lt(ld(5),0),ld(5)+1,ld(5)));"}[c]
    return "st(3,if(lt(ld(2),0.5),ld(2)*(1+ld(1)),ld(2)+ld(1)-ld(2)*ld(1)));st(4,2*ld(2)-ld(3));" + shift


def _channel_value() -> str:
    """@return geq text of one channel (0..1) from the helper values in slots 3 (upper), 4 (lower) and 5 (position)"""
    t = "ld(5)"
    return ("if(eq(ld(1),0),ld(2),if(lt(%s,1/6),ld(4)+(ld(3)-ld(4))*6*%s,if(lt(%s,0.5),ld(3),"
            "if(lt(%s,2/3),ld(4)+(ld(3)-ld(4))*(2/3-%s)*6,ld(4)))))" % (t, t, t, t, t))


@register
class HueSaturation(LatticeEffect):
    """
    Hue/Saturation with a master set and six color ranges, all sliders neutral at 0. See the module text for the
    rule. The ranges are in the order of Photoshop's list and fade into each other, so setting the same hue on all
    of them turns the whole picture by that angle.
    """

    id = "hue_saturation"
    name = "Hue/Saturation"
    params = _set("master", "Master") + tuple(p for key, name, _ in _BANDS for p in _set(key, name))

    def is_neutral(self, v: Mapping[str, float], ctx: RenderContext) -> bool:
        return not any(v[p.key] for p in self.params)

    def lattice_filters(self, v: Mapping[str, float], ctx: RenderContext) -> List[str]:
        """
        @param v    checked values (<set>_hue, <set>_sat, <set>_lum for the master and every range)
        @param ctx  picture the effect runs on
        @return     gbrp conversion and one geq filter that writes the new color of every lattice color
        """
        shared = _to_hsl() + _adjust(v)
        planes = ":".join("%s=%s" % (c, quote("%s255*clip(%s,0,1)" % (shared + _from_hsl(c), _channel_value())))
                          for c in "rgb")
        return ["format=gbrp", "geq=" + planes + ":interpolation=nearest"]
