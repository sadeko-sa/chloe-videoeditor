"""
preset.py - Presets of one category in one JSON file (no Tk in here).

preset/<category>.json holds every preset of that category; "effect" is the only category so far:
    {"category": "effect", "preset": [{"name": "Warm look", "effect": [{"effect": "...", "enabled": true, "values": {}}]}]}
An exported file has the same layout, so importing is a merge of two such files.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

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
    def from_layer(cls, name: str, layer) -> "EffectPreset":
        """@return a preset with a copy of the whole effect chain of the layer"""
        return cls(name.strip(), copy.deepcopy(layer.effects))

    def instance(self) -> List[EffectInstance]:
        """@return fresh copies to put into a layer; effects that are not installed are left out"""
        return [copy.deepcopy(e) for e in self.effect if fx.get(e.effect_id)]


def preset_path(category: str = CATEGORY) -> str:
    return os.path.join(PRESET_DIR, category + ".json")


def read_file(path: str, category: str = CATEGORY) -> List[EffectPreset]:
    """@raise PresetError if the file is missing, no JSON or of another category"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise PresetError(str(exc))
    if not isinstance(data, dict) or data.get("category") != category or not isinstance(data.get("preset"), list):
        raise PresetError("not a %s preset file" % category)
    return [p for p in map(EffectPreset.from_dict, data["preset"]) if p]


def write_file(path: str, preset: List[EffectPreset], category: str = CATEGORY) -> None:
    """Write the file (temporary file first). @raise PresetError if it cannot be written"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"category": category, "preset": [p.to_dict() for p in preset]}, f, indent=2, ensure_ascii=False)
            f.write("\n")
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
