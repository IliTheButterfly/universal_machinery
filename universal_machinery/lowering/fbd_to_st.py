"""Lower an FBD body to an equivalent list of ST statements.

PLCopen-conformant FBD bodies are graphs of wired elements; most
vendor backends (CLICK, OpenPLC, matiec-based targets) accept ST
but not FBD.  This pass walks an ``FbdNetwork`` in topological
order and synthesises ST statements + the temporary variable
declarations needed to thread block outputs through wires.

Algorithm
---------

Each FBD wire carries a value from one producer pin to one or
more consumer pins.  In ST we need to materialise that value as
a textual expression -- either:

  - **Inline expression** for stateless function calls with a
    single output that's used in exactly one place.  ``ADD(x, y)``
    expands inline as ``x + y`` (-> already a ``BinaryExpr``).
  - **Temporary variable** for stateful FB calls (whose outputs
    are accessed as ``Instance.PinName``) or multi-use producers.

Walk order
~~~~~~~~~~

Topological sort over the wire DAG: a node fires after all its
input producers.  Cycles are an authoring error (FBD without
explicit state-holding FBs is acyclic by construction); we
report them as a lowering error.

What each kind emits
~~~~~~~~~~~~~~~~~~~~

  InVariable     : nothing.  Its ``expression`` becomes the source
                    operand for downstream wires.
  FbBlock (FB)   : ``Inst(IN1 := <src>, IN2 := <src>);``.  Each
                    output pin becomes ``Inst.PinName`` for
                    consumers; if consumers reference an output,
                    we emit ``_t<n> := Inst.PinName;`` so the
                    expression is a stable name.
  FbBlock (Fn)   : single-output stateless functions become
                    ``_t<n> := Func(<src1>, <src2>);`` (in-place
                    expressions are still readable but uniform
                    temps keep the lowering predictable).
  OutVariable    : ``<expression> := <src>;``
  InOutVariable  : ``<expression> := <src>;`` -- equivalent to
                    an assignment with the variable cell as both
                    sink and downstream source.
  FbdLabel       : ``<label>:`` (jump target).
  FbdJump        : ``IF <gate-src> THEN GOTO <label>; END_IF;``
                    or unconditional ``GOTO <label>;`` if no gate.
  FbdReturn      : ``IF <gate-src> THEN RETURN; END_IF;``
                    or unconditional ``RETURN;`` if no gate.

Returns
~~~~~~~

A tuple ``(statements, temp_vars)``.  The caller injects
``temp_vars`` into the POU's ``local_vars`` (so the resulting ST
is well-formed) and uses ``statements`` as the body.

Limitations (deferred slices)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

  - No type inference: temp vars are declared ``INT`` (a sane
    default for the common case; a follow-up type-resolver pass
    will pick the right type per producer pin).
  - Stateless functions known to have an algebraic identity
    (``ADD`` → ``+``, ``AND`` → ``AND``, ...) could lower as
    BinaryExpr; for now they all go through ``FunctionCallExpr``.
  - Execution-order attributes (``executionOrderId``) are honoured
    when present (used as a tie-breaker in the topo sort).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..il import (
    Assignment, BinaryExpr, BinaryOp, CommentStatement, FbBlock, FbdJump,
    FbdLabel, FbdNetwork, FbdReturn, FunctionCallExpr,
    FunctionCallStatement, GotoStatement, IfStatement, InOutVariable,
    InVariable, LabelStatement, Literal, OutVariable, ReturnStatement,
    Statement, TagType, TagRef, UnaryExpr, UnaryOp, Var, VarDirection,
    VarRef,
)
from ..il.ast import Address


class LoweringError(Exception):
    """Raised when an FBD network can't be lowered to ST -- typically
    cycles in the wire graph or references to undeclared elements.
    Validation should catch most of these before lowering runs."""


# -----------------------------------------------------------------------------
# Producer-expression resolver
# -----------------------------------------------------------------------------


def _expr_from_text(text: str):
    """Turn an IEC-textual operand (``"x"``, ``"3.14"``,
    ``"TRUE"``) into an ST expression node.

    Treats the text as a variable name unless it parses as a
    numeric literal or a known boolean keyword; matches the
    builder's smart-string coercion behaviour.
    """
    s = text.strip()
    if not s:
        return Literal("", kind="raw")
    if s in ("TRUE", "FALSE"):
        return Literal(s, kind="bool")
    # Numeric?
    try:
        int(s)
        return Literal(s, kind="int")
    except ValueError:
        pass
    try:
        float(s)
        return Literal(s, kind="real")
    except ValueError:
        pass
    # Address-like (CLICK-style "X001" / IEC "%I0.0")
    if (s and s[0] == "%") or (s and s[0].isupper() and any(c.isdigit() for c in s)):
        # Conservative: only treat all-uppercase letters + digits as an
        # address; otherwise treat as a tag name.
        head = s.rstrip("0123456789.")
        if s.startswith("%") or (head.isalpha() and head.isupper()):
            return VarRef(Address(s))
    return VarRef(TagRef(s))


def _negate(expr):
    """Wrap an expression in NOT (unary)."""
    return UnaryExpr(op=UnaryOp.NOT, operand=expr)


# -----------------------------------------------------------------------------
# Topological sort
# -----------------------------------------------------------------------------


def _producers(element):
    """Return the local_ids this element depends on (incoming wires).

    ``InVariable`` and ``FbdLabel`` have no dependencies; everything
    else points back at zero-or-more producers via Connection.
    """
    deps: list[int] = []
    if isinstance(element, FbBlock):
        for p in element.inputs:
            if p.connection is not None:
                deps.append(p.connection.source_id)
        for p in element.in_outs:
            if p.connection is not None:
                deps.append(p.connection.source_id)
    elif isinstance(element, (OutVariable, InOutVariable, FbdJump, FbdReturn)):
        if element.connection is not None:
            deps.append(element.connection.source_id)
    # InVariable / FbdLabel have no inputs
    return deps


def _topological_order(net: FbdNetwork) -> list:
    """Kahn's-algorithm topo sort, breaking ties by ``executionOrderId``
    (when set) then ``local_id``.

    Cycles raise ``LoweringError``.  Authoring tools always produce
    acyclic FBD bodies for the simple case; explicit state holders
    (R_TRIG / SR / TON / ...) break loops at the block boundary
    because their inputs depend on the previous scan, not this one
    -- but our wire graph doesn't know that.  A follow-up slice
    can mark "scan-breaking" pin sets per FB type.
    """
    by_id = {e.local_id: e for e in net.elements}
    indeg = {e.local_id: 0 for e in net.elements}
    succ: dict[int, list[int]] = {e.local_id: [] for e in net.elements}

    for e in net.elements:
        for src_id in _producers(e):
            if src_id not in by_id:
                # Unresolved connection -- validation should've caught
                # this; we treat the wire as nonexistent here.
                continue
            indeg[e.local_id] += 1
            succ[src_id].append(e.local_id)

    def _key(eid: int) -> tuple:
        e = by_id[eid]
        return (getattr(e, "execution_order", None) or 1_000_000_000, eid)

    # Initial frontier: indeg == 0
    ready = sorted([eid for eid, d in indeg.items() if d == 0], key=_key)
    out: list = []
    while ready:
        eid = ready.pop(0)
        out.append(by_id[eid])
        for v in succ[eid]:
            indeg[v] -= 1
            if indeg[v] == 0:
                # Insert in sorted order
                k = _key(v)
                lo, hi = 0, len(ready)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if _key(ready[mid]) < k:
                        lo = mid + 1
                    else:
                        hi = mid
                ready.insert(lo, v)

    if len(out) != len(net.elements):
        remaining = [eid for eid in by_id if eid not in {e.local_id for e in out}]
        raise LoweringError(
            f"FBD network contains a cycle (or unresolved deps); "
            f"unable to order elements {remaining}"
        )
    return out


# -----------------------------------------------------------------------------
# Lowering core
# -----------------------------------------------------------------------------


@dataclass
class LoweringResult:
    """Output of ``lower_fbd_to_st``.

    ``statements`` is the synthesised ST body; ``temp_vars`` lists
    any local variables introduced for block outputs (the caller
    should merge these into the POU's ``local_vars``).
    """
    statements: list[Statement] = field(default_factory=list)
    temp_vars: list[Var] = field(default_factory=list)


#: Block ``type_name``s that map to native ST binary operators.
#: When a block of this kind has exactly two inputs + one output,
#: we lower its output as an inline ``BinaryExpr`` rather than a
#: function call -- the resulting ST is more idiomatic and
#: matches the form a hand-authored IEC program would use.
_BINARY_OP_BLOCKS = {
    "ADD": BinaryOp.ADD,
    "SUB": BinaryOp.SUB,
    "MUL": BinaryOp.MUL,
    "DIV": BinaryOp.DIV,
    "MOD": BinaryOp.MOD,
    "AND": BinaryOp.AND,
    "OR":  BinaryOp.OR,
    "XOR": BinaryOp.XOR,
    "GT":  BinaryOp.GT,
    "GE":  BinaryOp.GE,
    "LT":  BinaryOp.LT,
    "LE":  BinaryOp.LE,
    "EQ":  BinaryOp.EQ,
    "NE":  BinaryOp.NE,
}


def lower_fbd_to_st(net: FbdNetwork, *,
                    temp_prefix: str = "_t") -> LoweringResult:
    """Translate ``net`` to a list of ST statements + temp Vars.

    Walks the network in topological order.  Stateless single-
    output / two-input blocks whose ``type_name`` is in
    ``_BINARY_OP_BLOCKS`` lower as ``BinaryExpr``s; everything
    else goes through a temporary variable assignment.
    """
    by_id = {e.local_id: e for e in net.elements}
    order = _topological_order(net)

    # Per (local_id, pin_name) producer reference expression.
    # For in-variables, the pin name is the empty string (single
    # implicit output pin).
    producer_expr: dict[tuple[int, str], object] = {}
    statements: list[Statement] = []
    temp_vars: list[Var] = []
    temp_counter = 0

    def _get_source(conn) -> Optional[object]:
        if conn is None:
            return None
        key = (conn.source_id, conn.source_pin or "")
        return producer_expr.get(key)

    def _new_temp(hint: str = "") -> str:
        nonlocal temp_counter
        name = f"{temp_prefix}{temp_counter}"
        if hint:
            name = f"{temp_prefix}{temp_counter}_{hint}"
        temp_counter += 1
        return name

    for e in order:
        if isinstance(e, InVariable):
            expr = _expr_from_text(e.expression)
            if e.negated:
                expr = _negate(expr)
            producer_expr[(e.local_id, "")] = expr

        elif isinstance(e, FbBlock):
            input_exprs: list[tuple[str, object]] = []
            for p in e.inputs:
                src_expr = _get_source(p.connection)
                if src_expr is None:
                    # Pin left unwired -- use a "0" placeholder so the
                    # output ST is parseable.  Validation should catch
                    # this in practice.
                    src_expr = Literal("0", kind="int")
                if p.negated:
                    src_expr = _negate(src_expr)
                input_exprs.append((p.formal_parameter, src_expr))

            is_fb_call = e.instance_name is not None
            is_binary_op = (
                not is_fb_call
                and e.type_name in _BINARY_OP_BLOCKS
                and len(e.inputs) == 2
                and len(e.outputs) == 1
                and not e.in_outs
            )

            if is_binary_op:
                # Inline ``BinaryExpr``.  No statement emitted; the
                # output is just an expression downstream consumers
                # can paste in.
                op = _BINARY_OP_BLOCKS[e.type_name]
                lhs = input_exprs[0][1]
                rhs = input_exprs[1][1]
                producer_expr[(e.local_id, e.outputs[0].formal_parameter)] = (
                    BinaryExpr(op=op, lhs=lhs, rhs=rhs)
                )

            elif is_fb_call:
                # ``Inst(IN1 := src1, IN2 := src2);`` emitted as a
                # FunctionCallStatement-flavoured call.  Outputs are
                # accessed as ``Inst.PinName`` by downstream wires.
                call = FunctionCallExpr(
                    name=e.instance_name,
                    positional=(),
                    named=tuple(input_exprs),
                )
                statements.append(FunctionCallStatement(call=call))
                # Each output pin becomes ``Inst.PinName`` for
                # downstream consumers.
                for p in e.outputs:
                    field_ref = VarRef(TagRef(f"{e.instance_name}.{p.formal_parameter}"))
                    out_expr = _negate(field_ref) if p.negated else field_ref
                    producer_expr[(e.local_id, p.formal_parameter)] = out_expr
                for p in e.in_outs:
                    field_ref = VarRef(TagRef(f"{e.instance_name}.{p.formal_parameter}"))
                    producer_expr[(e.local_id, p.formal_parameter)] = field_ref

            else:
                # Stateless function call: ``tmp := Func(args);``
                # Each output pin gets its own temp.
                call = FunctionCallExpr(
                    name=e.type_name,
                    positional=tuple(arg for _, arg in input_exprs),
                    named=(),
                )
                if len(e.outputs) == 1:
                    pin = e.outputs[0]
                    tmp = _new_temp(pin.formal_parameter.lower())
                    temp_vars.append(Var(name=tmp, data_type=TagType.INT,
                                          direction=VarDirection.LOCAL))
                    statements.append(Assignment(
                        target=VarRef(TagRef(tmp)),
                        value=call,
                    ))
                    out_expr = VarRef(TagRef(tmp))
                    if pin.negated:
                        out_expr = _negate(out_expr)
                    producer_expr[(e.local_id, pin.formal_parameter)] = out_expr
                elif len(e.outputs) == 0:
                    # No outputs -- call only matters for side effects
                    # (rare for stateless; treat as a statement).
                    statements.append(FunctionCallStatement(call=call))
                else:
                    # Multi-output stateless function: route each
                    # output to a named temp via the function's
                    # convention.  IEC ST has no clean way to
                    # express this without ENO; emit the call as a
                    # statement and use ``FuncName.PinName`` (the
                    # function's "output" pseudo-fields).  Most
                    # multi-output things in practice are FBs anyway.
                    statements.append(FunctionCallStatement(call=call))
                    for p in e.outputs:
                        field_ref = VarRef(TagRef(f"{e.type_name}.{p.formal_parameter}"))
                        out_expr = _negate(field_ref) if p.negated else field_ref
                        producer_expr[(e.local_id, p.formal_parameter)] = out_expr

        elif isinstance(e, OutVariable):
            src_expr = _get_source(e.connection)
            if src_expr is None:
                src_expr = Literal("0", kind="int")
            if e.negated:
                src_expr = _negate(src_expr)
            target_expr = _expr_from_text(e.expression)
            statements.append(Assignment(target=target_expr, value=src_expr))

        elif isinstance(e, InOutVariable):
            src_expr = _get_source(e.connection)
            if src_expr is None:
                src_expr = Literal("0", kind="int")
            if e.negated_in:
                src_expr = _negate(src_expr)
            target_expr = _expr_from_text(e.expression)
            statements.append(Assignment(target=target_expr, value=src_expr))
            # InOutVariable is also a producer downstream; the
            # output side sees the variable's current value.
            out_expr = _expr_from_text(e.expression)
            if e.negated_out:
                out_expr = _negate(out_expr)
            producer_expr[(e.local_id, "")] = out_expr

        elif isinstance(e, FbdLabel):
            statements.append(LabelStatement(name=e.label))

        elif isinstance(e, FbdJump):
            gate = _get_source(e.connection)
            jump = GotoStatement(label=e.label)
            if gate is None:
                statements.append(jump)
            else:
                statements.append(IfStatement(
                    branches=((gate, (jump,)),),
                    else_branch=None,
                ))

        elif isinstance(e, FbdReturn):
            gate = _get_source(e.connection)
            ret = ReturnStatement()
            if gate is None:
                statements.append(ret)
            else:
                statements.append(IfStatement(
                    branches=((gate, (ret,)),),
                    else_branch=None,
                ))

    return LoweringResult(statements=statements, temp_vars=temp_vars)
