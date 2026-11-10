"""
effect.py - Everything an effect plugin needs, in one place: Param, RenderContext, GraphBuilder,
Effect, ChainEffect, the registry and the plugin loader.

An effect is one small class in a module of the effect/ folder; nothing else has to change. Most
effects are one straight chain of FFmpeg filters and extend ChainEffect:

    from engine.effect import ChainEffect, Param, register, num

    @register
    class Invert(ChainEffect):
        id = "invert"                      # stable id saved in projects: lowercase, digits, _
        name = "Invert"                    # English name; lang files may override it ("effect.invert")
        category = "color"                 # group in the "Add effect" menu ("category.color")
        params = (Param("amount", "Amount", "float", default=100, min=0, max=100),)

        def filters(self, v, ctx):         # v: checked parameter values, ctx: RenderContext
            return ["negate"] if v["amount"] > 0 else []

filters() returns FFmpeg filter strings, in order (the filters of one effect form a chain). An empty
list means "no change", so a neutral setting costs nothing in the export. Values that contain
, : ; [ ] or spaces must be wrapped with quote().

An effect that needs branches (a blurred copy blended back, ...) extends Effect directly and wires
itself into the graph through the GraphBuilder it is given: see Clarity in effect/detail.py.
Effects are stateless; the values of every use live in the project (engine/project.py).

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
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from . import curve

KINDS = ("float", "int", "bool", "choice", "curve")
"""
Parameter kinds: float / int = number between min and max, bool = on / off, choice = one of `choices`,
curve = tone curve (a list of [input, output] points, see engine/curve.py).
"""

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
    @param default  neutral / start value; for "choice" one of `choices`; for "curve" a list of points (straight line if none)
    @param min      lowest value (float / int)
    @param max      highest value (float / int)
    @param step     slider step (float / int)
    @param choices  allowed values of a "choice" (texts)
    @param group    name of the group the parameter is shown in ("" = always shown); the parameters of a group are
                     shown one group at a time, chosen from a list, and must follow each other in `params`
    """
    key: str
    label: str
    kind: str = "float"
    default: Any = 0
    min: float = 0.0
    max: float = 100.0
    step: float = 1.0
    choices: Tuple[str, ...] = ()
    group: str = ""

    def __post_init__(self):
        if not ID_RE.match(self.key):
            raise EffectError("bad parameter key: %r" % self.key)
        if self.kind not in KINDS:
            raise EffectError("parameter %s: unknown kind %r" % (self.key, self.kind))
        if self.kind == "choice" and (not self.choices or self.default not in self.choices):
            raise EffectError("parameter %s: needs choices that contain the default" % self.key)
        if self.kind in ("float", "int") and not self.min <= self.default <= self.max:
            raise EffectError("parameter %s: default outside min..max" % self.key)
        if self.kind == "curve":
            start = self.default if isinstance(self.default, (list, tuple)) else curve.identity()
            object.__setattr__(self, "default", tuple(tuple(point) for point in curve.normalize(start)))

    def start(self) -> Any:
        """@return a new copy of the neutral value (a curve is a list that the caller may change)"""
        if self.kind == "curve":
            return [list(point) for point in self.default]
        return self.default

    def coerce(self, value: Any) -> Any:
        """@return value as this kind of parameter, limited to its range; the default if unusable"""
        if self.kind == "curve":
            return curve.normalize(value, self.default)
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


class GraphBuilder(ABC):
    """
    What an effect may use to put itself into the filter graph. The compiler implements it, so the
    effects do not depend on the compiler.
    """

    @abstractmethod
    def chain(self, sources: Sequence[str], filters: Sequence[str]) -> str:
        """
        Add one chain of filters.

        @param sources  labels the first filter of the chain reads (two for a blend, ...)
        @param filters  filters in order
        @return         the label of the chain's output
        """

    @abstractmethod
    def split(self, source: str, count: int = 2) -> List[str]:
        """@return `count` labels that all carry the picture of `source`"""


class Effect(ABC):
    """
    Contract of every effect: it knows when it changes nothing and how to wire itself into the graph.
    Extend ChainEffect for a plain chain of filters; extend this class for anything with branches.
    """

    id: ClassVar[str]
    name: ClassVar[str]
    category: ClassVar[str] = "color"
    params: ClassVar[Tuple[Param, ...]] = ()

    def defaults(self) -> Dict[str, Any]:
        """@return the neutral values of all parameters"""
        return {p.key: p.start() for p in self.params}

    def normalize(self, values: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """@return values with every parameter present and in range; unknown keys are dropped"""
        values = values or {}
        return {p.key: p.coerce(values.get(p.key, p.default)) for p in self.params}

    @abstractmethod
    def is_neutral(self, v: Mapping[str, Any], ctx: RenderContext) -> bool:
        """
        @param v    checked values of all parameters (see normalize)
        @param ctx  picture the effect will run on
        @return     True if these values change nothing; the effect is then left out of the graph
        """

    @abstractmethod
    def apply(self, g: GraphBuilder, source: str, v: Mapping[str, Any], ctx: RenderContext) -> str:
        """
        Add the effect to the graph. Only called when is_neutral() is False.

        @param g       where to add chains
        @param source  label of the picture the effect works on
        @param v       checked values of all parameters
        @param ctx     picture the effect runs on
        @return        label of the changed picture
        """


class ChainEffect(Effect):
    """An effect that is one straight chain of FFmpeg filters; subclasses only write filters()."""

    @abstractmethod
    def filters(self, v: Mapping[str, Any], ctx: RenderContext) -> List[str]:
        """
        @param v    checked values of all parameters (see normalize)
        @param ctx  picture the filters will run on
        @return     FFmpeg filter strings in order, [] if the values change nothing
        """

    def is_neutral(self, v: Mapping[str, Any], ctx: RenderContext) -> bool:
        return not self.filters(v, ctx)

    def apply(self, g: GraphBuilder, source: str, v: Mapping[str, Any], ctx: RenderContext) -> str:
        return g.chain([source], self.filters(v, ctx))


class LatticeEffect(Effect):
    """
    An effect that gives every pixel a new color by a rule that looks only at the color of that pixel (no neighbours,
    no position): hue and saturation by color range, for example. A slow per pixel filter such as geq would need
    seconds for one HD picture, so the rule runs once on a small lattice that holds every color instead: FFmpeg's
    haldclutsrc makes the identity lattice (one picture of lattice_level ** 3 by lattice_level ** 3 pixels, one frame),
    lattice_filters() changes it, and haldclut looks every pixel of the video up in it, with tetrahedral interpolation
    between the lattice colors. The cost per frame is that of a lookup, whatever the rule is.
    Subclasses write lattice_filters() and is_neutral().
    """

    lattice_level: ClassVar[int] = 6
    """
    Level of the lattice (2..16). The lattice has lattice_level ** 2 colors along each of its three axes, so 6 gives
    36 and 7 gives 49, and the rule has to run on lattice_level ** 6 pixels: more levels follow sharp changes of
    the rule better but make the preview slower.
    """

    @abstractmethod
    def lattice_filters(self, v: Mapping[str, Any], ctx: RenderContext) -> List[str]:
        """
        @param v    checked values of all parameters (see normalize)
        @param ctx  picture the effect runs on
        @return     FFmpeg filters that change the identity lattice (an RGB picture) the way the effect should
                    change every color of the video
        """

    def apply(self, g: GraphBuilder, source: str, v: Mapping[str, Any], ctx: RenderContext) -> str:
        lattice = g.chain([], ["haldclutsrc=level=%d:rate=1:duration=1" % self.lattice_level]
                          + list(self.lattice_filters(v, ctx)))
        return g.chain([source, lattice], ["haldclut=interp=tetrahedral"])


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


def discover(package: str = "effect") -> int:
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
