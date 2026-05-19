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
    Address, AliasType, ArrayType, Configuration, DataBlock, EnumType,
    NamedType, PouInstance, PouKind, Program, Resource, Rung, StructType,
    SubrangeType, Subroutine, Tag, TagRef, TagType, TaskSpec, Var,
    VarDirection, type_name,
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


def emit_pou(sub: Subroutine, indent: str = "    ") -> str:
    """Return the full ST text for one POU.

    Output structure::

        PROGRAM Main
        VAR_INPUT
            a : INT;
        END_VAR
        VAR_OUTPUT
            result : INT;
        END_VAR
            <body statements indented by `indent`>
        END_PROGRAM
    """
    keyword = _pou_keyword(sub)
    header = keyword + " " + sub.name
    if sub.kind is PouKind.FUNCTION and sub.return_type is not None:
        header += " : " + _fmt_iec_type(sub.return_type)

    lines: list[str] = []
    if sub.comment:
        lines.append(f"(* {sub.comment} *)")
    lines.append(header)

    lines.extend(_fmt_var_block("VAR_INPUT",  sub.inputs))
    lines.extend(_fmt_var_block("VAR_OUTPUT", sub.outputs))
    lines.extend(_fmt_var_block("VAR_IN_OUT", sub.in_outs))
    lines.extend(_fmt_var_block("VAR",        sub.local_vars))

    if sub.sfc is not None:
        lines.append(f"{indent}(* SFC body not emitted in ST -- "
                     f"see PLCopen XML <SFC> *)")
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
