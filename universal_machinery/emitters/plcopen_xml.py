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
    OutVariable, PouInstance, PouKind, Position, Program, Resource, Rung,
    SfcNetwork, Step, StructType, SubrangeType, Subroutine, Tag, TagRef,
    TagType, TaskSpec, Transition, Var, VarDirection, is_signed_subrange,
)
from ..il.ops import (
    BinaryMath, Compare, ContactFallingEdge, ContactNC, ContactNO,
    ContactRisingEdge, Move, OutCoil, OutReset, OutSet, ParallelGroup,
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
    VarDirection.GLOBAL:   "globalVars",
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
        # PLCopen TC6 v2.01 schema uses lowercase tag names for
        # the variable-length character-string types
        # (``<string/>`` and ``<wstring/>``); everything else uses
        # the uppercase IEC keyword.
        if t is TagType.STRING:
            return "<string/>"
        if t is TagType.WSTRING:
            return "<wstring/>"
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
                       incoming: list) -> str:
    """One ``<step>`` (or ``<macroStep>``) element.

    ``incoming`` is the list of upstream sources for this step's
    incoming connection point.  Each entry is either:

      - an ``int`` localId (plain wire from a transition / marker)
      - a ``tuple[int, str]`` ``(src_id, formal_parameter)`` --
        used when the source is a divergence marker output pin

    If ``step.macro`` is set, this is a *macro step* per IEC
    §2.6.5: we emit ``<macroStep ...>...<body>...</body></macroStep>``
    instead of ``<step>...</step>``.  The macroStep's body carries
    its own (locally-scoped) SFC network.
    """
    is_macro = step.macro is not None
    tag = "macroStep" if is_macro else "step"
    attrs = [
        f'localId="{local_id}"',
        f"name={quoteattr(step.name)}",
    ]
    # macroStep doesn't carry initialStep in the XSD -- only plain
    # steps do.  An initial macro step is still legal per the spec,
    # but it's expressed via the inner network's initial steps;
    # we silently drop ``step.initial`` for macro steps.
    if step.initial and not is_macro:
        attrs.append('initialStep="true"')
    parts = [f"<{tag} {' '.join(attrs)}>",
             f"  {_position_xml(_sfc_step_position(local_id))}"]
    if incoming:
        inner_conns = "\n".join(
            _sfc_inner_connection_xml(src) for src in incoming
        )
        parts.append("  <connectionPointIn>")
        parts.append(inner_conns)
        parts.append("  </connectionPointIn>")
    # The schema requires a connectionPointOut on every step that
    # has an outgoing wire.  We always include it so steps with
    # downstream transitions stay well-formed.  Per the XSD,
    # ``<step>`` requires a formalParameter on its connectionPointOut
    # but ``<macroStep>`` does not.
    if is_macro:
        parts.append('  <connectionPointOut/>')
    else:
        parts.append('  <connectionPointOut formalParameter="OUT"/>')
    if step.actions and not is_macro:
        # Action blocks attach via this separate pin; the schema
        # requires a formalParameter so we use "OUT_ACTION".
        # macroStep doesn't carry actions (the inner network does
        # its own action handling) -- we don't emit OUT_ACTION
        # for macro steps even if Step.actions happens to be set.
        parts.append('  <connectionPointOutAction '
                     'formalParameter="OUT_ACTION"/>')
    if is_macro:
        # Emit the nested SFC body inside <body><SFC>...</SFC></body>.
        # The inner network's localIds are scoped to the macro --
        # they share no namespace with the outer body's IDs.
        inner_net = _emit_sfc_network_content(step.macro)
        parts.append("  <body>")
        parts.append("    <SFC>")
        # _emit_sfc_network_content already 4-space indents each
        # element; we add another 4 spaces to nest it inside
        # macroStep > body > SFC.
        parts.append(_indent(inner_net, "    "))
        parts.append("    </SFC>")
        parts.append("  </body>")
    if step.comment:
        parts.append(
            f'  <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
            f'{escape(step.comment)}</p></documentation>'
        )
    parts.append(f"</{tag}>")
    return "\n".join(parts)


#: PLCopen TC6 v2.01 valid ``actionQualifierType`` enum values.
#: An IL ``Action.qualifier`` outside this set falls back to ``N``
#: (Non-stored) for emission so the XML stays XSD-valid.
_ACTION_QUALIFIERS = frozenset({
    "P1", "N", "P0", "R", "S", "L", "D", "P",
    "DS", "DL", "SD", "SL",
})


def _action_target_text(target) -> str:
    """One ``Action.target`` -> the textual ``reference name=``.

    Per ``Action`` docstring, ``target`` is either an ``Address``
    (boolean coil) or a string naming an action / FB.  We render
    both the same way -- the ``<reference name=>`` body carries
    the operand text.
    """
    if isinstance(target, Address):
        return target.raw
    return str(target)


def _emit_sfc_action_block_xml(actions, local_id: int,
                                 step_id: int) -> str:
    """One ``<actionBlock>`` element attached to a step.

    Wires back to the step via ``<connectionPointIn>`` →
    ``refLocalId=step_id`` + ``formalParameter="OUT_ACTION"``.
    Each IL ``Action`` becomes one ``<action>`` child with its
    qualifier, optional duration, and a ``<reference name=>``
    body for the target.
    """
    parts = [f'<actionBlock localId="{local_id}">',
             f"  {_position_xml(_sfc_action_position(local_id))}",
             "  <connectionPointIn>",
             f'    <connection refLocalId="{step_id}" '
             f'formalParameter="OUT_ACTION"/>',
             "  </connectionPointIn>"]
    next_action_id = local_id + 1
    for action in actions:
        qual = (action.qualifier
                  if action.qualifier in _ACTION_QUALIFIERS
                  else "N")
        attrs = [f'localId="{next_action_id}"',
                  f'qualifier="{qual}"']
        if action.time_ms is not None:
            attrs.append(f'duration="T#{action.time_ms}ms"')
        next_action_id += 1
        # Per XSD the inner <action> child uses <relPosition>, not
        # <position>; the action block itself owns the absolute
        # position, the action's relPosition is its offset from
        # that anchor.
        action_parts = [f"  <action {' '.join(attrs)}>",
                         f'    <relPosition x="0" y="{(next_action_id - local_id - 1) * 20}"/>']
        # Body is a choice: <inline> wins over <reference> when the
        # IL Action carries an ``inline_body`` (a list of ST AST
        # statements).  Otherwise we render <reference name=> per
        # the target text.
        if action.inline_body:
            # The XSD's <action><inline> element has type ppx:body
            # WITHOUT the name=... attribute that transition's
            # condition <inline> requires (the two elements are
            # named the same but are different complexType
            # definitions).  Body content is a choice of IL / ST /
            # LD / FBD / SFC; we emit ST since the IL inline_body
            # is a list of ST AST statements.
            body_text = "\n".join(
                emit_st_body(list(action.inline_body),
                                indent="    ", level=0)
            )
            action_parts.append('    <inline>')
            action_parts.append('      <ST>')
            action_parts.append(
                f'        <xhtml:pre xmlns:xhtml="http://www.w3.org/1999/xhtml">'
                f'{escape(body_text)}</xhtml:pre>'
            )
            action_parts.append('      </ST>')
            action_parts.append('    </inline>')
        else:
            action_parts.append(
                f'    <reference name='
                f'{quoteattr(_action_target_text(action.target))}/>'
            )
        if action.comment:
            action_parts.append(
                f'    <documentation><p xmlns="http://www.w3.org/1999/xhtml">'
                f'{escape(action.comment)}</p></documentation>'
            )
        action_parts.append("  </action>")
        parts.extend(action_parts)
    parts.append("</actionBlock>")
    return "\n".join(parts)


def _sfc_action_position(idx: int) -> Position:
    """Action-block layout: rightward of the step column."""
    return Position(x=_SFC_GRID_X + 200.0,
                     y=20.0 + idx * _SFC_GRID_Y * 2)


def _sfc_divergence_position(idx: int) -> Position:
    """Divergence / convergence marker layout: column to the left
    of the step ladder so the markers don't overlap step rows."""
    return Position(x=_SFC_GRID_X - 100.0,
                     y=20.0 + idx * _SFC_GRID_Y)


def _emit_simultaneous_divergence_xml(local_id: int, source_id: int,
                                       num_outs: int) -> str:
    """``<simultaneousDivergence>`` -- one transition fanning out to
    multiple parallel steps.

    The single ``<connectionPointIn>`` references the upstream
    transition; each of ``num_outs`` ``<connectionPointOut>`` pins
    carries ``formalParameter="OUT<i>"`` so destination steps can
    point back at this marker via the right branch.
    """
    parts = [f'<simultaneousDivergence localId="{local_id}">',
             f"  {_position_xml(_sfc_divergence_position(local_id))}",
             "  <connectionPointIn>",
             f'    <connection refLocalId="{source_id}"/>',
             "  </connectionPointIn>"]
    for i in range(num_outs):
        parts.append(
            f'  <connectionPointOut formalParameter="OUT{i}"/>'
        )
    parts.append("</simultaneousDivergence>")
    return "\n".join(parts)


def _emit_simultaneous_convergence_xml(local_id: int,
                                         source_ids: list[int]) -> str:
    """``<simultaneousConvergence>`` -- multiple steps that must all
    be active before the downstream transition fires.

    Each ``<connectionPointIn>`` is its own element (XSD models
    ``minOccurs=0 maxOccurs=unbounded`` for convergence inputs --
    distinct from the single-element ``connectionPointIn`` on
    most other shapes).  The single ``<connectionPointOut>``
    feeds the downstream transition (which references this
    convergence's localId in its own connectionPointIn).
    """
    parts = [f'<simultaneousConvergence localId="{local_id}">',
             f"  {_position_xml(_sfc_divergence_position(local_id))}"]
    for src in source_ids:
        parts.append("  <connectionPointIn>")
        parts.append(f'    <connection refLocalId="{src}"/>')
        parts.append("  </connectionPointIn>")
    parts.append("  <connectionPointOut/>")
    parts.append("</simultaneousConvergence>")
    return "\n".join(parts)


def _emit_selection_divergence_xml(local_id: int, source_id: int,
                                     num_outs: int) -> str:
    """``<selectionDivergence>`` -- one step that branches into
    several mutually-exclusive transitions (only one fires per
    scan based on the guard).

    Identical wire shape to simultaneousDivergence; the marker
    type distinguishes XOR-fanout from AND-fanout for tools that
    care about scheduling semantics.
    """
    parts = [f'<selectionDivergence localId="{local_id}">',
             f"  {_position_xml(_sfc_divergence_position(local_id))}",
             "  <connectionPointIn>",
             f'    <connection refLocalId="{source_id}"/>',
             "  </connectionPointIn>"]
    for i in range(num_outs):
        parts.append(
            f'  <connectionPointOut formalParameter="OUT{i}"/>'
        )
    parts.append("</selectionDivergence>")
    return "\n".join(parts)


def _emit_sfc_jump_step_xml(local_id: int, source_id: int,
                              target_name: str) -> str:
    """``<jumpStep>`` -- a named jump target on an SFC back-edge.

    The XSD models jumpStep as "acts like a step" with a single
    ``<connectionPointIn>`` (where the upstream transition lands)
    and a required ``targetName=`` naming the actual destination
    step.  Note: there is no ``<connectionPointOut>`` -- the
    semantic linkage to the target is by name, not by wire.
    """
    return "\n".join([
        f'<jumpStep localId="{local_id}" '
        f'targetName={quoteattr(target_name)}>',
        f"  {_position_xml(_sfc_divergence_position(local_id))}",
        "  <connectionPointIn>",
        f'    <connection refLocalId="{source_id}"/>',
        "  </connectionPointIn>",
        "</jumpStep>",
    ])


def _emit_selection_convergence_xml(local_id: int,
                                      source_ids: list[int]) -> str:
    """``<selectionConvergence>`` -- multiple transitions any one
    of which (mutually exclusive at runtime) leads to the same
    downstream step.
    """
    parts = [f'<selectionConvergence localId="{local_id}">',
             f"  {_position_xml(_sfc_divergence_position(local_id))}"]
    for src in source_ids:
        parts.append("  <connectionPointIn>")
        parts.append(f'    <connection refLocalId="{src}"/>')
        parts.append("  </connectionPointIn>")
    parts.append("  <connectionPointOut/>")
    parts.append("</selectionConvergence>")
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


def _sfc_inner_connection_xml(src) -> str:
    """One ``<connection refLocalId=... [formalParameter=...]/>``
    line.  ``src`` is either a bare int (plain wire) or a 2-tuple
    ``(local_id, formal_parameter)`` for divergence marker
    outputs (which carry per-branch pin names like ``OUT0``).
    """
    if isinstance(src, tuple):
        sid, fp = src
        return (f'    <connection refLocalId="{sid}" '
                f'formalParameter={quoteattr(fp)}/>')
    return f'    <connection refLocalId="{src}"/>'


def _emit_sfc_transition_xml(trans: Transition, local_id: int,
                             from_sources: list) -> str:
    """One ``<transition>`` element.

    ``from_sources`` is the list of upstream localIds feeding this
    transition's incoming connection point.  Each entry is either:

      - an ``int`` localId (the simple "step → transition" wire,
        or the post-selection-divergence output pin)
      - a ``tuple[int, str]`` ``(src_id, formal_parameter)`` --
        used when the source is a marker output pin

    The condition lowers to ``<condition><inline name="cond">
    <ST><xhtml:pre>...</pre></ST></inline></condition>`` so any
    PLCopen-conformant tool can re-parse it.
    """
    parts = [f'<transition localId="{local_id}">',
             f"  {_position_xml(_sfc_transition_position(local_id))}"]
    if from_sources:
        inner_conns = "\n".join(
            _sfc_inner_connection_xml(src) for src in from_sources
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

    For steps with non-empty ``actions`` we additionally emit an
    ``<actionBlock>`` per step (one ``<action>`` child per IL
    ``Action``) wired back via the step's ``OUT_ACTION`` pin.

    Branching shapes (IEC §2.6.3) emit explicit marker nodes:

      - simultaneous divergence: a Transition with multiple
        ``to_steps`` lowers to ``<simultaneousDivergence>``
        between the transition and its destination steps
      - simultaneous convergence: a Transition with multiple
        ``from_steps`` lowers to ``<simultaneousConvergence>``
        between the source steps and the transition
      - selection divergence: a Step that's the only ``from_step``
        of multiple Transitions lowers to
        ``<selectionDivergence>`` between the step and those
        transitions
      - selection convergence: a Step that's the only ``to_step``
        of multiple Transitions lowers to
        ``<selectionConvergence>`` between those transitions and
        the step
    """
    net = sub.sfc
    if net is None or (not net.steps and not net.transitions):
        return "<body>\n  <SFC/>\n</body>"
    inner = _emit_sfc_network_content(net)
    return f"<body>\n  <SFC>\n{inner}\n  </SFC>\n</body>"


def _emit_sfc_network_content(net) -> str:
    """Lay out + emit the inner element block of one SFC network.

    Returns the concatenated, ``_indent``-padded text that goes
    *inside* a ``<SFC>...</SFC>`` wrapper.  Used both by the
    top-level POU body and by ``<macroStep>`` bodies, which carry
    their own (locally-scoped) SFC network.

    LocalIds in the returned content start from 0 and are unique
    only within this network -- the PLCopen XSD treats each
    ``<body>`` as its own localId scope.
    """
    # ----- Phase 1: allocate localIds for steps + transitions.
    step_id_by_name: dict[str, int] = {
        s.name: i for i, s in enumerate(net.steps)
    }
    trans_id_by_index: dict[int, int] = {
        i: len(net.steps) + i for i, _ in enumerate(net.transitions)
    }
    next_local_id = len(net.steps) + len(net.transitions)

    # ----- Phase 2: identify divergence/convergence markers.
    # We detect the four shapes from the IL graph topology and
    # allocate a marker localId for each, plus track the marker
    # endpoints so step / transition wiring can route through them.
    sim_div_for_trans: dict[int, int] = {}   # trans_index -> marker_id
    sim_conv_for_trans: dict[int, int] = {}  # trans_index -> marker_id
    sel_div_for_step: dict[str, int] = {}    # step_name -> marker_id
    sel_conv_for_step: dict[str, int] = {}   # step_name -> marker_id

    for ti, tr in enumerate(net.transitions):
        if len(tr.from_steps) > 1:
            sim_conv_for_trans[ti] = next_local_id
            next_local_id += 1
        if len(tr.to_steps) > 1:
            sim_div_for_trans[ti] = next_local_id
            next_local_id += 1

    # Selection markers only apply to "single-to / single-from"
    # transitions sharing a step -- multi-from/to transitions
    # already get a simultaneous marker per above.
    outgoing_by_step: dict[str, list[int]] = {s.name: [] for s in net.steps}
    incoming_by_step: dict[str, list[int]] = {s.name: [] for s in net.steps}
    for ti, tr in enumerate(net.transitions):
        if len(tr.from_steps) == 1 and tr.from_steps[0] in outgoing_by_step:
            outgoing_by_step[tr.from_steps[0]].append(ti)
        if len(tr.to_steps) == 1 and tr.to_steps[0] in incoming_by_step:
            incoming_by_step[tr.to_steps[0]].append(ti)

    for s in net.steps:
        if len(outgoing_by_step[s.name]) > 1:
            sel_div_for_step[s.name] = next_local_id
            next_local_id += 1
        if len(incoming_by_step[s.name]) > 1:
            sel_conv_for_step[s.name] = next_local_id
            next_local_id += 1

    # ----- Phase 2b: detect back-edge transitions for jumpStep emit.
    # A back-edge is a single-to transition whose target was declared
    # before the transition's from_step in net.steps order -- the
    # classic "loop back to an earlier step" pattern.  We promote
    # these to ``<jumpStep>`` markers so the rendered ladder doesn't
    # carry a long backward wire.  Skipped if the transition is
    # already part of any other marker (selection conv would have
    # claimed it; simultaneous div/conv have multi-to/multi-from
    # which definitionally excludes back-edge promotion).
    jump_step_for_trans: dict[int, int] = {}  # trans_index -> jumpStep_id
    for ti, tr in enumerate(net.transitions):
        if ti in sim_conv_for_trans or ti in sim_div_for_trans:
            continue
        if len(tr.to_steps) != 1 or len(tr.from_steps) != 1:
            continue
        to_name = tr.to_steps[0]
        from_name = tr.from_steps[0]
        if to_name in sel_conv_for_step:
            continue
        if to_name not in step_id_by_name or from_name not in step_id_by_name:
            continue
        # A back-edge: target declared strictly earlier than source.
        if step_id_by_name[to_name] < step_id_by_name[from_name]:
            jump_step_for_trans[ti] = next_local_id
            next_local_id += 1

    # ----- Phase 3: compute connectionPointIn refs for steps + transitions.
    # The presence of a marker reroutes the wiring through it:
    # a step "downstream" of a sim_div sees the marker (with
    # formalParameter pin), not the original transition.
    trans_sources: dict[int, list] = {ti: [] for ti in range(len(net.transitions))}
    step_sources: dict[str, list] = {s.name: [] for s in net.steps}

    for ti, tr in enumerate(net.transitions):
        # ---- transition's incoming side
        if ti in sim_conv_for_trans:
            # Multi-from -> the convergence node feeds the transition
            trans_sources[ti].append(sim_conv_for_trans[ti])
        else:
            # Single-from (or empty) -- the source step feeds us
            # directly, unless that step has selection-divergence
            # outgoing in which case we route through the marker.
            for src_name in tr.from_steps:
                if src_name not in step_id_by_name:
                    continue
                if src_name in sel_div_for_step:
                    # Find this transition's branch index among the
                    # step's outgoing transitions.
                    branch = outgoing_by_step[src_name].index(ti)
                    trans_sources[ti].append(
                        (sel_div_for_step[src_name], f"OUT{branch}")
                    )
                else:
                    trans_sources[ti].append(step_id_by_name[src_name])

    for ti, tr in enumerate(net.transitions):
        # ---- transition's outgoing side: feeds each to_step
        # (possibly through a sim_div, then possibly through a sel_conv).
        # Back-edge transitions route through a jumpStep instead --
        # the target step's connectionPointIn doesn't include this
        # transition's id at all (the jumpStep terminates the wire
        # visually; the semantic link is by name).
        if ti in jump_step_for_trans:
            continue
        for to_idx, to_name in enumerate(tr.to_steps):
            if to_name not in step_sources:
                continue
            if ti in sim_div_for_trans:
                # The destination step receives from the sim_div's
                # branch pin, not the transition directly.
                src = (sim_div_for_trans[ti], f"OUT{to_idx}")
            else:
                src = trans_id_by_index[ti]
            # If the destination step has selection-convergence
            # incoming, route through that marker.  Note: we add
            # the transition's id (or the sim_div pin) to the
            # sel_conv's incoming list, not the step's directly.
            if to_name in sel_conv_for_step and len(tr.to_steps) == 1:
                # We'll wire the sel_conv inputs after this loop;
                # tag this source for that step's sel_conv.
                pass  # handled below
            else:
                step_sources[to_name].append(src)

    # Selection-convergence inputs: the marker's connectionPointIn
    # list collects every transition feeding the destination step.
    # The step itself just refs the marker.
    sel_conv_inputs: dict[int, list] = {
        local_id: [] for local_id in sel_conv_for_step.values()
    }
    for ti, tr in enumerate(net.transitions):
        if len(tr.to_steps) != 1:
            continue
        to_name = tr.to_steps[0]
        if to_name not in sel_conv_for_step:
            continue
        # source for this leg: either the transition directly,
        # or through sim_div if it has one (rare; would mean a
        # transition has 1 to_step AND multi to_steps -- impossible
        # so this is just the transition).
        sel_conv_inputs[sel_conv_for_step[to_name]].append(
            trans_id_by_index[ti]
        )

    for s in net.steps:
        if s.name in sel_conv_for_step:
            step_sources[s.name].append(sel_conv_for_step[s.name])

    # ----- Phase 4: emit everything.  Steps + transitions first,
    # then markers, then action blocks (markers must precede
    # action-block IDs since marker allocation came earlier).
    inner_parts: list[str] = []
    for s in net.steps:
        inner_parts.append(_emit_sfc_step_xml(
            s, step_id_by_name[s.name], step_sources[s.name]
        ))
    for ti, tr in enumerate(net.transitions):
        inner_parts.append(_emit_sfc_transition_xml(
            tr, trans_id_by_index[ti], trans_sources[ti]
        ))

    # Marker emission: simultaneous div/conv tied to transitions,
    # then selection div/conv tied to steps.
    for ti in sorted(sim_conv_for_trans):
        marker_id = sim_conv_for_trans[ti]
        src_ids = [step_id_by_name[name]
                   for name in net.transitions[ti].from_steps
                   if name in step_id_by_name]
        inner_parts.append(_emit_simultaneous_convergence_xml(
            marker_id, src_ids
        ))
    for ti in sorted(sim_div_for_trans):
        marker_id = sim_div_for_trans[ti]
        num_outs = len(net.transitions[ti].to_steps)
        inner_parts.append(_emit_simultaneous_divergence_xml(
            marker_id, trans_id_by_index[ti], num_outs
        ))
    for name in net.steps:
        s_name = name.name
        if s_name in sel_div_for_step:
            marker_id = sel_div_for_step[s_name]
            num_outs = len(outgoing_by_step[s_name])
            inner_parts.append(_emit_selection_divergence_xml(
                marker_id, step_id_by_name[s_name], num_outs
            ))
        if s_name in sel_conv_for_step:
            marker_id = sel_conv_for_step[s_name]
            inner_parts.append(_emit_selection_convergence_xml(
                marker_id, sel_conv_inputs[marker_id]
            ))

    # jumpStep emission: one per back-edge transition.  The
    # jumpStep "consumes" the transition's outgoing wire and names
    # the target step textually -- the target step's
    # connectionPointIn does NOT reference this transition.
    for ti in sorted(jump_step_for_trans):
        jump_id = jump_step_for_trans[ti]
        inner_parts.append(_emit_sfc_jump_step_xml(
            jump_id, trans_id_by_index[ti],
            net.transitions[ti].to_steps[0]
        ))

    # Action blocks: allocated after all markers so IDs stay unique.
    for s in net.steps:
        if not s.actions:
            continue
        inner_parts.append(_emit_sfc_action_block_xml(
            s.actions, next_local_id, step_id_by_name[s.name]
        ))
        next_local_id += 1 + len(s.actions)

    return "\n".join(_indent(p, "    ") for p in inner_parts)


# -----------------------------------------------------------------------------
# POU body (LD) -- IEC §6.6 / PLCopen <LD>
# -----------------------------------------------------------------------------


#: Op kinds that can lower to native LD primitives.  Any rung
#: containing an op outside this set falls back to ST-text
#: emission so the body stays well-formed (mixed LD + FBD blocks
#: are valid per the XSD but deferred -- a future slice routes
#: math / call / stdlib ops through the FBD ``<block>`` shape).
#:
#: Edge contacts (rising / falling) emit via the standard
#: ``<contact>`` element with the XSD-defined ``edge=`` attribute.
#: ParallelGroup lowers to multi-incoming wires at the branch
#: join (the next op after the group), recursively handling
#: nested groups inside branches.
_NATIVE_LD_OPS = (
    ContactNO, ContactNC, ContactRisingEdge, ContactFallingEdge,
    OutCoil, OutSet, OutReset, ParallelGroup,
    # Compare ops lower into FBD ``<block typeName="GT|GE|EQ|LE|LT|NE">``
    # elements embedded in the LD body, with two ``<inVariable>``
    # operand sources and the block's OUT pin feeding the rung gate.
    Compare,
    # Move ops lower into ``<block typeName="MOVE">`` with an
    # ``<inVariable>`` source feeding IN and an ``<outVariable>``
    # destination receiving OUT.  The block's ENO output continues
    # the rung's boolean gate so chained ops still work.
    Move,
    # BinaryMath ops (ADD / SUB / MUL / DIV / MOD) lower into
    # ``<block typeName="...">`` with two ``<inVariable>`` operand
    # sources feeding IN1 / IN2 and an ``<outVariable>``
    # destination receiving OUT (IEC §2.5.2.5).
    BinaryMath,
)


#: IL BinaryMath.op symbol -> IEC §2.5.2.5 arithmetic function name.
#: Used for ``<block typeName=...>`` when lowering BinaryMath into LD.
_BINARY_MATH_OP_TO_BLOCK_NAME = {
    "+": "ADD",
    "-": "SUB",
    "*": "MUL",
    "/": "DIV",
    "%": "MOD",
}


#: IL Compare.op symbol -> IEC §2.5.2.8 comparison function name.
#: Used for ``<block typeName=...>`` when lowering a Compare into LD.
_COMPARE_OP_TO_BLOCK_NAME = {
    "==": "EQ",
    "!=": "NE",
    "<":  "LT",
    "<=": "LE",
    ">":  "GT",
    ">=": "GE",
}


def _ld_expression_text(value) -> str:
    """Render an IL Value (Address / TagRef / str literal) as the
    textual operand that an ``<inVariable>`` / ``<outVariable>``
    body carries in PLCopen XML."""
    if isinstance(value, Address):
        return value.raw
    if isinstance(value, TagRef):
        return value.name
    return str(value)


def _is_pure_ld_rung(rung: Rung) -> bool:
    """A rung is pure-LD if every op is a native LD primitive --
    contact (NO/NC/edge), coil (regular/SET/RESET), or
    ParallelGroup whose branches recursively contain only native
    ops.  Math, calls, stdlib, etc. still fall back to ST text."""
    def _ok(op) -> bool:
        if isinstance(op, ParallelGroup):
            return all(
                isinstance(b, (list, tuple))
                and all(_ok(inner) for inner in b)
                for b in op.branches
            )
        return isinstance(op, _NATIVE_LD_OPS)
    return all(_ok(op) for op in rung.ops)


def _is_pure_ld_body(rungs) -> bool:
    """The whole body is LD-renderable iff every rung is
    pure-LD and the body has at least one rung."""
    return bool(rungs) and all(_is_pure_ld_rung(r) for r in rungs)


def _ld_variable_text(addr) -> str:
    """One contact / coil ``<variable>`` body -- the IEC operand
    text.  Address wraps to its raw form; TagRef to its name."""
    if isinstance(addr, Address):
        return addr.raw
    if isinstance(addr, TagRef):
        return addr.name
    return str(addr)


#: LD layout grid: rungs stack vertically; ops chain horizontally
#: across one rung.
_LD_RUNG_HEIGHT = 100.0
_LD_OP_WIDTH = 100.0
_LD_LEFT_X = 20.0


def _emit_ld_contact_xml(op, this_id: int, parent_ids: list[int],
                            x: float, y: float) -> str:
    """Render one contact (NO / NC / rising / falling) with its
    incoming references.  Edge contacts may also carry the
    ``negated=true`` flag (the XSD allows the combination on a
    single ``<contact>`` element)."""
    attrs = []
    if isinstance(op, ContactNC):
        attrs.append('negated="true"')
    elif (isinstance(op, (ContactRisingEdge, ContactFallingEdge))
            and getattr(op, "negated", False)):
        attrs.append('negated="true"')
    if isinstance(op, ContactRisingEdge):
        attrs.append('edge="rising"')
    elif isinstance(op, ContactFallingEdge):
        attrs.append('edge="falling"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    cp_in = "".join(
        f'<connection refLocalId="{p}"/>' for p in parent_ids
    )
    return (
        f'<contact localId="{this_id}"{attr_str}>'
        f'<position x="{x:g}" y="{y:g}"/>'
        f'<connectionPointIn>{cp_in}</connectionPointIn>'
        f'<connectionPointOut/>'
        f'<variable>{escape(_ld_variable_text(op.address))}</variable>'
        '</contact>'
    )


def _emit_ld_coil_xml(op, this_id: int, parent_ids: list[int],
                        x: float, y: float) -> str:
    """Render one coil (regular / SET / RESET) with its incoming
    references."""
    attrs = []
    if isinstance(op, OutSet):
        attrs.append('storage="set"')
    elif isinstance(op, OutReset):
        attrs.append('storage="reset"')
    attr_str = (" " + " ".join(attrs)) if attrs else ""
    cp_in = "".join(
        f'<connection refLocalId="{p}"/>' for p in parent_ids
    )
    return (
        f'<coil localId="{this_id}"{attr_str}>'
        f'<position x="{x:g}" y="{y:g}"/>'
        f'<connectionPointIn>{cp_in}</connectionPointIn>'
        f'<connectionPointOut/>'
        f'<variable>{escape(_ld_variable_text(op.address))}</variable>'
        '</coil>'
    )


def _emit_ld_compare_block_xml(op: "Compare", block_type: str,
                                 in1_id: int, in2_id: int, block_id: int,
                                 parent_ids: list[int],
                                 x: float, y: float) -> list[str]:
    """Lower one Compare op into ``<inVariable>`` × 2 +
    ``<block typeName=GT|GE|EQ|LE|LT|NE>`` embedded in LD.

    Wiring:
      - ``in1_id`` -> block's ``IN1`` formal parameter
      - ``in2_id`` -> block's ``IN2`` formal parameter
      - upstream rung (``parent_ids``, typically the leftPowerRail
        or a preceding contact) feeds the block's ``EN`` input
      - block's ``OUT`` is read by the next downstream LD op as
        if it were a contact's connectionPointOut

    Wiring the rung's boolean signal into ``EN`` keeps the
    forward-walking LD reader happy (it traces consumers from the
    leftRail) and matches the convention every conformant PLCopen
    tool uses for Compare-in-LD.

    The XSD allows ``<block>`` / ``<inVariable>`` inside an
    ``<LD>`` body (the commonObjects group is shared between
    LD and FBD).
    """
    lhs_text = _ld_expression_text(op.lhs)
    rhs_text = _ld_expression_text(op.rhs)
    en_conn = "".join(
        f'<connection refLocalId="{p}"/>' for p in parent_ids
    )
    return [
        f'<inVariable localId="{in1_id}">'
        f'<position x="{x:g}" y="{y - 20:g}"/>'
        f'<connectionPointOut/>'
        f'<expression>{escape(lhs_text)}</expression>'
        f'</inVariable>',
        f'<inVariable localId="{in2_id}">'
        f'<position x="{x:g}" y="{y + 20:g}"/>'
        f'<connectionPointOut/>'
        f'<expression>{escape(rhs_text)}</expression>'
        f'</inVariable>',
        f'<block localId="{block_id}" typeName="{block_type}">'
        f'<position x="{x + _LD_OP_WIDTH:g}" y="{y:g}"/>'
        f'<inputVariables>'
        f'<variable formalParameter="EN">'
        f'<connectionPointIn>{en_conn}</connectionPointIn>'
        f'</variable>'
        f'<variable formalParameter="IN1">'
        f'<connectionPointIn>'
        f'<connection refLocalId="{in1_id}"/>'
        f'</connectionPointIn>'
        f'</variable>'
        f'<variable formalParameter="IN2">'
        f'<connectionPointIn>'
        f'<connection refLocalId="{in2_id}"/>'
        f'</connectionPointIn>'
        f'</variable>'
        f'</inputVariables>'
        f'<inOutVariables/>'
        f'<outputVariables>'
        f'<variable formalParameter="ENO">'
        f'<connectionPointOut/>'
        f'</variable>'
        f'<variable formalParameter="OUT">'
        f'<connectionPointOut/>'
        f'</variable>'
        f'</outputVariables>'
        f'</block>',
    ]


def _emit_ld_move_block_xml(op: "Move", in_id: int, block_id: int,
                              out_id: int, parent_ids: list[int],
                              x: float, y: float) -> list[str]:
    """Lower one Move op into ``<inVariable>`` + ``<block typeName="MOVE">``
    + ``<outVariable>`` embedded in LD.

    Wiring:
      - ``parent_ids`` (rung gate) -> block's ``EN`` input
      - ``in_id`` (inVariable carrying the src text) -> block's ``IN``
      - block's ``OUT`` -> ``out_id`` (outVariable carrying the dst text)
      - block's ``ENO`` is the new rung-gate cursor so chained ops
        continue downstream

    The IEC §2.5.2.1 ``MOVE`` function is the universal "copy
    value from source to destination" primitive, and the PLCopen
    TC6 v2.01 XSD models it via the generic ``<block typeName=...>``
    element (the schema doesn't reserve a special tag).
    """
    src_text = _ld_expression_text(op.src)
    dst_text = _ld_expression_text(op.dst)
    en_conn = "".join(
        f'<connection refLocalId="{p}"/>' for p in parent_ids
    )
    return [
        f'<inVariable localId="{in_id}">'
        f'<position x="{x:g}" y="{y:g}"/>'
        f'<connectionPointOut/>'
        f'<expression>{escape(src_text)}</expression>'
        f'</inVariable>',
        f'<block localId="{block_id}" typeName="MOVE">'
        f'<position x="{x + _LD_OP_WIDTH:g}" y="{y:g}"/>'
        f'<inputVariables>'
        f'<variable formalParameter="EN">'
        f'<connectionPointIn>{en_conn}</connectionPointIn>'
        f'</variable>'
        f'<variable formalParameter="IN">'
        f'<connectionPointIn>'
        f'<connection refLocalId="{in_id}"/>'
        f'</connectionPointIn>'
        f'</variable>'
        f'</inputVariables>'
        f'<inOutVariables/>'
        f'<outputVariables>'
        f'<variable formalParameter="ENO">'
        f'<connectionPointOut/>'
        f'</variable>'
        f'<variable formalParameter="OUT">'
        f'<connectionPointOut/>'
        f'</variable>'
        f'</outputVariables>'
        f'</block>',
        f'<outVariable localId="{out_id}">'
        f'<position x="{x + _LD_OP_WIDTH * 2:g}" y="{y:g}"/>'
        f'<connectionPointIn>'
        f'<connection refLocalId="{block_id}" formalParameter="OUT"/>'
        f'</connectionPointIn>'
        f'<expression>{escape(dst_text)}</expression>'
        f'</outVariable>',
    ]


def _emit_ld_binary_math_block_xml(op: "BinaryMath", block_type: str,
                                      in1_id: int, in2_id: int,
                                      block_id: int, out_id: int,
                                      parent_ids: list[int],
                                      x: float, y: float) -> list[str]:
    """Lower one BinaryMath op into ``<inVariable>`` × 2 +
    ``<block typeName="ADD|SUB|MUL|DIV|MOD">`` + ``<outVariable>``
    embedded in LD.

    Wiring:
      - ``parent_ids`` (rung gate) -> block's ``EN``
      - ``in1_id`` / ``in2_id`` -> block's ``IN1`` / ``IN2``
      - block's ``OUT`` -> ``out_id`` (outVariable carrying dst text)
      - block's ``ENO`` is the new rung-gate cursor
    """
    lhs_text = _ld_expression_text(op.lhs)
    rhs_text = _ld_expression_text(op.rhs)
    dst_text = _ld_expression_text(op.dst)
    en_conn = "".join(
        f'<connection refLocalId="{p}"/>' for p in parent_ids
    )
    return [
        f'<inVariable localId="{in1_id}">'
        f'<position x="{x:g}" y="{y - 20:g}"/>'
        f'<connectionPointOut/>'
        f'<expression>{escape(lhs_text)}</expression>'
        f'</inVariable>',
        f'<inVariable localId="{in2_id}">'
        f'<position x="{x:g}" y="{y + 20:g}"/>'
        f'<connectionPointOut/>'
        f'<expression>{escape(rhs_text)}</expression>'
        f'</inVariable>',
        f'<block localId="{block_id}" typeName="{block_type}">'
        f'<position x="{x + _LD_OP_WIDTH:g}" y="{y:g}"/>'
        f'<inputVariables>'
        f'<variable formalParameter="EN">'
        f'<connectionPointIn>{en_conn}</connectionPointIn>'
        f'</variable>'
        f'<variable formalParameter="IN1">'
        f'<connectionPointIn>'
        f'<connection refLocalId="{in1_id}"/>'
        f'</connectionPointIn>'
        f'</variable>'
        f'<variable formalParameter="IN2">'
        f'<connectionPointIn>'
        f'<connection refLocalId="{in2_id}"/>'
        f'</connectionPointIn>'
        f'</variable>'
        f'</inputVariables>'
        f'<inOutVariables/>'
        f'<outputVariables>'
        f'<variable formalParameter="ENO">'
        f'<connectionPointOut/>'
        f'</variable>'
        f'<variable formalParameter="OUT">'
        f'<connectionPointOut/>'
        f'</variable>'
        f'</outputVariables>'
        f'</block>',
        f'<outVariable localId="{out_id}">'
        f'<position x="{x + _LD_OP_WIDTH * 2:g}" y="{y:g}"/>'
        f'<connectionPointIn>'
        f'<connection refLocalId="{block_id}" formalParameter="OUT"/>'
        f'</connectionPointIn>'
        f'<expression>{escape(dst_text)}</expression>'
        f'</outVariable>',
    ]


def _emit_ld_ops_chain(ops, parent_ids: list[int],
                         next_local_id: int, x: float, y: float
                         ) -> tuple[list[str], list[int], int, float,
                                     Optional[int]]:
    """Walk a sequence of LD ops left-to-right, emitting elements.

    Returns ``(xml_lines, tail_ids, next_local_id, x_after,
    coil_id_or_None)``.  ``tail_ids`` is the list of localIds that
    feed into the next downstream op (single id for plain contact
    chains, multi-id list immediately after a ParallelGroup).
    ``coil_id_or_None`` is the localId of any coil encountered
    (so the caller can wire the rightPowerRail to it).
    """
    lines: list[str] = []
    cursor_ids = list(parent_ids)
    coil_id: Optional[int] = None
    for op in ops:
        if isinstance(op, (ContactNO, ContactNC,
                            ContactRisingEdge, ContactFallingEdge)):
            this_id = next_local_id; next_local_id += 1
            lines.append(_emit_ld_contact_xml(op, this_id, cursor_ids, x, y))
            cursor_ids = [this_id]
            x += _LD_OP_WIDTH
        elif isinstance(op, (OutCoil, OutSet, OutReset)):
            this_id = next_local_id; next_local_id += 1
            lines.append(_emit_ld_coil_xml(op, this_id, cursor_ids, x, y))
            cursor_ids = [this_id]
            coil_id = this_id
            x += _LD_OP_WIDTH
        elif isinstance(op, ParallelGroup):
            # Each branch starts from cursor_ids (the current rung
            # head) and produces its own tail.  All branch tails
            # union into the post-group cursor list -- the next op
            # downstream picks them up as its connectionPointIn.
            #
            # Branches recurse via _emit_ld_ops_chain so a branch
            # whose ops include another ParallelGroup is handled
            # without special-casing.  Each branch laid out on its
            # own y-row offset so the diagram stays readable;
            # localIds remain globally unique within the body.
            branch_tails: list[int] = []
            branch_x_max = x
            for bi, branch in enumerate(op.branches):
                branch_y = y + (bi - (len(op.branches) - 1) / 2.0) * 40.0
                b_lines, b_tails, next_local_id, bx, _coil = (
                    _emit_ld_ops_chain(
                        branch, cursor_ids, next_local_id, x, branch_y
                    )
                )
                lines.extend(b_lines)
                branch_tails.extend(b_tails)
                if bx > branch_x_max:
                    branch_x_max = bx
            cursor_ids = branch_tails
            x = branch_x_max
        elif isinstance(op, Move):
            # Lower Move into FBD ``<block typeName="MOVE">`` with
            # an ``<inVariable>`` source and an ``<outVariable>``
            # destination.  The block's ENO output keeps the rung's
            # boolean gate alive so downstream ops still chain.
            in_id = next_local_id; next_local_id += 1
            block_id = next_local_id; next_local_id += 1
            out_id = next_local_id; next_local_id += 1
            lines.extend(_emit_ld_move_block_xml(
                op, in_id, block_id, out_id, cursor_ids, x, y,
            ))
            cursor_ids = [block_id]
            x += _LD_OP_WIDTH * 3
        elif isinstance(op, BinaryMath):
            block_type = _BINARY_MATH_OP_TO_BLOCK_NAME.get(op.op, "ADD")
            in1_id = next_local_id; next_local_id += 1
            in2_id = next_local_id; next_local_id += 1
            block_id = next_local_id; next_local_id += 1
            out_id = next_local_id; next_local_id += 1
            lines.extend(_emit_ld_binary_math_block_xml(
                op, block_type, in1_id, in2_id, block_id, out_id,
                cursor_ids, x, y,
            ))
            cursor_ids = [block_id]
            x += _LD_OP_WIDTH * 3
        elif isinstance(op, Compare):
            # Lower Compare into FBD ``<block typeName="GT|...">``
            # embedded in the LD body.  The block consumes its
            # operands from two ``<inVariable>`` elements (one for
            # each Value) and produces a boolean on OUT that takes
            # the place of a contact's output in the rung gate
            # chain.
            block_type = _COMPARE_OP_TO_BLOCK_NAME.get(op.op, "EQ")
            in1_id = next_local_id; next_local_id += 1
            in2_id = next_local_id; next_local_id += 1
            block_id = next_local_id; next_local_id += 1
            lines.extend(_emit_ld_compare_block_xml(
                op, block_type, in1_id, in2_id, block_id,
                cursor_ids, x, y,
            ))
            cursor_ids = [block_id]
            x += _LD_OP_WIDTH * 2
        else:
            # Non-native op (math / call / stdlib / etc.) -- shouldn't
            # be reached because _is_pure_ld_rung filtered the body
            # back to ST emission already.  Defensive skip.
            continue
    return lines, cursor_ids, next_local_id, x, coil_id


def _emit_ld_rung_xml(rung: Rung, rung_idx: int,
                        next_local_id: int) -> tuple[list[str], int]:
    """One rung lowered to:

      leftPowerRail -> contact(s) / ParallelGroup(s) -> coil -> rightPowerRail

    Returns (xml_lines, new_next_local_id).  ``next_local_id`` is
    threaded across rungs so localIds stay unique within the body.

    Op order in the IL rung is left-to-right: leading contact-shape
    ops form the gate, the trailing op (coil-shape) is the rung's
    output.  ParallelGroup ops fan into multiple parallel branches
    that re-converge on the next downstream element via
    multi-incoming wires.
    """
    lines: list[str] = []
    y = 20.0 + rung_idx * _LD_RUNG_HEIGHT

    # leftPowerRail at the start of the rung
    left_id = next_local_id; next_local_id += 1
    lines.append(
        f'<leftPowerRail localId="{left_id}">'
        f'<position x="{_LD_LEFT_X:g}" y="{y:g}"/>'
        f'<connectionPointOut formalParameter="OUT"/>'
        '</leftPowerRail>'
    )

    op_lines, tail_ids, next_local_id, op_x, coil_id = _emit_ld_ops_chain(
        rung.ops, [left_id], next_local_id, _LD_LEFT_X + _LD_OP_WIDTH, y
    )
    lines.extend(op_lines)

    # rightPowerRail closing the rung.  Wires back from the coil
    # if present, else from the last op-chain tail (a "gate-only"
    # rung with no coil is degenerate but representable).
    right_id = next_local_id; next_local_id += 1
    incoming_ids = [coil_id] if coil_id is not None else tail_ids
    if incoming_ids and incoming_ids != [left_id]:
        cp_in = "".join(
            f'<connection refLocalId="{i}"/>' for i in incoming_ids
        )
        lines.append(
            f'<rightPowerRail localId="{right_id}">'
            f'<position x="{op_x:g}" y="{y:g}"/>'
            f'<connectionPointIn>{cp_in}</connectionPointIn>'
            '</rightPowerRail>'
        )
    else:
        lines.append(
            f'<rightPowerRail localId="{right_id}">'
            f'<position x="{op_x:g}" y="{y:g}"/>'
            '</rightPowerRail>'
        )
    return lines, next_local_id


def _emit_pou_body_ld(sub: Subroutine) -> str:
    """Body XML for an LD-bodied POU.

    Wraps ``sub.rungs`` in ``<body><LD>...</LD></body>``.  Every
    rung becomes ``leftPowerRail → contacts → coil → rightPowerRail``
    with localIds threaded sink-side so the resulting XML matches
    PLCopen TC6's connection-graph model.
    """
    rungs = sub.rungs
    if not rungs:
        return "<body>\n  <LD/>\n</body>"
    next_id = 0
    all_lines: list[str] = []
    for idx, rung in enumerate(rungs):
        rung_lines, next_id = _emit_ld_rung_xml(rung, idx, next_id)
        all_lines.extend(rung_lines)
    inner = "\n".join(_indent(line, "    ") for line in all_lines)
    return f"<body>\n  <LD>\n{inner}\n  </LD>\n</body>"


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
                      VarDirection.GLOBAL,
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
    # LD rungs that contain only contacts + coils lower to native
    # ``<LD>``; rungs with math / call / stdlib / etc. ops still
    # go through the ST translator (mixed LD+FBD bodies are a
    # follow-up slice).
    if sub.fbd_body is not None:
        parts.append(_indent(_emit_pou_body_fbd(sub), "  "))
    elif sub.sfc is not None:
        parts.append(_indent(_emit_pou_body_sfc(sub), "  "))
    elif _is_pure_ld_body(sub.rungs):
        parts.append(_indent(_emit_pou_body_ld(sub), "  "))
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
    if direction is VarDirection.GLOBAL:   return sub.global_vars
    if direction is VarDirection.EXTERNAL: return sub.external_vars
    if direction is VarDirection.TEMP:     return sub.temp_vars
    return ()


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
