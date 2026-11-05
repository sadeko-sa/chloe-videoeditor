"""Detail: blur, sharpen, grain."""

from engine.effect import Effect, Param, num, register


@register
class Blur(Effect):
    id = "blur"
    name = "Blur"
    category = "detail"
    params = (Param("radius", "Radius (px)", "float", 0, 0, 50, 0.5),)

    def filters(self, v, ctx):
        return ["gblur=sigma=%s" % num(ctx.px(v["radius"]), 3)] if v["radius"] > 0 else []


@register
class Sharpen(Effect):
    id = "sharpen"
    name = "Sharpen"
    category = "detail"
    params = (Param("amount", "Amount", "float", 0, 0, 100, 1),)

    def filters(self, v, ctx):
        if v["amount"] <= 0:
            return []
        size = max(3, int(round(5 * ctx.scale)) | 1)                 # odd matrix, smaller in a small preview
        return ["unsharp=lx=%d:ly=%d:la=%s" % (size, size, num(v["amount"] / 100 * 2))]


@register
class Grain(Effect):
    id = "grain"
    name = "Film grain"
    category = "detail"
    params = (Param("amount", "Amount", "float", 0, 0, 100, 1),)

    def filters(self, v, ctx):
        return ["noise=alls=%s:allf=t+u" % num(v["amount"] * 0.6)] if v["amount"] > 0 else []
