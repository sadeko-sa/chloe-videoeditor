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
from typing import Any, ClassVar, Dict, List, Optional

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


class ProjectError(Exception):
    """A project file cannot be read."""


def even(value: float) -> int:
    """@return value as an even whole number of at least 2 (video sizes must be even)"""
    return max(2, int(round(value)) // 2 * 2)


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
        return {"effect": self.effect_id, "enabled": self.enabled, "values": dict(self.values)}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["EffectInstance"]:
        if not isinstance(data, dict) or not _text(data.get("effect")):
            return None
        eff = fx.get(data["effect"])
        values = data.get("values") if isinstance(data.get("values"), dict) else {}
        return cls(data["effect"], eff.normalize(values) if eff else dict(values), data.get("enabled") is not False)


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
    """
    path: str = ""
    in_point: float = 0.0
    source_duration: float = 0.0
    has_audio: bool = False
    is_image: bool = False
    fit: str = "fit"
    volume: float = 1.0
    kind: ClassVar[str] = "media"

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data.update(path=self.path, in_point=self.in_point, source_duration=self.source_duration,
                    has_audio=self.has_audio, is_image=self.is_image, fit=self.fit, volume=self.volume)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MediaLayer":
        layer = cls(_text(data.get("name"), "Media"))
        layer._load_common(data)
        layer.path = _text(data.get("path"))
        layer.is_image = data.get("is_image") is True
        layer.has_audio = data.get("has_audio") is True and not layer.is_image
        layer.source_duration = _number(data.get("source_duration"), 0.0, 0.0, 1e6)
        layer.in_point = _number(data.get("in_point"), 0.0, 0.0, 1e6)
        layer.fit = data.get("fit") if data.get("fit") in FITS else "fit"
        layer.volume = _number(data.get("volume"), 1.0, 0.0, 2.0)
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
    """The whole edit: frame size, frame rate and the layers (bottom to top)."""
    width: int = 1280
    height: int = 720
    fps: float = 30.0
    layers: List[Layer] = field(default_factory=list)

    def duration(self) -> float:
        """@return the end of the last media layer in seconds (0 if there is no media)"""
        return max([l.start + l.duration for l in self.layers if isinstance(l, MediaLayer)] or [0.0])

    def media_layers(self) -> List[MediaLayer]:
        return [l for l in self.layers if isinstance(l, MediaLayer)]

    def find(self, layer_id: Optional[str]) -> Optional[Layer]:
        """@return the layer with this id, or None"""
        return next((l for l in self.layers if l.id == layer_id), None)

    def add_media(self, info: MediaInfo) -> MediaLayer:
        """
        Put a media file on top. The first media file sets the frame size and rate of the project.

        @param info  result of probing the file
        @return      the new layer
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
                           has_audio=info.has_audio, is_image=info.is_image)
        self.layers.append(layer)
        return layer

    def to_dict(self) -> Dict[str, Any]:
        return {"format": FORMAT, "version": FORMAT_VERSION, "width": self.width, "height": self.height,
                "fps": self.fps, "layers": [l.to_dict() for l in self.layers]}

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
        return project


def save_project(project: Project, path: str) -> None:
    """Write a project as JSON (temporary file first). @raise OSError on failure"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(project.to_dict(), f, indent=2, ensure_ascii=False)
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
