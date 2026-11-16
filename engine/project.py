"""
project.py - The project model: layers with effect chains, saved as JSON. Pure data, no FFmpeg and no Tk.

Layers are stored bottom to top, like the stacking in the picture: a layer is drawn over the ones
before it. A MediaLayer shows a video or an image between `start` and `start + duration` (seconds
of the timeline). An AdjustmentLayer has no picture of its own: its effects change everything
beneath it, within its own time range.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Tuple

from . import effect as fx

FORMAT = "chloe-videoeditor"
FORMAT_VERSION = 1
PROJECT_EXT = ".cvproj"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
"""Media files treated as a still image (a layer of fixed duration) instead of a video."""

IMAGE_SECONDS = 5.0
"""Duration of a still image layer when it is the first thing in the project."""

FITS = ("fit", "fill")
"""Media layer placement: "fit" = whole picture inside the frame, "fill" = frame filled, edges cropped."""

MIN_FRAME = 16
"""Smallest side in pixels of a project frame, and of the picture that is left after the project crop."""


class ProjectError(Exception):
    """A project file cannot be read."""


def even(value: float) -> int:
    """@return value as an even whole number of at least 2 (video sizes must be even)"""
    return max(2, int(round(value)) // 2 * 2)


def _even_down(value: Any) -> int:
    """@return value as a whole number of pixels, not below 0, rounded DOWN to an even number (a cut of 3 becomes 2)"""
    try:
        return max(0, int(value)) // 2 * 2
    except (TypeError, ValueError, OverflowError):
        return 0


def _axis_cuts(first: Any, second: Any, size: int) -> Tuple[int, int]:
    """
    The two cuts of one axis (left / right or top / bottom) made usable.

    @param first   cut from the first edge, pixels
    @param second  cut from the opposite edge, pixels
    @param size    side of the project frame on this axis (even)
    @return        both cuts, even and not negative; if together they leave less than MIN_FRAME pixels they are scaled
                   down in the same ratio. Every cut being even and the frame side being even, the side that is left
                   is even too (video sizes must be even) and an offset never falls between two chroma samples.
    """
    first, second = _even_down(first), _even_down(second)
    room = max(size - MIN_FRAME, 0)
    total = first + second
    if total > room:
        first, second = first * room // total, second * room // total
        first, second = first // 2 * 2, second // 2 * 2
    return first, second


def normalize_cuts(cuts: Sequence[Any], width: int, height: int) -> Tuple[int, int, int, int]:
    """
    @param cuts    (left, right, top, bottom) pixels of the project frame; anything that is not four numbers = no cut
    @param width   width of the project frame
    @param height  height of the project frame
    @return        (left, right, top, bottom) as the project keeps them: whole, even, and leaving at least MIN_FRAME
    """
    try:
        left, right, top, bottom = list(cuts)
    except (TypeError, ValueError):
        return 0, 0, 0, 0
    left, right = _axis_cuts(left, right, width)
    top, bottom = _axis_cuts(top, bottom, height)
    return left, right, top, bottom


def _number(value: Any, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return max(lo, min(hi, float(value)))


def _text(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


@dataclass
class MediaInfo:
    """What probing a media file found."""
    path: str
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool
    is_image: bool


@dataclass
class EffectInstance:
    """One use of an effect in a layer: which effect, its values, on / off."""
    effect_id: str
    values: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @classmethod
    def create(cls, effect_id: str) -> "EffectInstance":
        """@return a new instance with the default values of the effect"""
        eff = fx.get(effect_id)
        return cls(effect_id, eff.defaults() if eff else {})

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.effect_id, "enabled": self.enabled, "values": copy.deepcopy(self.values)}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["EffectInstance"]:
        if not isinstance(data, dict):
            return None
        effect_id = data.get("id", data.get("effect"))      # "effect" is the key that older files use
        if not _text(effect_id):
            return None
        eff = fx.get(effect_id)
        values = data.get("values") if isinstance(data.get("values"), dict) else {}
        return cls(effect_id, eff.normalize(values) if eff else dict(values), data.get("enabled") is not False)


@dataclass
class Layer:
    """Common part of all layers."""
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    enabled: bool = True
    opacity: float = 1.0
    start: float = 0.0
    duration: float = 0.0
    effects: List[EffectInstance] = field(default_factory=list)
    kind: ClassVar[str] = ""

    def length(self, project: "Project") -> float:
        """@return seconds this layer covers on the timeline"""
        return self.duration

    def active_at(self, t: float, project: "Project") -> bool:
        """@return True if the layer is on at timeline second t"""
        return self.start <= t < self.start + self.length(project)

    def clone(self) -> "Layer":
        """@return a copy with a new id"""
        other = copy.deepcopy(self)
        other.id = uuid.uuid4().hex[:12]
        return other

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "name": self.name, "enabled": self.enabled,
                "opacity": self.opacity, "start": self.start, "duration": self.duration,
                "effects": [e.to_dict() for e in self.effects]}

    def render_state(self) -> Dict[str, Any]:
        """@return everything that changes the picture (no name, id or on / off), for cache keys"""
        data = self.to_dict()
        for key in ("name", "id", "enabled"):
            del data[key]
        return data

    def _load_common(self, data: Dict[str, Any]) -> None:
        self.id = _text(data.get("id")) or self.id
        self.enabled = data.get("enabled") is not False
        self.opacity = _number(data.get("opacity"), 1.0, 0.0, 1.0)
        self.start = _number(data.get("start"), 0.0, 0.0, 1e6)
        self.duration = _number(data.get("duration"), self.duration, 0.0, 1e6)
        items = data.get("effects") if isinstance(data.get("effects"), list) else []
        self.effects = [e for e in (EffectInstance.from_dict(i) for i in items) if e]


@dataclass
class MediaLayer(Layer):
    """
    A video or image file on the timeline.

    @param path             media file
    @param in_point         second of the file where the layer starts
    @param duration         seconds shown (also the length on the timeline)
    @param source_duration  length of the file (0 for an image)
    @param width            width of the picture of the file as it is shown (turned files already turned), 0 = not known
    @param height           height of the picture of the file as it is shown, 0 = not known. Both only let the
                            compiler cut the project crop out of the file BEFORE it is scaled when the file has the
                            size of the frame; unknown (older project files) is always safe, only a little slower.
    """
    path: str = ""
    in_point: float = 0.0
    source_duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    is_image: bool = False
    fit: str = "fit"
    volume: float = 1.0
    audio_denoise: float = 0.0
    kind: ClassVar[str] = "media"

    def is_missing(self) -> bool:
        """@return True if the media file does not exist (moved, renamed, deleted, drive not connected)"""
        return not os.path.isfile(self.path)

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data.update(path=self.path, in_point=self.in_point, source_duration=self.source_duration,
                    has_audio=self.has_audio, is_image=self.is_image, fit=self.fit, volume=self.volume,
                    audio_denoise=self.audio_denoise)
        if self.width > 0 and self.height > 0:                      # only written when known: files stay as they were
            data.update(width=self.width, height=self.height)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaLayer":
        layer = cls(_text(data.get("name"), "Media"))
        layer._load_common(data)
        layer.path = _text(data.get("path"))
        layer.is_image = data.get("is_image") is True
        layer.has_audio = data.get("has_audio") is True and not layer.is_image
        layer.source_duration = _number(data.get("source_duration"), 0.0, 0.0, 1e6)
        layer.width = int(_number(data.get("width"), 0, 0, 16384))
        layer.height = int(_number(data.get("height"), 0, 0, 16384))
        if not (layer.width and layer.height):                      # both or none
            layer.width = layer.height = 0
        layer.in_point = _number(data.get("in_point"), 0.0, 0.0, 1e6)
        layer.fit = data.get("fit") if data.get("fit") in FITS else "fit"
        layer.volume = _number(data.get("volume"), 1.0, 0.0, 2.0)
        layer.audio_denoise = _number(data.get("audio_denoise"), 0.0, 0.0, 1.0)
        if layer.duration <= 0:
            layer.duration = max(layer.source_duration - layer.in_point, 0.1) if not layer.is_image else IMAGE_SECONDS
        return layer


@dataclass
class AdjustmentLayer(Layer):
    """A layer without a picture: its effects change all layers beneath it. duration 0 = to the end."""
    kind: ClassVar[str] = "adjustment"

    def length(self, project: "Project") -> float:
        return self.duration if self.duration > 0 else max(project.duration() - self.start, 0.0)

    def active_at(self, t: float, project: "Project") -> bool:
        return t >= self.start and (self.duration <= 0 or t < self.start + self.duration)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AdjustmentLayer":
        layer = cls(_text(data.get("name"), "Adjustment"))
        layer._load_common(data)
        return layer


@dataclass
class Project:
    """
    The whole edit: frame size, frame rate, the crop and the layers (bottom to top).

    @param width        width of the frame the layers are fitted into (before the crop)
    @param height       height of that frame
    @param crop         (left, right, top, bottom) pixels cut from the edges of that frame, like the crop of HandBrake.
                        What is left is the picture of the project: out_width x out_height. The picture, the preview and
                        the export all have that size, so nothing has to be set by hand after cropping. Use set_crop()
                        and cuts(), which keep the numbers whole, even and inside the frame.
    @param sel_layer    id of the layer the window had selected when the project was saved, None = none saved
    @param sel_effects  indices of the selected effects of that layer (Layer.effects)
    @param sel_active   the active one of the selected effects, None = none saved
    @param sel_time     timeline second of the frame the window showed when the project was saved, 0 = none saved
    @param export       export options last used for this project (ExportOptions.to_dict()), {} = none saved

    The selection and the export options are not part of the edit: to_dict() (the undo steps) leaves them out, so
    they are no change and no undo step. Only to_file_dict() (the project file) holds them; the window fills these
    fields before saving and reads them after loading. cvecli.py uses the export options when no preset or option
    asks for something else.
    """
    width: int = 1280
    height: int = 720
    fps: float = 30.0
    layers: List[Layer] = field(default_factory=list)
    crop: Tuple[int, int, int, int] = (0, 0, 0, 0)
    sel_layer: Optional[str] = field(default=None, compare=False)
    sel_effects: List[int] = field(default_factory=list, compare=False)
    sel_active: Optional[int] = field(default=None, compare=False)
    sel_time: float = field(default=0.0, compare=False)
    export: Dict[str, Any] = field(default_factory=dict, compare=False)

    def cuts(self) -> Tuple[int, int, int, int]:
        """@return (left, right, top, bottom): the crop as it is used (whole, even, leaving at least MIN_FRAME pixels)"""
        return normalize_cuts(self.crop, self.width, self.height)

    def set_crop(self, left: int, right: int, top: int, bottom: int) -> None:
        """Set the crop; the numbers are made usable (see normalize_cuts()), so cuts() may differ a little."""
        self.crop = normalize_cuts((left, right, top, bottom), self.width, self.height)

    @property
    def out_width(self) -> int:
        """@return width of the picture of the project: the frame without the left and right cut"""
        left, right, _top, _bottom = self.cuts()
        return self.width - left - right

    @property
    def out_height(self) -> int:
        """@return height of the picture of the project: the frame without the top and bottom cut"""
        _left, _right, top, bottom = self.cuts()
        return self.height - top - bottom

    def duration(self) -> float:
        """@return the end of the last media layer in seconds (0 if there is no media)"""
        return max([l.start + l.duration for l in self.layers if isinstance(l, MediaLayer)] or [0.0])

    def media_layers(self) -> List[MediaLayer]:
        return [l for l in self.layers if isinstance(l, MediaLayer)]

    def find(self, layer_id: Optional[str]) -> Optional[Layer]:
        """@return the layer with this id, or None"""
        return next((l for l in self.layers if l.id == layer_id), None)

    def add_media(self, info: MediaInfo, index: Optional[int] = None) -> MediaLayer:
        """
        Put a media file on top (or at `index`). The first media file sets the frame size and rate of the project.

        @param info   result of probing the file
        @param index  position in `layers` to insert at (the end of the list = the top); None = on top
        @return       the new layer
        """
        if not self.media_layers():
            self.width, self.height = even(info.width), even(info.height)
            if not info.is_image:
                self.fps = round(min(max(info.fps, 1.0), 120.0), 3)
        if info.is_image:
            length = self.duration() or IMAGE_SECONDS
        else:
            length = info.duration
        layer = MediaLayer(os.path.splitext(os.path.basename(info.path))[0] or "Media",
                           duration=length, path=info.path, source_duration=info.duration,
                           has_audio=info.has_audio, is_image=info.is_image, width=info.width, height=info.height)
        if index is None:
            self.layers.append(layer)
        else:
            self.layers.insert(max(0, min(index, len(self.layers))), layer)
        return layer

    def to_dict(self) -> Dict[str, Any]:
        data = {"format": FORMAT, "version": FORMAT_VERSION, "width": self.width, "height": self.height,
                "fps": self.fps, "layers": [l.to_dict() for l in self.layers]}
        if any(self.cuts()):                                        # only written when there is one: files stay as they were
            data["crop"] = list(self.cuts())
        return data

    def to_file_dict(self) -> Dict[str, Any]:
        """@return to_dict() plus the selection (layer and effects); this is what a project file holds"""
        data = self.to_dict()
        data["selection"] = {"layer": self.sel_layer, "effects": sorted(self.sel_effects), "active": self.sel_active,
                             "time": self.sel_time}
        if self.export:
            data["export"] = dict(self.export)
        return data

    def _load_selection(self, data: Any) -> None:
        """
        Take the selection of a project file. Anything wrong, or a layer that is not in the project, is ignored
        (the window then selects the top layer, like for a file without a selection).
        """
        if not isinstance(data, dict):
            return
        self.sel_time = _number(data.get("time"), 0.0, 0.0, 1e6)      # independent of the layer: kept even if that is gone
        layer = data.get("layer")
        if not isinstance(layer, str) or self.find(layer) is None:
            return
        self.sel_layer = layer
        whole = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0       # an index, not True / -1 / "x"
        effects = data.get("effects")
        self.sel_effects = [i for i in effects if whole(i)] if isinstance(effects, list) else []
        self.sel_active = data.get("active") if whole(data.get("active")) else None

    @classmethod
    def from_dict(cls, data: Any) -> "Project":
        """@raise ProjectError if data is not a project"""
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ProjectError("not a Chloe Videoeditor project")
        project = cls(even(_number(data.get("width"), 1280, 16, 16384)),
                      even(_number(data.get("height"), 720, 16, 16384)),
                      _number(data.get("fps"), 30.0, 1.0, 120.0))
        for item in data.get("layers") if isinstance(data.get("layers"), list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("kind") == "media":
                project.layers.append(MediaLayer.from_dict(item))
            elif item.get("kind") == "adjustment":
                project.layers.append(AdjustmentLayer.from_dict(item))
        project.crop = normalize_cuts(data.get("crop") if isinstance(data.get("crop"), list) else [], project.width,
                                      project.height)
        project._load_selection(data.get("selection"))
        if isinstance(data.get("export"), dict):
            project.export = dict(data["export"])
        return project


def save_project(project: Project, path: str) -> None:
    """Write a project as JSON, with its selection (temporary file first). @raise OSError on failure"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(project.to_file_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_project(path: str) -> Project:
    """@raise ProjectError if the file cannot be read or is not a project"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Project.from_dict(json.load(f))
    except (OSError, ValueError) as exc:
        raise ProjectError(str(exc))


class History:
    """
    Undo / redo by snapshots of the project. Call record() after every change; changes with the
    same merge_key that follow each other quickly (a slider drag) become one undo step.
    """

    LIMIT = 100
    MERGE_SECONDS = 1.0

    def __init__(self, project: Project):
        self.reset(project)

    def reset(self, project: Project) -> None:
        """Forget all steps; `project` is the new starting point."""
        self._undo: List[Dict[str, Any]] = []
        self._redo: List[Dict[str, Any]] = []
        self.current: Dict[str, Any] = project.to_dict()
        self._key: Any = None
        self._time = 0.0

    def record(self, project: Project, merge_key: Any = None) -> bool:
        """@return True if the project differs from the last recorded state (a step was added or merged)"""
        state = project.to_dict()
        if state == self.current:
            return False
        now = time.monotonic()
        merge = merge_key is not None and merge_key == self._key and now - self._time < self.MERGE_SECONDS
        if not merge:
            self._undo.append(self.current)
            del self._undo[:-self.LIMIT]
        self._redo.clear()
        self.current, self._key, self._time = state, merge_key, now
        return True

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> Optional[Dict[str, Any]]:
        """@return the previous state (project dict), None if there is none"""
        if not self._undo:
            return None
        self._redo.append(self.current)
        self.current, self._key = self._undo.pop(), None
        return self.current

    def redo(self) -> Optional[Dict[str, Any]]:
        """@return the next state (project dict), None if there is none"""
        if not self._redo:
            return None
        self._undo.append(self.current)
        self.current, self._key = self._redo.pop(), None
        return self.current
