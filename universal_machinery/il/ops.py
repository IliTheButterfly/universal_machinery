"""IL instruction (op) types.

The op set is structured per IEC 61131-3 Part 3 (LD/IL/ST primitives)
plus a few CLICK / Allen-Bradley conveniences that map onto IEC ops.
Every op is a frozen dataclass so the AST is hashable and structurally
comparable.

Categories:
  - Bit input: ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge
  - Bit output: OutCoil, OutSet, OutReset
  - Timers: TON, TOF, TP
  - Counters: CTU, CTD, CTUD
  - Compare: Eq, Ne, Lt, Le, Gt, Ge
  - Math: Move (Copy), Add, Sub, Mul, Div, Mod
  - Control: Call (POU invocation with optional arg/return/instance
    bindings; covers both FUNCTION and FUNCTION_BLOCK calls), Return,
    End, Jump, Label
  - Topology: ParallelGroup -- represents OR'd branches in LD

Future:
  - PID, Shift/Rotate, Logical AND/OR/XOR, BCD/Hex conversion ops
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from .ast import Address


# -----------------------------------------------------------------------------
# Bit-input ops (contacts)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ContactNO:
    """Normally-open contact: passes power when its address is TRUE."""
    address: Address


@dataclass(frozen=True)
class ContactNC:
    """Normally-closed contact: passes power when its address is FALSE."""
    address: Address


@dataclass(frozen=True)
class ContactRisingEdge:
    """One-shot rising-edge contact (also called Positive Transition / |P|)."""
    address: Address


@dataclass(frozen=True)
class ContactFallingEdge:
    """One-shot falling-edge contact (Negative Transition / |N|)."""
    address: Address


# -----------------------------------------------------------------------------
# Bit-output ops (coils)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class OutCoil:
    """Standard output coil: writes the rung's logic state to its address."""
    address: Address


@dataclass(frozen=True)
class OutSet:
    """Latch coil (S): sets the address to TRUE when energised, holds it."""
    address: Address


@dataclass(frozen=True)
class OutReset:
    """Unlatch coil (R): clears the address to FALSE when energised, holds."""
    address: Address


# -----------------------------------------------------------------------------
# Timers (IEC 61131-3 §2.5.2.3.1)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TON:
    """On-delay timer: output goes TRUE after `preset` time of input being TRUE."""
    address: Address               # the timer's symbol (e.g. "T0")
    preset_ms: int                  # preset value in milliseconds
    accumulator: Address | None = None
    done_bit: Address | None = None


@dataclass(frozen=True)
class TOF:
    """Off-delay timer: output goes FALSE after `preset` time of input being FALSE."""
    address: Address
    preset_ms: int
    accumulator: Address | None = None
    done_bit: Address | None = None


@dataclass(frozen=True)
class TP:
    """Pulse timer: output goes TRUE for exactly `preset` time on rising edge."""
    address: Address
    preset_ms: int
    accumulator: Address | None = None
    done_bit: Address | None = None


# -----------------------------------------------------------------------------
# Counters (IEC 61131-3 §2.5.2.3.2)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CTU:
    """Up-counter: increments accumulator on each rising edge of input."""
    address: Address
    preset: int
    reset: Address | None = None        # reset bit input
    accumulator: Address | None = None  # current count storage
    done_bit: Address | None = None     # done output (acc >= preset)


@dataclass(frozen=True)
class CTD:
    """Down-counter: decrements accumulator on each rising edge."""
    address: Address
    preset: int
    load: Address | None = None
    accumulator: Address | None = None
    done_bit: Address | None = None     # done output (acc <= 0)


@dataclass(frozen=True)
class CTUD:
    """Up/down counter: separate up and down inputs."""
    address: Address
    preset: int
    cu_input: Address                   # count-up input
    cd_input: Address                   # count-down input
    reset: Address | None = None
    load: Address | None = None
    accumulator: Address | None = None
    qu: Address | None = None           # up done (acc >= preset)
    qd: Address | None = None           # down done (acc <= 0)


# -----------------------------------------------------------------------------
# Compare ops (binary comparisons producing a logic bit)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Compare:
    """Binary comparison: output bit goes TRUE when ``lhs <op> rhs``.

    `op` is one of "==", "!=", "<", "<=", ">", ">=".
    `lhs` and `rhs` are addresses or numeric literals (as strings).
    """
    op: str
    lhs: Address | str
    rhs: Address | str


# -----------------------------------------------------------------------------
# Math / data ops
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """Move/Copy: write `src` into `dst`.  CLICK calls this `Copy`."""
    src: Address | str             # source address or literal
    dst: Address                   # destination address


@dataclass(frozen=True)
class BinaryMath:
    """Generic binary arithmetic op: ``dst = lhs <op> rhs``.

    `op` is one of "+", "-", "*", "/", "%".
    """
    op: str
    lhs: Address | str
    rhs: Address | str
    dst: Address


# -----------------------------------------------------------------------------
# Control flow
# -----------------------------------------------------------------------------


#: Argument binding at a call site: ``(formal_name, source)``.
#: ``source`` is the actual address (or literal, as a string) to feed
#: into the callee's VAR_INPUT slot named ``formal_name``.
ArgIn  = tuple[str, "Address | str"]

#: Output binding at a call site: ``(formal_name, destination)``.
#: After the callee returns, its VAR_OUTPUT slot ``formal_name`` is
#: copied into ``destination``.
ArgOut = tuple[str, "Address"]


@dataclass(frozen=True)
class Call:
    """Invoke a POU by name.

    Three usage modes, all covered by the same op:

      * **Subroutine** (CLICK-style, no interface)::

            Call(target="Sub1")

      * **Function** (stateless, returns one value)::

            Call(target="Average",
                 inputs=(("a", Address("DS10")), ("b", Address("DS11"))),
                 return_to=Address("DS12"))

      * **Function block** (stateful instance)::

            Call(target="PID",
                 instance=Address("DB7"),       # the instance DB's base
                 inputs=(("SP", Address("DS20")), ("PV", Address("DS21"))),
                 outputs=(("OUT", Address("DS22")),))

    ``inputs`` / ``outputs`` use formal-parameter names so reordering
    declarations in the callee doesn't break call sites.  Backends
    lower these into their native calling convention -- for CLICK
    that's Move ops against per-POU reserved DS slots; see
    ``docs/click_calling_convention.md``.
    """
    target: str
    inputs:  tuple[ArgIn, ...]  = field(default_factory=tuple)
    outputs: tuple[ArgOut, ...] = field(default_factory=tuple)
    instance:  Optional[Address] = None
    return_to: Optional[Address] = None


@dataclass(frozen=True)
class Return:
    """Return from a subroutine."""


@dataclass(frozen=True)
class End:
    """End of main program."""


@dataclass(frozen=True)
class Jump:
    """Jump to a labeled rung in the same subroutine when energised."""
    label: str


@dataclass(frozen=True)
class Label:
    """A named rung that ``Jump`` can target.  Placed at the start of a rung."""
    name: str


# -----------------------------------------------------------------------------
# Topology
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ParallelGroup:
    """A parallel branch (OR) inside a rung.

    Each branch is an ordered list of ops representing one path; the
    group passes power if ANY branch's contacts all conduct.

    Example: ``A AND (B OR C) -> D`` is::

        Rung([
            ContactNO(A),
            ParallelGroup(branches=[
                [ContactNO(B)],
                [ContactNO(C)],
            ]),
            OutCoil(D),
        ])
    """
    branches: tuple[tuple[object, ...], ...]


# -----------------------------------------------------------------------------
# Type aliases + helpers
# -----------------------------------------------------------------------------

#: Union of every concrete op type.  Backends typically dispatch on
#: ``isinstance(op, X)``; static-type checkers see ``Op`` as the union.
Op = Union[
    ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
    OutCoil, OutSet, OutReset,
    TON, TOF, TP,
    CTU, CTD, CTUD,
    Compare,
    Move, BinaryMath,
    Call, Return, End, Jump, Label,
    ParallelGroup,
]


def addresses_of(op: object) -> set[Address]:
    """Walk an op (recursively for ParallelGroup) and collect every Address it references."""
    out: set[Address] = set()
    if isinstance(op, (ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
                       OutCoil, OutSet, OutReset)):
        out.add(op.address)
    elif isinstance(op, (TON, TOF, TP)):
        out.add(op.address)
        for a in (op.accumulator, op.done_bit):
            if a is not None:
                out.add(a)
    elif isinstance(op, (CTU, CTD)):
        out.add(op.address)
        for a in (op.reset if isinstance(op, CTU) else op.load,
                  op.accumulator, op.done_bit):
            if a is not None:
                out.add(a)
    elif isinstance(op, CTUD):
        out.add(op.address)
        for a in (op.cu_input, op.cd_input, op.reset, op.load,
                  op.accumulator, op.qu, op.qd):
            if a is not None:
                out.add(a)
    elif isinstance(op, Compare):
        for v in (op.lhs, op.rhs):
            if isinstance(v, Address):
                out.add(v)
    elif isinstance(op, Move):
        if isinstance(op.src, Address):
            out.add(op.src)
        out.add(op.dst)
    elif isinstance(op, BinaryMath):
        for v in (op.lhs, op.rhs):
            if isinstance(v, Address):
                out.add(v)
        out.add(op.dst)
    elif isinstance(op, ParallelGroup):
        for branch in op.branches:
            for inner in branch:
                out.update(addresses_of(inner))
    elif isinstance(op, Call):
        for _, src in op.inputs:
            if isinstance(src, Address):
                out.add(src)
        for _, dst in op.outputs:
            out.add(dst)
        if op.instance is not None:
            out.add(op.instance)
        if op.return_to is not None:
            out.add(op.return_to)
    # Return / End / Jump / Label have no addresses
    return out
