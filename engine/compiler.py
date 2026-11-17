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

The project crop (Project.crop) is cut off at the START of every media layer, before its effects, so nothing
downstream (effects, blends, the encoder) ever handles a pixel that is thrown away. The black frame, the effects and
the result have the size of what is left (Project.out_width x out_height): the frame follows the crop, like in HandBrake.
FFmpeg's crop filter only moves the data pointers of a frame, it copies nothing. A file that has the size of the project
frame is cut before it is scaled at all (see _cut_first()); any other file is fitted to the frame and cut straight
after (see _fit_then_cut()). Without a crop the graph is exactly the one of a project without the feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import effect as fx
from .effect import GraphBuilder, RenderContext, num, quote
from .project import AdjustmentLayer, Layer, MediaLayer, Project, even


class CompileError(Exception):
    """The project cannot be turned into a graph (an effect failed, nothing to render)."""


@dataclass
class Job:
    """
    @param inputs    FFmpeg arguments of each input file, in input order (["-ss", "3", "-i", "a.mp4"], ...)
    @param graph     the -filter_complex text (picture and sound chains together)
    @param graph_video  only the picture chains of `graph`; use it when the sound is not mapped into the output
                     (audio copy, no audio, first pass) because FFmpeg stops with "Filter ... has an unconnected output"
                     if a filter output of the graph is not used
    @param graph_audio  only the sound chains of `graph` ("" without sound); add it to `graph_video` when `audio` is mapped
    @param video     label of the finished picture
    @param audio     label of the mixed audio, None if there is none
    @param audio_source  input arguments (["-ss", .., "-t", .., "-i", file]) of the audio track that "audio copy" writes
                     as it is: the FIRST enabled clip with sound. Volume, denoise, start offset and other clips are
                     ignored on purpose (copy means the source audio untouched); None only if no clip has sound
    @param prefix    label of the picture after the first `tap` layers (frame jobs only), else None
    @param duration  length of the result in seconds
    """
    inputs: List[List[str]] = field(default_factory=list)
    graph: str = ""
    graph_video: str = ""
    graph_audio: str = ""
    video: str = ""
    audio: Optional[str] = None
    audio_source: Optional[List[str]] = None
    prefix: Optional[str] = None
    duration: float = 0.0
    tags: str = ""          # colour tags of the finished picture, "colorspace=..:color_primaries=..:color_trc=..:range=tv"

    def input_args(self) -> List[str]:
        """@return the arguments of all inputs in one list"""
        return [arg for item in self.inputs for arg in item]


class _Graph(GraphBuilder):
    """Collects the chains of a filter graph and makes up the labels between them."""

    def __init__(self, prefix: str = "n"):
        """@param prefix  start of the labels this graph makes up (two graphs that are joined need different ones)"""
        self.chains: List[str] = []
        self._count = 0
        self._prefix = prefix

    def chain(self, sources: Sequence[str], filters: Sequence[str]) -> str:
        """
        Add one chain.

        @param sources  labels (or stream names like "0:v") the chain reads
        @param filters  filters in order; [] is a pass through
        @return         the label of its output
        """
        self._count += 1
        out = "%s%d" % (self._prefix, self._count)
        self.chains.append("".join("[%s]" % s for s in sources) + ",".join(filters or ["null"]) + "[%s]" % out)
        return out

    def split(self, source: str, count: int = 2) -> List[str]:
        """@return `count` labels that all carry the picture of `source`"""
        self._count += 1
        labels = ["%s%d%s" % (self._prefix, self._count, chr(ord("a") + i)) for i in range(count)]
        self.chains.append("[%s]split=%d%s" % (source, count, "".join("[%s]" % x for x in labels)))
        return labels

    def text(self) -> str:
        return ";".join(self.chains)


def fps_text(fps: float) -> str:
    """@return the frame rate as an exact fraction for FFmpeg (29.97 -> 30000/1001)"""
    frac = Fraction(fps).limit_denominator(1001)
    return str(frac.numerator) if frac.denominator == 1 else "%d/%d" % (frac.numerator, frac.denominator)


def color_tags(height: int) -> str:
    """
    @return setparams options that label the finished picture with its matrix, primaries and transfer.
            Without them FFmpeg 7.1+ writes only the matrix and players guess the rest (brighter/darker than a browser).
    """
    name = "bt709" if color_matrix(height) == "bt709" else "smpte170m"
    return "colorspace=%s:color_primaries=%s:color_trc=%s:range=tv" % (name, name, name)


def color_matrix(height: int) -> str:
    """@return the YUV matrix players assume for a picture of this height"""
    return "bt709" if height >= 720 else "bt601"


_Step = Tuple[fx.Effect, Dict[str, Any]]
"""An effect of a layer that changes the picture, with its checked values."""


def _steps(layer: Layer, ctx: RenderContext) -> List[_Step]:
    """@return the enabled, installed effects of a layer that change something, in order"""
    out: List[_Step] = []
    for inst in layer.effects:
        eff = fx.get(inst.effect_id)
        if not inst.enabled or eff is None:
            continue
        values = eff.normalize(inst.values)
        try:
            neutral = eff.is_neutral(values, ctx)
        except Exception as exc:                                    # a plugin bug must not crash the program
            raise CompileError("%s: %s" % (eff.id, exc))
        if not neutral:
            out.append((eff, values))
    return out


def _wire(g: _Graph, source: str, steps: Sequence[_Step], ctx: RenderContext) -> str:
    """@return label of the picture after `steps` were added to the graph one after the other"""
    for eff, values in steps:
        try:
            source = eff.apply(g, source, values, ctx)
        except Exception as exc:                                    # a plugin bug must not crash the program
            raise CompileError("%s: %s" % (eff.id, exc))
    return source


def _fit(layer: MediaLayer, width: int, height: int, matrix_height: Optional[int] = None) -> List[str]:
    """
    @param matrix_height  height that decides the YUV matrix (the height of the finished picture); None = `height`
    @return filters that bring a media picture to the frame (converting RGB with the right matrix)
    """
    matrix = "out_color_matrix=" + color_matrix(matrix_height or height)
    if layer.fit == "fill":
        return ["scale=%d:%d:force_original_aspect_ratio=increase:%s" % (width, height, matrix),
                "crop=%d:%d" % (width, height), "setsar=1"]
    return ["scale=%d:%d:force_original_aspect_ratio=decrease:force_divisible_by=2:%s" % (width, height, matrix),
            "setsar=1"]


@dataclass(frozen=True)
class _Frame:
    """
    Where the project crop cuts, in the pixels of the picture that is being made (the export, or a smaller preview).

    @param base_w  width of the frame the media layers are fitted into (the project frame, before the crop)
    @param base_h  height of that frame
    @param left    pixels cut from its left edge
    @param right   pixels cut from its right edge
    @param top     pixels cut from its top edge
    @param bottom  pixels cut from its bottom edge
    @param out_w   width of what is left (the picture that is made)
    @param out_h   height of what is left
    """
    base_w: int
    base_h: int
    left: int
    right: int
    top: int
    bottom: int
    out_w: int
    out_h: int

    @property
    def cut(self) -> bool:
        """@return True if anything is cut"""
        return bool(self.left or self.right or self.top or self.bottom)


def _frame_for(project: Project, width: int, height: int) -> _Frame:
    """
    @param project  the project (its crop and frame)
    @param width    width of the picture to make; the project's out_width in the export, less in a preview
    @param height   height of the picture to make
    @return         the frame plan; in a smaller picture the cuts are scaled the same way, so the preview shows the
                    same cut as the export (the cut sides add up to the picture size again)
    """
    left, right, top, bottom = project.cuts()
    if (width, height) != (project.out_width, project.out_height):
        sx, sy = width / float(project.out_width), height / float(project.out_height)
        left, right, top, bottom = (int(round(left * sx)), int(round(right * sx)),
                                    int(round(top * sy)), int(round(bottom * sy)))
    return _Frame(width + left + right, height + top + bottom, left, right, top, bottom, width, height)


def _crop_to(frame: _Frame, safe: bool) -> str:
    """
    @param frame  the frame plan (must cut something)
    @param safe   True = the picture the filter gets may be a pixel smaller than the frame (rounding when a file of
                  nearly the frame's shape was fitted); the window is then kept inside the picture instead of
                  failing. False = the picture is exactly the frame.
    @return       the crop filter; exact, so no side is rounded to the chroma grid (every cut is even anyway)
    """
    if not safe:
        return "crop=%d:%d:%d:%d:exact=1" % (frame.out_w, frame.out_h, frame.left, frame.top)
    return "crop=w=%s:h=%s:x=%s:y=%s:exact=1" % (
        quote("min(%d,iw-%d)" % (frame.out_w, frame.left)), quote("min(%d,ih-%d)" % (frame.out_h, frame.top)),
        quote("min(%d,iw-ow)" % frame.left), quote("min(%d,ih-oh)" % frame.top))


def _same_shape(layer: MediaLayer, frame: _Frame) -> bool:
    """@return True if the file is known and is fitted to the frame without an empty margin (up to a pixel of rounding)"""
    if not (layer.width and layer.height):
        return False
    return abs(frame.base_w * layer.height - layer.width * frame.base_h) <= max(layer.width, layer.height)


def _cut_first(layer: MediaLayer, frame: _Frame) -> List[str]:
    """
    A file that has exactly the size of the frame (the usual one clip project, in the export): the cut is made on the
    file's own pixels BEFORE any scaling, so the scaler and everything after it only see what is kept.

    @return filters: cut, then the fit to the size that is left (the same size, so only the colour matrix is applied)
    """
    return [_crop_to(frame, False)] + _fit(layer, frame.out_w, frame.out_h)


def _fit_then_cut(layer: MediaLayer, frame: _Frame) -> List[str]:
    """
    Any other file: fitted to the frame like without a crop, then cut. A file that does not fill the frame ("fit" with a
    different shape) is centred with an empty margin that counts as already cut. To place it right whichever edges
    are cut it is first padded to the whole frame with TRANSPARENT margins (so the layers beneath still show), then cut.

    @return filters ending in a picture of exactly the size that is left
    """
    steps = _fit(layer, frame.base_w, frame.base_h, frame.out_h)
    margin = layer.fit != "fill" and not _same_shape(layer, frame)
    if margin:
        steps += ["format=yuva420p",
                  "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black@0" % (frame.base_w, frame.base_h)]
    return steps + [_crop_to(frame, not margin)]


def _opacity(layer: Layer) -> List[str]:
    return ["format=rgba", "colorchannelmixer=aa=%s" % num(layer.opacity)] if layer.opacity < 1 else []


_OVERLAY = "overlay=x=(main_w-overlay_w)/2:y=(main_h-overlay_h)/2:eof_action=pass:format=auto"


def _media_picture(g: _Graph, layer: MediaLayer, index: int, head: Sequence[str], tail: Sequence[str],
                   ctx: RenderContext, frame: Optional[_Frame] = None) -> str:
    """
    One media layer ready to be drawn: input -> head -> project crop and fit to the frame -> effects -> opacity -> tail.

    @param index  number of the layer's input file
    @param head   filters before the fit (timestamps, frame rate)
    @param tail   filters after the opacity (time shift)
    @param frame  where the project crop cuts; None or nothing cut = the fit of a project without a crop
    @return       label of the picture
    """
    if frame is None or not frame.cut:
        shape = _fit(layer, ctx.width, ctx.height)
    elif layer.width == frame.base_w and layer.height == frame.base_h:
        shape = _cut_first(layer, frame)
    else:
        shape = _fit_then_cut(layer, frame)
    picture = g.chain(["%d:v" % index], list(head) + shape)
    picture = _wire(g, picture, _steps(layer, ctx), ctx)
    after = _opacity(layer) + list(tail)
    return g.chain([picture], after) if after else picture


def _adjust(g: _Graph, current: str, layer: Layer, steps: Sequence[_Step], ctx: RenderContext,
            window: Optional[str]) -> str:
    """
    Apply the effects of an adjustment layer to the picture so far.

    @param steps   the effects that change something (see _steps), not empty
    @param window  FFmpeg time expression ("between(t,1,2)") in which the layer works, None = always
    @return        label of the new picture
    """
    if layer.opacity >= 1 and window is None:
        return _wire(g, current, steps, ctx)
    below, above = g.split(current)
    changed = _wire(g, above, steps, ctx)
    # blend: output = first * opacity + second * (1 - opacity), and a disabled blend passes the FIRST input.
    # So the untouched picture goes first and gets the weight 1 - layer opacity.
    options = "blend=all_mode=normal:all_opacity=%s" % num(1 - layer.opacity)
    if window:
        options += ":enable=" + quote(window)
    return g.chain([below, changed], [options])


def _canvas(width: int, height: int, fps: str, seconds: float) -> List[str]:
    return ["color=c=black:s=%dx%d:r=%s:d=%s" % (width, height, fps, num(seconds, 6)), "format=yuv420p10le"]


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
    width, height, fps = project.out_width, project.out_height, fps_text(project.fps)     # the picture after the crop
    ctx = RenderContext(width, height, 1.0)
    frame = _frame_for(project, width, height)
    job, g = Job(duration=total), _Graph()
    ag = _Graph("a")                    # the sound chains live in their own graph so they can be left out of a command
    current = g.chain([], _canvas(width, height, fps, total))
    sounds: List[str] = []
    copy_source: Optional[List[str]] = None     # first clip with sound, for "audio copy" (ignores volume/denoise/delay)
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
            shift = ["setpts=PTS+%s/TB" % num(layer.start, 6)] if layer.start > 0 else []
            picture = _media_picture(g, layer, index, ["setpts=PTS-STARTPTS", "fps=" + fps], shift, ctx, frame)
            current = g.chain([current, picture], [_OVERLAY])
            if layer.has_audio and not layer.is_image and copy_source is None:
                copy_source = list(job.inputs[index])
            if layer.has_audio and layer.volume > 0:
                delay = int(round(layer.start * 1000))
                tail = ["adelay=%d|%d" % (delay, delay)] if delay else []
                # audio denoise: afftdn removes steady noise (hiss, fan). The amount sets both the reduction (100 % = 30 dB)
                # and the assumed noise floor (-60 dB at 0 % up to -25 dB at 100 %: a stronger setting assumes a noisier
                # recording); the noise is tracked and its residue removed, so the floor is only the start value
                amount = layer.audio_denoise
                clean = ["afftdn=nr=%s:nf=%s:nt=w:tn=1:tr=1" % (num(max(amount * 30, 0.01), 2), num(-60 + 35 * amount, 1))] if amount > 0 else []
                sounds.append(ag.chain(["%d:a" % index], [
                    "asetpts=PTS-STARTPTS", "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"]
                    + clean + ["volume=%s" % num(layer.volume)] + tail))
        else:
            steps = _steps(layer, ctx)
            if not steps:
                continue
            begin = num(layer.start, 6)
            if layer.duration > 0:
                window = "between(t,%s,%s)" % (begin, num(layer.start + layer.duration, 6))
            else:
                window = "gte(t,%s)" % begin if layer.start > 0 else None
            current = _adjust(g, current, layer, steps, ctx, window)
    job.tags = color_tags(height)
    job.video = g.chain([current], ["format=yuv420p10le", "setparams=" + job.tags])
    if len(sounds) == 1:
        job.audio = sounds[0]
    elif sounds:
        job.audio = ag.chain(sounds, ["amix=inputs=%d:normalize=0:duration=longest" % len(sounds)])
    job.audio_source = copy_source
    job.graph_video, job.graph_audio = g.text(), ag.text()
    job.graph = ";".join(part for part in (job.graph_video, job.graph_audio) if part)
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
    @param size     (width, height) of the picture; smaller than the project's picture (out_width x out_height, the
                    frame without the crop) for a preview
    @param base     PNG of the picture beneath `layers` (a cached preview); None = black frame
    @param tap      if set, also output the picture after the first `tap` layers (as Job.prefix)
    @return         Job with the picture (and the prefix); inputs open only the one frame needed
    """
    width, height = even(size[0]), even(size[1])
    ctx = RenderContext(width, height, width / project.out_width)
    frame = _frame_for(project, width, height)
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
            picture = _media_picture(g, layer, index, ["setpts=PTS-STARTPTS"], [], ctx, frame)
            current = g.chain([current, picture], [_OVERLAY])
        else:
            steps = _steps(layer, ctx)
            if steps:
                current = _adjust(g, current, layer, steps, ctx, None)
        if tap == done:
            current, tapped = g.split(current)
            job.prefix = _to_rgb(g, tapped, height)
    job.video = _to_rgb(g, current, height)
    job.graph = g.text()
    return job
