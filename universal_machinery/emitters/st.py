"""IEC 61131-3 §3 Structured Text emitter.

Walks the IL and produces ST source text.  Three entry points:

  ``emit_rung(rung)``     -> ``list[str]`` of ST statements
  ``emit_pou(sub)``       -> ``str`` (full POU: keyword, interface, body)
  ``emit_program(prog)``  -> ``str`` (VAR_GLOBAL tags + POUs)

Translation rules
-----------------

The IL's rung model has a contact prefix (inputs that AND together,
possibly with ``ParallelGroup`` ORs) gating one-or-more output ops.
The ST equivalent of a rung is:

  - Build the gate expression from the input ops.
  - For each output op, emit an ST statement.  Coil-like outputs
    (``OutCoil``) assign the gate expression directly.  Other outputs
    (Move, BinaryMath, Call, StdFunc, ...) are wrapped in
    ``IF <gate> THEN ... END_IF;`` unless the gate is unconditional.

The result is structurally equivalent to the rung -- a vendor ST
compiler reading the output produces the same control logic as the
IL evaluator running the rung.

Limitations
-----------

  - Edge contacts (``ContactRisingEdge`` / ``ContactFallingEdge``) and
    the dedicated FB ops (``TON``, ``CTU``, ``RTrig``, ``SR``, ...) need
    explicit instance variables in ST.  This first cut emits a
    comment placeholder for them; a follow-up pass will synthesise
    the instance declarations into the POU's VAR section and emit
    the canonical ST ``Inst(IN := ..., PT := ..., Q => ...);`` form.
  - ``VendorOp`` has no ST equivalent and is emitted as a comment
    naming the vendor + op name.
  - ``SfcNetwork`` bodies are not yet translated (IEC ST has no
    direct SFC representation; that uses the SFC body type in
    PLCopen XML, not ST).
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence, Union

from ..il import (
    AccessSpec, Address, AliasType, ArrayType, Assignment, BinaryExpr,
    CaseStatement, CommentStatement, Configuration, ContinueStatement,
    DataBlock, EnumType, ExitStatement, FieldAccess, ForStatement,
    FunctionCallExpr, FunctionCallStatement, GotoStatement, IfStatement,
    IndexAccess, Interface, LabelStatement, Literal, Method, NamedType,
    PouInstance, PouKind, Program, RepeatStatement, Resource,
    ReturnStatement, Rung, Statement, StructType, SubrangeType, Subroutine,
    Tag, TagRef, TagType, TaskSpec, UnaryExpr, Var, VarDirection, VarRef,
    WhileStatement, type_name,
)
from ..il.ops import (
    BinaryMath, Call, Compare, ContactFallingEdge, ContactNC, ContactNO,
    ContactRisingEdge, CTD, CTU, CTUD, End, FTrig, Jump, Label, Move, OutCoil,
    OutReset, OutSet, ParallelGroup, RS, RTrig, Return, SR, StdFunc, TOF, TON,
    TP, VendorOp,
)


#: Ops that contribute to the rung's *gate* expression (boolean
#: inputs).  Anything else is treated as an output.
_INPUT_OPS = (
    ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
    Compare, ParallelGroup,
)


# -----------------------------------------------------------------------------
# Operand formatting
# -----------------------------------------------------------------------------


def _fmt_value(v) -> str:
    """Format a Value (Address / TagRef / literal) as an ST operand."""
    if isinstance(v, Address):
        return v.raw
    if isinstance(v, TagRef):
        return v.name
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    raise TypeError(f"can't format value: {v!r}")


def _fmt_iec_type(t) -> str:
    """IEC ST type name for any ``DataType``.

    Elementary types (``TagType``) render as their IEC keyword
    (``INT``, ``BOOL``, ``REAL``, ...).  User-defined types
    (``NamedType``, ``StructType``, ``ArrayType``, ``EnumType``,
    ``AliasType``) render as the type's name; the type itself must be
    declared in ``Program.user_types`` and emitted via
    ``_fmt_user_type_decl`` for the resulting ST to compile.
    """
    return type_name(t)


# -----------------------------------------------------------------------------
# Gate expression formatting
# -----------------------------------------------------------------------------


def _fmt_term(op) -> str:
    """Format one input op as a boolean ST sub-expression."""
    if isinstance(op, ContactNO):
        return _fmt_value(op.address)
    if isinstance(op, ContactNC):
        return f"NOT {_fmt_value(op.address)}"
    if isinstance(op, ContactRisingEdge):
        # In conformant ST this is an R_TRIG instance: <inst>(CLK := X).Q
        # A follow-up pass will synthesise the instance.  For now,
        # use a transparent helper-call form so the structure is
        # preserved verbatim.
        return f"R_EDGE({_fmt_value(op.address)})"
    if isinstance(op, ContactFallingEdge):
        return f"F_EDGE({_fmt_value(op.address)})"
    if isinstance(op, Compare):
        return f"({_fmt_value(op.lhs)} {op.op} {_fmt_value(op.rhs)})"
    if isinstance(op, ParallelGroup):
        branches = [_fmt_branch(b) for b in op.branches]
        return "(" + " OR ".join(branches) + ")"
    raise ValueError(f"not an input op: {type(op).__name__}")


def _fmt_branch(branch_ops: Sequence) -> str:
    """A parallel branch is an AND-chain of input terms."""
    if not branch_ops:
        return "FALSE"
    return " AND ".join(_fmt_term(op) for op in branch_ops)


def _fmt_gate(gate_ops: Sequence) -> str:
    """AND-join all gate ops; ``TRUE`` if empty."""
    if not gate_ops:
        return "TRUE"
    return " AND ".join(_fmt_term(op) for op in gate_ops)


# -----------------------------------------------------------------------------
# Output formatting
# -----------------------------------------------------------------------------


def _wrap_if(stmt: str, gate: str) -> list[str]:
    """Wrap a statement in ``IF gate THEN ... END_IF;`` unless
    the gate is unconditionally true."""
    if gate == "TRUE":
        return [stmt]
    return [f"IF {gate} THEN {stmt} END_IF;"]


def _fmt_output(op, gate: str) -> list[str]:
    """Emit ST for one output op gated by ``gate``."""

    if isinstance(op, OutCoil):
        return [f"{_fmt_value(op.address)} := {gate};"]

    if isinstance(op, OutSet):
        return _wrap_if(f"{_fmt_value(op.address)} := TRUE;", gate)

    if isinstance(op, OutReset):
        return _wrap_if(f"{_fmt_value(op.address)} := FALSE;", gate)

    if isinstance(op, Move):
        return _wrap_if(
            f"{_fmt_value(op.dst)} := {_fmt_value(op.src)};", gate,
        )

    if isinstance(op, BinaryMath):
        return _wrap_if(
            f"{_fmt_value(op.dst)} := "
            f"{_fmt_value(op.lhs)} {op.op} {_fmt_value(op.rhs)};",
            gate,
        )

    if isinstance(op, StdFunc):
        in_str = ", ".join(_fmt_value(v) for v in op.inputs)
        return _wrap_if(
            f"{_fmt_value(op.output)} := {op.name}({in_str});", gate,
        )

    if isinstance(op, Call):
        return _wrap_if(_fmt_call(op), gate)

    if isinstance(op, Return):
        return _wrap_if("RETURN;", gate)

    if isinstance(op, End):
        # Body ends at the end of the POU in ST; no explicit statement.
        return []

    if isinstance(op, Jump):
        return _wrap_if(f"GOTO {op.label};", gate)

    if isinstance(op, Label):
        return [f"{op.name}:"]

    # Stateful FBs.  Conformant ST requires an instance variable per
    # use site; this first cut emits a structural-comment form so
    # the rung's intent is preserved verbatim.  A follow-up pass
    # will synthesise instance declarations + canonical ST calls.
    if isinstance(op, (TON, TOF, TP)):
        kind = type(op).__name__
        return _wrap_if(
            f"(* {kind}: {_fmt_value(op.address)} PT:=T#{op.preset_ms}ms *)",
            gate,
        )

    if isinstance(op, (CTU, CTD)):
        kind = type(op).__name__
        return _wrap_if(
            f"(* {kind}: {_fmt_value(op.address)} PV:={op.preset} *)",
            gate,
        )

    if isinstance(op, CTUD):
        return _wrap_if(
            f"(* CTUD: {_fmt_value(op.address)} PV:={op.preset} *)", gate,
        )

    if isinstance(op, RTrig):
        return _wrap_if(
            f"(* R_TRIG: state={_fmt_value(op.state)} "
            f"clk={_fmt_value(op.clk)} q={_fmt_value(op.q)} *)",
            gate,
        )

    if isinstance(op, FTrig):
        return _wrap_if(
            f"(* F_TRIG: state={_fmt_value(op.state)} "
            f"clk={_fmt_value(op.clk)} q={_fmt_value(op.q)} *)",
            gate,
        )

    if isinstance(op, SR):
        return _wrap_if(
            f"(* SR: q1={_fmt_value(op.q1)} "
            f"s1={_fmt_value(op.s1)} r={_fmt_value(op.r)} *)",
            gate,
        )

    if isinstance(op, RS):
        return _wrap_if(
            f"(* RS: q1={_fmt_value(op.q1)} "
            f"r1={_fmt_value(op.r1)} s={_fmt_value(op.s)} *)",
            gate,
        )

    if isinstance(op, VendorOp):
        return [f"(* VendorOp {op.vendor}:{op.name} -- "
                f"no ST equivalent *)"]

    raise ValueError(f"don't know how to emit ST for: {type(op).__name__}")


def _fmt_call(op: Call) -> str:
    """Format a ``Call`` op as an ST function/FB invocation.

    Forms covered:
      bare:           ``Target();``
      function:       ``ret := Target(in := src);``
      FB w/ outputs:  ``Inst(in := src, out => dst);``
    """
    parts: list[str] = []
    for name, src in op.inputs:
        parts.append(f"{name} := {_fmt_value(src)}")
    for name, dst in op.outputs:
        parts.append(f"{name} => {_fmt_value(dst)}")
    args = ", ".join(parts)

    # FB call site with an explicit instance binds against the
    # instance, not the POU directly.  ST syntax: <Inst>(args).
    invocation_target = (
        _fmt_value(op.instance) if op.instance is not None else op.target
    )

    if op.return_to is not None:
        return f"{_fmt_value(op.return_to)} := {invocation_target}({args});"
    return f"{invocation_target}({args});"


# -----------------------------------------------------------------------------
# Rung emitter
# -----------------------------------------------------------------------------


def _split_gate_outputs(ops: Sequence) -> tuple[list, list]:
    """Split a rung's ops into (gate, outputs).

    The gate is the leading run of input-type ops; everything after is
    treated as an output.  This matches how every lowering pass in the
    project structures rung handling.
    """
    gate: list = []
    i = 0
    for op in ops:
        if isinstance(op, _INPUT_OPS):
            gate.append(op)
            i += 1
        else:
            break
    return gate, list(ops[i:])


def emit_rung(rung: Rung) -> list[str]:
    """Return ST statements equivalent to ``rung``'s logic.

    A rung with N outputs typically becomes N statements (one per
    output, each gated by the rung's contact prefix).  An empty
    rung returns an empty list.  Rung comment, if any, is emitted
    as an ST comment ahead of the statements.
    """
    statements: list[str] = []
    if rung.comment:
        statements.append(f"(* {rung.comment} *)")

    gate_ops, output_ops = _split_gate_outputs(rung.ops)
    gate = _fmt_gate(gate_ops)
    for op in output_ops:
        statements.extend(_fmt_output(op, gate))
    return statements


# -----------------------------------------------------------------------------
# ST AST emitter (IEC §3 Structured Text -- first-class body kind)
# -----------------------------------------------------------------------------


#: Precedence of each ST binary operator (IEC §3.3.1 Table 55).
#: Higher number = binds tighter.  Unary ops (NEG / NOT) sit between
#: EXP (highest binary) and the rest of binary -- modelled by giving
#: them a synthetic ``UNARY_PRECEDENCE``.
_BINARY_PRECEDENCE = {
    "**":   12,
    "*":    10, "/": 10, "MOD": 10,
    "+":    9,  "-": 9,
    "<":    8,  ">": 8,  "<=": 8, ">=": 8,
    "=":    7,  "<>": 7,
    "AND":  6,
    "XOR":  5,
    "OR":   4,
}
_UNARY_PRECEDENCE = 11


def _fmt_expr(expr, parent_prec: int = 0) -> str:
    """Render an Expression as ST source text.

    ``parent_prec`` is the precedence of the enclosing operator; we
    wrap sub-expressions in parentheses when their precedence is
    lower than (or equal to, for right-associativity safety) the
    parent's.
    """
    if isinstance(expr, Literal):
        return expr.value

    if isinstance(expr, VarRef):
        ref = expr.ref
        if isinstance(ref, Address):
            return ref.raw
        if isinstance(ref, TagRef):
            return ref.name
        raise TypeError(f"VarRef.ref must be Address|TagRef: {ref!r}")

    if isinstance(expr, FieldAccess):
        return f"{_fmt_expr(expr.base, parent_prec=99)}.{expr.field}"

    if isinstance(expr, IndexAccess):
        idx = ", ".join(_fmt_expr(i) for i in expr.indices)
        return f"{_fmt_expr(expr.base, parent_prec=99)}[{idx}]"

    if isinstance(expr, UnaryExpr):
        inner = _fmt_expr(expr.operand, parent_prec=_UNARY_PRECEDENCE)
        if expr.op.value == "NOT":
            return f"NOT {inner}"
        return f"-{inner}"

    if isinstance(expr, BinaryExpr):
        my_prec = _BINARY_PRECEDENCE[expr.op.value]
        lhs = _fmt_expr(expr.lhs, parent_prec=my_prec)
        rhs = _fmt_expr(expr.rhs, parent_prec=my_prec + 1)
        body = f"{lhs} {expr.op.value} {rhs}"
        if my_prec < parent_prec:
            return f"({body})"
        return body

    if isinstance(expr, FunctionCallExpr):
        parts: list[str] = []
        for p in expr.positional:
            parts.append(_fmt_expr(p))
        for n, v in expr.named:
            parts.append(f"{n} := {_fmt_expr(v)}")
        return f"{expr.name}({', '.join(parts)})"

    raise TypeError(f"unknown Expression: {type(expr).__name__}")


def emit_statement(stmt, indent: str = "    ", level: int = 0) -> list[str]:
    """Render one Statement as a list of ST source lines.

    Multi-line constructs (IF / CASE / FOR / WHILE / REPEAT) return
    multiple lines; simple statements return one.  ``level`` is the
    current nesting depth (each level adds one ``indent``).
    """
    prefix = indent * level

    if isinstance(stmt, Assignment):
        return [f"{prefix}{_fmt_expr(stmt.target)} := {_fmt_expr(stmt.value)};"]

    if isinstance(stmt, IfStatement):
        lines: list[str] = []
        for i, (cond, body) in enumerate(stmt.branches):
            keyword = "IF" if i == 0 else "ELSIF"
            lines.append(f"{prefix}{keyword} {_fmt_expr(cond)} THEN")
            for s in body:
                lines.extend(emit_statement(s, indent, level + 1))
        if stmt.else_branch is not None:
            lines.append(f"{prefix}ELSE")
            for s in stmt.else_branch:
                lines.extend(emit_statement(s, indent, level + 1))
        lines.append(f"{prefix}END_IF;")
        return lines

    if isinstance(stmt, CaseStatement):
        lines = [f"{prefix}CASE {_fmt_expr(stmt.selector)} OF"]
        for clause in stmt.clauses:
            labels = ", ".join(_fmt_expr(l) for l in clause.labels)
            lines.append(f"{prefix}{indent}{labels}:")
            for s in clause.body:
                lines.extend(emit_statement(s, indent, level + 2))
        if stmt.else_branch is not None:
            lines.append(f"{prefix}ELSE")
            for s in stmt.else_branch:
                lines.extend(emit_statement(s, indent, level + 1))
        lines.append(f"{prefix}END_CASE;")
        return lines

    if isinstance(stmt, WhileStatement):
        lines = [f"{prefix}WHILE {_fmt_expr(stmt.condition)} DO"]
        for s in stmt.body:
            lines.extend(emit_statement(s, indent, level + 1))
        lines.append(f"{prefix}END_WHILE;")
        return lines

    if isinstance(stmt, RepeatStatement):
        lines = [f"{prefix}REPEAT"]
        for s in stmt.body:
            lines.extend(emit_statement(s, indent, level + 1))
        lines.append(f"{prefix}UNTIL {_fmt_expr(stmt.until)} END_REPEAT;")
        return lines

    if isinstance(stmt, ForStatement):
        step = (f" BY {_fmt_expr(stmt.step)}"
                if stmt.step is not None else "")
        lines = [f"{prefix}FOR {stmt.index_var} := "
                 f"{_fmt_expr(stmt.start)} TO {_fmt_expr(stmt.end)}{step} DO"]
        for s in stmt.body:
            lines.extend(emit_statement(s, indent, level + 1))
        lines.append(f"{prefix}END_FOR;")
        return lines

    if isinstance(stmt, ReturnStatement):
        return [f"{prefix}RETURN;"]

    if isinstance(stmt, ExitStatement):
        return [f"{prefix}EXIT;"]

    if isinstance(stmt, ContinueStatement):
        return [f"{prefix}CONTINUE;"]

    if isinstance(stmt, FunctionCallStatement):
        return [f"{prefix}{_fmt_expr(stmt.call)};"]

    if isinstance(stmt, CommentStatement):
        return [f"{prefix}(* {stmt.text} *)"]

    if isinstance(stmt, GotoStatement):
        return [f"{prefix}GOTO {stmt.label};"]

    if isinstance(stmt, LabelStatement):
        # IEC labels sit at column zero per convention; emit at the
        # caller's indent for readability and so the surrounding
        # block structure stays visually aligned.
        return [f"{prefix}{stmt.name}:"]

    raise TypeError(f"unknown Statement: {type(stmt).__name__}")


def emit_st_body(stmts, indent: str = "    ", level: int = 1) -> list[str]:
    """Render a sequence of Statements as ST source lines.

    Default ``level=1`` because POU bodies indent one level inside
    the keyword block.  Use ``level=0`` for free-standing snippets.
    """
    lines: list[str] = []
    for s in stmts:
        lines.extend(emit_statement(s, indent=indent, level=level))
    return lines


# -----------------------------------------------------------------------------
# POU emitter
# -----------------------------------------------------------------------------


def _fmt_var_block(direction_keyword: str, vars_: Sequence[Var]) -> list[str]:
    """One VAR_INPUT / VAR_OUTPUT / VAR / etc. block as ST text."""
    if not vars_:
        return []
    lines = [direction_keyword]
    for v in vars_:
        init = f" := {v.initial_value}" if v.initial_value else ""
        comment = f"  (* {v.comment} *)" if v.comment else ""
        lines.append(f"    {v.name} : {_fmt_iec_type(v.data_type)}{init};{comment}")
    lines.append("END_VAR")
    return lines


_POU_KEYWORD = {
    PouKind.PROGRAM:        "PROGRAM",
    PouKind.FUNCTION:       "FUNCTION",
    PouKind.FUNCTION_BLOCK: "FUNCTION_BLOCK",
    # SUBROUTINE is a vendor-extension kind (CLICK).  IEC ST has no
    # direct equivalent; emit as PROGRAM if it's the entry point,
    # otherwise as FUNCTION_BLOCK (stateful, callable, no params).
}


def _pou_keyword(sub: Subroutine) -> str:
    if sub.kind in _POU_KEYWORD:
        return _POU_KEYWORD[sub.kind]
    # SUBROUTINE fallback
    return "PROGRAM" if sub.main else "FUNCTION_BLOCK"


def _fmt_method(m: Method, indent: str = "    ") -> str:
    """Render one ``METHOD ... END_METHOD`` block.

    Layout::

        METHOD [PUBLIC|PRIVATE|...] [ABSTRACT|OVERRIDE] name [: ReturnType]
            VAR_INPUT ... END_VAR
            ...
            <body>
        END_METHOD

    Abstract methods omit the body (just the signature block).
    """
    parts: list[str] = []
    qualifiers = [m.access.value]
    if m.abstract:
        qualifiers.append("ABSTRACT")
    if m.override:
        qualifiers.append("OVERRIDE")
    header = f"METHOD {' '.join(qualifiers)} {m.name}"
    if m.return_type is not None:
        header += f" : {_fmt_iec_type(m.return_type)}"
    if m.comment:
        parts.append(f"(* {m.comment} *)")
    parts.append(header)
    parts.extend(_fmt_var_block("VAR_INPUT",  m.inputs))
    parts.extend(_fmt_var_block("VAR_OUTPUT", m.outputs))
    parts.extend(_fmt_var_block("VAR_IN_OUT", m.in_outs))
    parts.extend(_fmt_var_block("VAR",        m.local_vars))
    if not m.abstract:
        if m.st_body is not None:
            parts.extend(emit_st_body(m.st_body, indent=indent, level=1))
        else:
            for rung in m.rungs:
                for stmt in emit_rung(rung):
                    parts.append(indent + stmt)
    parts.append("END_METHOD")
    return "\n".join(parts)


def _fmt_interface_decl(iface: Interface) -> str:
    """Render one ``INTERFACE ... END_INTERFACE`` declaration.

    Interfaces contain only abstract method signatures (no bodies);
    each method renders as a METHOD block whose ABSTRACT qualifier
    is implicit at the interface level."""
    parts: list[str] = []
    if iface.comment:
        parts.append(f"(* {iface.comment} *)")
    parts.append(f"INTERFACE {iface.name}")
    for m in iface.methods:
        parts.append(_fmt_method(m))
    parts.append("END_INTERFACE")
    return "\n".join(parts)


def emit_pou(sub: Subroutine, indent: str = "    ") -> str:
    """Return the full ST text for one POU.

    Output structure for a simple POU::

        PROGRAM Main
        VAR_INPUT
            a : INT;
        END_VAR
        VAR_OUTPUT
            result : INT;
        END_VAR
            <body statements indented by `indent`>
        END_PROGRAM

    FUNCTION_BLOCK POUs additionally carry IEC 3rd-edition OOP
    qualifiers in the header (``ABSTRACT``, ``EXTENDS Parent``,
    ``IMPLEMENTS I1, I2``) and any declared ``METHOD ... END_METHOD``
    blocks appear between the variable declarations and the body.
    """
    keyword = _pou_keyword(sub)
    header = keyword
    # IEC 3rd-edition OOP qualifiers on FUNCTION_BLOCK headers
    if sub.kind is PouKind.FUNCTION_BLOCK and sub.abstract:
        header += " ABSTRACT"
    header += " " + sub.name
    if sub.kind is PouKind.FUNCTION and sub.return_type is not None:
        header += " : " + _fmt_iec_type(sub.return_type)
    if sub.kind is PouKind.FUNCTION_BLOCK:
        if sub.extends:
            header += f" EXTENDS {sub.extends}"
        if sub.implements:
            header += f" IMPLEMENTS {', '.join(sub.implements)}"

    lines: list[str] = []
    if sub.comment:
        lines.append(f"(* {sub.comment} *)")
    lines.append(header)

    lines.extend(_fmt_var_block("VAR_INPUT",  sub.inputs))
    lines.extend(_fmt_var_block("VAR_OUTPUT", sub.outputs))
    lines.extend(_fmt_var_block("VAR_IN_OUT", sub.in_outs))
    lines.extend(_fmt_var_block("VAR",        sub.local_vars))

    # Methods (FUNCTION_BLOCKs only) sit between the interface
    # declarations and the body proper, matching IEC 3rd-edition
    # convention.
    for m in sub.methods:
        lines.append(_fmt_method(m, indent=indent))

    if sub.st_body is not None:
        # First-class ST body -- render the AST directly.
        lines.extend(emit_st_body(sub.st_body, indent=indent, level=1))
    elif sub.sfc is not None:
        lines.append(f"{indent}(* SFC body not emitted in ST -- "
                     f"see PLCopen XML <SFC> *)")
    elif sub.fbd_body is not None:
        # Lower the FBD network to an equivalent ST statement list
        # (topological sort + producer-expression resolution +
        # temp-var allocation).  Temp vars are *not* injected into
        # the POU's local_vars by this path; the emitter declares
        # them inline via the VAR section above only if the caller
        # ran the lowering pass explicitly first.  For round-trip
        # safety, run the lowering at emit time and rely on the
        # lowering-internal naming (_t0, _t1, ...) being collision-
        # free with user names.
        from ..lowering.fbd_to_st import lower_fbd_to_st
        result = lower_fbd_to_st(sub.fbd_body)
        if result.temp_vars:
            # Inject a synthetic VAR section for the temps so the
            # output ST is self-contained.
            lines.append("VAR  (* FBD lowering temporaries *)")
            for v in result.temp_vars:
                lines.append(
                    f"{indent}{v.name} : {_fmt_iec_type(v.data_type)};"
                )
            lines.append("END_VAR")
        lines.extend(emit_st_body(result.statements,
                                    indent=indent, level=1))
    else:
        for rung in sub.rungs:
            for stmt in emit_rung(rung):
                lines.append(indent + stmt)

    lines.append(f"END_{keyword}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Program emitter
# -----------------------------------------------------------------------------


def _fmt_user_type_decl(ut) -> str:
    """Render one UDT as an IEC ``TYPE ... END_TYPE`` block.

    Maps each UDT variant to its IEC textual form:

      AliasType  -> ``TYPE Name : Base; END_TYPE``
      EnumType   -> ``TYPE Name : (V1, V2, V3); END_TYPE``
      ArrayType  -> ``TYPE Name : ARRAY [lo..hi, lo..hi] OF ElemType; END_TYPE``
      StructType -> ``TYPE Name : STRUCT field : type; ... END_STRUCT; END_TYPE``

    Nested type references (a struct member of struct type, an array
    of structs, an alias of a struct, ...) resolve via
    ``_fmt_iec_type`` -- which renders both elementary and
    user-defined types by their IEC name.
    """
    if isinstance(ut, SubrangeType):
        body = (f"    {ut.name} : "
                f"{_fmt_iec_type(ut.base)} "
                f"({ut.lower}..{ut.upper});")
        return "\n".join(["TYPE", body, "END_TYPE"])

    if isinstance(ut, AliasType):
        body = f"    {ut.name} : {_fmt_iec_type(ut.base)};"
        return "\n".join(["TYPE", body, "END_TYPE"])

    if isinstance(ut, EnumType):
        values = ", ".join(ut.values)
        body = f"    {ut.name} : ({values});"
        return "\n".join(["TYPE", body, "END_TYPE"])

    if isinstance(ut, ArrayType):
        bounds_str = ", ".join(f"{lo}..{hi}" for lo, hi in ut.bounds)
        elem = _fmt_iec_type(ut.element_type)
        body = f"    {ut.name} : ARRAY [{bounds_str}] OF {elem};"
        return "\n".join(["TYPE", body, "END_TYPE"])

    if isinstance(ut, StructType):
        lines = ["TYPE", f"    {ut.name} :", "        STRUCT"]
        for m in ut.members:
            init = f" := {m.initial_value}" if m.initial_value else ""
            comment = f"  (* {m.comment} *)" if m.comment else ""
            lines.append(
                f"            {m.name} : {_fmt_iec_type(m.data_type)}{init};{comment}"
            )
        lines.extend(["        END_STRUCT;", "END_TYPE"])
        return "\n".join(lines)

    raise ValueError(f"unknown UserType: {type(ut).__name__}")


def _fmt_task(task: TaskSpec) -> str:
    """One IEC ``TASK Name(...);`` declaration."""
    attrs: list[str] = []
    if task.interval is not None:
        attrs.append(f"INTERVAL := {task.interval}")
    if task.single is not None:
        attrs.append(f"SINGLE := {task.single}")
    if task.interrupt is not None:
        attrs.append(f"INTERRUPT := {task.interrupt}")
    attrs.append(f"PRIORITY := {task.priority}")
    return f"        TASK {task.name}({', '.join(attrs)});"


def _fmt_pou_instance(inst: PouInstance) -> str:
    """One IEC ``PROGRAM Name WITH Task : Type;`` declaration."""
    bind = f" WITH {inst.task}" if inst.task else ""
    return f"        PROGRAM {inst.name}{bind} : {inst.type_name};"


def _fmt_resource(r: Resource) -> str:
    """Emit one ``RESOURCE name ON PLC ... END_RESOURCE`` block.

    Structure per IEC §2.7.1::

        RESOURCE name ON PLC
            VAR_GLOBAL
                ...
            END_VAR
            TASK Fast(INTERVAL := T#10ms, PRIORITY := 1);
            PROGRAM MainProg WITH Fast : Main;
        END_RESOURCE
    """
    lines: list[str] = [f"    RESOURCE {r.name} ON PLC"]
    if r.comment:
        lines.append(f"        (* {r.comment} *)")

    if r.global_vars:
        lines.append("        VAR_GLOBAL")
        for v in r.global_vars:
            init = f" := {v.initial_value}" if v.initial_value else ""
            comment = f"  (* {v.comment} *)" if v.comment else ""
            lines.append(
                f"            {v.name} : {_fmt_iec_type(v.data_type)}{init};{comment}"
            )
        lines.append("        END_VAR")

    for t in r.tasks:
        lines.append(_fmt_task(t))

    for inst in r.pou_instances:
        lines.append(_fmt_pou_instance(inst))

    lines.append("    END_RESOURCE")
    return "\n".join(lines)


def _fmt_configuration(cfg: Configuration) -> str:
    """Emit one ``CONFIGURATION ... END_CONFIGURATION`` block.

    Structure::

        CONFIGURATION name
            VAR_GLOBAL
                ...
            END_VAR
            VAR_ACCESS
                ...
            END_VAR
            RESOURCE ... END_RESOURCE
            ...
        END_CONFIGURATION
    """
    lines: list[str] = [f"CONFIGURATION {cfg.name}"]
    if cfg.comment:
        lines.append(f"    (* {cfg.comment} *)")

    if cfg.global_vars:
        lines.append("    VAR_GLOBAL")
        for v in cfg.global_vars:
            init = f" := {v.initial_value}" if v.initial_value else ""
            comment = f"  (* {v.comment} *)" if v.comment else ""
            lines.append(
                f"        {v.name} : {_fmt_iec_type(v.data_type)}{init};{comment}"
            )
        lines.append("    END_VAR")

    if cfg.access_vars:
        lines.append("    VAR_ACCESS")
        for v in cfg.access_vars:
            comment = f"  (* {v.comment} *)" if v.comment else ""
            lines.append(
                f"        {v.name} : {_fmt_iec_type(v.data_type)};{comment}"
            )
        lines.append("    END_VAR")

    for r in cfg.resources:
        lines.append(_fmt_resource(r))

    lines.append("END_CONFIGURATION")
    return "\n".join(lines)


def emit_program(prog: Program) -> str:
    """Return the full ST text for a Program.

    Sections, in order::

        (optional) TYPE ... END_TYPE block per user-defined type
                   (StructType, ArrayType, EnumType, AliasType)
        (optional) VAR_GLOBAL declarations for all Tags
        (optional) DATA_BLOCK declarations (non-instance) -- emitted as
                   typed VAR_GLOBAL groups for now; IEC has TYPE/STRUCT
                   declarations for those too, but the IL doesn't yet
                   model the DB-as-STRUCT translation explicitly.
        One POU per Subroutine in declaration order.
    """
    sections: list[str] = []

    # User-defined types first -- subsequent VAR sections can reference
    # them by name.
    for ut in prog.user_types:
        sections.append(_fmt_user_type_decl(ut))

    # IEC 3rd-edition INTERFACE declarations -- POUs that IMPLEMENT
    # them appear afterwards.
    for iface in prog.interfaces:
        sections.append(_fmt_interface_decl(iface))

    if prog.tags:
        lines = ["VAR_GLOBAL"]
        for tag in prog.tags.values():
            init = ""  # Tag has no initial_value field today
            comment = f"  (* {tag.description} *)" if tag.description else ""
            if tag.address is not None:
                # Direct-representation form would be %X; we use the raw
                # address as a comment until the direct-rep parser lands.
                comment = f"  (* AT {tag.address.raw}{tag.description and ': ' + tag.description or ''} *)"
            lines.append(
                f"    {tag.name} : {_fmt_iec_type(tag.data_type)}{init};{comment}"
            )
        lines.append("END_VAR")
        sections.append("\n".join(lines))

    if prog.data_blocks:
        for db in prog.data_blocks:
            lines = [f"(* DATA_BLOCK {db.name}"
                     f"{' instance of ' + db.fb_template if db.fb_template else ''} *)"]
            lines.append("VAR_GLOBAL")
            for m in db.members:
                lines.append(
                    f"    {db.name}_{m.name} : "
                    f"{_fmt_iec_type(m.data_type)};"
                )
            lines.append("END_VAR")
            sections.append("\n".join(lines))

    for sub in prog.subroutines:
        sections.append(emit_pou(sub))

    # Configurations after POUs -- their PROGRAM declarations reference
    # POUs by type name, so the POU declarations should appear first.
    for cfg in prog.configurations:
        sections.append(_fmt_configuration(cfg))

    return "\n\n".join(sections) + "\n"
