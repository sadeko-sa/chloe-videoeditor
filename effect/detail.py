"""Detail: blur, sharpen, clarity, grain."""

from engine.effect import ChainEffect, Effect, GraphBuilder, Param, num, register


@register
class Blur(ChainEffect):
    id = "blur"
    name = "Blur"
    category = "detail"
    params = (Param("radius", "Radius (px)", "float", 0, 0, 50, 0.5),)

    def filters(self, v, ctx):
        return ["gblur=sigma=%s" % num(ctx.px(v["radius"]), 3)] if v["radius"] > 0 else []


@register
class Sharpen(ChainEffect):
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
class Clarity(Effect):
    """
    Clarity: local contrast in the mid tones, like the slider of Camera Raw and Lightroom (their exact
    algorithm is not published; this is the usual approximation). Only the brightness is changed, so
    colors keep their hue and saturation.

    Positive: the "high pass + overlay" method. The picture is blurred with a large radius, the
    difference between picture and blur (the local detail) is laid over the picture with the
    hard light blend, which weakens the effect towards black and white, and the result is mixed
    into the picture by the amount. Negative: the blurred picture is mixed in, which softens the
    detail. Like the original it makes halos around large edges when pushed far.

    The radius is a share of the shorter side of the picture, so the look does not depend on the
    resolution (nor on the size of the preview). All blends use FFmpeg's built in modes; they are
    about ten times faster than a geq expression.
    """

    id = "clarity"
    name = "Clarity"
    category = "detail"
    params = (Param("amount", "Amount", "float", 0, -100, 100, 1),
              Param("radius", "Radius (%)", "float", 3, 0.5, 10, 0.1))

    SOFTEN_MAX = 0.5
    """Share of the blurred picture that is mixed in at amount -100 (1 would blur everything)."""

    _PLANAR = "format=yuv420p|yuv422p|yuv444p"
    """A format with a separate brightness plane; a picture that already has one is not converted."""

    def is_neutral(self, v, ctx):
        return v["amount"] == 0

    def apply(self, g: GraphBuilder, source: str, v, ctx) -> str:
        planar = g.chain([source], [self._PLANAR])
        sigma = max(v["radius"] / 100 * min(ctx.width, ctx.height), 0.5)
        strength = abs(v["amount"]) / 100
        if v["amount"] > 0:
            return self._boost(g, planar, sigma, strength)
        return self._soften(g, planar, sigma, strength * self.SOFTEN_MAX)

    @staticmethod
    def _blur(g: GraphBuilder, source: str, sigma: float) -> str:
        """@return label of the picture with its brightness plane blurred (the colors stay sharp)"""
        return g.chain([source], ["gblur=sigma=%s:planes=1" % num(sigma, 2)])

    @staticmethod
    def _luma_blend(mode: str) -> str:
        """
        @param mode  blend mode of the brightness plane (first input = top layer, second = bottom layer)
        @return      blend filter that only touches the brightness plane: the color planes of the
                     second input are passed on unchanged
        """
        return "blend=c0_mode=%s:c1_opacity=0:c2_opacity=0" % mode

    @staticmethod
    def _luma_mix(share: float) -> str:
        """
        @param share  0..1, how much of the first input is in the result (0 = only the second)
        @return       blend filter that mixes the brightness planes of its two inputs. Only the
                      "normal" mode fades between the two pictures; the opacity of every other mode
                      fades between the first input and the blend result, so it is never used here.
        """
        return "blend=c0_mode=normal:c0_opacity=%s:c1_opacity=0:c2_opacity=0" % num(share)

    def _boost(self, g: GraphBuilder, source: str, sigma: float, strength: float) -> str:
        """Local detail (picture - blur + 128) laid over the picture with hard light, then mixed in."""
        original, blurred, base, plain = g.split(source, 4)
        detail = g.chain([original, self._blur(g, blurred, sigma)], [self._luma_blend("grainextract")])
        full = g.chain([detail, base], [self._luma_blend("hardlight")])
        return g.chain([full, plain], [self._luma_mix(strength)])

    def _soften(self, g: GraphBuilder, source: str, sigma: float, share: float) -> str:
        """The blurred picture mixed over the picture with the weight `share`."""
        original, blurred = g.split(source)
        return g.chain([self._blur(g, blurred, sigma), original], [self._luma_mix(share)])


@register
class Grain(ChainEffect):
    id = "grain"
    name = "Film grain"
    category = "detail"
    params = (Param("amount", "Amount", "float", 0, 0, 100, 1),)

    def filters(self, v, ctx):
        return ["noise=alls=%s:allf=t+u" % num(v["amount"] * 0.6)] if v["amount"] > 0 else []


@register
class Denoise(ChainEffect):
    """
    Video denoise with FFmpeg's hqdn3d: a smoothing that works inside each picture (spatial) and across
    neighbouring pictures (temporal). The temporal part removes flicker noise in flat areas and costs almost
    nothing in speed; on a single still frame only the spatial part acts. Amount 33 is hqdn3d's own default
    (luma 4, chroma 3, temporal 6 and 4.5); 100 is three times stronger and makes faces and textures soft.
    """

    id = "denoise"
    name = "Denoise"
    category = "detail"
    params = (Param("amount", "Amount", "float", 0, 0, 100, 1),)

    def filters(self, v, ctx):
        """
        @param v    checked values (amount)
        @param ctx  picture the filters run on
        @return     one hqdn3d filter, [] when the amount is 0
        """
        if v["amount"] <= 0:
            return []
        luma = v["amount"] * 0.12                                    # spatial strength of the brightness
        chroma = luma * 0.75                                         # color noise is smoothed a little less
        return ["hqdn3d=%s:%s:%s:%s" % (num(luma, 3), num(chroma, 3), num(luma * 1.5, 3), num(chroma * 1.5, 3))]
