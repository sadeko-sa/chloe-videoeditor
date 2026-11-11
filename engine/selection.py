"""
selection.py - Which effects of a layer are selected, and what moving them does. No Tk in here.

Effects are numbered by their index in Layer.effects (the order of application). A selection can hold several
effects, like several layers in Photoshop; one of them is the active one (the one last clicked), which is the one
whose values the parameter panel shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class EffectSelection:
    """
    An immutable selection of effect indices.

    @param indices  selected indices
    @param active   the active one; always part of `indices` (None only for an empty selection)
    """
    indices: frozenset = frozenset()
    active: Optional[int] = None

    def __post_init__(self):
        if self.active is None and self.indices:        # no active given: take the top one, like a fresh layer
            object.__setattr__(self, "active", max(self.indices))
        elif self.active is not None and self.active not in self.indices:
            object.__setattr__(self, "indices", self.indices | {self.active})

    @classmethod
    def none(cls) -> "EffectSelection":
        return cls()

    @classmethod
    def single(cls, index: Optional[int]) -> "EffectSelection":
        """@return a selection of one effect (empty for None)"""
        return cls() if index is None else cls(frozenset((index,)), index)

    @classmethod
    def of(cls, indices: Iterable[int], active: Optional[int] = None) -> "EffectSelection":
        """@return a selection of these indices; `active` is used if it is among them, else the top one"""
        found = frozenset(indices)
        return cls(found, active if active in found else None)

    @classmethod
    def top(cls, count: int) -> "EffectSelection":
        """@return the selection of the top effect of a chain of `count` effects (empty chain: nothing)"""
        return cls.single(count - 1 if count else None)

    def ordered(self) -> List[int]:
        """@return the selected indices, lowest (applied first) to highest"""
        return sorted(self.indices)

    def __len__(self) -> int:
        return len(self.indices)

    def __contains__(self, index: object) -> bool:
        return index in self.indices

    def clamped(self, count: int, keep_empty: bool = False) -> "EffectSelection":
        """
        @param count       number of effects the chain has now
        @param keep_empty  True = a selection that ends up empty stays empty (the user deselected everything);
                           False = the top effect is selected then
        @return            this selection without the indices that do not exist (any more)
        """
        valid = frozenset(i for i in self.indices if 0 <= i < count)
        if not valid:
            return EffectSelection.none() if keep_empty else EffectSelection.top(count)
        return EffectSelection.of(valid, self.active)


def move_block(count: int, selected: Iterable[int], delta: int) -> Optional[Tuple[List[int], frozenset]]:
    """
    Move the selected effects one place (delta = +1: applied later, -1: applied earlier). They keep their order among
    each other. A selected effect that cannot move (it is at the end of the chain, or the one in its way cannot
    move) stays, the others move past the effects that are not selected.

    @param count     number of effects in the chain
    @param selected  indices of the selected effects
    @param delta     +1 or -1
    @return          (order, new indices) or None if nothing moves. order[p] = old index of the effect that comes to
                     place p: new_chain = [chain[o] for o in order].
    """
    if delta not in (-1, 1):
        raise ValueError("delta must be -1 or 1")
    chosen = {i for i in selected if 0 <= i < count}
    order = list(range(count))
    stuck = set()
    for i in sorted(chosen, reverse=delta > 0):             # the ones in front of the move first
        j = i + delta
        if 0 <= j < count and j not in stuck:
            order[i], order[j] = order[j], order[i]
        else:
            stuck.add(i)
    if order == list(range(count)):
        return None
    return order, frozenset(p for p, old in enumerate(order) if old in chosen)
