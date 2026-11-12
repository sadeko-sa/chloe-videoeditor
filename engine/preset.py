"""
preset.py - Presets of one category in one JSON file (no Tk in here).

preset/<category>.json holds every preset of that category; "effect" is the only category so far:
    {"category": "effect", "preset": [{"name": "Warm look", "effect": [{"id": "...", "enabled": true, "values": {}}]}]}
An exported file has the same layout, so importing is a merge of two such files. The same text is what the
clipboard holds when effects are copied, so a copy can be pasted into a layer, into the preset manager or a chat.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

from . import effect as fx
from .ffmpeg import APP_DIR
from .project import EffectInstance

CATEGORY = "effect"
PRESET_DIR = os.path.join(APP_DIR, "preset")


class PresetError(Exception):
    """A preset file cannot be read or written."""


@dataclass
class EffectPreset:
    """A name and an ordered effect chain (bottom of the effect list first, like Layer.effects)."""
    name: str
    effect: List[EffectInstance] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "effect": [e.to_dict() for e in self.effect]}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["EffectPreset"]:
        if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
            return None
        items = data.get("effect") if isinstance(data.get("effect"), list) else []
        return cls(data["name"].strip(), [i for i in map(EffectInstance.from_dict, items) if i])

    @classmethod
    def from_layer(cls, name: str, layer, indices: Optional[Iterable[int]] = None) -> "EffectPreset":
        """
        @param name     name of the preset
        @param layer    layer whose effects are taken
        @param indices  indices in layer.effects of the effects to take; None = the whole chain
        @return         a preset with copies of these effects, in the order of the chain
        """
        picked = range(len(layer.effects)) if indices is None else sorted(set(indices))
        return cls(name.strip(), [copy.deepcopy(layer.effects[i]) for i in picked])

    def instance(self) -> List[EffectInstance]:
        """@return fresh copies to put into a layer; effects that are not installed are left out"""
        return [copy.deepcopy(e) for e in self.effect if fx.get(e.effect_id)]


def preset_path(category: str = CATEGORY) -> str:
    return os.path.join(PRESET_DIR, category + ".json")


INDENT = "\t"
"""One level of indentation in preset text."""

COMPACT_DEPTH = 4
"""Containers nested this deep are written on one line: file > preset list > preset > effect list > one effect."""


def _dump(value: Any, depth: int = 0) -> str:
    """@return the JSON text of value; the outer levels one item per line, indented with tabs, the inner ones in one line"""
    if depth >= COMPACT_DEPTH or not isinstance(value, (dict, list)) or not value:
        return json.dumps(value, ensure_ascii=False)
    pad = INDENT * (depth + 1)
    if isinstance(value, dict):
        rows = ["%s%s: %s" % (pad, json.dumps(k, ensure_ascii=False), _dump(v, depth + 1)) for k, v in value.items()]
        opening, closing = "{", "}"
    else:
        rows = [pad + _dump(v, depth + 1) for v in value]
        opening, closing = "[", "]"
    return "%s\n%s\n%s%s" % (opening, ",\n".join(rows), INDENT * depth, closing)


def to_text(preset: List[EffectPreset], category: str = CATEGORY) -> str:
    """@return the preset as the text of a preset file (what write_file writes and the clipboard holds)"""
    return _dump({"category": category, "preset": [p.to_dict() for p in preset]}) + "\n"


def from_text(text: str, category: str = CATEGORY) -> List[EffectPreset]:
    """@raise PresetError if the text is no JSON or of another category"""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PresetError(str(exc))
    if not isinstance(data, dict) or data.get("category") != category or not isinstance(data.get("preset"), list):
        raise PresetError("not %s preset data" % category)
    return [p for p in map(EffectPreset.from_dict, data["preset"]) if p]


def chain(preset: Iterable[EffectPreset]) -> List[EffectInstance]:
    """@return fresh copies of the effects of all the preset that are installed, one preset after the other"""
    return [inst for p in preset for inst in p.instance()]


def read_file(path: str, category: str = CATEGORY) -> List[EffectPreset]:
    """@raise PresetError if the file is missing, no JSON or of another category"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return from_text(f.read(), category)
    except (OSError, UnicodeDecodeError) as exc:
        raise PresetError(str(exc))


def write_file(path: str, preset: List[EffectPreset], category: str = CATEGORY) -> None:
    """Write the file (temporary file first). @raise PresetError if it cannot be written"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(to_text(preset, category))
        os.replace(path + ".tmp", path)
    except OSError as exc:
        raise PresetError(str(exc))


def load() -> List[EffectPreset]:
    """@return the saved effect preset; empty if there is no (valid) file yet"""
    try:
        return read_file(preset_path())
    except PresetError:
        return []


def save(preset: List[EffectPreset]) -> None:
    write_file(preset_path(), preset)


def unique_name(name: str, taken) -> str:
    """@return name, or "name 2", "name 3" ... if it is in `taken` already (case does not matter)"""
    used = {n.lower() for n in taken}
    candidate, number = name, 1
    while candidate.lower() in used:
        number += 1
        candidate = "%s %d" % (name, number)
    return candidate
