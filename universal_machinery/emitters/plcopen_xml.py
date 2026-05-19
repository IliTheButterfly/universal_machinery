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
  - ✅ ST body via emit_pou_body_st() reusing the ST emitter
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
from typing import Optional, Sequence
from xml.sax.saxutils import escape, quoteattr

from ..il import (
    Address, PouKind, Program, Subroutine, Tag, TagType, Var, VarDirection,
)
from .st import emit_pou as _emit_pou_st, emit_rung


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
    """One ``<variable>`` element with type + optional initial value."""
    parts: list[str] = []
    parts.append(f'<variable name={quoteattr(var.name)}>')
    parts.append(f"  <type><{_iec_type_element(var.data_type)}/></type>")
    if var.initial_value:
        parts.append(
            f"  <initialValue>"
            f"<simpleValue value={quoteattr(var.initial_value)}/>"
            f"</initialValue>"
        )
    if var.address is not None:
        parts.append(f"  <!-- AT {escape(var.address.raw)} -->")
    if var.comment:
        parts.append(f"  <documentation><xhtml>{escape(var.comment)}"
                     f"</xhtml></documentation>")
    parts.append("</variable>")
    return "\n".join(parts)


def _iec_type_element(t: TagType) -> str:
    """PLCopen XML uses self-closing tags for elementary types:
    ``<BOOL/>``, ``<INT/>``, ``<REAL/>``, etc.  TagType.value is the
    IEC keyword (uppercase) -- the schema's element names match."""
    return t.value


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


def _emit_pou_body_st(sub: Subroutine) -> str:
    """Body XML for an ST-bodied POU.

    PLCopen wraps Structured Text inside
    ``<body><ST><xhtml>...</xhtml></ST></body>``.  We reuse the
    standalone ST emitter (``emitters.st.emit_rung``) for each rung's
    statements; the resulting text is the body's textual content.
    """
    lines: list[str] = []
    if sub.sfc is not None:
        lines.append("(* SFC body not emitted in ST -- see <SFC> body *)")
    else:
        for rung in sub.rungs:
            for stmt in emit_rung(rung):
                lines.append(stmt)
    body_text = "\n".join(lines) if lines else "(* empty *)"
    # PLCopen schema requires the textual content inside an <xhtml>
    # element so it's well-formed XML even if the ST has special chars.
    return (
        "<body>\n"
        "  <ST>\n"
        f"    <xhtml xmlns=\"http://www.w3.org/1999/xhtml\">"
        f"{escape(body_text)}</xhtml>\n"
        "  </ST>\n"
        "</body>"
    )


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
            f"<returnType><{_iec_type_element(sub.return_type)}/></returnType>"
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

    # <body>
    parts.append(_indent(_emit_pou_body_st(sub), "  "))

    # documentation
    if sub.comment:
        parts.append(f"  <documentation><xhtml>{escape(sub.comment)}"
                     f"</xhtml></documentation>")

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
        lines.append(f'      <variable name={quoteattr(tag.name)}>')
        lines.append(f"        <type><{_iec_type_element(tag.data_type)}/></type>")
        if tag.address is not None:
            lines.append(f"        <!-- AT {escape(tag.address.raw)} -->")
        if tag.description:
            lines.append(
                f"        <documentation><xhtml>"
                f"{escape(tag.description)}</xhtml></documentation>"
            )
        lines.append('      </variable>')
    lines.extend([
        '    </localVars>',
        '  </interface>',
        '  <body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml">'
        '(* synthesised globals holder *)</xhtml></ST></body>',
        '</pou>',
    ])
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Top-level project document
# -----------------------------------------------------------------------------


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
        parts.append(f"    <comment>{escape(desc)}</comment>")
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
    parts.append('    <dataTypes/>')
    parts.append('    <pous>')

    globals_pou = _emit_globals_pou(prog.tags)
    if globals_pou is not None:
        parts.append(_indent(globals_pou, "      "))

    for sub in prog.subroutines:
        parts.append(_indent(emit_pou_xml(sub), "      "))

    parts.append('    </pous>')
    parts.append('  </types>')
    parts.append('  <instances><configurations/></instances>')
    parts.append('</project>')
    return "\n".join(parts) + "\n"
