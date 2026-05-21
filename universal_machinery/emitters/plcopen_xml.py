"""IEC 61131-3 / PLCopen TC6 XML emitter.

Produces a PLCopen TC6 v2.01-compliant XML document from an IL
``Program``.  This is the interchange format that conformant
IEC 61131-3 tools consume and produce -- writing it is the
certification deliverable for any PLCopen conformance claim.

Schema reference
----------------

PLCopen TC6 XML for IEC 61131-3, v2.01 (the version most widely
adopted).  Root element ``<project xmlns="http://www.plcopen.org/xml/tc6_0201">``
contains:

  ``<fileHeader>``      authoring metadata (company, product, time)
  ``<contentHeader>``   project name, version, modification time
  ``<types>``           POU declarations (interface + body)
  ``<instances>``       CONFIGURATION/RESOURCE/TASK (not yet emitted)

Each POU under ``<types><pous>`` has an ``<interface>`` (variable
declarations grouped by direction) and a body element selected by
the POU's source language: ``<ST>`` (Structured Text), ``<LD>``
(Ladder Diagram), ``<FBD>`` (Function Block Diagram), or ``<SFC>``.
This emitter uses ``<ST>`` bodies populated by the
``universal_machinery.emitters.st`` module -- the most portable
output (every conformant tool accepts ST).

Coverage scope (first cut)
--------------------------

  - ✅ POU declarations: PROGRAM, FUNCTION, FUNCTION_BLOCK
  - ✅ Variable declarations: VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / VAR
  - ✅ Return type for FUNCTION
  - ✅ ST body via emit_pou_body_st() -- routes authored
        ``st_body`` (IEC §3 AST) directly through the ST emitter;
        falls back to LD-rung-to-ST translation when ``st_body``
        is absent
  - ✅ Tag declarations as a global VAR_GLOBAL block (PLCopen
        wraps these as ``<globalVars>`` inside ``<configurations>``;
        without the full configuration model we emit them as a
        synthetic ``Globals`` POU's VAR section for round-trip
        portability)
  - ⚠️ DataBlocks: emitted as commented placeholders; proper
        STRUCT type declarations need the user-defined-type slice
  - ❌ CONFIGURATION / RESOURCE / TASK (PLCopen ``<instances>``):
        skipped; deferred to the configuration-model slice
  - ❌ SFC and LD body XML: ST-only first cut
  - ❌ METHOD / INTERFACE (IEC 3rd ed.): skipped

Output is hand-rolled XML rather than using xml.etree because:

  - We want compact, deterministic output (consistent attribute
    ordering, predictable whitespace) for diffability / round-trip
    byte-equality tests.
  - The PLCopen schema is small and stable enough that hand-rolling
    is simpler than configuring etree's namespace handling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Optional, Sequence
from xml.sax.saxutils import escape, quoteattr

from ..il import (
    AccessVar, Address, AliasType, ArrayType, BlockPin, ConfigVar,
    Configuration, Connection, DataBlock, EnumType, FbBlock, FbdJump,
    FbdLabel, FbdNetwork, FbdReturn, InOutVariable, InVariable, NamedType,
    OutVariable, PouInstance, PouKind, Position, Program, Resource,
    SfcNetwork, Step, StructType, SubrangeType, Subroutine, Tag, TagType,
    TaskSpec, Transition, Var, VarDirection, is_signed_subrange,
)
from .st import emit_pou as _emit_pou_st, emit_rung, emit_st_body


#: PLCopen TC6 XML namespace used throughout the document.
PLCOPEN_NS = "http://www.plcopen.org/xml/tc6_0201"

#: PLCopen schema version this emitter targets.
PLCOPEN_VERSION = "2.01"


# -----------------------------------------------------------------------------
# Variable-direction mapping (IL VarDirection -> PLCopen element name)
# -----------------------------------------------------------------------------


_DIRECTION_TO_ELEMENT = {
    VarDirection.INPUT:    "inputVars",
    VarDirection.OUTPUT:   "outputVars",
    VarDirection.IN_OUT:   "inOutVars",
    VarDirection.LOCAL:    "localVars",
    VarDirection.EXTERNAL: "externalVars",
    VarDirection.TEMP:     "tempVars",
}


_POU_TYPE = {
    PouKind.PROGRAM:        "program",
    PouKind.FUNCTION:       "function",
    PouKind.FUNCTION_BLOCK: "functionBlock",
    # SUBROUTINE is a vendor-extension kind with no PLCopen equivalent.
    # We emit it as `program` if it's the entry point, else `functionBlock`.
}


# -----------------------------------------------------------------------------
# Low-level XML building
# -----------------------------------------------------------------------------


def _attrs(**kw: Optional[str]) -> str:
    """Render keyword arguments as XML attributes (skipping None)."""
    parts: list[str] = []
    for k, v in kw.items():
        if v is None:
            continue
        parts.append(f'{k}={quoteattr(str(v))}')
    return (" " + " ".join(parts)) if parts else ""


def _indent(text: str, prefix: str) -> str:
    """Indent every non-empty line of ``text`` by ``prefix``."""
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


# -----------------------------------------------------------------------------
# Variable declarations
# -----------------------------------------------------------------------------


def _emit_var(var: Var) -> str:
    """One ``<variable>`` element with type + optional initial value.

    When ``var.address`` is set:

      - IEC direct-representation form (``%I0.0``, ``%MW5``, etc.)
        renders as the standards-conformant ``address`` attribute
        on the ``<variable>`` element -- consumed by every PLCopen
        TC6 tool.
      - Other address forms (CLICK-style ``X001``, vendor symbols)
        emit as an XML comment alongside the variable.  The schema's
        ``address`` attribute is a free-form xsd:string so these
        could be carried there too, but the comment form keeps the
        IEC-conformant attribute reserved for IEC-conformant syntax.
    """
    attrs = [f"name={quoteattr(var.name)}"]
    if var.address is not None and var.address.raw.startswith("%"):
        attrs.append(f"address={quoteattr(var.address.raw)}")
    parts: list[str] = [f"<variable {' '.join(attrs)}>"]
    parts.append(f"  <type>{_iec_type_element(var.data_type)}</type>")
    if var.initial_value:
        parts.append(
            f"  <initialValue>"
            f"<simpleValue value={quoteattr(var.initial_value)}/>"
            f"</initialValue>"
        )
    if var.address is not None and not var.address.raw.startswith("%"):
        parts.append(f"  <!-- AT {escape(var.address.raw)} -->")
    if var.comment:
        parts.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(var.comment)}</p></documentation>'
        )
    parts.append("</variable>")
    return "\n".join(parts)


def _iec_type_element(t) -> str:
    """Render a ``DataType`` as a PLCopen ``<type>``-body element
    (full self-closing form, including angle brackets).

    Elementary ``TagType`` emits as ``<BOOL/>`` / ``<INT/>`` /
    ``<REAL/>`` / etc. -- the schema's elementary-type element names
    match ``TagType.value``.

    User-defined type references emit as ``<derived name="..."/>``
    per the schema's ``derivedTypes`` group; the named type must be
    declared in ``Program.user_types`` for the resulting XML to
    compile in a PLCopen tool.  Inline ``StructType`` / ``ArrayType``
    / etc. resolve via the type's ``name``.
    """
    if isinstance(t, TagType):
        return f"<{t.value}/>"
    if isinstance(t, NamedType):
        return f'<derived name="{escape(t.name)}"/>'
    if isinstance(t, (StructType, ArrayType, EnumType, AliasType)):
        return f'<derived name="{escape(t.name)}"/>'
    raise TypeError(f"can't emit type element for: {type(t).__name__}")


def _emit_var_block(direction: VarDirection,
                    vars_: Sequence[Var]) -> Optional[str]:
    """One ``<inputVars>``/``<outputVars>``/etc. element wrapping its
    ``<variable>`` children.  Returns None for empty blocks."""
    if not vars_:
        return None
    element = _DIRECTION_TO_ELEMENT[direction]
    inner = "\n".join(_indent(_emit_var(v), "  ") for v in vars_)
    return f"<{element}>\n{inner}\n</{element}>"


# -----------------------------------------------------------------------------
# POU body (ST)
# -----------------------------------------------------------------------------


def _render_pou_body_text(sub: Subroutine) -> str:
    """Pick the right textual ST source for a POU body.

    Body-kind dispatch:
      - ``sub.st_body`` set    : render the authored ST AST directly
                                  via ``emit_st_body`` (preferred
                                  path; preserves authored syntax).
      - ``sub.fbd_body`` set   : lower the FBD network to ST + emit
                                  the lowered statement list (the
                                  XML emitter uses native ``<FBD>``;
                                  this path is only hit when the
                                  caller asks for ``<ST>`` body XML
                                  for an FBD-authored POU).
      - ``sub.sfc`` set        : leave a marker comment -- a future
                                  slice adds a real ``<SFC>`` body.
      - ``sub.rungs`` (default): translate the LD rungs to ST text.

    The four are mutually exclusive (validator enforces).
    """
    lines: list[str] = []
    if sub.st_body is not None:
        lines.extend(emit_st_body(sub.st_body, level=0))
    elif sub.fbd_body is not None:
        from ..lowering.fbd_to_st import lower_fbd_to_st
        result = lower_fbd_to_st(sub.fbd_body)
        lines.extend(emit_st_body(result.statements, level=0))
    elif sub.sfc is not None:
        lines.append("(* SFC body not emitted in ST -- see <SFC> body *)")
    else:
        for rung in sub.rungs:
            for stmt in emit_rung(rung):
                lines.append(stmt)
    return "\n".join(lines) if lines else "(* empty *)"


def _emit_pou_body_st(sub: Subroutine) -> str:
    """Body XML for an ST-bodied POU.

    PLCopen wraps Structured Text inside
    ``<body><ST><xhtml>...</xhtml></ST></body>``.  The textual
    content is sourced from ``_render_pou_body_text``, which picks
    the right form (authored ST AST, SFC marker, or rung
    translation) based on what the IL Subroutine carries.
    """
    body_text = _render_pou_body_text(sub)
    # PLCopen schema requires the textual content inside an element
    # from the XHTML namespace (xsd:any namespace="..xhtml").  We use
    # ``<pre>`` (preformatted text) so the ST source's whitespace +
    # line breaks are preserved; ``xmlns`` declared inline so the
    # output is self-contained even when ``emit_pou_xml`` is used
    # standalone (not inside a full ``emit_xml`` document).
    return (
        "<body>\n"
        "  <ST>\n"
        f'    <pre xmlns="http://www.w3.org/1999/xhtml">'
        f'{escape(body_text)}</pre>\n'
        "  </ST>\n"
        "</body>"
    )


# -----------------------------------------------------------------------------
# POU body (FBD)
# -----------------------------------------------------------------------------


#: Layout grid for elements whose ``position`` is ``None``.  We
#: sweep left-to-right, row-major; FbBlock columns are wider than
#: variable connectors so the resulting XML is at least visually
#: passable in a PLCopen-aware editor.  Values in pixels.
_FBD_GRID_X = 200.0
_FBD_GRID_Y = 100.0
_FBD_GRID_COLS = 6


def _auto_position(idx: int) -> Position:
    """Row-major position when an element doesn't carry one.

    Generates a (x, y) on a coarse grid so the XML is XSD-valid
    (``<position>`` is required) and renderers don't pile every
    element on top of each other.
    """
    col = idx % _FBD_GRID_COLS
    row = idx // _FBD_GRID_COLS
    return Position(x=20.0 + col * _FBD_GRID_X, y=20.0 + row * _FBD_GRID_Y)


def _position_xml(pos: Position) -> str:
    """``<position x= y=/>`` with PLCopen-conformant decimal formatting."""
    return f'<position x="{pos.x:g}" y="{pos.y:g}"/>'


def _connection_inner_xml(conn: Connection) -> str:
    """Inner ``<connection refLocalId="..." [formalParameter="..."]/>``."""
    if conn.source_pin is not None:
        return (f'<connection refLocalId="{conn.source_id}" '
                f'formalParameter={quoteattr(conn.source_pin)}/>')
    return f'<connection refLocalId="{conn.source_id}"/>'


def _connection_point_in_xml(conn: Optional[Connection]) -> str:
    """``<connectionPointIn>`` element.

    If ``conn`` is set, embeds the back-pointing ``<connection>``;
    otherwise emits an empty point (PLCopen accepts that -- it
    means the pin is unwired).
    """
    if conn is None:
        return "<connectionPointIn/>"
    inner = _connection_inner_xml(conn)
    return f"<connectionPointIn>{inner}</connectionPointIn>"


_PIN_OPT_ATTRS = ("negated", "edge", "storage")


def _pin_modifiers(pin: BlockPin) -> str:
    """Attribute fragment for a pin's negated / edge / storage flags.

    Defaults (``negated=false``, ``edge="none"``, ``storage="none"``)
    are omitted so the output stays clean."""
    attrs: list[str] = []
    if pin.negated:
        attrs.append('negated="true"')
    if pin.edge:
        attrs.append(f"edge={quoteattr(pin.edge)}")
    if pin.storage:
        attrs.append(f"storage={quoteattr(pin.storage)}")
    return (" " + " ".join(attrs)) if attrs else ""


def _emit_block_xml(b: FbBlock, idx: int) -> str:
    """Render one ``<block typeName=...>`` element from an ``FbBlock``."""
    pos = b.position if b.position is not None else _auto_position(idx)
    attrs = [
        f'localId="{b.local_id}"',
        f"typeName={quoteattr(b.type_name)}",
    ]
    if b.instance_name is not None:
        attrs.append(f"instanceName={quoteattr(b.instance_name)}")
    if b.execution_order is not None:
        attrs.append(f'executionOrderId="{b.execution_order}"')
    parts = [f"<block {' '.join(attrs)}>", f"  {_position_xml(pos)}"]

    # <inputVariables>
    parts.append("  <inputVariables>")
    for p in b.inputs:
        mods = _pin_modifiers(p)
        cpoint = _connection_point_in_xml(p.connection)
        parts.append(
            f"    <variable formalParameter={quoteattr(p.formal_parameter)}"
            f"{mods}>{cpoint}</variable>"
        )
    parts.append("  </inputVariables>")

    # <inOutVariables>
    parts.append("  <inOutVariables>")
    for p in b.in_outs:
        mods = _pin_modifiers(p)
        cpoint = _connection_point_in_xml(p.connection)
        parts.append(
            f"    <variable formalParameter={quoteattr(p.formal_parameter)}"
            f"{mods}>{cpoint}<connectionPointOut/></variable>"
        )
    parts.append("  </inOutVariables>")

    # <outputVariables>
    parts.append("  <outputVariables>")
    for p in b.outputs:
        mods = _pin_modifiers(p)
        parts.append(
            f"    <variable formalParameter={quoteattr(p.formal_parameter)}"
            f"{mods}><connectionPointOut/></variable>"
        )
    parts.append("  </outputVariables>")

    parts.append("</block>")
    return "\n".join(parts)


def _modifier_attrs_var(negated: bool, edge: str, storage: str) -> str:
    attrs: list[str] = []
    if negated:
        attrs.append('negated="true"')
    if edge:
        attrs.append(f"edge={quoteattr(edge)}")
    if storage:
        attrs.append(f"storage={quoteattr(storage)}")
    return (" " + " ".join(attrs)) if attrs else ""


def _emit_in_variable_xml(v: InVariable, idx: int) -> str:
    pos = v.position if v.position is not None else _auto_position(idx)
    mods = _modifier_attrs_var(v.negated, v.edge, v.storage)
    eo = (f' executionOrderId="{v.execution_order}"'
          if v.execution_order is not None else "")
    return (
        f'<inVariable localId="{v.local_id}"{eo}{mods}>'
        f"{_position_xml(pos)}"
        f"<connectionPointOut/>"
        f"<expression>{escape(v.expression)}</expression>"
        f"</inVariable>"
    )


def _emit_out_variable_xml(v: OutVariable, idx: int) -> str:
    pos = v.position if v.position is not None else _auto_position(idx)
    mods = _modifier_attrs_var(v.negated, v.edge, v.storage)
    eo = (f' executionOrderId="{v.execution_order}"'
          if v.execution_order is not None else "")
    cpoint = _connection_point_in_xml(v.connection)
    return (
        f'<outVariable localId="{v.local_id}"{eo}{mods}>'
        f"{_position_xml(pos)}"
        f"{cpoint}"
        f"<expression>{escape(v.expression)}</expression>"
        f"</outVariable>"
    )


def _emit_inout_variable_xml(v: InOutVariable, idx: int) -> str:
    pos = v.position if v.position is not None else _auto_position(idx)
    attrs: list[str] = []
    if v.negated_in:
        attrs.append('negatedIn="true"')
    if v.negated_out:
        attrs.append('negatedOut="true"')
    if v.edge_in:
        attrs.append(f"edgeIn={quoteattr(v.edge_in)}")
    if v.edge_out:
        attrs.append(f"edgeOut={quoteattr(v.edge_out)}")
    if v.storage_in:
        attrs.append(f"storageIn={quoteattr(v.storage_in)}")
    if v.storage_out:
        attrs.append(f"storageOut={quoteattr(v.storage_out)}")
    mods = (" " + " ".join(attrs)) if attrs else ""
    eo = (f' executionOrderId="{v.execution_order}"'
          if v.execution_order is not None else "")
    cpoint = _connection_point_in_xml(v.connection)
    return (
        f'<inOutVariable localId="{v.local_id}"{eo}{mods}>'
        f"{_position_xml(pos)}"
        f"{cpoint}"
        f"<connectionPointOut/>"
        f"<expression>{escape(v.expression)}</expression>"
        f"</inOutVariable>"
    )


def _emit_fbd_label_xml(e: FbdLabel, idx: int) -> str:
    pos = e.position if e.position is not None else _auto_position(idx)
    eo = (f' executionOrderId="{e.execution_order}"'
          if e.execution_order is not None else "")
    return (
        f'<label localId="{e.local_id}" label={quoteattr(e.label)}{eo}>'
        f"{_position_xml(pos)}"
        f"</label>"
    )


def _emit_fbd_jump_xml(e: FbdJump, idx: int) -> str:
    pos = e.position if e.position is not None else _auto_position(idx)
    eo = (f' executionOrderId="{e.execution_order}"'
          if e.execution_order is not None else "")
    cpoint = _connection_point_in_xml(e.connection)
    return (
        f'<jump localId="{e.local_id}" label={quoteattr(e.label)}{eo}>'
        f"{_position_xml(pos)}"
        f"{cpoint}"
        f"</jump>"
    )


def _emit_fbd_return_xml(e: FbdReturn, idx: int) -> str:
    pos = e.position if e.position is not None else _auto_position(idx)
    eo = (f' executionOrderId="{e.execution_order}"'
          if e.execution_order is not None else "")
    cpoint = _connection_point_in_xml(e.connection)
    return (
        f'<return localId="{e.local_id}"{eo}>'
        f"{_position_xml(pos)}"
        f"{cpoint}"
        f"</return>"
    )


def _emit_fbd_element_xml(e, idx: int) -> str:
    """Dispatch on element kind to the right renderer."""
    if isinstance(e, FbBlock):       return _emit_block_xml(e, idx)
    if isinstance(e, InVariable):    return _emit_in_variable_xml(e, idx)
    if isinstance(e, OutVariable):   return _emit_out_variable_xml(e, idx)
    if isinstance(e, InOutVariable): return _emit_inout_variable_xml(e, idx)
    if isinstance(e, FbdLabel):      return _emit_fbd_label_xml(e, idx)
    if isinstance(e, FbdJump):       return _emit_fbd_jump_xml(e, idx)
    if isinstance(e, FbdReturn):     return _emit_fbd_return_xml(e, idx)
    raise TypeError(f"unknown FBD element: {type(e).__name__}")


def _emit_pou_body_fbd(sub: Subroutine) -> str:
    """Body XML for an FBD-bodied POU.

    Wraps the network in ``<body><FBD>...</FBD></body>`` per the
    PLCopen schema.  Elements without an explicit ``position`` get
    auto-laid out on a coarse grid so the output is XSD-valid.
    """
    net = sub.fbd_body
    if net is None or not net.elements:
        return "<body>\n  <FBD/>\n</body>"
    inner_parts = [_emit_fbd_element_xml(e, idx)
                   for idx, e in enumerate(net.elements)]
    inner = "\n".join(_indent(p, "    ") for p in inner_parts)
    return f"<body>\n  <FBD>\n{inner}\n  </FBD>\n</body>"


# -----------------------------------------------------------------------------
# POU body (SFC) -- IEC §2.6 / PLCopen <SFC>
# -----------------------------------------------------------------------------


#: SFC layout grid.  Steps + transitions alternate down a column;
#: each row occupies 100 px of vertical space.
_SFC_GRID_X = 200.0
_SFC_GRID_Y = 100.0


def _sfc_step_position(idx: int) -> Position:
    """Vertical step ladder, one step per two rows (the row between
    is reserved for transitions)."""
    return Position(x=_SFC_GRID_X, y=20.0 + idx * _SFC_GRID_Y * 2)


def _sfc_transition_position(idx: int) -> Position:
    """Transitions sit between consecutive step rows."""
    return Position(x=_SFC_GRID_X, y=70.0 + idx * _SFC_GRID_Y * 2)


def _emit_sfc_step_xml(step: Step, local_id: int,
                       incoming: list[int]) -> str:
    """One ``<step>`` element.

    ``incoming`` is the list of transition localIds whose outgoing
    wires land on this step's incoming connection point.
    """
    attrs = [
        f'localId="{local_id}"',
        f"name={quoteattr(step.name)}",
    ]
    if step.initial:
        attrs.append('initialStep="true"')
    parts = [f"<step {' '.join(attrs)}>",
             f"  {_position_xml(_sfc_step_position(local_id))}"]
    if incoming:
        inner_conns = "\n".join(
            f'    <connection refLocalId="{src}"/>' for src in incoming
        )
        parts.append("  <connectionPointIn>")
        parts.append(inner_conns)
        parts.append("  </connectionPointIn>")
    # The schema requires a connectionPointOut on every step that
    # has an outgoing wire.  We always include it so steps with
    # downstream transitions stay well-formed; the formalParameter
    # attribute is required.
    parts.append('  <connectionPointOut formalParameter="OUT"/>')
    if step.comment:
        parts.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(step.comment)}</p></documentation>'
        )
    parts.append("</step>")
    return "\n".join(parts)


def _condition_to_inline_st(cond_ops) -> str:
    """Render a transition's IL condition tuple as a boolean ST
    expression.

    The transition stores its guard as a sequence of LD-style
    input ops (typically ``ContactNO`` / ``ContactNC`` /
    ``Compare`` / ``ParallelGroup``).  We reuse the ST emitter's
    gate formatter to render the AND-chain of contact terms as
    a readable boolean expression embedded inside the
    ``<transition><condition><inline>`` shape.  An empty
    condition emits ``TRUE`` (unconditional transition).
    """
    if not cond_ops:
        return "TRUE"
    from .st import _fmt_gate
    return _fmt_gate(list(cond_ops))


def _emit_sfc_transition_xml(trans: Transition, local_id: int,
                             from_steps_ids: list[int]) -> str:
    """One ``<transition>`` element.

    ``from_steps_ids`` is the list of step localIds whose outgoing
    wires feed into this transition's incoming connection point.
    The condition lowers to ``<condition><inline name="cond">
    <ST><xhtml:pre>...</pre></ST></inline></condition>`` so any
    PLCopen-conformant tool can re-parse it.
    """
    parts = [f'<transition localId="{local_id}">',
             f"  {_position_xml(_sfc_transition_position(local_id))}"]
    if from_steps_ids:
        inner_conns = "\n".join(
            f'    <connection refLocalId="{src}"/>'
            for src in from_steps_ids
        )
        parts.append("  <connectionPointIn>")
        parts.append(inner_conns)
        parts.append("  </connectionPointIn>")
    parts.append("  <connectionPointOut/>")
    cond_text = _condition_to_inline_st(trans.condition)
    parts.append("  <condition>")
    parts.append('    <inline name="cond">')
    parts.append('      <ST>')
    parts.append(
        f'        <xhtml:pre xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        f'{escape(cond_text)}</xhtml:pre>'
    )
    parts.append('      </ST>')
    parts.append('    </inline>')
    parts.append("  </condition>")
    if trans.comment:
        parts.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(trans.comment)}</p></documentation>'
        )
    parts.append("</transition>")
    return "\n".join(parts)


def _emit_pou_body_sfc(sub: Subroutine) -> str:
    """Body XML for an SFC-bodied POU.

    Allocates localIds for every step and transition (steps first,
    then transitions, both numbered sequentially) and walks the
    network twice:

      1. Build the localId map.
      2. For each transition, find its incoming step localIds
         from ``from_steps`` and its outgoing step localIds from
         ``to_steps``.  Each step's incoming-connection list is
         the union of all transitions that target it via ``to_steps``.

    Actions, action blocks, selectionDivergence, and
    simultaneousDivergence are not emitted in this slice -- a
    follow-up adds the action block + divergence shapes.
    """
    net = sub.sfc
    if net is None or (not net.steps and not net.transitions):
        return "<body>\n  <SFC/>\n</body>"

    # localId allocation: steps first, then transitions, in
    # declaration order.  Maps step name -> localId, transition
    # index -> localId.
    step_id_by_name: dict[str, int] = {
        s.name: i for i, s in enumerate(net.steps)
    }
    trans_id_by_index: dict[int, int] = {
        i: len(net.steps) + i for i, _ in enumerate(net.transitions)
    }

    # For each step, collect the localIds of transitions whose
    # to_steps includes it.
    step_incoming: dict[str, list[int]] = {s.name: [] for s in net.steps}
    for ti, tr in enumerate(net.transitions):
        for to_name in tr.to_steps:
            if to_name in step_incoming:
                step_incoming[to_name].append(trans_id_by_index[ti])

    inner_parts: list[str] = []
    for s in net.steps:
        inner_parts.append(_emit_sfc_step_xml(
            s, step_id_by_name[s.name], step_incoming[s.name]
        ))
    for ti, tr in enumerate(net.transitions):
        from_ids = [step_id_by_name[name]
                     for name in tr.from_steps
                     if name in step_id_by_name]
        inner_parts.append(_emit_sfc_transition_xml(
            tr, trans_id_by_index[ti], from_ids
        ))

    inner = "\n".join(_indent(p, "    ") for p in inner_parts)
    return f"<body>\n  <SFC>\n{inner}\n  </SFC>\n</body>"


# -----------------------------------------------------------------------------
# POU element
# -----------------------------------------------------------------------------


def _resolve_pou_type(sub: Subroutine) -> str:
    """Map IL PouKind to PLCopen pouType attribute."""
    if sub.kind in _POU_TYPE:
        return _POU_TYPE[sub.kind]
    # Vendor-extension SUBROUTINE has no PLCopen equivalent.
    return "program" if sub.main else "functionBlock"


def emit_pou_xml(sub: Subroutine) -> str:
    """Emit ``<pou name=... pouType=...>...</pou>`` XML for one Subroutine."""
    pou_type = _resolve_pou_type(sub)
    parts = [f'<pou name={quoteattr(sub.name)} pouType={quoteattr(pou_type)}>']

    # <interface> with variable blocks + optional return type
    interface_inner: list[str] = []

    # FUNCTION return type goes inside <interface> as <returnType>
    if sub.kind is PouKind.FUNCTION and sub.return_type is not None:
        interface_inner.append(
            f"<returnType>{_iec_type_element(sub.return_type)}</returnType>"
        )

    for direction in (VarDirection.INPUT, VarDirection.OUTPUT,
                      VarDirection.IN_OUT, VarDirection.LOCAL,
                      VarDirection.EXTERNAL, VarDirection.TEMP):
        block_vars = [v for v in _vars_by_direction(sub, direction)]
        block = _emit_var_block(direction, block_vars)
        if block is not None:
            interface_inner.append(block)

    if interface_inner:
        inner = "\n".join(_indent(s, "  ") for s in interface_inner)
        parts.append(f"  <interface>\n{inner}\n  </interface>")

    # <body> dispatch -- pick the native graphical form when
    # authored as one; fall through to ST translation otherwise.
    if sub.fbd_body is not None:
        parts.append(_indent(_emit_pou_body_fbd(sub), "  "))
    elif sub.sfc is not None:
        parts.append(_indent(_emit_pou_body_sfc(sub), "  "))
    else:
        parts.append(_indent(_emit_pou_body_st(sub), "  "))

    # documentation
    if sub.comment:
        parts.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(sub.comment)}</p></documentation>'
        )

    parts.append("</pou>")
    return "\n".join(parts)


def _vars_by_direction(sub: Subroutine,
                       direction: VarDirection) -> Sequence[Var]:
    """Look up the var-list field on a Subroutine for one direction."""
    if direction is VarDirection.INPUT:    return sub.inputs
    if direction is VarDirection.OUTPUT:   return sub.outputs
    if direction is VarDirection.IN_OUT:   return sub.in_outs
    if direction is VarDirection.LOCAL:    return sub.local_vars
    return ()                              # EXTERNAL/TEMP not on Subroutine yet


# -----------------------------------------------------------------------------
# Tags (emitted as a synthetic Globals POU's VAR section for portability)
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# User-defined types
# -----------------------------------------------------------------------------


def _emit_struct_baseType(s: StructType) -> str:
    """Render a StructType's body as a ``<baseType><struct>...</struct></baseType>``.

    Inside ``<struct>`` is a ``varListPlain``: a sequence of
    ``<variable name="..."><type>...</type><initialValue/></variable>``
    elements, one per member.  Members are Var instances; we reuse
    ``_emit_var`` to render each.
    """
    member_xml = "\n".join(_indent(_emit_var(m), "    ") for m in s.members)
    return (
        "<baseType>\n"
        "  <struct>\n"
        f"{member_xml}\n"
        "  </struct>\n"
        "</baseType>"
    )


def _emit_array_baseType(a: ArrayType) -> str:
    """Render an ArrayType's body as ``<baseType><array>...</array></baseType>``.

    ``<array>`` has ``<dimension lower=".." upper=".."/>`` per
    dimension and a single ``<baseType>`` for the element type.
    """
    dims = "\n".join(
        f'    <dimension lower="{lo}" upper="{hi}"/>' for lo, hi in a.bounds
    )
    elem_xml = _iec_type_element(a.element_type)
    return (
        "<baseType>\n"
        "  <array>\n"
        f"{dims}\n"
        f"    <baseType>{elem_xml}</baseType>\n"
        "  </array>\n"
        "</baseType>"
    )


def _emit_enum_baseType(e: EnumType) -> str:
    """Render an EnumType's body as ``<baseType><enum>...</enum></baseType>``.

    Each value is a ``<value name="..."/>``.  IEC's optional explicit
    numeric ``value`` attribute is omitted (PLCopen tools assign
    them implicitly based on declaration order).
    """
    values_xml = "\n".join(
        f'      <value name="{escape(v)}"/>' for v in e.values
    )
    return (
        "<baseType>\n"
        "  <enum>\n"
        "    <values>\n"
        f"{values_xml}\n"
        "    </values>\n"
        "  </enum>\n"
        "</baseType>"
    )


def _emit_subrange_baseType(s: SubrangeType) -> str:
    """Render a SubrangeType's body.

    Per the TC6 schema the choice between ``<subrangeSigned>`` and
    ``<subrangeUnsigned>`` is driven by the signedness of the base
    integer type::

        <baseType>
          <subrangeSigned>
            <range lower="-100" upper="100"/>
            <baseType><INT/></baseType>
          </subrangeSigned>
        </baseType>
    """
    elem = "subrangeSigned" if is_signed_subrange(s) else "subrangeUnsigned"
    return (
        "<baseType>\n"
        f"  <{elem}>\n"
        f'    <range lower="{s.lower}" upper="{s.upper}"/>\n'
        f"    <baseType>{_iec_type_element(s.base)}</baseType>\n"
        f"  </{elem}>\n"
        "</baseType>"
    )


def _emit_alias_baseType(a: AliasType) -> str:
    """Render an AliasType's body as ``<baseType>{elem-or-derived}</baseType>``.

    Aliases simply wrap their base type in the dataType's baseType
    element -- no intermediate ``<alias>`` wrapper (the PLCopen
    schema treats alias-of-elementary as a dataType with a plain
    elementary baseType).
    """
    return f"<baseType>{_iec_type_element(a.base)}</baseType>"


def _emit_user_type(ut) -> str:
    """Render one UDT as a ``<dataType name="..">...</dataType>`` element.

    The dispatch picks the right base-type body based on the UDT
    variant; all four IEC §2.3.3 forms are supported.
    """
    if isinstance(ut, StructType):
        body = _emit_struct_baseType(ut)
    elif isinstance(ut, ArrayType):
        body = _emit_array_baseType(ut)
    elif isinstance(ut, EnumType):
        body = _emit_enum_baseType(ut)
    elif isinstance(ut, SubrangeType):
        body = _emit_subrange_baseType(ut)
    elif isinstance(ut, AliasType):
        body = _emit_alias_baseType(ut)
    else:
        raise TypeError(f"not a UserType: {type(ut).__name__}")

    lines = [f'<dataType name={quoteattr(ut.name)}>']
    lines.append(_indent(body, "  "))
    if ut.comment:
        lines.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(ut.comment)}</p></documentation>'
        )
    lines.append("</dataType>")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Synthetic GlobalsHolder (tags as a POU's localVars)
# -----------------------------------------------------------------------------


def _emit_globals_pou(tags: dict) -> Optional[str]:
    """If the program has Tag declarations, emit a synthetic
    ``GlobalsHolder`` PROGRAM POU whose ``<localVars>`` contains them.

    The PLCopen-conformant home for global vars is inside
    ``<configurations><configuration><globalVars>`` -- but emitting
    that requires the full configuration model.  This synthetic POU
    is a portable fallback that round-trips with tools that don't
    require an explicit configuration."""
    if not tags:
        return None
    lines = [
        '<pou name="GlobalsHolder" pouType="program">',
        '  <interface>',
        '    <localVars>',
    ]
    for tag in tags.values():
        # IEC direct-representation addresses go in the schema's
        # `address` attribute; vendor-style addresses (X001 etc.)
        # remain as XML-comment annotations.
        attrs = [f"name={quoteattr(tag.name)}"]
        if tag.address is not None and tag.address.raw.startswith("%"):
            attrs.append(f"address={quoteattr(tag.address.raw)}")
        lines.append(f"      <variable {' '.join(attrs)}>")
        lines.append(f"        <type>{_iec_type_element(tag.data_type)}</type>")
        if tag.address is not None and not tag.address.raw.startswith("%"):
            lines.append(f"        <!-- AT {escape(tag.address.raw)} -->")
        if tag.description:
            lines.append(
                f'        <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
                f'{escape(tag.description)}</p></documentation>'
            )
        lines.append('      </variable>')
    lines.extend([
        '    </localVars>',
        '  </interface>',
        '  <body><ST><pre xmlns="http://www.w3.org/1999/xhtml">(* synthesised globals holder *)'
        '</pre></ST></body>',
        '</pou>',
    ])
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Top-level project document
# -----------------------------------------------------------------------------


def _emit_pou_instance(inst: PouInstance) -> str:
    """One ``<pouInstance name="..." typeName="..."/>`` element.

    Per the TC6 schema, pouInstance has no taskName attribute --
    binding to a task is expressed by *nesting* the pouInstance
    inside the task's element.  Resource-level instances (with no
    task binding) appear directly under ``<resource>``.
    """
    parts: list[str] = [
        f'<pouInstance name={quoteattr(inst.name)} '
        f'typeName={quoteattr(inst.type_name)}'
    ]
    if inst.comment:
        parts.append(">")
        parts.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(inst.comment)}</p></documentation>'
        )
        parts.append("</pouInstance>")
    else:
        parts[-1] += "/>"
    return "\n".join(parts)


def _emit_task(task: TaskSpec,
               instances_for_task: list[PouInstance]) -> str:
    """One ``<task name="..." priority="..." ...>`` element.

    Per the schema, ``<task>`` carries the task's name, priority, and
    one of ``interval`` / ``single`` attributes (interrupt is not in
    the schema's attribute list -- vendor-specific, treated as an
    interval for now).  POU instances scheduled by this task are
    nested as ``<pouInstance>`` children.
    """
    attrs = [f"name={quoteattr(task.name)}"]
    if task.interval is not None:
        attrs.append(f"interval={quoteattr(task.interval)}")
    if task.single is not None:
        attrs.append(f"single={quoteattr(task.single)}")
    # Note: PLCopen TC6 v2.01 doesn't carry an explicit "interrupt"
    # attribute on <task>; we render interrupt tasks with the
    # interrupt source in the single attribute as a vendor-extension
    # fallback (real-world tools vary on this).
    if task.interrupt is not None and task.single is None:
        attrs.append(f"single={quoteattr(task.interrupt)}")
    attrs.append(f"priority={quoteattr(str(task.priority))}")
    open_tag = f"<task {' '.join(attrs)}"

    if not instances_for_task:
        return open_tag + "/>"

    lines = [open_tag + ">"]
    for inst in instances_for_task:
        lines.append(_indent(_emit_pou_instance(inst), "  "))
    lines.append("</task>")
    return "\n".join(lines)


def _emit_globalVars_block(vars_: list[Var]) -> str:
    """Emit a ``<globalVars>`` block containing ``<variable>`` children."""
    if not vars_:
        return ""
    inner = "\n".join(_indent(_emit_var(v), "  ") for v in vars_)
    return f"<globalVars>\n{inner}\n</globalVars>"


def _emit_resource(r: Resource) -> str:
    """One ``<resource name="...">`` element.

    Layout per the TC6 schema::

        <resource name="...">
          <task name="..." priority="..." interval="...">
            <pouInstance .../>     ← bound to this task
          </task>*
          <globalVars>...</globalVars>?
          <pouInstance .../>*      ← resource-level (no task binding)
        </resource>
    """
    parts: list[str] = [f'<resource name={quoteattr(r.name)}>']

    # Group POU instances by task name.
    by_task: dict[str, list[PouInstance]] = {}
    unbound: list[PouInstance] = []
    for inst in r.pou_instances:
        if inst.task is None:
            unbound.append(inst)
        else:
            by_task.setdefault(inst.task, []).append(inst)

    # Emit tasks, each with its bound instances.
    for task in r.tasks:
        parts.append(_indent(_emit_task(task, by_task.get(task.name, [])),
                             "  "))

    # Resource-level globals.
    if r.global_vars:
        parts.append(_indent(_emit_globalVars_block(r.global_vars), "  "))

    # Unbound POU instances at the resource level.
    for inst in unbound:
        parts.append(_indent(_emit_pou_instance(inst), "  "))

    parts.append("</resource>")
    return "\n".join(parts)


def _emit_configuration(cfg: Configuration) -> str:
    """One ``<configuration name="...">`` element.

    Layout per the TC6 schema::

        <configuration name="...">
          <resource ...>...</resource>*
          <globalVars>...</globalVars>?
          <accessVars>...</accessVars>?
        </configuration>

    accessVars uses ``ppx:varListAccess`` -- structurally a varList
    with extra attributes per declaration; we emit each as a plain
    ``<variable>`` for now (the schema's extra attributes are
    optional).
    """
    parts: list[str] = [f'<configuration name={quoteattr(cfg.name)}>']

    for r in cfg.resources:
        parts.append(_indent(_emit_resource(r), "  "))

    if cfg.global_vars:
        parts.append(_indent(_emit_globalVars_block(cfg.global_vars), "  "))

    if cfg.access_vars:
        parts.append(_indent(_emit_access_vars(cfg.access_vars), "  "))

    if cfg.config_vars:
        parts.append(_indent(_emit_config_vars(cfg.config_vars), "  "))

    parts.append("</configuration>")
    return "\n".join(parts)


#: PLCopen XSD's ``accessType`` enumeration uses camelCase, but IEC
#: ST keywords are uppercase with underscores.  Map between them.
_ACCESS_DIRECTION_XML = {
    "READ_ONLY":  "readOnly",
    "READ_WRITE": "readWrite",
}


def _emit_access_vars(access_vars: Sequence[AccessVar]) -> str:
    """``<accessVars>`` block per the PLCopen TC6 ``varListAccess``
    type.

    Each ``AccessVar`` becomes one ``<accessVariable>`` with the
    required ``alias`` and ``instancePathAndName`` attributes plus
    the optional ``direction`` (``readOnly`` / ``readWrite``).
    Body holds the ``<type>`` declaration and optional
    ``<documentation>`` for comments.
    """
    inner_lines: list[str] = []
    for v in access_vars:
        attrs = (
            f"alias={quoteattr(v.alias)} "
            f"instancePathAndName={quoteattr(v.instance_path)}"
        )
        direction_xml = _ACCESS_DIRECTION_XML.get(v.direction)
        if direction_xml is not None:
            attrs += f' direction="{direction_xml}"'
        elem = (
            f"  <accessVariable {attrs}>\n"
            f"    <type>{_iec_type_element(v.data_type)}</type>"
        )
        if v.comment:
            elem += (
                f"\n    <documentation>"
                f'<p xmlns="http://www.w3.org/1999/xhtml">'
                f"{escape(v.comment)}</p></documentation>"
            )
        elem += "\n  </accessVariable>"
        inner_lines.append(elem)
    return "<accessVars>\n" + "\n".join(inner_lines) + "\n</accessVars>"


def _emit_config_vars(config_vars: Sequence[ConfigVar]) -> str:
    """``<configVars>`` block per the PLCopen TC6 ``varListConfig``
    type.

    Each ``ConfigVar`` becomes one ``<configVariable>`` with the
    required ``instancePathAndName`` attribute and a ``<type>`` /
    optional ``<initialValue>`` body.
    """
    inner_lines: list[str] = []
    for v in config_vars:
        attrs = f"instancePathAndName={quoteattr(v.instance_path)}"
        body_lines = [f"    <type>{_iec_type_element(v.data_type)}</type>"]
        if v.initial_value:
            # PLCopen schema's ``<initialValue>`` wraps a ``<simpleValue
            # value="..."/>`` for elementary types.  We keep the
            # textual form verbatim so IEC-typed literals
            # (``T#100ms``, ``16#FF``, etc.) round-trip.
            body_lines.append(
                f'    <initialValue>'
                f'<simpleValue value={quoteattr(v.initial_value)}/>'
                f"</initialValue>"
            )
        if v.comment:
            body_lines.append(
                f"    <documentation>"
                f'<p xmlns="http://www.w3.org/1999/xhtml">'
                f"{escape(v.comment)}</p></documentation>"
            )
        inner_lines.append(
            f"  <configVariable {attrs}>\n"
            + "\n".join(body_lines)
            + "\n  </configVariable>"
        )
    return "<configVars>\n" + "\n".join(inner_lines) + "\n</configVars>"


def emit_xml(prog: Program,
             company: str = "universal_machinery",
             product: str = "universal_machinery IL emitter",
             content_description: str = "",
             time_now: Optional[datetime] = None) -> str:
    """Emit the full PLCopen TC6 XML document for a Program.

    ``company`` / ``product`` populate ``<fileHeader>``.  Conformant
    consumers don't typically inspect these but the schema requires
    them.  ``time_now`` is used for both ``creationDateTime`` (in
    fileHeader) and ``modificationDateTime`` (in contentHeader);
    defaults to the current UTC time.  Callers wanting deterministic
    output (for round-trip byte-equality tests, version control)
    should pass a fixed datetime.
    """
    if time_now is None:
        time_now = datetime.now(timezone.utc)
    iso_ts = time_now.replace(microsecond=0).isoformat()

    parts: list[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>')
    parts.append(
        f'<project xmlns={quoteattr(PLCOPEN_NS)} '
        f'xmlns:xhtml="http://www.w3.org/1999/xhtml" '
        f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    )

    parts.append(
        '  <fileHeader'
        f' companyName={quoteattr(company)}'
        f' productName={quoteattr(product)}'
        f' productVersion={quoteattr("0.1")}'
        f' creationDateTime={quoteattr(iso_ts)}/>'
    )
    parts.append(
        '  <contentHeader'
        f' name={quoteattr(prog.project_name or "untitled")}'
        f' modificationDateTime={quoteattr(iso_ts)}>'
    )
    if content_description or prog.comment:
        desc = content_description or prog.comment
        parts.append(f"    <Comment>{escape(desc)}</Comment>")
    parts.append('    <coordinateInfo>')
    # PLCopen schema requires page-size info even if we don't lay out
    # graphical bodies; sensible defaults.
    parts.append('      <pageSize x="1024" y="768"/>')
    parts.append('      <fbd><scaling x="1" y="1"/></fbd>')
    parts.append('      <ld><scaling x="1" y="1"/></ld>')
    parts.append('      <sfc><scaling x="1" y="1"/></sfc>')
    parts.append('    </coordinateInfo>')
    parts.append('  </contentHeader>')

    parts.append('  <types>')
    if prog.user_types:
        parts.append('    <dataTypes>')
        for ut in prog.user_types:
            parts.append(_indent(_emit_user_type(ut), "      "))
        parts.append('    </dataTypes>')
    else:
        parts.append('    <dataTypes/>')
    parts.append('    <pous>')

    globals_pou = _emit_globals_pou(prog.tags)
    if globals_pou is not None:
        parts.append(_indent(globals_pou, "      "))

    for sub in prog.subroutines:
        parts.append(_indent(emit_pou_xml(sub), "      "))

    parts.append('    </pous>')
    parts.append('  </types>')
    if prog.configurations:
        parts.append('  <instances>')
        parts.append('    <configurations>')
        for cfg in prog.configurations:
            parts.append(_indent(_emit_configuration(cfg), "      "))
        parts.append('    </configurations>')
        parts.append('  </instances>')
    else:
        parts.append('  <instances><configurations/></instances>')
    parts.append('</project>')
    return "\n".join(parts) + "\n"


# -----------------------------------------------------------------------------
# Schema validation
# -----------------------------------------------------------------------------


#: Name of the bundled PLCopen TC6 v2.01 XSD file.
_BUNDLED_XSD_NAME = "tc6_xml_v201.xsd"


class XMLSchemaError(Exception):
    """Raised by ``validate_plcopen_xml`` when the document doesn't
    conform to the PLCopen TC6 schema, or when the validator
    dependency (``xmlschema``) isn't installed.

    Carries the underlying validator's diagnostic message verbatim so
    callers can surface it as a compile error or CI failure.
    """


def bundled_xsd_path() -> Path:
    """Return the filesystem path of the PLCopen TC6 XSD that ships
    with this package.

    The schema is installed as package data; resolved via
    ``importlib.resources``.  Useful when integrating with external
    XSD-validating tools that take a schema path argument.
    """
    pkg = "universal_machinery.emitters.schemas"
    return Path(resources.files(pkg) / _BUNDLED_XSD_NAME)  # type: ignore[arg-type]


def validate_plcopen_xml(xml_text: str,
                         xsd_path: Optional[Path] = None) -> None:
    """Validate ``xml_text`` against a PLCopen TC6 XSD.

    On success, returns ``None``.  On failure, raises
    ``XMLSchemaError`` carrying the underlying validator's message
    (line/column/path of the offending element).

    ``xsd_path`` defaults to the bundled v2.01 schema; pass an
    explicit path when targeting a different schema version (e.g.
    the user's own PLCopen-member-distributed copy).

    Requires the ``xmlschema`` package -- install via the
    ``[validation]`` extra (or ``[dev]``):

        pip install universal_machinery[validation]

    Raises ``XMLSchemaError`` if the dependency is missing.

    Round-trip discipline -- this function is the cert verification
    loop's first checkpoint.  A real cert claim additionally needs
    a round-trip through PLCopen's reference tools (matiec,
    Beremiz, OpenPLC editor, etc.) and ideally hardware behaviour
    verification.  This XSD check is necessary but not sufficient.
    """
    try:
        import xmlschema    # type: ignore[import-not-found]
    except ImportError as exc:
        raise XMLSchemaError(
            "xmlschema package is required for PLCopen XSD validation. "
            "Install with: pip install universal_machinery[validation]"
        ) from exc

    if xsd_path is None:
        xsd_path = bundled_xsd_path()

    if not Path(xsd_path).exists():
        raise XMLSchemaError(f"XSD schema file not found: {xsd_path}")

    schema = _load_schema(str(xsd_path))
    try:
        schema.validate(xml_text)
    except xmlschema.XMLSchemaException as exc:
        raise XMLSchemaError(str(exc)) from exc


@lru_cache(maxsize=8)
def _load_schema(path: str):
    """Cache parsed ``XMLSchema`` instances by path.

    ``xmlschema.XMLSchema(...)`` is non-trivial -- parsing the
    1700-line TC6 XSD takes ~1s.  Validation against multiple XML
    documents in the same process (tests, CI, batch validation)
    should reuse the parsed schema."""
    import xmlschema    # imported lazily; presence already checked above
    return xmlschema.XMLSchema(path)


def is_valid_plcopen_xml(xml_text: str,
                         xsd_path: Optional[Path] = None) -> bool:
    """Convenience wrapper: return ``True``/``False`` instead of
    raising.  Doesn't surface the validation message -- use
    ``validate_plcopen_xml`` directly when you need diagnostics.
    """
    try:
        validate_plcopen_xml(xml_text, xsd_path=xsd_path)
    except XMLSchemaError:
        return False
    return True
