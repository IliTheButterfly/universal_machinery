"""Sequential Function Chart (SFC / grafcet) per IEC 61131-3 §2.6.

A grafcet describes a POU's behaviour as a directed graph of
**steps** (states), connected by **transitions** (guarded edges).
At any moment a set of steps is *active*; while active, a step
executes its **actions**.  A transition fires when all its
``from_steps`` are active and its boolean ``condition`` evaluates
true; firing deactivates the from-set and activates the to-set,
atomically (typical implementations defer activation by one scan
to avoid hazards, but that's a lowering detail).

This module is intentionally minimal -- step / transition / action
-- and reuses the existing ``il.ops`` primitives for transition
guards.  Backends that don't natively speak SFC lower it onto LD
by assigning a one-bit "step active" flag per step and emitting
guard/transition logic as rungs (see ``docs/click_calling_convention.md``
for CLICK's approach).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .ast import Address


@dataclass(frozen=True)
class Action:
    """An action attached to a step.

    ``qualifier`` is one of IEC §2.6.4.4:

      N          Non-stored: active while the step is active
      R          Reset (overrides any stored set)
      S          Set (stored; persists until R)
      L          Time-limited: active for at most ``time`` ms while step is active
      D          Time-delayed: becomes active ``time`` ms after step activation
      P          Pulse: active for exactly one scan on step activation
      P0 / P1    Falling / rising edge pulse
      SD, DS, SL Composite (set-delayed, delayed-set, stored-limited)

    ``target`` drives one of:
      - an ``Address`` (boolean coil) -- typical case
      - a string naming another POU to invoke while the step is active
    """

    qualifier: str
    target: "Address | str"
    time_ms: Optional[int] = None      # required for L / D / SD / DS / SL
    comment: str = ""


@dataclass(frozen=True)
class Step:
    """A grafcet step: a named state in the state machine.

    ``initial=True`` marks one or more steps that are active when the
    POU first starts (IEC requires exactly one initial step in a
    well-formed simple graph; parallel branches may declare several).
    """

    name: str
    initial: bool = False
    actions: tuple[Action, ...] = ()
    comment: str = ""


@dataclass(frozen=True)
class Transition:
    """A guarded edge between sets of steps.

    The transition is *enabled* when every step in ``from_steps`` is
    active; it *fires* when, in addition, the boolean expression in
    ``condition`` evaluates true.  Firing atomically deactivates the
    ``from_steps`` and activates the ``to_steps``.

    ``condition`` is a sequence of ``il.ops`` instructions evaluated
    as an LD-style rung whose right-most logic state is the guard.
    An empty condition means "always true" (an unconditional
    transition that fires as soon as the source step becomes active).
    """

    from_steps: tuple[str, ...]
    to_steps: tuple[str, ...]
    condition: tuple[object, ...] = ()   # tuple[Op, ...]
    comment: str = ""


@dataclass
class SfcNetwork:
    """A grafcet body for a POU.

    A POU's body is either ``rungs`` (LD/IL) or an ``SfcNetwork``,
    never both.  Backends that lack native SFC support lower the
    network onto LD: one boolean per step, plus a transition rung
    per Transition.  See ``docs/click_calling_convention.md`` for
    how CLICK does this.
    """

    steps: list[Step] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    comment: str = ""

    def find_step(self, name: str) -> Optional[Step]:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    def step_names(self) -> set[str]:
        return {s.name for s in self.steps}

    def initial_steps(self) -> list[Step]:
        return [s for s in self.steps if s.initial]

    def validate(self) -> list[str]:
        """Cheap structural checks; returns a list of human-readable issues.

        - every transition endpoint references a declared step
        - at least one step is marked initial
        - step names are unique
        """
        issues: list[str] = []
        names = [s.name for s in self.steps]
        if len(set(names)) != len(names):
            issues.append("duplicate step names")
        declared = set(names)
        for tr in self.transitions:
            for s in (*tr.from_steps, *tr.to_steps):
                if s not in declared:
                    issues.append(f"transition references undeclared step {s!r}")
        if not self.initial_steps():
            issues.append("no initial step declared")
        return issues
