"""Detail: blur, sharpen, clarity, grain, denoise."""

import math

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


_DENOISE_METHODS = ("hqdn3d", "fftdnoiz", "nlmeans")
"""Denoise methods, fast to slow: hqdn3d (fast), fftdnoiz (good), nlmeans (best, about 3 times slower)."""

_DENOISE_STRENGTHS = ("off", "ultralight", "light", "medium", "strong")
"""Strength presets, like the ones of HandBrake. "off" leaves the picture alone."""

_DENOISE_LADDER = {
    #               strength:     (spatial, temporal)
    "hqdn3d":   {"ultralight": (1.0, 1.5), "light": (2.0, 3.0), "medium": (4.0, 6.0), "strong": (8.0, 12.0)},
    "fftdnoiz": {"ultralight": (1.5, 1.0), "light": (3.0, 2.0), "medium": (5.0, 4.0), "strong": (8.0, 6.0)},
    "nlmeans":  {"ultralight": (2.0, 1.0), "light": (3.0, 2.0), "medium": (4.0, 4.0), "strong": (6.0, 6.0)},
}
"""
Preset table: method -> strength -> (spatial, temporal). Spatial is the luma strength of hqdn3d, the sigma of
fftdnoiz or the s of nlmeans. Temporal is the luma strength of the temporal hqdn3d stage. hqdn3d's medium is
FFmpeg's own default (4:3:6:4.5). The steps were chosen so that every method gets clearly stronger from step to
step on a noisy test clip, and so that nlmeans (which does almost nothing below s = 2) starts where it begins to act.
"""

_DENOISE_CHROMA = 0.75
"""Color noise is smoothed a little less than brightness noise (hqdn3d chroma strength = luma strength * 0.75)."""


def _hqdn3d(spatial, temporal):
    """
    @param spatial   luma strength inside the picture (0 = none)
    @param temporal  luma strength across pictures (0 = none)
    @return          one hqdn3d filter
    """
    return "hqdn3d=%s:%s:%s:%s" % (num(spatial, 3), num(spatial * _DENOISE_CHROMA, 3),
                                   num(temporal, 3), num(temporal * _DENOISE_CHROMA, 3))


@register
class Denoise(ChainEffect):
    """
    Video denoise with a choice of three FFmpeg filters and four strength presets.

    Method: hqdn3d (fast), fftdnoiz (good, frequency domain) or nlmeans (best, non-local means, about 3 times
    slower). Strength: Ultralight, Light, Medium or Strong, from the preset table above. Spatial (%) and
    Temporal (%) then adjust the preset's two parts from -100 to 100: 0 = the preset as it is, -100 = leave that
    part out, +100 = twice the preset (stored as "spatial_adj" / "temporal_adj").

    Spatial smoothing works inside each picture, temporal smoothing across neighbouring pictures: it removes
    flicker and grain in still areas, does nothing on a moving area, and on a single still frame only the spatial
    part acts. Only hqdn3d has both parts in one filter. fftdnoiz and nlmeans look at one picture at a time,
    so their temporal part is a temporal-only hqdn3d in front of them (temporal first, then spatial). nlmeans
    also smooths the color planes, which its own default leaves out.

    Projects from before the presets (one "amount" slider, hqdn3d only) are converted when they are loaded and
    give the same filter as before.
    """

    id = "denoise"
    name = "Denoise"
    category = "detail"
    params = (Param("method", "Method", "choice", "hqdn3d", choices=_DENOISE_METHODS),
              Param("strength", "Strength", "choice", "off", choices=_DENOISE_STRENGTHS),
              Param("spatial_adj", "Spatial (%)", "float", 0, -100, 100, 1),
              Param("temporal_adj", "Temporal (%)", "float", 0, -100, 100, 1))

    def normalize(self, values):
        """
        @param values  stored values of this effect; the old form has only "amount" (0..100)
        @return        checked values; an old "amount" becomes hqdn3d at the nearest preset with the two
                       percentages set so that the filter stays exactly the same as the old slider made it
        """
        values = dict(values or {})
        old = values.get("amount")
        if ("strength" not in values and isinstance(old, (int, float)) and not isinstance(old, bool)
                and old == old and old > 0):
            luma = min(old, 100) * 0.12                                  # spatial luma strength of the old slider
            ladder = _DENOISE_LADDER["hqdn3d"]
            nearest = min(ladder, key=lambda name: abs(math.log(luma / ladder[name][0])))
            percent = luma / ladder[nearest][0] * 100
            for key, value in (("method", "hqdn3d"), ("strength", nearest), ("spatial", percent), ("temporal", percent)):
                values.setdefault(key, value)
        for key in ("spatial", "temporal"):                              # projects from before: 0..200, 100 = preset
            before = values.get(key)
            if key + "_adj" not in values and isinstance(before, (int, float)) and not isinstance(before, bool):
                values[key + "_adj"] = before - 100
        return super().normalize(values)

    def filters(self, v, ctx):
        """
        @param v    checked values (method, strength, spatial, temporal)
        @param ctx  picture the filters run on
        @return     the filters of the chosen method (temporal hqdn3d first, then the spatial filter),
                    [] when the strength is off or both parts are 0
        """
        step = _DENOISE_LADDER[v["method"]].get(v["strength"])
        if not step:
            return []
        spatial = step[0] * (100 + v["spatial_adj"]) / 100
        temporal = step[1] * (100 + v["temporal_adj"]) / 100
        if v["method"] == "hqdn3d":
            return [_hqdn3d(spatial, temporal)] if spatial > 0 or temporal > 0 else []
        filters = [_hqdn3d(0, temporal)] if temporal > 0 else []
        if spatial > 0:
            if v["method"] == "fftdnoiz":
                filters.append("fftdnoiz=sigma=%s:amount=1" % num(min(spatial, 100), 3))
            else:                                                        # nlmeans: odd patches, color planes too
                filters.append("nlmeans=s=%s:p=7:pc=5:r=9:rc=5" % num(max(1.0, min(spatial, 30)), 3))
        return filters
