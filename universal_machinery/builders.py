"""Builder DSL for the universal_machinery IL.

The raw IL dataclasses (``universal_machinery.il``) are explicit but
verbose -- a four-contact rung becomes::

    Rung([
        ContactNO(Address("X001")),
        ContactNC(Address("X002")),
        ParallelGroup(branches=(
            (ContactNO(Address("X003")),),
            (ContactNO(Address("X004")),),
        )),
        OutCoil(Address("Y001")),
    ])

The builder DSL trims that to::

    from universal_machinery.builders import rung, no, nc, parallel, coil

    rung(
        no("X001"),
        nc("X002"),
        parallel([no("X003")], [no("X004")]),
        coil("Y001"),
    )

Design choices:

  * **Smart string coercion.**  Strings that look like CLICK-style
    addresses (uppercase letters followed by digits: ``X001``, ``Y5``,
    ``DS9000``, ``C2256``, etc.) are coerced to ``Address`` automatically.
    Other strings are coerced to ``TagRef`` (symbolic tag reference).
    Use ``loc("...")`` / ``tag("...")`` to force one or the other when
    the smart classifier would guess wrong.
  * **Short, IEC-flavoured names.**  Contacts are ``no``/``nc``; coils
    are ``coil``/``set_``/``reset_``.  Math uses ``add``/``sub``/...;
    compare uses ``eq``/``ne``/``lt``/...  Matches what an engineer
    types into a ladder editor.
  * **No state.**  Every helper is a pure constructor returning an op or
    composite.  No global builder context, no implicit assembly.  POU
    and Program helpers compose by passing built children as keyword
    args -- not by mutating shared state.

This module is opt-in; the raw dataclass API stays the canonical form.
Tests / examples / agent harnesses can mix the two.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence, Union

from .il import (
    Address, DataBlock, PouKind, Program, Rung, Subroutine, Tag, TagRef,
    TagType, Var, VarDirection,
)
from .il.ops import (
    BinaryMath, Call, Compare, ContactFallingEdge, ContactNC, ContactNO,
    ContactRisingEdge, CTD, CTU, CTUD, End, Jump, Label, Move, OutCoil,
    OutReset, OutSet, ParallelGroup, Return, TOF, TON, TP, VendorOp,
)


# -----------------------------------------------------------------------------
# Type aliases
# -----------------------------------------------------------------------------

#: Anything the user can pass to a location-accepting parameter.
#: Resolved through ``_loc()`` -- strings get classified into Address
#: or TagRef per the regex below.
LocLike   = Union[Address, TagRef, str]

#: A LocLike OR a literal (for op fields that accept literals like
#: Compare.rhs or Move.src).  Resolved through ``_value()``.
ValueLike = Union[Address, TagRef, str, int, float]


# -----------------------------------------------------------------------------
# String coercion
# -----------------------------------------------------------------------------


#: CLICK-style addresses: one-or-more uppercase letters followed by
#: one-or-more digits.  Covers X001, Y5, DS9000, C2256, T0, CT1, SC1...
#: Anything that doesn't match is treated as a symbolic ``TagRef``.
_ADDR_PATTERN = re.compile(r"^[A-Z]+\d+$")

#: Numeric literals: optional sign, digits, optional decimal.  When
#: ``_value()`` sees a string matching this, it keeps it as a raw
#: string literal (the on-wire form vendor backends consume).
_NUMERIC_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")


def _loc(x: LocLike) -> Union[Address, TagRef]:
    """Coerce a string / Address / TagRef into a Loc.

    Strings matching ``[A-Z]+\\d+`` become ``Address``; everything
    else becomes ``TagRef``.  Pre-built ``Address`` / ``TagRef``
    values pass through unchanged.
    """
    if isinstance(x, (Address, TagRef)):
        return x
    if isinstance(x, str):
        if _ADDR_PATTERN.match(x):
            return Address(x)
        return TagRef(x)
    raise TypeError(f"expected str / Address / TagRef, got {type(x).__name__}")


def _value(x: ValueLike) -> Union[Address, TagRef, str]:
    """Coerce a value-typed input.

    Routing:
      - Pre-built ``Address`` / ``TagRef`` pass through.
      - Numeric (``int`` / ``float``) becomes its string representation
        -- the literal form ops carry.
      - Strings matching ``_NUMERIC_PATTERN`` are literal numerics;
        kept as-is.
      - Strings matching ``_ADDR_PATTERN`` become ``Address``.
      - All other strings become ``TagRef`` (symbolic reference).

    Use for fields like ``Compare.rhs`` / ``Move.src`` / ``BinaryMath.lhs``
    that accept either a location or a literal value.
    """
    if isinstance(x, (Address, TagRef)):
        return x
    if isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, str):
        if _NUMERIC_PATTERN.match(x):
            return x
        if _ADDR_PATTERN.match(x):
            return Address(x)
        return TagRef(x)
    raise TypeError(f"expected value, got {type(x).__name__}")


def loc(x: LocLike) -> Address:
    """Explicitly construct an ``Address`` from a string.

    Bypasses the smart classifier -- useful when a user-named tag
    coincidentally matches the address regex (e.g. a custom ``X001``
    symbolic name)."""
    if isinstance(x, Address):
        return x
    if isinstance(x, TagRef):
        raise TypeError(f"can't convert TagRef({x.name!r}) to Address; "
                        f"use tag() if you want a symbolic ref")
    return Address(x)


def tag(name: str) -> TagRef:
    """Explicitly construct a ``TagRef`` from a name.

    Bypasses the smart classifier -- useful when you want a symbolic
    reference whose name happens to match the address regex (``tag("X001")``
    when "X001" is a user-named tag, not a physical address)."""
    return TagRef(name)


# -----------------------------------------------------------------------------
# Contact / coil shortcuts
# -----------------------------------------------------------------------------


def no(addr: LocLike) -> ContactNO:
    """Normally-open contact: passes power when its address is TRUE."""
    return ContactNO(_loc(addr))


def nc(addr: LocLike) -> ContactNC:
    """Normally-closed contact: passes power when its address is FALSE."""
    return ContactNC(_loc(addr))


def redge(addr: LocLike) -> ContactRisingEdge:
    """Rising-edge contact (one-shot on FALSE -> TRUE transition)."""
    return ContactRisingEdge(_loc(addr))


def fedge(addr: LocLike) -> ContactFallingEdge:
    """Falling-edge contact (one-shot on TRUE -> FALSE transition)."""
    return ContactFallingEdge(_loc(addr))


def coil(addr: LocLike) -> OutCoil:
    """Standard output coil: writes the rung's logic state to its address."""
    return OutCoil(_loc(addr))


def set_(addr: LocLike) -> OutSet:
    """Latch coil (S): sets the address TRUE when energised; persists."""
    return OutSet(_loc(addr))


def reset_(addr: LocLike) -> OutReset:
    """Unlatch coil (R): clears the address to FALSE when energised."""
    return OutReset(_loc(addr))


# -----------------------------------------------------------------------------
# Timer / counter shortcuts
# -----------------------------------------------------------------------------


def ton(addr: LocLike, ms: int,
        accumulator: Optional[LocLike] = None,
        done_bit: Optional[LocLike] = None) -> TON:
    """On-delay timer: output goes TRUE after ``ms`` of continuous input."""
    return TON(
        address=_loc(addr), preset_ms=ms,
        accumulator=_loc(accumulator) if accumulator is not None else None,
        done_bit=_loc(done_bit) if done_bit is not None else None,
    )


def tof(addr: LocLike, ms: int,
        accumulator: Optional[LocLike] = None,
        done_bit: Optional[LocLike] = None) -> TOF:
    """Off-delay timer: output goes FALSE after ``ms`` of continuous absence."""
    return TOF(
        address=_loc(addr), preset_ms=ms,
        accumulator=_loc(accumulator) if accumulator is not None else None,
        done_bit=_loc(done_bit) if done_bit is not None else None,
    )


def tp(addr: LocLike, ms: int,
       accumulator: Optional[LocLike] = None,
       done_bit: Optional[LocLike] = None) -> TP:
    """Pulse timer: output TRUE for exactly ``ms`` on rising edge."""
    return TP(
        address=_loc(addr), preset_ms=ms,
        accumulator=_loc(accumulator) if accumulator is not None else None,
        done_bit=_loc(done_bit) if done_bit is not None else None,
    )


def ctu(addr: LocLike, preset: int,
        reset: Optional[LocLike] = None,
        accumulator: Optional[LocLike] = None,
        done_bit: Optional[LocLike] = None) -> CTU:
    """Up-counter."""
    return CTU(
        address=_loc(addr), preset=preset,
        reset=_loc(reset) if reset is not None else None,
        accumulator=_loc(accumulator) if accumulator is not None else None,
        done_bit=_loc(done_bit) if done_bit is not None else None,
    )


def ctd(addr: LocLike, preset: int,
        load: Optional[LocLike] = None,
        accumulator: Optional[LocLike] = None,
        done_bit: Optional[LocLike] = None) -> CTD:
    """Down-counter."""
    return CTD(
        address=_loc(addr), preset=preset,
        load=_loc(load) if load is not None else None,
        accumulator=_loc(accumulator) if accumulator is not None else None,
        done_bit=_loc(done_bit) if done_bit is not None else None,
    )


def ctud(addr: LocLike, preset: int,
         cu_input: LocLike, cd_input: LocLike,
         reset: Optional[LocLike] = None,
         load: Optional[LocLike] = None,
         accumulator: Optional[LocLike] = None,
         qu: Optional[LocLike] = None,
         qd: Optional[LocLike] = None) -> CTUD:
    """Up/down counter."""
    return CTUD(
        address=_loc(addr), preset=preset,
        cu_input=_loc(cu_input), cd_input=_loc(cd_input),
        reset=_loc(reset) if reset is not None else None,
        load=_loc(load) if load is not None else None,
        accumulator=_loc(accumulator) if accumulator is not None else None,
        qu=_loc(qu) if qu is not None else None,
        qd=_loc(qd) if qd is not None else None,
    )


# -----------------------------------------------------------------------------
# Compare shortcuts
# -----------------------------------------------------------------------------


def _cmp(op: str, lhs: ValueLike, rhs: ValueLike) -> Compare:
    return Compare(op=op, lhs=_value(lhs), rhs=_value(rhs))


def eq(lhs: ValueLike, rhs: ValueLike) -> Compare: return _cmp("==", lhs, rhs)
def ne(lhs: ValueLike, rhs: ValueLike) -> Compare: return _cmp("!=", lhs, rhs)
def lt(lhs: ValueLike, rhs: ValueLike) -> Compare: return _cmp("<",  lhs, rhs)
def le(lhs: ValueLike, rhs: ValueLike) -> Compare: return _cmp("<=", lhs, rhs)
def gt(lhs: ValueLike, rhs: ValueLike) -> Compare: return _cmp(">",  lhs, rhs)
def ge(lhs: ValueLike, rhs: ValueLike) -> Compare: return _cmp(">=", lhs, rhs)


# -----------------------------------------------------------------------------
# Math / move shortcuts
# -----------------------------------------------------------------------------


def move(src: ValueLike, dst: LocLike) -> Move:
    """Move/Copy: write ``src`` into ``dst``."""
    return Move(src=_value(src), dst=_loc(dst))


def _math(op: str, lhs: ValueLike, rhs: ValueLike,
          dst: LocLike) -> BinaryMath:
    return BinaryMath(op=op, lhs=_value(lhs), rhs=_value(rhs), dst=_loc(dst))


def add(lhs: ValueLike, rhs: ValueLike, dst: LocLike) -> BinaryMath:
    return _math("+", lhs, rhs, dst)


def sub(lhs: ValueLike, rhs: ValueLike, dst: LocLike) -> BinaryMath:
    return _math("-", lhs, rhs, dst)


def mul(lhs: ValueLike, rhs: ValueLike, dst: LocLike) -> BinaryMath:
    return _math("*", lhs, rhs, dst)


def div(lhs: ValueLike, rhs: ValueLike, dst: LocLike) -> BinaryMath:
    return _math("/", lhs, rhs, dst)


def mod(lhs: ValueLike, rhs: ValueLike, dst: LocLike) -> BinaryMath:
    return _math("%", lhs, rhs, dst)


# -----------------------------------------------------------------------------
# Control flow shortcuts
# -----------------------------------------------------------------------------


def call(target: str,
         inputs: Optional[Sequence[tuple[str, ValueLike]]] = None,
         outputs: Optional[Sequence[tuple[str, LocLike]]] = None,
         instance: Optional[LocLike] = None,
         return_to: Optional[LocLike] = None) -> Call:
    """Build a (possibly parameterized) ``Call`` op.

    ``inputs`` and ``outputs`` are sequences of ``(formal_name, src/dst)``
    pairs; the source/destination operands are coerced through
    ``_value`` / ``_loc``.  ``instance`` and ``return_to`` use ``_loc``.

    Bare unparameterized form: ``call("Sub1")``.
    """
    in_bindings = tuple((n, _value(v)) for n, v in (inputs or ()))
    out_bindings = tuple((n, _loc(v)) for n, v in (outputs or ()))
    return Call(
        target=target,
        inputs=in_bindings,
        outputs=out_bindings,
        instance=_loc(instance) if instance is not None else None,
        return_to=_loc(return_to) if return_to is not None else None,
    )


def ret() -> Return:
    """Return from a subroutine."""
    return Return()


def end() -> End:
    """End of main program."""
    return End()


def jump(label_name: str) -> Jump:
    """Jump to a labeled rung in the same subroutine when energised."""
    return Jump(label=label_name)


def label_(name: str) -> Label:
    """A named rung that ``jump`` can target."""
    return Label(name=name)


# -----------------------------------------------------------------------------
# Parallel topology
# -----------------------------------------------------------------------------


def parallel(*branches: Sequence[object]) -> ParallelGroup:
    """Build a ``ParallelGroup`` of OR'd branches.

    Each argument is a sequence of ops forming one branch.  Example::

        parallel([no("X1"), nc("X2")], [no("X3")])

    is "(X1 AND NOT X2) OR X3".
    """
    return ParallelGroup(branches=tuple(tuple(b) for b in branches))


# -----------------------------------------------------------------------------
# Rung helper
# -----------------------------------------------------------------------------


def rung(*ops: object, comment: str = "") -> Rung:
    """Build a ``Rung`` from a sequence of ops.

    The first ops are inputs (contacts, compares, parallel groups);
    the trailing ops are outputs (coils, calls, moves, math, etc.)::

        rung(no("X001"), nc("X002"), coil("Y001"))

    No magic about left vs. right -- the IL is unordered with respect
    to op categories; backends decide visual layout.
    """
    return Rung(ops=list(ops), comment=comment)


# -----------------------------------------------------------------------------
# Variable / Tag declarations
# -----------------------------------------------------------------------------


def var(name: str, type_: TagType,
        direction: VarDirection = VarDirection.LOCAL,
        initial: str = "",
        address: Optional[LocLike] = None,
        comment: str = "") -> Var:
    """Build a ``Var`` (POU parameter or local).

    Direction defaults to LOCAL.  Use ``var_in``/``var_out``/``var_inout``
    for typed parameter declarations -- they're thin wrappers."""
    return Var(
        name=name, data_type=type_, direction=direction,
        initial_value=initial,
        address=loc(address) if address is not None else None,
        comment=comment,
    )


def var_in(name: str, type_: TagType, **kw) -> Var:
    """VAR_INPUT declaration."""
    return var(name, type_, direction=VarDirection.INPUT, **kw)


def var_out(name: str, type_: TagType, **kw) -> Var:
    """VAR_OUTPUT declaration."""
    return var(name, type_, direction=VarDirection.OUTPUT, **kw)


def var_inout(name: str, type_: TagType, **kw) -> Var:
    """VAR_IN_OUT declaration."""
    return var(name, type_, direction=VarDirection.IN_OUT, **kw)


def tag_decl(name: str, type_: TagType, description: str = "",
             locked: Optional[LocLike] = None) -> Tag:
    """Declare a ``Tag`` in the program's symbol table.

    ``locked`` pins the tag to a specific address -- the backend must
    honour it verbatim (right for HMI-pinned tags, physical I/O,
    fieldbus-exposed tags).  ``locked=None`` (default) means
    "dynamic" -- the tag allocator picks a free address."""
    return Tag(
        name=name, data_type=type_, description=description,
        address=loc(locked) if locked is not None else None,
    )


def data_block(name: str,
               members: Optional[Sequence[Var]] = None,
               base_address: Optional[LocLike] = None,
               fb_template: Optional[str] = None,
               comment: str = "") -> DataBlock:
    """Build a ``DataBlock`` (typed memory aggregate / FB instance state)."""
    return DataBlock(
        name=name,
        members=list(members or []),
        base_address=loc(base_address) if base_address is not None else None,
        fb_template=fb_template,
        comment=comment,
    )


# -----------------------------------------------------------------------------
# POU shortcuts
# -----------------------------------------------------------------------------


def _make_pou(kind: PouKind, name: str, *,
              main: bool = False,
              rungs: Optional[Sequence[Rung]] = None,
              inputs: Optional[Sequence[Var]] = None,
              outputs: Optional[Sequence[Var]] = None,
              in_outs: Optional[Sequence[Var]] = None,
              local_vars: Optional[Sequence[Var]] = None,
              return_type: Optional[TagType] = None,
              sfc=None,
              comment: str = "") -> Subroutine:
    return Subroutine(
        name=name, kind=kind, main=main,
        rungs=list(rungs or []),
        inputs=list(inputs or []),
        outputs=list(outputs or []),
        in_outs=list(in_outs or []),
        local_vars=list(local_vars or []),
        return_type=return_type,
        sfc=sfc, comment=comment,
    )


def subroutine(name: str, **kw) -> Subroutine:
    """Vendor-native unparameterized subroutine (PouKind.SUBROUTINE).

    Spelled out to avoid colliding with ``sub`` (the math op).
    """
    return _make_pou(PouKind.SUBROUTINE, name, **kw)


def prog(name: str, **kw) -> Subroutine:
    """PROGRAM POU.  Pass ``main=True`` to mark as the entry point."""
    return _make_pou(PouKind.PROGRAM, name, **kw)


def fn(name: str, *, return_type: Optional[TagType] = None, **kw) -> Subroutine:
    """FUNCTION POU (stateless; returns one value).

    Convention: the first VAR_OUTPUT is the implicit return slot;
    ``return_type`` declares its type."""
    return _make_pou(PouKind.FUNCTION, name, return_type=return_type, **kw)


def fb(name: str, **kw) -> Subroutine:
    """FUNCTION_BLOCK POU (stateful; state lives in an instance DataBlock)."""
    return _make_pou(PouKind.FUNCTION_BLOCK, name, **kw)


# -----------------------------------------------------------------------------
# Top-level Program builder
# -----------------------------------------------------------------------------


def program(*,
            subroutines: Optional[Sequence[Subroutine]] = None,
            tags: Optional[Sequence[Tag]] = None,
            data_blocks: Optional[Sequence[DataBlock]] = None,
            cpu_model: str = "",
            project_name: str = "",
            comment: str = "") -> Program:
    """Build a complete ``Program``.

    ``tags`` is a flat list; the constructor keys them by name into
    ``Program.tags``.  Pass POUs in any order -- ``main_subroutine``
    discrimination is by ``Subroutine.main`` flag, not list position.
    """
    return Program(
        subroutines=list(subroutines or []),
        tags={t.name: t for t in (tags or [])},
        data_blocks=list(data_blocks or []),
        cpu_model=cpu_model,
        project_name=project_name,
        comment=comment,
    )


# -----------------------------------------------------------------------------
# What ``from universal_machinery.builders import *`` exports
# -----------------------------------------------------------------------------


__all__ = [
    # Coercion helpers
    "loc", "tag",
    # Contacts / coils
    "no", "nc", "redge", "fedge", "coil", "set_", "reset_",
    # Timers / counters
    "ton", "tof", "tp", "ctu", "ctd", "ctud",
    # Compare
    "eq", "ne", "lt", "le", "gt", "ge",
    # Math / move
    "move", "add", "sub", "mul", "div", "mod",
    # Control flow
    "call", "ret", "end", "jump", "label_",
    # Topology
    "parallel",
    # Rung
    "rung",
    # Declarations
    "var", "var_in", "var_out", "var_inout",
    "tag_decl", "data_block",
    # POUs
    "subroutine", "prog", "fn", "fb",
    # Program
    "program",
]
