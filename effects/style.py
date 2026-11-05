"""Style: vignette and flip."""

import math

from engine.effect import Effect, Param, num, register


@register
class Vignette(Effect):
    id = "vignette"
    name = "Vignette"
    category = "style"
    params = (Param("amount", "Amount", "float", 0, 0, 100, 1),)

    def filters(self, v, ctx):
        if v["amount"] <= 0:
            return []
        angle = math.pi / 2 - v["amount"] / 100 * (math.pi / 2 - math.pi / 8)   # smaller angle = darker edges
        return ["vignette=angle=%s:eval=init" % num(angle, 5)]


@register
class Flip(Effect):
    id = "flip"
    name = "Flip"
    category = "style"
    params = (Param("mode", "Direction", "choice", "none", choices=("none", "horizontal", "vertical", "both")),)

    def filters(self, v, ctx):
        return {"none": [], "horizontal": ["hflip"], "vertical": ["vflip"],
                "both": ["hflip", "vflip"]}[v["mode"]]
