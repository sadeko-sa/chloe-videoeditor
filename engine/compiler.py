"""
compiler.py - Turns a project into one FFmpeg filter graph. Nothing is processed in Python.

Two graphs come out of the same layer code:
  compile_export()  the whole timeline: media layers are shifted in time, adjustment layers use
                    enable='between(t,..)'; audio is mixed.
  compile_frame()   ONE picture at a timeline second: every layer that is off at that second is left
                    out, media files are opened with a seek and decoded for a single frame, the time
                    ranges are decided here instead of in the graph.

The picture is built bottom to top on a black frame:
  media layer       scale to the frame -> its effects -> opacity -> overlay on the picture so far
  adjustment layer  its effects run on the picture so far (a copy is blended back when the layer has
                    a time range or an opacity below 100%)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Sequence, Tuple

from . import effect as fx
from .effect import RenderContext, num, quote
from .project import AdjustmentLayer, Layer, MediaLayer, Project, even


class CompileError(Exception):
    """The project cannot be turned into a graph (an effect failed, nothing to render)."""


@dataclass
class Job:
    """
    @param inputs    FFmpeg arguments of each input file, in input order (["-ss", "3", "-i", "a.mp4"], ...)
    @param graph     the -filter_complex text
    @param video     label of the finished picture
    @param audio     label of the mixed audio, None if there is none
    @param prefix    label of the picture after the first `tap` layers (frame jobs only), else None
    @param duration  length of the result in seconds
    """
    inputs: List[List[str]] = field(default_factory=list)
    graph: str = ""
    video: str = ""
    audio: Optional[str] = None
    prefix: Optional[str] = None
    duration: float = 0.0

    def input_args(self) -> List[str]:
        """@return the arguments of all inputs in one list"""
        return [arg for item in self.inputs for arg in item]


class _Graph:
    """Collects the chains of a filter graph and makes up the labels between them."""

    def __init__(self):
        self.chains: List[str] = []
        self._count = 0

    def chain(self, sources: Sequence[str], filters: Sequence[str]) -> str:
        """
        Add one chain.

        @param sources  labels (or stream names like "0:v") the chain reads
        @param filters  filters in order; [] is a pass through
        @return         the label of its output
        """
        self._count += 1
        out = "n%d" % self._count
        self.chains.append("".join("[%s]" % s for s in sources) + ",".join(filters or ["null"]) + "[%s]" % out)
        return out

    def split(self, source: str) -> Tuple[str, str]:
        """@return two labels that both carry the picture of `source`"""
        self._count += 1
        a, b = "n%da" % self._count, "n%db" % self._count
        self.chains.append("[%s]split[%s][%s]" % (source, a, b))
        return a, b

    def text(self) -> str:
        return ";".join(self.chains)


def fps_text(fps: float) -> str:
    """@return the frame rate as an exact fraction for FFmpeg (29.97 -> 30000/1001)"""
    frac = Fraction(fps).limit_denominator(1001)
    return str(frac.numerator) if frac.denominator == 1 else "%d/%d" % (frac.numerator, frac.denominator)


def color_matrix(height: int) -> str:
    """@return the YUV matrix players assume for a picture of this height"""
    return "bt709" if height >= 720 else "bt601"


def _effects(layer: Layer, ctx: RenderContext) -> List[str]:
    """@return the filters of the enabled effects of a layer, in order"""
    out: List[str] = []
    for inst in layer.effects:
        eff = fx.get(inst.effect_id)
        if not inst.enabled or eff is None:
            continue
        try:
            out.extend(eff.filters(eff.normalize(inst.values), ctx))
        except Exception as exc:                                    # a plugin bug must not crash the program
            raise CompileError("%s: %s" % (eff.id, exc))
    return out


def _fit(layer: MediaLayer, width: int, height: int) -> List[str]:
    """@return filters that bring a media picture to the frame (converting RGB with the right matrix)"""
    matrix = "out_color_matrix=" + color_matrix(height)
    if layer.fit == "fill":
        return ["scale=%d:%d:force_original_aspect_ratio=increase:%s" % (width, height, matrix),
                "crop=%d:%d" % (width, height), "setsar=1"]
    return ["scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2:%s" % (width, height, matrix),
            "setsar=1"]


def _opacity(layer: Layer) -> List[str]:
    return ["format=rgba", "colorchannelmixer=aa=%s" % num(layer.opacity)] if layer.opacity < 1 else []


_OVERLAY = "overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2:eof_action=pass:format=auto"


def _adjust(g: _Graph, current: str, layer: Layer, filters: List[str], window: Optional[str]) -> str:
    """
    Apply the effects of an adjustment layer to the picture so far.

    @param window  FFmpeg time expression ("between(t,1,2)") in which the layer works, None = always
    @return        label of the new picture
    """
    if layer.opacity >= 1 and window is None:
        return g.chain([current], filters)
    below, above = g.split(current)
    changed = g.chain([above], filters)
    # blend: output = first * opacity + second * (1 - opacity), and a disabled blend passes the FIRST input.
    # So the untouched picture goes first and gets the weight 1 - layer opacity.
    options = "blend=all_mode=normal:all_opacity=%s" % num(1 - layer.opacity)
    if window:
        options += ":enable=" + quote(window)
    return g.chain([below, changed], [options])


def _canvas(width: int, height: int, fps: str, seconds: float) -> List[str]:
    return ["color=c=black:s=%dx%d:r=%s:d=%s" % (width, height, fps, num(seconds, 6)), "format=yuv420p"]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def compile_export(project: Project) -> Job:
    """
    Graph for the whole timeline.

    @return  Job with the picture and, if any layer has sound, the mixed audio
    @raise CompileError  if the project has no media or an effect fails
    """
    total = project.duration()
    if total <= 0:
        raise CompileError("nothing to export")
    width, height, fps = project.width, project.height, fps_text(project.fps)
    ctx = RenderContext(width, height, 1.0)
    job, g = Job(duration=total), _Graph()
    current = g.chain([], _canvas(width, height, fps, total))
    sounds: List[str] = []
    for layer in project.layers:
        if not layer.enabled:
            continue
        if isinstance(layer, MediaLayer):
            index = len(job.inputs)
            if layer.is_image:
                job.inputs.append(["-loop", "1", "-framerate", fps, "-t", num(layer.duration, 6), "-i", layer.path])
            else:
                seek = ["-ss", num(layer.in_point, 6)] if layer.in_point > 0 else []
                job.inputs.append(seek + ["-t", num(layer.duration, 6), "-i", layer.path])
            body = ["setpts=PTS-STARTPTS", "fps=" + fps] + _fit(layer, width, height) + _effects(layer, ctx) + _opacity(layer)
            if layer.start > 0:
                body.append("setpts=PTS+%s/TB" % num(layer.start, 6))
            current = g.chain([current, g.chain(["%d:v" % index], body)], [_OVERLAY])
            if layer.has_audio and layer.volume > 0:
                delay = int(round(layer.start * 1000))
                tail = ["adelay=%d|%d" % (delay, delay)] if delay else []
                sounds.append(g.chain(["%d:a" % index], [
                    "asetpts=PTS-STARTPTS", "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
                    "volume=%s" % num(layer.volume)] + tail))
        else:
            filters = _effects(layer, ctx)
            if not filters:
                continue
            begin = num(layer.start, 6)
            if layer.duration > 0:
                window = "between(t,%s,%s)" % (begin, num(layer.start + layer.duration, 6))
            else:
                window = "gte(t,%s)" % begin if layer.start > 0 else None
            current = _adjust(g, current, layer, filters, window)
    job.video = g.chain([current], ["format=yuv420p"])
    if len(sounds) == 1:
        job.audio = sounds[0]
    elif sounds:
        job.audio = g.chain(sounds, ["amix=inputs=%d:normalize=0:duration=longest" % len(sounds)])
    job.graph = g.text()
    return job


# --------------------------------------------------------------------------- #
# Single frame
# --------------------------------------------------------------------------- #


def _to_rgb(g: _Graph, label: str, height: int) -> str:
    return g.chain([label], ["scale=in_color_matrix=%s" % color_matrix(height), "format=rgb24"])


def compile_frame(project: Project, layers: Sequence[Layer], t: float, size: Tuple[int, int],
                  base: Optional[str] = None, tap: Optional[int] = None) -> Job:
    """
    Graph for ONE picture. The caller decides which layers are on (see PreviewEngine); this only
    builds the picture.

    @param layers   layers to draw, bottom to top (already limited to those that are on at t)
    @param t        timeline second of the picture
    @param size     (width, height) of the picture; smaller than the project for a preview
    @param base     PNG of the picture beneath `layers` (a cached preview); None = black frame
    @param tap      if set, also output the picture after the first `tap` layers (as Job.prefix)
    @return         Job with the picture (and the prefix); inputs open only the one frame needed
    """
    width, height = even(size[0]), even(size[1])
    ctx = RenderContext(width, height, width / project.width)
    fps = fps_text(project.fps)
    job, g = Job(duration=1.0 / project.fps), _Graph()
    if base:
        job.inputs.append(["-i", base])
        current = g.chain(["0:v"], ["setpts=PTS-STARTPTS", "scale=out_color_matrix=%s" % color_matrix(height),
                                    "format=yuv420p"])
    else:
        current = g.chain([], _canvas(width, height, fps, 1))
    for done, layer in enumerate(layers, 1):
        if isinstance(layer, MediaLayer):
            index = len(job.inputs)
            if layer.is_image:
                job.inputs.append(["-i", layer.path])
            else:
                local = layer.in_point + max(t - layer.start, 0.0)
                if layer.source_duration > 0:
                    local = min(local, max(layer.source_duration - 1.0 / project.fps, 0.0))
                job.inputs.append(["-ss", num(local, 6), "-i", layer.path])
            body = ["setpts=PTS-STARTPTS"] + _fit(layer, width, height) + _effects(layer, ctx) + _opacity(layer)
            current = g.chain([current, g.chain(["%d:v" % index], body)], [_OVERLAY])
        else:
            filters = _effects(layer, ctx)
            if filters:
                current = _adjust(g, current, layer, filters, None)
        if tap == done:
            current, tapped = g.split(current)
            job.prefix = _to_rgb(g, tapped, height)
    job.video = _to_rgb(g, current, height)
    job.graph = g.text()
    return job
