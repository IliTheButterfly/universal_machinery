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

from .ast import Address, TagRef


#: A location reference inside an op -- either a concrete vendor
#: ``Address`` or a symbolic ``TagRef``.  A resolver pass swaps each
#: ``TagRef`` for an ``Address`` once tag allocation has bound names
#: to slots; backends should never see an unresolved ``TagRef`` at
#: emit time.
Loc = Union[Address, TagRef]

#: A value carried by an op: a ``Loc`` (location to read from) OR a
#: string literal (a vendor-formatted constant like ``"100"`` or
#: ``"1.5"``).  Used wherever an op accepts either an address or an
#: immediate.
Value = Union[Address, TagRef, str]


# -----------------------------------------------------------------------------
# Bit-input ops (contacts)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ContactNO:
    """Normally-open contact: passes power when its address is TRUE."""
    address: Loc


@dataclass(frozen=True)
class ContactNC:
    """Normally-closed contact: passes power when its address is FALSE."""
    address: Loc


@dataclass(frozen=True)
class ContactRisingEdge:
    """One-shot rising-edge contact (also called Positive Transition / |P|).

    ``negated`` (default False) corresponds to the PLCopen TC6
    XSD's combination of ``negated="true"`` plus ``edge="rising"``
    on a single ``<contact>`` element -- a rare-but-valid LD
    shape meaning "edge-detected NOT signal" (a one-shot that
    fires when the input falls to FALSE *and* the previous scan
    held the negated form TRUE).  Vendor support varies; the IL
    keeps the flag so PLCopen documents that carry it round-trip
    losslessly.
    """
    address: Loc
    negated: bool = False


@dataclass(frozen=True)
class ContactFallingEdge:
    """One-shot falling-edge contact (Negative Transition / |N|).

    ``negated`` mirrors ContactRisingEdge's flag; see that class's
    docstring for the precise PLCopen XSD shape.
    """
    address: Loc
    negated: bool = False


# -----------------------------------------------------------------------------
# Bit-output ops (coils)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class OutCoil:
    """Standard output coil: writes the rung's logic state to its address."""
    address: Loc


@dataclass(frozen=True)
class OutSet:
    """Latch coil (S): sets the address to TRUE when energised, holds it."""
    address: Loc


@dataclass(frozen=True)
class OutReset:
    """Unlatch coil (R): clears the address to FALSE when energised, holds."""
    address: Loc


# -----------------------------------------------------------------------------
# Timers (IEC 61131-3 §2.5.2.3.1)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TON:
    """On-delay timer: output goes TRUE after `preset` time of input being TRUE."""
    address: Loc               # the timer's symbol (e.g. "T0")
    preset_ms: int                  # preset value in milliseconds
    accumulator: Loc | None = None
    done_bit: Loc | None = None


@dataclass(frozen=True)
class TOF:
    """Off-delay timer: output goes FALSE after `preset` time of input being FALSE."""
    address: Loc
    preset_ms: int
    accumulator: Loc | None = None
    done_bit: Loc | None = None


@dataclass(frozen=True)
class TP:
    """Pulse timer: output goes TRUE for exactly `preset` time on rising edge."""
    address: Loc
    preset_ms: int
    accumulator: Loc | None = None
    done_bit: Loc | None = None


# -----------------------------------------------------------------------------
# Counters (IEC 61131-3 §2.5.2.3.2)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CTU:
    """Up-counter: increments accumulator on each rising edge of input."""
    address: Loc
    preset: int
    reset: Loc | None = None        # reset bit input
    accumulator: Loc | None = None  # current count storage
    done_bit: Loc | None = None     # done output (acc >= preset)


@dataclass(frozen=True)
class CTD:
    """Down-counter: decrements accumulator on each rising edge."""
    address: Loc
    preset: int
    load: Loc | None = None
    accumulator: Loc | None = None
    done_bit: Loc | None = None     # done output (acc <= 0)


@dataclass(frozen=True)
class CTUD:
    """Up/down counter: separate up and down inputs."""
    address: Loc
    preset: int
    cu_input: Loc                       # count-up input
    cd_input: Loc                       # count-down input
    reset: Loc | None = None
    load: Loc | None = None
    accumulator: Loc | None = None
    qu: Loc | None = None           # up done (acc >= preset)
    qd: Loc | None = None           # down done (acc <= 0)


# -----------------------------------------------------------------------------
# Bistables and edge triggers (IEC 61131-3 §2.5.2.3.3)
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RTrig:
    """IEC 61131-3 rising-edge trigger (R_TRIG).

    ``q`` is TRUE for exactly one scan when ``clk`` transitions
    FALSE -> TRUE.  Equivalent to ``ContactRisingEdge(clk) -> Coil(q)``
    but spelled as a single named FB instance per IEC conformance.

    ``state`` is the memory bit that holds the previous CLK value
    (the FB instance's hidden state).  Backends may collapse it
    into the ``q`` output's storage where the runtime allows; the
    IL keeps it explicit so the symbol table stays complete.
    """
    state: Loc           # previous CLK value (instance state)
    clk:   Loc           # input
    q:     Loc           # output (pulse on rising edge)


@dataclass(frozen=True)
class FTrig:
    """IEC 61131-3 falling-edge trigger (F_TRIG).

    Mirror of ``RTrig``: ``q`` pulses on TRUE -> FALSE transition
    of ``clk``.
    """
    state: Loc
    clk:   Loc
    q:     Loc


@dataclass(frozen=True)
class SR:
    """IEC 61131-3 set-dominant bistable (SR).

    ``q1`` := ``s1`` OR (``q1`` AND NOT ``r``).
    When both inputs fire simultaneously, **Set takes priority**
    (``q1`` stays / becomes TRUE).

    ``q1`` is both the output address and the FB instance's state
    storage -- one memory bit serves both roles per IEC's model.
    """
    q1: Loc              # output AND state
    s1: Loc              # set (dominant)
    r:  Loc              # reset


@dataclass(frozen=True)
class RS:
    """IEC 61131-3 reset-dominant bistable (RS).

    ``q1`` := NOT ``r1`` AND (``s`` OR ``q1``).
    When both inputs fire, **Reset takes priority** (``q1`` clears).
    """
    q1: Loc
    r1: Loc              # reset (dominant)
    s:  Loc              # set


# -----------------------------------------------------------------------------
# Generic IEC standard-library function call (§2.5.2)
# -----------------------------------------------------------------------------


#: IEC 61131-3 §2.5.2.1 elementary types that participate in
#: standard-named ``<SRC>_TO_<DST>`` conversions.  Every pair
#: appears in ``STD_FUNCTION_NAMES``; backends decide which
#: conversions are physically meaningful on their target (e.g.
#: ``BOOL_TO_TIME`` is unusual but accepted by some compilers).
_IEC_CONVERTIBLE_TYPES = (
    # Boolean
    "BOOL",
    # Bit-string
    "BYTE", "WORD", "DWORD", "LWORD",
    # Signed integer
    "SINT", "INT", "DINT", "LINT",
    # Unsigned integer
    "USINT", "UINT", "UDINT", "ULINT",
    # Real
    "REAL", "LREAL",
    # Time / date
    "TIME", "DATE", "TOD", "DT",
    # Strings
    "STRING", "WSTRING",
)


def _build_type_conversions() -> set[str]:
    """All ``<SRC>_TO_<DST>`` pairs for IEC elementary types.

    21 source types × 20 distinct destinations = 420 names.  Some
    are nonsensical (e.g. ``BOOL_TO_TIME``) but IEC §2.5.2.1
    permits implementation-defined conversion semantics for any
    pair, and PLC vendors widely accept the lot.  Including all
    pairs makes the registry exhaustive for validation purposes.
    """
    return {
        f"{src}_TO_{dst}"
        for src in _IEC_CONVERTIBLE_TYPES
        for dst in _IEC_CONVERTIBLE_TYPES
        if src != dst
    }


#: BCD (binary-coded decimal) conversions per §2.5.2.1.  Common on
#: legacy industrial controllers that store integers as packed
#: BCD digits.
_BCD_CONVERSIONS = frozenset({
    "BCD_TO_INT", "INT_TO_BCD",
    "BCD_TO_DINT", "DINT_TO_BCD",
    "BCD_TO_USINT", "USINT_TO_BCD",
    "BCD_TO_UINT", "UINT_TO_BCD",
    "BCD_TO_UDINT", "UDINT_TO_BCD",
    "BCD_TO_ULINT", "ULINT_TO_BCD",
})


#: TRUNC family per §2.5.2.1.  ``TRUNC`` truncates REAL/LREAL → DINT;
#: typed variants ``REAL_TRUNC_*`` and ``LREAL_TRUNC_*`` target a
#: specific integer destination type.
_TRUNC_FUNCTIONS = frozenset({
    "TRUNC",
    "REAL_TRUNC_INT", "REAL_TRUNC_DINT", "REAL_TRUNC_LINT",
    "REAL_TRUNC_SINT", "REAL_TRUNC_USINT", "REAL_TRUNC_UINT",
    "REAL_TRUNC_UDINT", "REAL_TRUNC_ULINT",
    "LREAL_TRUNC_INT", "LREAL_TRUNC_DINT", "LREAL_TRUNC_LINT",
    "LREAL_TRUNC_SINT", "LREAL_TRUNC_USINT", "LREAL_TRUNC_UINT",
    "LREAL_TRUNC_UDINT", "LREAL_TRUNC_ULINT",
})


#: §2.5.2.10 Time / date arithmetic.  Per IEC table 30: every
#: combination of TIME / DATE / TOD / DT with the result-typed
#: ``ADD_`` / ``SUB_`` / ``MUL_`` / ``DIV_`` / ``CONCAT_`` /
#: extraction functions.
_TIME_DATE_FUNCTIONS = frozenset({
    # TIME arithmetic
    "ADD_TIME",  "SUB_TIME",
    "MULTIME",   "DIVTIME",
    "MUL_TIME",  "DIV_TIME",   # alternate names some vendors use
    # DATE/TOD/DT + TIME
    "ADD_TOD_TIME", "SUB_TOD_TIME",
    "ADD_DT_TIME",  "SUB_DT_TIME",
    # Same-type subtraction (yields TIME)
    "SUB_DATE_DATE", "SUB_TOD_TOD", "SUB_DT_DT",
    # Composition
    "CONCAT_DATE_TOD",
    # Extraction (DT -> DATE / TOD)
    "DT_TO_DATE", "DT_TO_TOD", "DT_TO_TIME",
    "DATE_AND_TIME_TO_TIME_OF_DAY",
    "DATE_AND_TIME_TO_DATE",
    # Comparison operators for date/time also live in §2.5.2.10 but
    # we model those via the dedicated Compare op for ``>``/``<``/
    # ``=``/etc. across all elementary types.
})


#: §2.5.2.9 Character / string-handling functions.  Names per IEC
#: §2.5.2.9 table 28 -- complete for the standard.
_STRING_FUNCTIONS = frozenset({
    "LEN", "LEFT", "RIGHT", "MID",
    "CONCAT", "INSERT", "DELETE", "REPLACE", "FIND",
})


#: §2.5.2.4 Numerical functions.
_NUMERICAL_FUNCTIONS = frozenset({
    "ABS", "SQRT", "LN", "LOG", "EXP",
    "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
})


#: §2.5.2.6 Bit-string functions.
_BIT_STRING_FUNCTIONS = frozenset({
    "SHL", "SHR", "ROR", "ROL",
})


#: §2.5.2.7 Bitwise / logical functions.
_LOGICAL_FUNCTIONS = frozenset({
    "AND", "OR", "XOR", "NOT",
})


#: §2.5.2.8 Selection / multiplexing functions.
_SELECTION_FUNCTIONS = frozenset({
    "SEL", "MAX", "MIN", "LIMIT", "MUX",
})


#: The set of IEC 61131-3 §2.5.2 standard-function names this IL
#: recognises by default.  Backends may emit any name they support;
#: the registry exists for validation passes that want to flag
#: unknown names early.  Names follow IEC conventions (uppercase
#: ASCII, underscores).
#:
#: The set is the union of:
#:   - Programmatically-generated ``<SRC>_TO_<DST>`` pairs for the
#:     20 IEC elementary types in ``_IEC_CONVERTIBLE_TYPES``.
#:   - BCD conversions (legacy packed-decimal integers).
#:   - TRUNC family.
#:   - Numerical / bit-string / logical / selection helpers.
#:   - Character-string library.
#:   - Time / date arithmetic.
STD_FUNCTION_NAMES = frozenset(
    _build_type_conversions()
    | _BCD_CONVERSIONS
    | _TRUNC_FUNCTIONS
    | _NUMERICAL_FUNCTIONS
    | _BIT_STRING_FUNCTIONS
    | _LOGICAL_FUNCTIONS
    | _SELECTION_FUNCTIONS
    | _STRING_FUNCTIONS
    | _TIME_DATE_FUNCTIONS
)


def is_iec_std_function(name: str) -> bool:
    """True iff ``name`` is in ``STD_FUNCTION_NAMES``.

    Convenience predicate for backends / validation that want to
    distinguish IEC-standard names from vendor extensions
    (custom function blocks, vendor-specific instructions).
    """
    return name in STD_FUNCTION_NAMES


def iec_convertible_types() -> tuple[str, ...]:
    """Snapshot of the elementary type list participating in
    ``<SRC>_TO_<DST>`` conversions.

    Returned as a tuple so callers can't mutate the underlying
    registry.  Useful for documentation generators / conformance
    reports.
    """
    return _IEC_CONVERTIBLE_TYPES


@dataclass(frozen=True)
class StdFunc:
    """A call to an IEC 61131-3 §2.5.2 standard-library *function*
    (stateless).

    Why a single op rather than one dataclass per function?  IEC
    defines ~100 standard functions and they share the same shape:
    inputs -> single output.  A generic op keeps the IL compact;
    backends dispatch on ``name`` to emit the right vendor call.

    For *stateful* FBs (timers, counters, bistables, edge triggers),
    use their dedicated ops (TON, CTU, RTrig, SR, ...) -- IEC defines
    those as FUNCTION_BLOCKs with explicit instance state, and the
    dedicated ops carry that state in their fields.

    Examples::

        StdFunc(name="ABS",         inputs=(Address("DS10"),),
                                    output=Address("DS11"))
        StdFunc(name="SEL",         inputs=(Address("X001"),
                                            Address("DS10"),
                                            Address("DS20")),
                                    output=Address("DS30"))
        StdFunc(name="INT_TO_REAL", inputs=(TagRef("count"),),
                                    output=TagRef("count_real"))

    ``name`` should normally be a member of ``STD_FUNCTION_NAMES``;
    backends that recognise additional vendor extensions may accept
    unknown names but should declare a corresponding capability.
    """
    name:    str
    inputs:  tuple[Value, ...]
    output:  Loc


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
    lhs: Value
    rhs: Value


# -----------------------------------------------------------------------------
# Math / data ops
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """Move/Copy: write `src` into `dst`.  CLICK calls this `Copy`."""
    src: Value             # source address or literal
    dst: Loc                       # destination location


@dataclass(frozen=True)
class BinaryMath:
    """Generic binary arithmetic op: ``dst = lhs <op> rhs``.

    `op` is one of "+", "-", "*", "/", "%".
    """
    op: str
    lhs: Value
    rhs: Value
    dst: Loc


# -----------------------------------------------------------------------------
# Control flow
# -----------------------------------------------------------------------------


#: Argument binding at a call site: ``(formal_name, source)``.
#: ``source`` is a ``Value`` (Address, TagRef, or string literal) to
#: feed into the callee's VAR_INPUT slot named ``formal_name``.
ArgIn  = tuple[str, "Value"]

#: Output binding at a call site: ``(formal_name, destination)``.
#: After the callee returns, its VAR_OUTPUT slot ``formal_name`` is
#: copied into ``destination`` (a ``Loc``).
ArgOut = tuple[str, "Loc"]


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
    instance:  Optional[Loc] = None
    return_to: Optional[Loc] = None


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
# Vendor extension protocol
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class VendorOp:
    """A vendor-specific operation preserved verbatim through the IL.

    ``VendorOp`` is the escape hatch for instructions a vendor's
    runtime provides natively but the IL does not model as primitives.
    Three uses:

      1. **Round-trip preservation.**  A CLICK ``DRUM`` instruction
         read by the decoder is stored as ``VendorOp(vendor="click",
         name="DRUM", ...)`` and re-emitted on write -- the original
         instruction identity is preserved instead of being
         decomposed into primitives.

      2. **Performance hand-optimisation.**  A vendor-specific
         function block (Siemens ``SCL_S_LOOP``, Allen-Bradley
         ``PIDE``, etc.) is more efficient than the synthesised
         equivalent.  Users can invoke it explicitly when targeting
         that vendor.

      3. **Hardware-tied operations.**  Motion-control axis
         instructions, drive-parameter writes, vendor-proprietary
         comm blocks that don't have a meaningful IL primitive
         equivalent.

    Important framing -- ``VendorOp`` is **not** for things the IL
    can't express.  The IL targets the union of all PLC features
    via compilation (see ``docs/click_calling_convention.md`` for
    the model: any feature can be lowered onto any target with
    enough memory).  ``VendorOp`` is specifically for preserving
    vendor instruction *identity*, not for adding missing IL
    features.

    Behaviour
    ---------
    A backend lowering a Program raises ``UnsupportedOpError`` when
    it encounters a ``VendorOp`` whose ``vendor`` doesn't match its
    own name.  It must never silently drop or attempt to synthesise
    -- if the user authored a CLICK ``DRUM`` explicitly, they meant
    that instruction; refusing to emit it is more honest than
    emitting an approximation.

    Subclassing
    -----------
    Backends may subclass ``VendorOp`` to give their instructions
    stronger types::

        @dataclass(frozen=True)
        class ClickDrum(VendorOp):
            vendor: str = "click"
            name:   str = "DRUM"
            preset: int = 0
            steps:  tuple[Address, ...] = ()

    Subclasses must populate ``addresses`` (or override
    ``addresses_of`` behaviour) so the symbol-table walker sees the
    op's address references.
    """

    vendor: str                    # short vendor id matching Backend.name
    name: str                      # vendor's instruction name (DRUM, PIDE, ...)
    operands: tuple[object, ...] = ()
    attributes: tuple[tuple[str, object], ...] = ()
    addresses: tuple[Address, ...] = ()
    comment: str = ""


# -----------------------------------------------------------------------------
# Type aliases + helpers
# -----------------------------------------------------------------------------

#: Union of every concrete op type.  Backends typically dispatch on
#: ``isinstance(op, X)``; static-type checkers see ``Op`` as the union.
#: ``VendorOp`` is the open-extension hatch -- backend-specific subclasses
#: of ``VendorOp`` are still ``Op`` via inheritance.
Op = Union[
    ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
    OutCoil, OutSet, OutReset,
    TON, TOF, TP,
    CTU, CTD, CTUD,
    RTrig, FTrig, SR, RS,
    StdFunc,
    Compare,
    Move, BinaryMath,
    Call, Return, End, Jump, Label,
    ParallelGroup,
    VendorOp,
]


def _locs_of(op: object) -> list[Loc]:
    """Internal: collect every ``Loc`` reference (Address or TagRef) an
    op touches.  ``addresses_of`` and ``tags_of`` filter the result.

    Recurses through ``ParallelGroup`` branches.  String literals
    (``Move(src="5", ...)``) and POU/label names are not Locs and are
    skipped.  ``Return`` / ``End`` / ``Jump`` / ``Label`` contribute none.
    """
    out: list[Loc] = []
    if isinstance(op, (ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
                       OutCoil, OutSet, OutReset)):
        out.append(op.address)
    elif isinstance(op, (TON, TOF, TP)):
        out.append(op.address)
        for a in (op.accumulator, op.done_bit):
            if a is not None:
                out.append(a)
    elif isinstance(op, (CTU, CTD)):
        out.append(op.address)
        for a in (op.reset if isinstance(op, CTU) else op.load,
                  op.accumulator, op.done_bit):
            if a is not None:
                out.append(a)
    elif isinstance(op, CTUD):
        out.append(op.address)
        for a in (op.cu_input, op.cd_input, op.reset, op.load,
                  op.accumulator, op.qu, op.qd):
            if a is not None:
                out.append(a)
    elif isinstance(op, (RTrig, FTrig)):
        out.extend([op.state, op.clk, op.q])
    elif isinstance(op, SR):
        out.extend([op.q1, op.s1, op.r])
    elif isinstance(op, RS):
        out.extend([op.q1, op.r1, op.s])
    elif isinstance(op, StdFunc):
        for v in op.inputs:
            if isinstance(v, (Address, TagRef)):
                out.append(v)
        out.append(op.output)
    elif isinstance(op, Compare):
        for v in (op.lhs, op.rhs):
            if isinstance(v, (Address, TagRef)):
                out.append(v)
    elif isinstance(op, Move):
        if isinstance(op.src, (Address, TagRef)):
            out.append(op.src)
        out.append(op.dst)
    elif isinstance(op, BinaryMath):
        for v in (op.lhs, op.rhs):
            if isinstance(v, (Address, TagRef)):
                out.append(v)
        out.append(op.dst)
    elif isinstance(op, ParallelGroup):
        for branch in op.branches:
            for inner in branch:
                out.extend(_locs_of(inner))
    elif isinstance(op, Call):
        for _, src in op.inputs:
            if isinstance(src, (Address, TagRef)):
                out.append(src)
        for _, dst in op.outputs:
            out.append(dst)
        if op.instance is not None:
            out.append(op.instance)
        if op.return_to is not None:
            out.append(op.return_to)
    elif isinstance(op, VendorOp):
        out.extend(op.addresses)
    return out


def addresses_of(op: object) -> set[Address]:
    """Walk an op and collect every concrete ``Address`` it references.

    Symbolic ``TagRef`` references are skipped -- they're unresolved
    until a tag-allocator pass binds them to addresses.  Use
    ``tags_of`` for those.
    """
    return {loc for loc in _locs_of(op) if isinstance(loc, Address)}


def tags_of(op: object) -> set[str]:
    """Walk an op and collect every symbolic ``TagRef`` name it references.

    Backends use this (via ``Program.referenced_tags``) to verify that
    every symbolic reference has a corresponding ``Tag`` declaration
    before running the TagRef → Address resolver.
    """
    return {loc.name for loc in _locs_of(op) if isinstance(loc, TagRef)}
