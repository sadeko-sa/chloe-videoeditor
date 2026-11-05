"""
effect.py - Everything an effect plugin needs, in one place: Param, RenderContext, Effect, the
registry and the plugin loader.

An effect is one small class in a module of the effects/ folder; nothing else has to change:

    from engine.effect import Effect, Param, register, num

    @register
    class Invert(Effect):
        id = "invert"                      # stable id saved in projects: lowercase, digits, _
        name = "Invert"                    # English name; lang files may override it ("effect.invert")
        category = "color"                 # group in the "Add effect" menu ("category.color")
        params = (Param("amount", "Amount", "float", default=100, min=0, max=100),)

        def filters(self, v, ctx):         # v: checked parameter values, ctx: RenderContext
            return ["negate"] if v["amount"] > 0 else []

filters() returns FFmpeg filter strings, in order (the filters of one effect form a chain). An empty
list means "no change", so a neutral setting costs nothing in the export. Values that contain
, : ; [ ] or spaces must be wrapped with quote(). Effects are stateless; the values of every use live
in the project (engine/project.py).

Optional texts in lang/<code>.json (the English name and labels above are the fallback):
    effect.<id>                 name of the effect
    effect.<id>.<param key>     label of a parameter
    choice.<id>.<key>.<value>   text of one choice of a "choice" parameter
    category.<category>         name of the category
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Tuple

KINDS = ("float", "int", "bool", "choice")
"""Parameter kinds: float / int = number between min and max, bool = on / off, choice = one of `choices`."""

ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
"""Allowed effect ids and parameter keys."""


class EffectError(Exception):
    """An effect is defined wrongly (raised while it is registered)."""


def num(value: float, digits: int = 4) -> str:
    """@return value as a short decimal text for a filter option (never an exponent, never a comma)"""
    text = ("%.*f" % (digits, value)).rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def quote(text: str) -> str:
    """@return text in single quotes, so FFmpeg takes , : ; [ ] and spaces literally"""
    if "'" in text or "\\" in text:
        raise ValueError("quote() cannot hold ' or \\")
    return "'" + text + "'"


@dataclass(frozen=True)
class Param:
    """
    One adjustable value of an effect (the user sees it as a slider, a check box or a list).

    @param key      name in the values dict (lowercase, digits, _)
    @param label    English label (fallback of lang entry "effect.<id>.<key>")
    @param kind     one of KINDS
    @param default  neutral / start value; for "choice" one of `choices`
    @param min      lowest value (float / int)
    @param max      highest value (float / int)
    @param step     slider step (float / int)
    @param choices  allowed values of a "choice" (texts)
    """
    key: str
    label: str
    kind: str = "float"
    default: Any = 0
    min: float = 0.0
    max: float = 100.0
    step: float = 1.0
    choices: Tuple[str, ...] = ()

    def __post_init__(self):
        if not ID_RE.match(self.key):
            raise EffectError("bad parameter key: %r" % self.key)
        if self.kind not in KINDS:
            raise EffectError("parameter %s: unknown kind %r" % (self.key, self.kind))
        if self.kind == "choice" and (not self.choices or self.default not in self.choices):
            raise EffectError("parameter %s: needs choices that contain the default" % self.key)
        if self.kind in ("float", "int") and not self.min <= self.default <= self.max:
            raise EffectError("parameter %s: default outside min..max" % self.key)

    def coerce(self, value: Any) -> Any:
        """@return value as this kind of parameter, limited to its range; the default if unusable"""
        if self.kind == "bool":
            return bool(value) if isinstance(value, (bool, int)) else self.default
        if self.kind == "choice":
            return value if value in self.choices else self.default
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return self.default
        value = max(self.min, min(self.max, value))
        return int(round(value)) if self.kind == "int" else float(value)


@dataclass(frozen=True)
class RenderContext:
    """
    What an effect may need to know about the picture it is applied to.

    @param width   width of the picture in pixels (preview or export)
    @param height  height of the picture in pixels
    @param scale   picture width / project width: 1.0 in the export, less in a smaller preview
    """
    width: int
    height: int
    scale: float = 1.0

    def px(self, value: float) -> float:
        """@return a size given in project pixels, converted to this picture (blur radius, ...)"""
        return value * self.scale


class Effect(ABC):
    """Base class of all effects. See the module text for an example."""

    id: ClassVar[str]
    name: ClassVar[str]
    category: ClassVar[str] = "color"
    params: ClassVar[Tuple[Param, ...]] = ()

    def defaults(self) -> Dict[str, Any]:
        """@return the neutral values of all parameters"""
        return {p.key: p.default for p in self.params}

    def normalize(self, values: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """@return values with every parameter present and in range; unknown keys are dropped"""
        values = values or {}
        return {p.key: p.coerce(values.get(p.key, p.default)) for p in self.params}

    @abstractmethod
    def filters(self, v: Mapping[str, Any], ctx: RenderContext) -> List[str]:
        """
        @param v    checked values of all parameters (see normalize)
        @param ctx  picture the filters will run on
        @return     FFmpeg filter strings in order, [] if the values change nothing
        """


# --------------------------------------------------------------------------- #
# Registry and plugin loader
# --------------------------------------------------------------------------- #

_effects: Dict[str, Effect] = {}
_load_errors: List[str] = []


def register(cls):
    """Class decorator: check the effect, create its one instance and make it available by id."""
    if not ID_RE.match(getattr(cls, "id", "") or ""):
        raise EffectError("%s: id must be lowercase letters, digits and _" % cls.__name__)
    if not getattr(cls, "name", ""):
        raise EffectError("%s: name is missing" % cls.__name__)
    keys = [p.key for p in cls.params]
    if len(keys) != len(set(keys)):
        raise EffectError("%s: duplicate parameter keys" % cls.__name__)
    if cls.id in _effects:
        raise EffectError("effect id %r is used twice" % cls.id)
    _effects[cls.id] = cls()
    return cls


def get(effect_id: str) -> Optional[Effect]:
    """@return the effect with this id, None if it is not installed"""
    return _effects.get(effect_id)


def all_effects() -> List[Effect]:
    """@return every registered effect in registration order"""
    return list(_effects.values())


def categories() -> List[Tuple[str, List[Effect]]]:
    """@return [(category, effects)] in order of first appearance"""
    groups: Dict[str, List[Effect]] = {}
    for eff in _effects.values():
        groups.setdefault(eff.category, []).append(eff)
    return list(groups.items())


def load_errors() -> List[str]:
    """@return one text per plugin module that could not be loaded"""
    return list(_load_errors)


def discover(package: str = "effects") -> int:
    """
    Import every module of the plugin package so its effects register. A broken module is skipped
    and reported by load_errors(); the others still load.

    @param package  importable package name (a folder with __init__.py)
    @return         number of registered effects
    """
    _load_errors.clear()
    try:
        pkg = importlib.import_module(package)
    except ImportError as exc:
        _load_errors.append("%s: %s" % (package, exc))
        return len(_effects)
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda m: m.name):
        if info.name.startswith("_"):
            continue
        try:
            importlib.import_module("%s.%s" % (package, info.name))
        except Exception as exc:                                    # a plugin must never stop the program
            _load_errors.append("%s.%s: %s" % (package, info.name, exc))
    return len(_effects)
