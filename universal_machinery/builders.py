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
    AccessSpec, Address, AliasType, ArrayType, Assignment, BinaryExpr,
    BinaryOp, CaseClause, CaseStatement, Configuration, ContinueStatement,
    DataBlock, DataType, EnumType, ExitStatement, Expression, FieldAccess,
    ForStatement, FunctionCallExpr, FunctionCallStatement, IfStatement,
    IndexAccess, Interface, Literal, Method, NamedType, PouInstance,
    PouKind, Program, RepeatStatement, Resource, ReturnStatement, Rung,
    Statement, StructType, SubrangeType, Subroutine, Tag, TagRef, TagType,
    TaskSpec, UnaryExpr, UnaryOp, UserType, Var, VarDirection, VarRef,
    WhileStatement,
)
from .il.ops import (
    BinaryMath, Call, Compare, ContactFallingEdge, ContactNC, ContactNO,
    ContactRisingEdge, CTD, CTU, CTUD, End, FTrig, Jump, Label, Move, OutCoil,
    OutReset, OutSet, ParallelGroup, Return, RS, RTrig, SR, StdFunc, TOF, TON,
    TP, VendorOp,
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

#: IEC 61131-3 §2.4.1.1 direct representation:
#:   %  location-prefix  size-prefix?  index ('.' subindex)*
#: where location-prefix is I (input), Q (output), or M (memory);
#: size-prefix is X (bit, default), B (byte), W (word), D (dword), L
#: (lword).  Examples: %I0.0  %IX0.0  %QB1  %MW5  %MD100  %I0.0.0
#: (hierarchical addresses for nested I/O modules).
_IEC_DIRECT_REP_PATTERN = re.compile(r"^%[IQM][XBWDL]?\d+(\.\d+)*$")

#: Numeric literals: optional sign, digits, optional decimal.  When
#: ``_value()`` sees a string matching this, it keeps it as a raw
#: string literal (the on-wire form vendor backends consume).
_NUMERIC_PATTERN = re.compile(r"^-?\d+(\.\d+)?$")


def _is_address_string(s: str) -> bool:
    """True iff ``s`` looks like an address rather than a tag name.

    Recognises either CLICK-style vendor addresses (``X001``,
    ``DS9000``, ``C2256``...) or IEC §2.4.1.1 direct-representation
    addresses (``%I0.0``, ``%MW5``, ``%QX1.0``...).
    """
    return bool(_ADDR_PATTERN.match(s) or _IEC_DIRECT_REP_PATTERN.match(s))


def _loc(x: LocLike) -> Union[Address, TagRef]:
    """Coerce a string / Address / TagRef into a Loc.

    Strings that look like an address (CLICK-style ``X001``,
    ``DS9000`` OR IEC direct-rep ``%I0.0``, ``%MW5``) become
    ``Address``; everything else becomes ``TagRef``.  Pre-built
    ``Address`` / ``TagRef`` values pass through unchanged.
    """
    if isinstance(x, (Address, TagRef)):
        return x
    if isinstance(x, str):
        if _is_address_string(x):
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
      - Strings that look like an address (CLICK-style ``[A-Z]+\\d+``
        OR IEC direct-rep ``%[IQM]...``) become ``Address``.
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
        if _is_address_string(x):
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
# IEC 61131-3 §2.5.2.3.3 bistables and edge triggers
# -----------------------------------------------------------------------------


def r_trig(state: LocLike, clk: LocLike, q: LocLike) -> RTrig:
    """IEC R_TRIG: ``q`` pulses for one scan on rising edge of ``clk``."""
    return RTrig(state=_loc(state), clk=_loc(clk), q=_loc(q))


def f_trig(state: LocLike, clk: LocLike, q: LocLike) -> FTrig:
    """IEC F_TRIG: ``q`` pulses for one scan on falling edge of ``clk``."""
    return FTrig(state=_loc(state), clk=_loc(clk), q=_loc(q))


def sr(q1: LocLike, s1: LocLike, r: LocLike) -> SR:
    """IEC SR: set-dominant bistable.  Set wins on simultaneous fire."""
    return SR(q1=_loc(q1), s1=_loc(s1), r=_loc(r))


def rs(q1: LocLike, r1: LocLike, s: LocLike) -> RS:
    """IEC RS: reset-dominant bistable.  Reset wins on simultaneous fire."""
    return RS(q1=_loc(q1), r1=_loc(r1), s=_loc(s))


# -----------------------------------------------------------------------------
# IEC 61131-3 §2.5.2 standard-library function call (generic + convenience)
# -----------------------------------------------------------------------------


def std_func(name: str, inputs: Sequence[ValueLike],
             output: LocLike) -> StdFunc:
    """Generic IEC standard-library function call.

    Use the named convenience helpers below (``abs_``, ``sqrt``,
    ``and_``, ``or_``, ``sel``, ...) when the function is one of
    the common ones; ``std_func`` is the escape hatch for anything
    in ``STD_FUNCTION_NAMES`` (or vendor extensions a backend
    accepts).
    """
    return StdFunc(
        name=name,
        inputs=tuple(_value(v) for v in inputs),
        output=_loc(output),
    )


def _single(name: str):
    """Factory: returns a one-input helper for IEC function ``name``."""
    def _fn(arg: ValueLike, output: LocLike) -> StdFunc:
        return std_func(name, [arg], output)
    _fn.__name__ = name.lower() + "_"
    return _fn


def _binary(name: str):
    """Factory: returns a two-input helper for IEC function ``name``."""
    def _fn(a: ValueLike, b: ValueLike, output: LocLike) -> StdFunc:
        return std_func(name, [a, b], output)
    _fn.__name__ = name.lower() + "_"
    return _fn


# Numerical (§2.5.2.4)
abs_  = _single("ABS")
sqrt  = _single("SQRT")
ln    = _single("LN")
log   = _single("LOG")
exp   = _single("EXP")
sin   = _single("SIN")
cos   = _single("COS")
tan   = _single("TAN")
asin  = _single("ASIN")
acos  = _single("ACOS")
atan  = _single("ATAN")

# Bitwise / logical (§2.5.2.7).  Trailing underscore: ``and``/``or``/``not``
# are Python keywords.
and_  = _binary("AND")
or_   = _binary("OR")
xor_  = _binary("XOR")
not_  = _single("NOT")

# Bit-string (§2.5.2.6).  Each is binary: SHL(value, count, output).
shl   = _binary("SHL")
shr   = _binary("SHR")
ror   = _binary("ROR")
rol   = _binary("ROL")

# Selection / comparison (§2.5.2.8).  ``max``/``min`` are Python builtins.
max_  = _binary("MAX")
min_  = _binary("MIN")


def sel(g: ValueLike, in0: ValueLike, in1: ValueLike,
        output: LocLike) -> StdFunc:
    """IEC SEL: 2-way selector.  Output := IN1 if G else IN0."""
    return std_func("SEL", [g, in0, in1], output)


def limit(lo: ValueLike, value: ValueLike, hi: ValueLike,
          output: LocLike) -> StdFunc:
    """IEC LIMIT: clamp value to [lo, hi]."""
    return std_func("LIMIT", [lo, value, hi], output)


def mux(k: ValueLike, *inputs: ValueLike,
        output: LocLike) -> StdFunc:
    """IEC MUX: select the K-th of N inputs (K is 0-indexed)."""
    return std_func("MUX", [k, *inputs], output)


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
# Structured Text expression / statement builders (IEC §3)
# -----------------------------------------------------------------------------


ExprLike = Union["Expression", LocLike, int, float, bool]


def _expr(x: ExprLike) -> Expression:
    """Coerce a Python value into an ST ``Expression``.

    Routing:
      - Pre-built ``Expression`` nodes (``Literal``, ``VarRef``, ...)
        pass through.
      - ``Address`` / ``TagRef`` get wrapped as ``VarRef``.
      - ``bool`` (True / False) becomes ``Literal("TRUE"|"FALSE",
        kind="bool")``.
      - ``int`` / ``float`` become numeric ``Literal``.
      - Strings: if they look like an address (CLICK-style or IEC
        direct rep), become ``VarRef(Address)``; otherwise become
        ``VarRef(TagRef)``.  Use ``lit(...)`` to force a literal.

    The smart-string coercion matches the rest of the builders DSL
    so ``assign("count", add_e("count", 1))`` reads naturally.
    """
    if isinstance(x, (Literal, VarRef, FieldAccess, IndexAccess,
                      UnaryExpr, BinaryExpr, FunctionCallExpr)):
        return x
    if isinstance(x, bool):
        return Literal("TRUE" if x else "FALSE", kind="bool")
    if isinstance(x, int):
        return Literal(str(x), kind="int")
    if isinstance(x, float):
        return Literal(repr(x), kind="real")
    if isinstance(x, Address):
        return VarRef(x)
    if isinstance(x, TagRef):
        return VarRef(x)
    if isinstance(x, str):
        return VarRef(_loc(x))
    raise TypeError(f"can't coerce to Expression: {x!r}")


def lit(value, kind: Optional[str] = None) -> Literal:
    """Build an ST ``Literal`` verbatim.

    Use to inject a typed-literal form the smart coercion wouldn't
    pick up: ``lit("T#100ms", kind="time")``, ``lit("16#FF",
    kind="int")``, ``lit("'hello'", kind="string")``.  For plain
    Python int/float/bool, just pass the value through ``_expr``
    -- it'll wrap appropriately.
    """
    if isinstance(value, bool):
        return Literal("TRUE" if value else "FALSE", kind=kind or "bool")
    if isinstance(value, int):
        return Literal(str(value), kind=kind or "int")
    if isinstance(value, float):
        return Literal(repr(value), kind=kind or "real")
    return Literal(str(value), kind=kind or "raw")


def var_ref(x: LocLike) -> VarRef:
    """Build a ``VarRef`` from a name or address.  Bypasses ``_expr``."""
    return VarRef(_loc(x))


def field_(base: ExprLike, name: str) -> FieldAccess:
    """Build a ``FieldAccess``: ``base.name`` (chainable)."""
    return FieldAccess(base=_expr(base), field=name)


def index_(base: ExprLike, *indices: ExprLike) -> IndexAccess:
    """Build an ``IndexAccess``: ``base[i, j, ...]``."""
    return IndexAccess(base=_expr(base),
                       indices=tuple(_expr(i) for i in indices))


def neg(operand: ExprLike) -> UnaryExpr:
    """Arithmetic negation: ``-operand``."""
    return UnaryExpr(op=UnaryOp.NEG, operand=_expr(operand))


def not_e(operand: ExprLike) -> UnaryExpr:
    """Logical/bitwise complement: ``NOT operand``.

    Suffixed ``_e`` (expression) to avoid clashing with ``not_``
    (the StdFunc shortcut)."""
    return UnaryExpr(op=UnaryOp.NOT, operand=_expr(operand))


def _binop(op: BinaryOp):
    def _fn(lhs: ExprLike, rhs: ExprLike) -> BinaryExpr:
        return BinaryExpr(op=op, lhs=_expr(lhs), rhs=_expr(rhs))
    _fn.__name__ = op.name.lower() + "_e"
    return _fn


# Arithmetic / bitwise expression builders.  ``_e`` suffix to
# avoid colliding with the rung-op builders (add / sub / mul / ...).
add_e = _binop(BinaryOp.ADD)
sub_e = _binop(BinaryOp.SUB)
mul_e = _binop(BinaryOp.MUL)
div_e = _binop(BinaryOp.DIV)
mod_e = _binop(BinaryOp.MOD)
exp_e = _binop(BinaryOp.EXP)

# Comparison
eq_e = _binop(BinaryOp.EQ)
ne_e = _binop(BinaryOp.NE)
lt_e = _binop(BinaryOp.LT)
le_e = _binop(BinaryOp.LE)
gt_e = _binop(BinaryOp.GT)
ge_e = _binop(BinaryOp.GE)

# Logical
and_e = _binop(BinaryOp.AND)
or_e  = _binop(BinaryOp.OR)
xor_e = _binop(BinaryOp.XOR)


def fcall_expr(name: str,
               *positional: ExprLike,
               **named: ExprLike) -> FunctionCallExpr:
    """Build a function-call **expression** (value-producing).

    Positional args come first; keyword args become IEC's named-
    parameter form (``DoIt(in := value)``).  Use ``call_stmt`` for
    the side-effecting form (no return-value capture).
    """
    return FunctionCallExpr(
        name=name,
        positional=tuple(_expr(p) for p in positional),
        named=tuple((k, _expr(v)) for k, v in named.items()),
    )


def assign(target: ExprLike, value: ExprLike) -> Assignment:
    """``target := value;`` -- assign a value to a variable.

    ``target`` must be an lvalue (``VarRef`` / ``FieldAccess`` /
    ``IndexAccess`` or a string that coerces to one).  Validation
    catches non-lvalue targets at validate-time."""
    return Assignment(target=_expr(target), value=_expr(value))


def if_(*branches_and_else,
        else_: Optional[Sequence[Statement]] = None) -> IfStatement:
    """Build an ``IF / ELSIF / ELSE / END_IF`` statement.

    Branches are passed as alternating ``(condition, body)`` pairs::

        if_((c1, [s1, s2]),
            (c2, [s3]),
            else_=[s4])

    Body lists are coerced to tuples internally so the dataclass
    stays hashable.
    """
    branch_tuples = tuple(
        (_expr(cond), tuple(body))
        for (cond, body) in branches_and_else
    )
    return IfStatement(
        branches=branch_tuples,
        else_branch=tuple(else_) if else_ is not None else None,
    )


def case_clause(labels: Sequence[ExprLike],
                body: Sequence[Statement]) -> CaseClause:
    """One ``label_list : body`` clause inside a CASE."""
    return CaseClause(
        labels=tuple(_expr(l) for l in labels),
        body=tuple(body),
    )


def case_(selector: ExprLike,
          *clauses: CaseClause,
          else_: Optional[Sequence[Statement]] = None) -> CaseStatement:
    """``CASE selector OF clause+ [ELSE body] END_CASE``."""
    return CaseStatement(
        selector=_expr(selector),
        clauses=tuple(clauses),
        else_branch=tuple(else_) if else_ is not None else None,
    )


def while_(condition: ExprLike,
           body: Sequence[Statement]) -> WhileStatement:
    """``WHILE c DO body END_WHILE``."""
    return WhileStatement(condition=_expr(condition), body=tuple(body))


def repeat_(body: Sequence[Statement],
            until: ExprLike) -> RepeatStatement:
    """``REPEAT body UNTIL c END_REPEAT``."""
    return RepeatStatement(body=tuple(body), until=_expr(until))


def for_(index_var: str,
         start: ExprLike,
         end: ExprLike,
         body: Sequence[Statement],
         step: Optional[ExprLike] = None) -> ForStatement:
    """``FOR i := start TO end [BY step] DO body END_FOR``."""
    return ForStatement(
        index_var=index_var,
        start=_expr(start),
        end=_expr(end),
        body=tuple(body),
        step=_expr(step) if step is not None else None,
    )


def call_stmt(name: str,
              *positional: ExprLike,
              **named: ExprLike) -> FunctionCallStatement:
    """Build a function-call **statement** (side-effecting; result
    discarded).  Same arg shape as ``fcall_expr``."""
    return FunctionCallStatement(
        call=fcall_expr(name, *positional, **named),
    )


def ret_st() -> ReturnStatement:
    """``RETURN;`` -- exit the enclosing POU early.  Spelled
    ``ret_st`` to avoid clashing with ``ret`` (the LD return op)."""
    return ReturnStatement()


def exit_st() -> ExitStatement:
    """``EXIT;`` -- break out of the innermost loop."""
    return ExitStatement()


def continue_st() -> ContinueStatement:
    """``CONTINUE;`` -- skip to the next iteration (IEC 3rd ed.)."""
    return ContinueStatement()


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
              return_type: Optional[DataType] = None,
              sfc=None,
              st_body: Optional[Sequence[Statement]] = None,
              methods: Optional[Sequence[Method]] = None,
              extends: Optional[str] = None,
              implements: Optional[Sequence[str]] = None,
              abstract: bool = False,
              comment: str = "") -> Subroutine:
    return Subroutine(
        name=name, kind=kind, main=main,
        rungs=list(rungs or []),
        inputs=list(inputs or []),
        outputs=list(outputs or []),
        in_outs=list(in_outs or []),
        local_vars=list(local_vars or []),
        return_type=return_type,
        sfc=sfc,
        st_body=list(st_body) if st_body is not None else None,
        comment=comment,
        methods=list(methods or []),
        extends=extends,
        implements=list(implements or []),
        abstract=abstract,
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
    """FUNCTION_BLOCK POU (stateful; state lives in an instance DataBlock).

    Accepts the IEC 3rd-edition OOP additions: ``methods=[...]``,
    ``extends="ParentFB"``, ``implements=["IDrive", "IBraking"]``,
    ``abstract=True`` for an FB that can't be instantiated directly
    (subclasses must override its abstract methods).
    """
    return _make_pou(PouKind.FUNCTION_BLOCK, name, **kw)


# -----------------------------------------------------------------------------
# IEC 3rd-edition OOP: methods + interfaces (§2.5.1.5)
# -----------------------------------------------------------------------------


def method(name: str, *,
           rungs: Optional[Sequence[Rung]] = None,
           st_body: Optional[Sequence[Statement]] = None,
           inputs: Optional[Sequence[Var]] = None,
           outputs: Optional[Sequence[Var]] = None,
           in_outs: Optional[Sequence[Var]] = None,
           local_vars: Optional[Sequence[Var]] = None,
           return_type: Optional[DataType] = None,
           access: AccessSpec = AccessSpec.PUBLIC,
           override: bool = False,
           comment: str = "") -> Method:
    """Declare a concrete method on a FUNCTION_BLOCK (IEC §2.5.1.5).

    A method has the same parameter shape as a FUNCTION plus an
    access specifier (PUBLIC by default) and an ``override=True``
    flag when it implements a parent FB's or interface's signature.
    The body is either ``rungs`` (LD) or ``st_body`` (ST); pass
    exactly one.  The method has implicit access to the enclosing
    FB's state."""
    return Method(
        name=name,
        rungs=list(rungs or []),
        st_body=list(st_body) if st_body is not None else None,
        inputs=list(inputs or []),
        outputs=list(outputs or []),
        in_outs=list(in_outs or []),
        local_vars=list(local_vars or []),
        return_type=return_type,
        access=access,
        abstract=False,
        override=override,
        comment=comment,
    )


def abstract_method(name: str, *,
                    inputs: Optional[Sequence[Var]] = None,
                    outputs: Optional[Sequence[Var]] = None,
                    in_outs: Optional[Sequence[Var]] = None,
                    return_type: Optional[DataType] = None,
                    access: AccessSpec = AccessSpec.PUBLIC,
                    comment: str = "") -> Method:
    """Declare an abstract method signature.

    Use this for interface methods (an ``Interface`` collects
    abstract-method signatures) and for declaring abstract slots on
    an ``abstract=True`` FUNCTION_BLOCK.  Has no body."""
    return Method(
        name=name,
        rungs=[],
        inputs=list(inputs or []),
        outputs=list(outputs or []),
        in_outs=list(in_outs or []),
        local_vars=[],
        return_type=return_type,
        access=access,
        abstract=True,
        override=False,
        comment=comment,
    )


def interface(name: str, *,
              methods: Optional[Sequence[Method]] = None,
              comment: str = "") -> Interface:
    """Declare an IEC ``INTERFACE`` -- an abstract contract of method
    signatures that ``FUNCTION_BLOCK``s ``IMPLEMENT``.

    All methods on an interface should be abstract (signatures only,
    no body); use ``abstract_method(...)`` to construct them."""
    return Interface(name=name,
                     methods=list(methods or []),
                     comment=comment)


# -----------------------------------------------------------------------------
# User-defined type helpers (IEC 61131-3 §2.3.3)
# -----------------------------------------------------------------------------


def named_type(name: str) -> NamedType:
    """Reference an existing UDT by name (for use as a Var.data_type
    or as a struct field type)."""
    return NamedType(name=name)


def struct_type(name: str, members: Sequence[Var],
                comment: str = "") -> StructType:
    """Declare a STRUCT user-defined type.

    Members are ``Var`` instances -- typically created with ``var(...)``,
    ``var_in(...)``, etc.  Direction defaults to LOCAL for struct
    fields (the schema-level direction concept is for POU parameters,
    not struct members)."""
    return StructType(name=name, members=tuple(members), comment=comment)


def array_type(name: str,
               element_type: DataType,
               bounds: Sequence[tuple[int, int]],
               comment: str = "") -> ArrayType:
    """Declare an ARRAY user-defined type.

    ``bounds`` is a sequence of ``(lo, hi)`` pairs -- one per
    dimension.  Single-dimensional: ``bounds=[(0, 9)]`` gives a
    10-element array.  Multi-dimensional: ``bounds=[(0, 2), (0, 2)]``
    gives a 3x3 array."""
    return ArrayType(name=name, element_type=element_type,
                     bounds=tuple(bounds), comment=comment)


def enum_type(name: str, values: Sequence[str],
              comment: str = "") -> EnumType:
    """Declare an ENUM user-defined type with the given symbolic values."""
    return EnumType(name=name, values=tuple(values), comment=comment)


def alias_type(name: str, base: DataType,
               comment: str = "") -> AliasType:
    """Declare a SIMPLE / ALIAS user-defined type.

    The alias renames an underlying ``DataType`` -- elementary or
    user-defined -- without changing its runtime representation.
    Useful for giving domain-meaningful names (``Distance``,
    ``Velocity``)."""
    return AliasType(name=name, base=base, comment=comment)


def subrange_type(name: str, base: DataType,
                  lower: int, upper: int,
                  comment: str = "") -> SubrangeType:
    """Declare a SUBRANGE user-defined type (IEC §2.3.3.1).

    Restricts an integer ``base`` (or NamedType pointing at one) to
    the inclusive range ``[lower, upper]``::

        subrange_type("SmallInt", TagType.INT,  lower=-100, upper=100)
        subrange_type("Percent",  TagType.UINT, lower=0,    upper=100)

    PLCopen XML emission picks ``<subrangeSigned>`` vs
    ``<subrangeUnsigned>`` based on whether ``base`` is a signed or
    unsigned IEC integer type."""
    return SubrangeType(name=name, base=base,
                        lower=lower, upper=upper, comment=comment)


# -----------------------------------------------------------------------------
# CONFIGURATION / RESOURCE / TASK (IEC 61131-3 §2.7)
# -----------------------------------------------------------------------------


def task_spec(name: str, *,
              priority: int = 1,
              interval: Optional[str] = None,
              single: Optional[str] = None,
              interrupt: Optional[str] = None,
              comment: str = "") -> TaskSpec:
    """Build a ``TaskSpec`` for resource scheduling.

    Exactly one of ``interval`` (cyclic), ``single`` (single-shot), or
    ``interrupt`` (interrupt-driven) should be set per IEC §2.7.2.
    The DSL doesn't enforce mutex -- callers and validation passes
    do.
    """
    return TaskSpec(
        name=name, priority=priority,
        interval=interval, single=single, interrupt=interrupt,
        comment=comment,
    )


def pou_instance(name: str, type_name: str, *,
                 task: Optional[str] = None,
                 comment: str = "") -> PouInstance:
    """A runtime instance of a POU bound to a task."""
    return PouInstance(name=name, type_name=type_name, task=task,
                       comment=comment)


def resource(name: str, *,
             tasks: Optional[Sequence[TaskSpec]] = None,
             pou_instances: Optional[Sequence[PouInstance]] = None,
             global_vars: Optional[Sequence[Var]] = None,
             comment: str = "") -> Resource:
    """Build a ``Resource`` -- one PLC CPU / runtime."""
    return Resource(
        name=name,
        tasks=list(tasks or []),
        pou_instances=list(pou_instances or []),
        global_vars=list(global_vars or []),
        comment=comment,
    )


def configuration(name: str, *,
                  resources: Optional[Sequence[Resource]] = None,
                  global_vars: Optional[Sequence[Var]] = None,
                  access_vars: Optional[Sequence[Var]] = None,
                  comment: str = "") -> Configuration:
    """Build a ``Configuration`` -- top-level system organisation.

    Use one Configuration per project; multi-PLC projects use multiple
    Resources inside the same Configuration."""
    return Configuration(
        name=name,
        resources=list(resources or []),
        global_vars=list(global_vars or []),
        access_vars=list(access_vars or []),
        comment=comment,
    )


# -----------------------------------------------------------------------------
# Top-level Program builder
# -----------------------------------------------------------------------------


def program(*,
            subroutines: Optional[Sequence[Subroutine]] = None,
            tags: Optional[Sequence[Tag]] = None,
            data_blocks: Optional[Sequence[DataBlock]] = None,
            user_types: Optional[Sequence[UserType]] = None,
            configurations: Optional[Sequence[Configuration]] = None,
            interfaces: Optional[Sequence[Interface]] = None,
            cpu_model: str = "",
            project_name: str = "",
            comment: str = "") -> Program:
    """Build a complete ``Program``.

    ``tags`` is a flat list; the constructor keys them by name into
    ``Program.tags``.  Pass POUs in any order -- ``main_subroutine``
    discrimination is by ``Subroutine.main`` flag, not list position.

    ``user_types`` collects ``StructType``/``ArrayType``/``EnumType``/
    ``AliasType`` declarations; the constructor stores them on
    ``Program.user_types`` and emitters render them in IEC's
    ``TYPE ... END_TYPE`` block.

    ``configurations`` collects IEC §2.7 system-organisation
    Configuration objects -- each holds Resources, Tasks, POU
    instances, and global variables.  When a Program declares
    Configurations explicitly, the PLCopen XML emitter produces the
    proper ``<instances><configurations>`` structure; without them,
    a synthetic ``GlobalsHolder`` POU is used as a fallback for
    Tag declarations.
    """
    return Program(
        subroutines=list(subroutines or []),
        tags={t.name: t for t in (tags or [])},
        data_blocks=list(data_blocks or []),
        user_types=list(user_types or []),
        configurations=list(configurations or []),
        interfaces=list(interfaces or []),
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
    # IEC bistables / edge triggers
    "r_trig", "f_trig", "sr", "rs",
    # IEC stdlib (generic + named)
    "std_func",
    "abs_", "sqrt", "ln", "log", "exp",
    "sin", "cos", "tan", "asin", "acos", "atan",
    "and_", "or_", "xor_", "not_",
    "shl", "shr", "ror", "rol",
    "max_", "min_", "sel", "limit", "mux",
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
    # User-defined types
    "named_type", "struct_type", "array_type", "enum_type", "alias_type",
    "subrange_type",
    # Configuration / Resource / Task
    "task_spec", "pou_instance", "resource", "configuration",
    # POUs
    "subroutine", "prog", "fn", "fb",
    # IEC 3rd-edition OOP
    "method", "abstract_method", "interface",
    # ST expression / statement helpers (IEC §3)
    "lit", "var_ref", "field_", "index_",
    "neg", "not_e",
    "add_e", "sub_e", "mul_e", "div_e", "mod_e", "exp_e",
    "eq_e", "ne_e", "lt_e", "le_e", "gt_e", "ge_e",
    "and_e", "or_e", "xor_e",
    "fcall_expr", "assign",
    "if_", "case_clause", "case_",
    "while_", "repeat_", "for_",
    "call_stmt", "ret_st", "exit_st", "continue_st",
    # Program
    "program",
]
