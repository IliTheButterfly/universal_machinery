"""PLCopen TC6 v2.01 XML reader.

Parses a ``<project>`` document into an ``il.Program``.  This is
the inverse of ``universal_machinery.emitters.plcopen_xml``: round-
tripping a Program through ``emit_xml`` then ``parse_plcopen_xml``
yields a structurally equivalent Program (modulo bodies that
haven't been parsed yet -- see Coverage below).

Public API
----------

``parse_plcopen_xml(xml: str) -> Program``
    Parse an in-memory XML string.

``parse_plcopen_xml_file(path: str | Path) -> Program``
    Convenience for parsing from disk.

Both raise :class:`PlcopenParseError` for malformed XML or
schema-shaped content we can't yet handle (a follow-up slice
adds graphical-body parsing).

Coverage (V1)
-------------

  - ✅ POU declarations: PROGRAM / FUNCTION / FUNCTION_BLOCK with
        their interface variable blocks, return types, and
        comments.
  - ✅ Variable blocks: inputVars / outputVars / inOutVars /
        localVars / externalVars / tempVars with elementary
        ``TagType`` -- per-variable name, type, address,
        initialValue, comment.
  - ✅ Configuration / Resource / Task / PouInstance / globalVars
        at both configuration and resource scope.
  - ✅ accessVars / configVars round-trip via the new ``AccessVar``
        and ``ConfigVar`` dataclasses.
  - ✅ ST body text captured verbatim into
        ``Subroutine.st_body`` as a single
        :class:`il.CommentStatement` carrying the raw source --
        round-trip-safe but not parsed into structured ST AST.
        A follow-up slice adds the real ST text parser.

Coverage gaps (deferred)
~~~~~~~~~~~~~~~~~~~~~~~~

  - ❌ Graphical bodies: LD / FBD / SFC.  Element-level XSD
        structure is well-defined; reversing the connection
        graph is a focused slice of its own.
  - ❌ User-defined types (``<dataTypes>``): STRUCT / ARRAY /
        ENUM / SUBRANGE / ALIAS declarations -- the emitter
        produces them but V1 reader skips with a warning.
  - ❌ Methods / Interfaces (IEC 3rd ed.): TC6 v2.01 XSD has
        no native shape for these, so emitter / reader both
        skip the OOP layer pending v2.02+ schema upgrade.

The reader is namespace-aware: it accepts both the canonical
PLCopen TC6 namespace (``http://www.plcopen.org/xml/tc6_0201``)
and bare-tag-name documents (no namespace, which some hand-rolled
tools produce).  XHTML inside ``<pre>`` content is preserved.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

from ..il import (
    AccessVar, Address, AliasType, ArrayType, BlockPin, CommentStatement,
    ConfigVar, Configuration, Connection, EnumType, FbBlock, FbdJump,
    FbdLabel, FbdNetwork, FbdReturn, InOutVariable, InVariable, NamedType,
    OutVariable, PouInstance, PouKind, Position, Program, Resource, Rung,
    Action, SfcNetwork, Step, StructType, SubrangeType, Subroutine, Tag,
    TagRef, TagType, TaskSpec, Transition, Var, VarDirection,
)
from ..il.ops import (
    ContactFallingEdge, ContactNC, ContactNO, ContactRisingEdge,
    OutCoil, OutReset, OutSet,
)


#: PLCopen TC6 XML namespace (v2.01 schema).
PLCOPEN_NS = "http://www.plcopen.org/xml/tc6_0201"


class PlcopenParseError(Exception):
    """Raised when a ``<project>`` document can't be parsed into IL.

    Carries the offending element's tag and (where useful) its
    location in the document.  Most failures should be either:

      - well-formedness errors (caught by the underlying
        ``xml.etree`` parser and re-raised as ``PlcopenParseError``)
      - shape errors: unexpected child element, missing required
        attribute, unknown elementary type, etc.

    XSD-schema-level validity is a separate axis -- callers wanting
    full PLCopen TC6 v2.01 conformance should run
    :func:`universal_machinery.emitters.plcopen_xml.validate_plcopen_xml`
    before parsing.
    """


# -----------------------------------------------------------------------------
# Inverse maps: PLCopen schema element/attribute -> IL enum
# -----------------------------------------------------------------------------


#: PLCopen ``pouType`` attribute -> IL ``PouKind``.  ``program``
#: pulls a ``main=True`` flag in the read pass; ``functionBlock``
#: and ``function`` map directly to their PouKind.
_POU_TYPE_TO_KIND = {
    "program":       PouKind.PROGRAM,
    "function":      PouKind.FUNCTION,
    "functionBlock": PouKind.FUNCTION_BLOCK,
}


#: PLCopen var-block element name -> IL ``VarDirection``.
_ELEMENT_TO_DIRECTION = {
    "inputVars":    VarDirection.INPUT,
    "outputVars":   VarDirection.OUTPUT,
    "inOutVars":    VarDirection.IN_OUT,
    "localVars":    VarDirection.LOCAL,
    "externalVars": VarDirection.EXTERNAL,
    "tempVars":     VarDirection.TEMP,
}


#: PLCopen ``accessType`` enum (camelCase) -> IL direction (IEC
#: keyword form).  Inverse of the map in the emitter.
_XML_ACCESS_DIRECTION = {
    "readOnly":  "READ_ONLY",
    "readWrite": "READ_WRITE",
}


# -----------------------------------------------------------------------------
# Namespace handling
# -----------------------------------------------------------------------------


def _strip_ns(tag: str) -> str:
    """Return the local name of an ElementTree tag.

    ElementTree formats namespaced tags as ``{ns}localName``.  We
    work with local names internally so the reader is namespace-
    agnostic (some tools emit bare TC6 elements with no namespace).
    """
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _children(elem: ET.Element, name: Optional[str] = None) -> list[ET.Element]:
    """All direct children of ``elem`` whose local name matches
    ``name``.  ``name=None`` returns every direct child."""
    if name is None:
        return list(elem)
    return [c for c in elem if _strip_ns(c.tag) == name]


def _child(elem: ET.Element, name: str) -> Optional[ET.Element]:
    """First direct child with local name ``name``, or ``None``."""
    for c in elem:
        if _strip_ns(c.tag) == name:
            return c
    return None


def _require_child(elem: ET.Element, name: str,
                   context: str = "") -> ET.Element:
    c = _child(elem, name)
    if c is None:
        loc = f" in {context}" if context else ""
        raise PlcopenParseError(
            f"<{_strip_ns(elem.tag)}> missing required child "
            f"<{name}>{loc}"
        )
    return c


# -----------------------------------------------------------------------------
# Type parsing
# -----------------------------------------------------------------------------


#: Per the PLCopen TC6 XSD, ``<string>`` / ``<wstring>`` use
#: lowercase element names (unlike the rest of the elementary
#: type group).  Map them back to the matching uppercase
#: ``TagType`` enum value so the reader sees them as STRING /
#: WSTRING.
_XSD_LOWERCASE_ELEMENTARY_ALIASES = {
    "string":  TagType.STRING,
    "wstring": TagType.WSTRING,
}


def _parse_type_element(type_elem: ET.Element):
    """Read a ``<type>`` element's first child as a ``DataType``.

    Handles both shapes:

      - Elementary type: ``<INT/>`` / ``<BOOL/>`` / ``<REAL/>`` /
        etc.  Tag name matches ``TagType.value``.  Lowercase
        ``<string/>`` and ``<wstring/>`` (PLCopen schema's
        non-uppercase exceptions) map back to STRING / WSTRING.
      - User-defined-type reference: ``<derived name="MyStruct"/>``
        becomes a ``NamedType("MyStruct")`` -- the validator
        resolves the reference against ``Program.user_types``.

    Unknown elementary tags fall back to ``TagType.INT`` rather than
    raising, so partial-import paths stay usable.
    """
    children = list(type_elem)
    if not children:
        raise PlcopenParseError("<type> element has no body")
    child = children[0]
    local = _strip_ns(child.tag)
    if local == "derived":
        name = child.get("name")
        if not name:
            raise PlcopenParseError("<derived> missing required name=")
        return NamedType(name=name)
    aliased = _XSD_LOWERCASE_ELEMENTARY_ALIASES.get(local)
    if aliased is not None:
        return aliased
    try:
        return TagType(local)
    except ValueError:
        return TagType.INT


# -----------------------------------------------------------------------------
# User-defined type parsing (IEC §2.3.3)
# -----------------------------------------------------------------------------


def _parse_dimensions(array_elem: ET.Element) -> tuple[tuple[int, int], ...]:
    """``<dimension lower=".." upper=".."/>`` children -> tuple of
    ``(lo, hi)`` per axis."""
    dims: list[tuple[int, int]] = []
    for d in _children(array_elem, "dimension"):
        lo_s = d.get("lower")
        hi_s = d.get("upper")
        if lo_s is None or hi_s is None:
            raise PlcopenParseError(
                "<dimension> missing required lower= / upper="
            )
        try:
            dims.append((int(lo_s), int(hi_s)))
        except ValueError as exc:
            raise PlcopenParseError(
                f"<dimension> non-integer bounds "
                f"lower={lo_s!r}, upper={hi_s!r}"
            ) from exc
    return tuple(dims)


def _parse_struct_members(struct_elem: ET.Element) -> tuple[Var, ...]:
    """``<struct>`` body is a ``varListPlain`` of ``<variable>``
    children -- one per field.  We parse each into a ``Var`` (with
    direction LOCAL since IEC struct members don't carry one)."""
    members: list[Var] = []
    for v in _children(struct_elem, "variable"):
        members.append(_parse_variable(v, VarDirection.LOCAL))
    return tuple(members)


def _parse_enum_values(enum_elem: ET.Element) -> tuple[str, ...]:
    """``<enum><values><value name="A"/><value name="B"/></values></enum>``."""
    values_wrap = _child(enum_elem, "values")
    if values_wrap is None:
        return ()
    out: list[str] = []
    for v in _children(values_wrap, "value"):
        name = v.get("name")
        if not name:
            raise PlcopenParseError("<value> missing required name=")
        out.append(name)
    return tuple(out)


def _parse_dataType(dt_elem: ET.Element):
    """One ``<dataType name="...">`` declaration.

    Dispatches on the first non-documentation child of the
    nested ``<baseType>``:

      <baseType><struct>...</struct></baseType>      -> StructType
      <baseType><array>...</array></baseType>        -> ArrayType
      <baseType><enum>...</enum></baseType>          -> EnumType
      <baseType><subrangeSigned>...</...></baseType> -> SubrangeType
      <baseType><subrangeUnsigned>...</...></...>    -> SubrangeType
      <baseType><elementary/></baseType>             -> AliasType
      <baseType><derived name=>/></baseType>         -> AliasType (NamedType base)
    """
    name = dt_elem.get("name")
    if not name:
        raise PlcopenParseError("<dataType> missing required name=")
    base_wrap = _require_child(dt_elem, "baseType",
                                 f"<dataType name={name!r}>")
    body_children = [c for c in base_wrap
                       if _strip_ns(c.tag) not in ("documentation", "addData")]
    if not body_children:
        raise PlcopenParseError(
            f"<dataType name={name!r}> has empty <baseType>"
        )
    body = body_children[0]
    local = _strip_ns(body.tag)
    comment = _extract_documentation(dt_elem)

    if local == "struct":
        return StructType(
            name=name,
            members=_parse_struct_members(body),
            comment=comment,
        )

    if local == "array":
        bounds = _parse_dimensions(body)
        elem_base = _require_child(body, "baseType",
                                     f"<dataType name={name!r}> <array>")
        # The element type lives inside the inner ``<baseType>``.
        element_type = _parse_type_element(elem_base)
        return ArrayType(
            name=name,
            element_type=element_type,
            bounds=bounds,
            comment=comment,
        )

    if local == "enum":
        return EnumType(
            name=name,
            values=_parse_enum_values(body),
            comment=comment,
        )

    if local in ("subrangeSigned", "subrangeUnsigned"):
        rng = _require_child(body, "range",
                               f"<dataType name={name!r}> <{local}>")
        lo_s = rng.get("lower")
        hi_s = rng.get("upper")
        if lo_s is None or hi_s is None:
            raise PlcopenParseError(
                f"<range> missing lower= / upper= in subrange {name!r}"
            )
        try:
            lower = int(lo_s); upper = int(hi_s)
        except ValueError as exc:
            raise PlcopenParseError(
                f"<range> non-integer bounds in subrange {name!r}: "
                f"lower={lo_s!r}, upper={hi_s!r}"
            ) from exc
        sub_base = _require_child(body, "baseType",
                                    f"<dataType name={name!r}> <{local}>")
        base_type = _parse_type_element(sub_base)
        return SubrangeType(
            name=name,
            base=base_type,
            lower=lower,
            upper=upper,
            comment=comment,
        )

    # Anything else inside <baseType> is either an elementary tag
    # (``<INT/>``, etc.) or a ``<derived name=>`` reference -- in
    # both cases the dataType is an alias.
    base_type = _parse_type_element(base_wrap)
    return AliasType(name=name, base=base_type, comment=comment)


# -----------------------------------------------------------------------------
# Variable parsing
# -----------------------------------------------------------------------------


def _parse_variable(var_elem: ET.Element, direction: VarDirection) -> Var:
    """One ``<variable name="...">`` declaration.

    Schema (per ``ppx:varListPlain``):

      <variable name="..." address="..."?>
        <type>...</type>
        <initialValue>...</initialValue>?
        <documentation>...</documentation>?
      </variable>

    The reader doesn't expand initialValue's ``<simpleValue/>``
    wrapper -- it stores the ``value`` attribute verbatim as the
    Var's ``initial_value`` so IEC-typed literals (``T#100ms``,
    ``16#FF``, ``DT#2026-05-20-...``) round-trip lossless.
    """
    name = var_elem.get("name")
    if not name:
        raise PlcopenParseError("<variable> missing required name=")

    type_child = _require_child(var_elem, "type", f"<variable name={name!r}>")
    data_type = _parse_type_element(type_child)

    address = var_elem.get("address")
    init_elem = _child(var_elem, "initialValue")
    initial_value = ""
    if init_elem is not None:
        # Expect: <initialValue><simpleValue value="..."/></initialValue>
        simple = _child(init_elem, "simpleValue")
        if simple is not None:
            initial_value = simple.get("value", "")
        else:
            # Hand-rolled tools sometimes inline the value as text.
            initial_value = (init_elem.text or "").strip()

    comment = _extract_documentation(var_elem)

    return Var(
        name=name,
        data_type=data_type,
        direction=direction,
        initial_value=initial_value,
        address=Address(address) if address else None,
        comment=comment,
    )


def _parse_var_block(elem: ET.Element) -> list[Var]:
    """All ``<variable>`` children of one var-block element
    (``<inputVars>`` / ``<localVars>`` / etc.) with the right
    ``VarDirection`` set on each."""
    direction = _ELEMENT_TO_DIRECTION[_strip_ns(elem.tag)]
    return [_parse_variable(v, direction)
            for v in _children(elem, "variable")]


# -----------------------------------------------------------------------------
# Documentation / comment extraction
# -----------------------------------------------------------------------------


def _extract_documentation(elem: ET.Element) -> str:
    """Pull the comment text out of any ``<documentation>`` child.

    PLCopen wraps comments inside an XHTML ``<p>``; we strip the
    wrapper and return the inner text.  Empty / missing returns
    "".
    """
    doc = _child(elem, "documentation")
    if doc is None:
        return ""
    # The wrapper element is typically an XHTML ``<p>``; concatenate
    # all descendant text so multi-paragraph docs flatten cleanly.
    return "".join(doc.itertext()).strip()


# -----------------------------------------------------------------------------
# Body parsing
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# FBD body parsing (IEC §6.7)
# -----------------------------------------------------------------------------


def _parse_position(elem: ET.Element) -> Optional[Position]:
    """Read the optional ``<position x= y=/>`` child as an
    ``il.Position``.

    PLCopen requires ``<position>`` on every FBD element so the
    schema is satisfied; we still return ``None`` when the
    attribute coercion fails, so partial-import paths stay usable.
    """
    pos_elem = _child(elem, "position")
    if pos_elem is None:
        return None
    x_s = pos_elem.get("x")
    y_s = pos_elem.get("y")
    if x_s is None or y_s is None:
        return None
    try:
        return Position(x=float(x_s), y=float(y_s))
    except ValueError:
        return None


def _parse_local_id(elem: ET.Element) -> int:
    """Required ``localId`` attribute -> ``int``.  IEC's
    ``xsd:unsignedLong`` accepts arbitrary non-negative integers;
    we narrow to ``int`` and raise on malformed values."""
    raw = elem.get("localId")
    if raw is None:
        raise PlcopenParseError(
            f"<{_strip_ns(elem.tag)}> missing required localId="
        )
    try:
        return int(raw)
    except ValueError as exc:
        raise PlcopenParseError(
            f"<{_strip_ns(elem.tag)}> non-integer localId={raw!r}"
        ) from exc


def _parse_optional_execution_order(elem: ET.Element) -> Optional[int]:
    raw = elem.get("executionOrderId")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise PlcopenParseError(
            f"<{_strip_ns(elem.tag)}> non-integer executionOrderId="
            f"{raw!r}"
        ) from exc


def _parse_connection_element(conn_elem: ET.Element) -> Connection:
    """One ``<connection refLocalId= [formalParameter=]/>`` element.

    Inverse of ``_connection_inner_xml`` from the emitter.
    """
    ref = conn_elem.get("refLocalId")
    if ref is None:
        raise PlcopenParseError(
            "<connection> missing required refLocalId="
        )
    try:
        source_id = int(ref)
    except ValueError as exc:
        raise PlcopenParseError(
            f"<connection> non-integer refLocalId={ref!r}"
        ) from exc
    return Connection(
        source_id=source_id,
        source_pin=conn_elem.get("formalParameter"),
    )


def _parse_connection_point_in(
    cp_elem: Optional[ET.Element],
) -> Optional[Connection]:
    """Pull a ``Connection`` out of a sink pin's
    ``<connectionPointIn>`` wrapper, or ``None`` for unwired pins.

    The schema allows the wrapper to be absent OR present-but-
    empty; both shapes map to ``None``.
    """
    if cp_elem is None:
        return None
    conn_elem = _child(cp_elem, "connection")
    if conn_elem is None:
        return None
    return _parse_connection_element(conn_elem)


def _parse_bool_attr(elem: ET.Element, name: str,
                     default: bool = False) -> bool:
    raw = elem.get(name)
    if raw is None:
        return default
    return raw.lower() == "true"


def _parse_block_pin(var_elem: ET.Element,
                     pin_side: str) -> BlockPin:
    """One ``<variable formalParameter=>`` element inside a
    ``<block>``'s input/output/inOut list.

    ``pin_side`` is ``"input"``/``"in_out"``/``"output"`` -- the
    sink-side pin shapes (input, in_out) read a
    ``<connectionPointIn>``; the producer-side (output) doesn't
    carry a wire (its connections live on the consumers).
    """
    formal = var_elem.get("formalParameter")
    if formal is None:
        raise PlcopenParseError(
            "<variable> inside <block> missing formalParameter="
        )
    connection: Optional[Connection] = None
    if pin_side in ("input", "in_out"):
        connection = _parse_connection_point_in(
            _child(var_elem, "connectionPointIn")
        )
    return BlockPin(
        formal_parameter=formal,
        connection=connection,
        negated=_parse_bool_attr(var_elem, "negated"),
        edge=var_elem.get("edge") or "",
        storage=var_elem.get("storage") or "",
    )


def _parse_block_pins(parent: Optional[ET.Element],
                      pin_side: str) -> list[BlockPin]:
    if parent is None:
        return []
    return [_parse_block_pin(v, pin_side)
            for v in _children(parent, "variable")]


def _parse_block(elem: ET.Element) -> FbBlock:
    """One ``<block typeName= instanceName=>`` element.

    Inverse of ``_emit_block_xml``.
    """
    type_name = elem.get("typeName")
    if not type_name:
        raise PlcopenParseError("<block> missing required typeName=")
    return FbBlock(
        local_id=_parse_local_id(elem),
        type_name=type_name,
        instance_name=elem.get("instanceName"),
        inputs=_parse_block_pins(_child(elem, "inputVariables"), "input"),
        outputs=_parse_block_pins(_child(elem, "outputVariables"),
                                    "output"),
        in_outs=_parse_block_pins(_child(elem, "inOutVariables"), "in_out"),
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        comment=_extract_documentation(elem),
    )


def _parse_in_variable(elem: ET.Element) -> InVariable:
    expr = _child(elem, "expression")
    expr_text = (expr.text or "").strip() if expr is not None else ""
    return InVariable(
        local_id=_parse_local_id(elem),
        expression=expr_text,
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        negated=_parse_bool_attr(elem, "negated"),
        edge=elem.get("edge") or "",
        storage=elem.get("storage") or "",
        comment=_extract_documentation(elem),
    )


def _parse_out_variable(elem: ET.Element) -> OutVariable:
    expr = _child(elem, "expression")
    expr_text = (expr.text or "").strip() if expr is not None else ""
    return OutVariable(
        local_id=_parse_local_id(elem),
        expression=expr_text,
        connection=_parse_connection_point_in(
            _child(elem, "connectionPointIn")
        ),
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        negated=_parse_bool_attr(elem, "negated"),
        edge=elem.get("edge") or "",
        storage=elem.get("storage") or "",
        comment=_extract_documentation(elem),
    )


def _parse_inout_variable(elem: ET.Element) -> InOutVariable:
    expr = _child(elem, "expression")
    expr_text = (expr.text or "").strip() if expr is not None else ""
    return InOutVariable(
        local_id=_parse_local_id(elem),
        expression=expr_text,
        connection=_parse_connection_point_in(
            _child(elem, "connectionPointIn")
        ),
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        negated_in=_parse_bool_attr(elem, "negatedIn"),
        negated_out=_parse_bool_attr(elem, "negatedOut"),
        edge_in=elem.get("edgeIn") or "",
        edge_out=elem.get("edgeOut") or "",
        storage_in=elem.get("storageIn") or "",
        storage_out=elem.get("storageOut") or "",
        comment=_extract_documentation(elem),
    )


def _parse_fbd_label(elem: ET.Element) -> FbdLabel:
    label = elem.get("label")
    if label is None:
        raise PlcopenParseError("<label> missing required label=")
    return FbdLabel(
        local_id=_parse_local_id(elem),
        label=label,
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        comment=_extract_documentation(elem),
    )


def _parse_fbd_jump(elem: ET.Element) -> FbdJump:
    label = elem.get("label")
    if label is None:
        raise PlcopenParseError("<jump> missing required label=")
    return FbdJump(
        local_id=_parse_local_id(elem),
        label=label,
        connection=_parse_connection_point_in(
            _child(elem, "connectionPointIn")
        ),
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        comment=_extract_documentation(elem),
    )


def _parse_fbd_return(elem: ET.Element) -> FbdReturn:
    return FbdReturn(
        local_id=_parse_local_id(elem),
        connection=_parse_connection_point_in(
            _child(elem, "connectionPointIn")
        ),
        position=_parse_position(elem),
        execution_order=_parse_optional_execution_order(elem),
        comment=_extract_documentation(elem),
    )


_FBD_ELEMENT_PARSERS = {
    "block":         _parse_block,
    "inVariable":    _parse_in_variable,
    "outVariable":   _parse_out_variable,
    "inOutVariable": _parse_inout_variable,
    "label":         _parse_fbd_label,
    "jump":          _parse_fbd_jump,
    "return":        _parse_fbd_return,
}


def _parse_fbd_body(fbd_elem: ET.Element) -> FbdNetwork:
    """Walk every child of ``<FBD>`` and dispatch to the right
    element parser.  Unknown elements are silently skipped (the
    PLCopen schema allows ``commonObjects`` like ``<comment>`` /
    ``<actionBlock>`` we don't model yet)."""
    elements = []
    for child in fbd_elem:
        local = _strip_ns(child.tag)
        parser = _FBD_ELEMENT_PARSERS.get(local)
        if parser is not None:
            elements.append(parser(child))
    return FbdNetwork(elements=elements)


# -----------------------------------------------------------------------------
# SFC body parsing (IEC §2.6 / PLCopen <SFC>)
# -----------------------------------------------------------------------------


def _expression_to_ld_ops(expr) -> Optional[tuple]:
    """Lower an ST ``Expression`` to a tuple of IL LD-style ops.

    Recognises the common subset of boolean expressions that
    transition conditions use:

      - ``VarRef(name)``                       -> ``(ContactNO(name),)``
      - ``UnaryExpr(NOT, VarRef(name))``       -> ``(ContactNC(name),)``
      - ``BinaryExpr(AND, left, right)``       -> left_ops + right_ops
      - ``BinaryExpr(OR, left, right)``        -> ``(ParallelGroup(left, right),)``

    Returns ``None`` when the expression doesn't fit -- caller
    should fall back to the textual placeholder.  The function is
    intentionally conservative: anything outside the AND/OR/NOT
    subset (literals, arithmetic, function calls, comparisons,
    field/index access) is rejected rather than misrendered.
    """
    from ..il import (
        BinaryExpr, BinaryOp, FieldAccess, IndexAccess, Literal,
        TagRef, UnaryExpr, UnaryOp, VarRef,
    )
    from ..il.ast import Address
    from ..il.ops import ContactNO, ContactNC, ParallelGroup

    def _operand(addr_expr) -> Optional[object]:
        """Pull the IL operand (Address|TagRef) out of a VarRef."""
        if isinstance(addr_expr, VarRef):
            ref = addr_expr.ref
            if isinstance(ref, (Address, TagRef)):
                return ref
        return None

    if isinstance(expr, VarRef):
        op_addr = _operand(expr)
        if op_addr is None:
            return None
        return (ContactNO(op_addr),)

    if isinstance(expr, UnaryExpr) and expr.op is UnaryOp.NOT:
        operand = expr.operand
        # ``NOT a`` -> ContactNC; ``NOT NOT a`` collapses to NO
        inner = _expression_to_ld_ops(operand)
        if inner is None:
            return None
        # If inner was a single ContactNO, flip it; otherwise we
        # can't represent the negation inside the LD-op grammar.
        if len(inner) == 1 and isinstance(inner[0], ContactNO):
            return (ContactNC(inner[0].address),)
        if len(inner) == 1 and isinstance(inner[0], ContactNC):
            return (ContactNO(inner[0].address),)
        return None

    if isinstance(expr, BinaryExpr):
        if expr.op is BinaryOp.AND:
            lhs_ops = _expression_to_ld_ops(expr.lhs)
            rhs_ops = _expression_to_ld_ops(expr.rhs)
            if lhs_ops is None or rhs_ops is None:
                return None
            return lhs_ops + rhs_ops
        if expr.op is BinaryOp.OR:
            lhs_ops = _expression_to_ld_ops(expr.lhs)
            rhs_ops = _expression_to_ld_ops(expr.rhs)
            if lhs_ops is None or rhs_ops is None:
                return None
            # Flatten chained ORs: an OR whose own operands are
            # already ParallelGroups should merge their branches.
            def _branches(ops):
                if (len(ops) == 1
                        and isinstance(ops[0], ParallelGroup)):
                    return list(ops[0].branches)
                return [tuple(ops)]
            branches = tuple(_branches(lhs_ops) + _branches(rhs_ops))
            return (ParallelGroup(branches=branches),)

    return None


def _parse_sfc_condition(cond_elem: ET.Element) -> str:
    """Extract a transition's condition as a textual ST
    expression.

    PLCopen allows three condition shapes:

      - ``<reference name="X"/>`` -- a named transition declared
        elsewhere in the resource; we render it as the bare name.
      - ``<connectionPointIn>...</connectionPointIn>`` -- the
        condition comes from an FBD-style wire; we capture the
        source ref-localId as text (best-effort).
      - ``<inline name="cond"><ST><xhtml:pre>...</...></ST></inline>``
        -- inline ST text.  We strip the wrapper and return the
        textual condition.

    On unknown shape the function returns ``"TRUE"`` so partial-
    import paths stay usable.  The textual condition lives on
    ``Transition.condition`` not as IL ops -- a future slice
    parses it via ``parse_st_expression``.
    """
    ref = _child(cond_elem, "reference")
    if ref is not None:
        name = ref.get("name")
        return name or "TRUE"
    inline = _child(cond_elem, "inline")
    if inline is not None:
        st = _child(inline, "ST")
        if st is not None:
            text = "".join(st.itertext()).strip()
            return text or "TRUE"
    return "TRUE"


def _collect_refs(cp_in: Optional[ET.Element]) -> list[int]:
    """All ``<connection refLocalId=>`` children of a
    ``<connectionPointIn>``, returned as int localIds.  Empty /
    missing wrappers yield ``[]``."""
    if cp_in is None:
        return []
    out: list[int] = []
    for c in _children(cp_in, "connection"):
        raw = c.get("refLocalId")
        if raw is None:
            raise PlcopenParseError(
                "<connection> in SFC <connectionPointIn> missing "
                "refLocalId="
            )
        try:
            out.append(int(raw))
        except ValueError as exc:
            raise PlcopenParseError(
                f"<connection> non-integer refLocalId={raw!r}"
            ) from exc
    return out


#: PLCopen TC6 v2.01 valid ``actionQualifierType`` enum values.
#: A document carrying anything else is non-conformant; we
#: tolerate it on read (fall back to "N") rather than reject the
#: whole document, mirroring the emitter's policy.
_VALID_ACTION_QUALIFIERS = frozenset({
    "P1", "N", "P0", "R", "S", "L", "D", "P",
    "DS", "DL", "SD", "SL",
})


def _parse_duration_ms(raw: Optional[str]) -> Optional[int]:
    """``T#123ms`` / ``T#1s500ms`` / ``T#2s`` -> integer ms.

    PLCopen action ``duration=`` carries an IEC TIME literal.  We
    handle the common shapes used by emitters (``T#<ms>ms`` and
    ``T#<sec>s[<ms>ms]``); anything we don't understand returns
    ``None`` so the round-trip silently drops a duration we can't
    re-emit reliably rather than blowing up the read.
    """
    if not raw:
        return None
    s = raw.strip().upper()
    if s.startswith("T#"):
        s = s[2:]
    elif s.startswith("TIME#"):
        s = s[5:]
    total = 0
    cur = ""
    # crude lex: walk digits, then unit
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isdigit():
            cur += ch
            i += 1
            continue
        # unit: try ms first, then s, then m, then h
        if s[i:i + 2] == "MS":
            if not cur:
                return None
            total += int(cur)
            cur = ""
            i += 2
        elif ch == "S":
            if not cur:
                return None
            total += int(cur) * 1000
            cur = ""
            i += 1
        elif ch == "M":
            if not cur:
                return None
            total += int(cur) * 60_000
            cur = ""
            i += 1
        elif ch == "H":
            if not cur:
                return None
            total += int(cur) * 3_600_000
            cur = ""
            i += 1
        elif ch in "_ ":
            i += 1
        else:
            return None
    if cur:
        return None
    return total


def _parse_action_block(ab_elem: ET.Element) -> tuple[Optional[int], list[Action]]:
    """One ``<actionBlock>`` -> (source_step_local_id, [Action, ...]).

    The source step is the localId carried in this block's
    ``<connectionPointIn>``; later we use it to attach the parsed
    actions to the matching ``Step``.  Returns ``(None, [...])``
    if the block isn't wired to a step (defensive -- shouldn't
    happen in valid documents).
    """
    incoming = _collect_refs(_child(ab_elem, "connectionPointIn"))
    source_id = incoming[0] if incoming else None
    actions: list[Action] = []
    for a_elem in _children(ab_elem, "action"):
        qualifier_raw = a_elem.get("qualifier", "N")
        qualifier = (qualifier_raw
                      if qualifier_raw in _VALID_ACTION_QUALIFIERS
                      else "N")
        time_ms = _parse_duration_ms(a_elem.get("duration"))
        comment = _extract_documentation(a_elem)
        # target: <reference name=> (action POU / boolean var) or
        # <inline> (deferred -- we don't model inline action bodies).
        ref_elem = _child(a_elem, "reference")
        if ref_elem is None:
            # Inline action body -- skip until a future slice models it.
            continue
        target_name = ref_elem.get("name")
        if not target_name:
            raise PlcopenParseError(
                "<action><reference> missing required name="
            )
        actions.append(Action(
            qualifier=qualifier,
            target=target_name,
            time_ms=time_ms,
            comment=comment,
        ))
    return source_id, actions


def _parse_sfc_body(sfc_elem: ET.Element) -> SfcNetwork:
    """Walk ``<step>`` and ``<transition>`` children, then
    reconstruct the IL's name-based step graph from the
    connection-point references.

    Algorithm:

      1. First pass: collect all step / transition local IDs +
         their primary attributes (name, initial flag, condition
         text, incoming-connection list).
      2. Second pass: for each transition, ``from_steps`` is the
         list of step names whose localIds appear in its
         ``<connectionPointIn>``.  ``to_steps`` is the inverse
         lookup -- for every step, scan its
         ``<connectionPointIn>`` for transition localIds and add
         the step's name into those transitions' to_steps.

    Conditions parse back as textual ST stored as a single
    ``TagRef`` operand inside the IL Transition.condition tuple.
    The emitter recovers this on re-emission via the standard
    gate formatter.  Round-trips that go through a real condition
    parser (a follow-up slice) will replace this with structured
    ops.

    Divergence / convergence markers (``<simultaneousDivergence>``,
    ``<simultaneousConvergence>``, ``<selectionDivergence>``,
    ``<selectionConvergence>``) are dissolved during ref
    resolution -- they don't appear in the IL graph, but the
    multi-to / multi-from they imply round-trip into multi-tuple
    ``Transition.from_steps`` / ``to_steps`` correctly.
    """
    # Step records carry an optional pre-parsed inner SfcNetwork for
    # ``<macroStep>`` elements (None for plain ``<step>``).
    steps_raw: list[tuple[int, str, bool, str, list[int],
                             "Optional[SfcNetwork]"]] = []
    transitions_raw: list[tuple[int, str, list[int]]] = []
    # step_localId -> accumulated list of parsed Actions (a single
    # step can have multiple action blocks attached; we union them).
    actions_by_step_id: dict[int, list[Action]] = {}
    # marker localId -> list of upstream refLocalIds.  Used during
    # the second pass to "trace through" markers when reconstructing
    # the step ↔ transition graph.
    marker_incoming: dict[int, list[int]] = {}
    # jumpStep records: (target_name, [upstream refLocalIds]).
    # A jumpStep's connectionPointIn names the upstream transition
    # whose firing leads to the named target step; we use this to
    # augment that transition's reconstructed ``to_steps``.
    jump_steps_raw: list[tuple[str, list[int]]] = []

    # First pass: parse element-level attributes and incoming refs.
    for child in sfc_elem:
        local = _strip_ns(child.tag)
        if local == "step":
            step_id = _parse_local_id(child)
            name = child.get("name")
            if not name:
                raise PlcopenParseError("<step> missing required name=")
            initial = _parse_bool_attr(child, "initialStep")
            incoming = _collect_refs(_child(child, "connectionPointIn"))
            comment = _extract_documentation(child)
            steps_raw.append((step_id, name, initial, comment, incoming,
                                None))
        elif local == "macroStep":
            step_id = _parse_local_id(child)
            name = child.get("name")
            if not name:
                raise PlcopenParseError(
                    "<macroStep> missing required name="
                )
            # macroStep can't carry an initialStep attribute in the
            # XSD; its initial state is expressed by the nested
            # network's own initial step(s).
            incoming = _collect_refs(_child(child, "connectionPointIn"))
            comment = _extract_documentation(child)
            # Walk into the inner body and recurse on its <SFC>
            # element.  A macroStep body could also hold <LD> /
            # <FBD> / <ST>, but those aren't yet modelled as macro
            # inner bodies on the IL side -- silently skip them
            # rather than fail.
            inner_net: "Optional[SfcNetwork]" = None
            body_elem = _child(child, "body")
            if body_elem is not None:
                inner_sfc = _child(body_elem, "SFC")
                if inner_sfc is not None:
                    inner_net = _parse_sfc_body(inner_sfc)
            steps_raw.append((step_id, name, False, comment, incoming,
                                inner_net))
        elif local == "transition":
            trans_id = _parse_local_id(child)
            cond_elem = _child(child, "condition")
            cond_text = (_parse_sfc_condition(cond_elem)
                          if cond_elem is not None else "TRUE")
            incoming = _collect_refs(_child(child, "connectionPointIn"))
            transitions_raw.append((trans_id, cond_text, incoming))
        elif local == "actionBlock":
            src_id, parsed_actions = _parse_action_block(child)
            if src_id is not None and parsed_actions:
                actions_by_step_id.setdefault(src_id, []).extend(parsed_actions)
        elif local in {"simultaneousDivergence", "simultaneousConvergence",
                        "selectionDivergence", "selectionConvergence"}:
            marker_id = _parse_local_id(child)
            # Convergence shapes have multiple <connectionPointIn>
            # elements (one per source); divergence shapes have a
            # single <connectionPointIn> with one ref.  We union
            # everything into a flat list of source localIds.
            sources: list[int] = []
            for cpin in _children(child, "connectionPointIn"):
                sources.extend(_collect_refs(cpin))
            marker_incoming[marker_id] = sources
        elif local == "jumpStep":
            target_name = child.get("targetName")
            if not target_name:
                raise PlcopenParseError(
                    "<jumpStep> missing required targetName="
                )
            incoming = _collect_refs(_child(child, "connectionPointIn"))
            jump_steps_raw.append((target_name, incoming))
        # macroStep still deferred -- silently skipped.

    def _trace_to_endpoints(ref_id: int, depth: int = 0) -> list[int]:
        """Resolve a ``refLocalId=`` by following marker indirection
        until we hit a non-marker (step / transition) id.

        Returns a list because convergence markers expand one ref
        into multiple endpoints.  Depth guard defends against
        pathological cycles in malformed documents.
        """
        if depth > 16:
            return []
        if ref_id not in marker_incoming:
            return [ref_id]
        out: list[int] = []
        for src in marker_incoming[ref_id]:
            out.extend(_trace_to_endpoints(src, depth + 1))
        return out

    # Build localId -> kind/name maps for the reverse lookup.
    name_by_step_id: dict[int, str] = {
        sid: name for sid, name, _i, _c, _inc, _m in steps_raw
    }
    trans_id_set: set[int] = {tid for tid, _c, _inc in transitions_raw}

    # Second pass: derive each transition's from_steps + to_steps,
    # tracing through any markers along the way.
    steps: list[Step] = []
    for sid, name, initial, comment, _incoming, macro in steps_raw:
        steps.append(Step(
            name=name,
            initial=initial,
            actions=tuple(actions_by_step_id.get(sid, ())),
            comment=comment,
            macro=macro,
        ))

    # jumpStep contributions: each jumpStep names an upstream
    # transition (via its connectionPointIn) and a target step
    # name; we add that step name to the transition's to_steps.
    # Built once and consulted per-transition below.
    jump_targets_by_trans: dict[int, list[str]] = {}
    step_names = {name for _sid, name, _i, _c, _inc, _m in steps_raw}
    for target_name, incoming in jump_steps_raw:
        if target_name not in step_names:
            # Dangling jump target -- skip rather than fail; the
            # document is malformed but we'd rather lose a wire
            # than the whole network.
            continue
        for src_ref in incoming:
            for ep in _trace_to_endpoints(src_ref):
                if ep in trans_id_set:
                    jump_targets_by_trans.setdefault(ep, []).append(target_name)

    # For each transition, the from_steps are the steps whose
    # localIds appear in (or trace through markers from) the
    # transition's incoming-connection list.  The to_steps are
    # the steps whose incoming-connection list (after marker
    # resolution) references this transition's localId, plus any
    # jumpStep that names a step and points at this transition.
    transitions: list[Transition] = []
    for tid, cond_text, incoming in transitions_raw:
        resolved_sources: list[int] = []
        for ref in incoming:
            resolved_sources.extend(_trace_to_endpoints(ref))
        from_steps: list[str] = []
        for src in resolved_sources:
            if src in name_by_step_id and name_by_step_id[src] not in from_steps:
                from_steps.append(name_by_step_id[src])
        to_steps: list[str] = []
        for sid, sname, _i, _c, s_incoming, _m in steps_raw:
            resolved = []
            for r in s_incoming:
                resolved.extend(_trace_to_endpoints(r))
            if tid in resolved:
                to_steps.append(sname)
        for jt in jump_targets_by_trans.get(tid, ()):
            if jt not in to_steps:
                to_steps.append(jt)
        # Lower the textual condition to structured IL LD ops:
        # parse via the ST expression parser, then convert the
        # resulting Expression tree to a tuple of contacts / NOTs /
        # ParallelGroups.  If the expression doesn't match the
        # supported subset (anything outside AND / OR / NOT over
        # bare variable refs), fall back to a single placeholder
        # ContactNO carrying the raw text so the emitter's gate
        # formatter renders it back verbatim.  Empty / "TRUE"
        # conditions stay as empty tuples.
        condition_ops: tuple = ()
        if cond_text and cond_text.upper() != "TRUE":
            from ..il.ops import ContactNO
            from ..il import TagRef
            from .st_text import StParseError, parse_st_expression
            try:
                expr = parse_st_expression(cond_text)
            except StParseError:
                expr = None
            lowered = (_expression_to_ld_ops(expr)
                        if expr is not None else None)
            if lowered is not None:
                condition_ops = lowered
            else:
                condition_ops = (ContactNO(TagRef(name=cond_text)),)
        transitions.append(Transition(
            from_steps=tuple(from_steps),
            to_steps=tuple(to_steps),
            condition=condition_ops,
        ))

    return SfcNetwork(steps=steps, transitions=transitions)


# -----------------------------------------------------------------------------
# LD body parsing (IEC §6.6 / PLCopen <LD>)
# -----------------------------------------------------------------------------


def _parse_ld_variable_operand(elem: ET.Element):
    """``<variable>X001</variable>`` text body -> ``Address`` or
    ``TagRef`` matching the builder's smart-string coercion.

    Returns ``TagRef`` for symbolic names, ``Address`` for CLICK-
    style (``X001``) or IEC direct-rep (``%I0.0``) operands.
    """
    var_elem = _child(elem, "variable")
    text = (var_elem.text or "").strip() if var_elem is not None else ""
    if not text:
        return TagRef(name="")
    if text.startswith("%"):
        return Address(text)
    # CLICK-style: uppercase letters followed by digits, no dots
    head = text.rstrip("0123456789")
    if head.isalpha() and head.isupper() and head != text:
        return Address(text)
    return TagRef(name=text)


def _parse_ld_body(ld_elem: ET.Element) -> list[Rung]:
    """Walk an ``<LD>`` body and group its elements into ``Rung``s.

    Algorithm:

      1. First pass: index every element by ``localId`` and
         record its kind (left-rail, contact, coil, right-rail).
         Capture each element's incoming-connection localIds
         (sink-side wires).
      2. For each ``<leftPowerRail>``, walk forward through the
         sink-side graph until reaching a ``<rightPowerRail>``.
         The walk visits each chained contact / coil in order;
         the resulting list becomes one ``Rung``.

    Mixed-language bodies (LD with embedded ``<block>`` elements
    from the ``fbdObjects`` group) are not supported in V1 --
    such elements are silently skipped, which produces a partial
    but well-formed rung.  A future slice routes ``<block>``
    inside LD through the FBD primitive reader.

    Elements that don't link back to any ``<leftPowerRail>`` (e.g.
    orphan coils a hand-rolled tool emitted) are skipped.
    """
    elements_by_id: dict[int, ET.Element] = {}
    kind_by_id: dict[int, str] = {}
    incoming_by_id: dict[int, list[int]] = {}

    for child in ld_elem:
        local = _strip_ns(child.tag)
        if local not in ("leftPowerRail", "rightPowerRail",
                         "contact", "coil"):
            continue
        lid = _parse_local_id(child)
        elements_by_id[lid] = child
        kind_by_id[lid] = local
        incoming_by_id[lid] = _collect_refs(_child(child, "connectionPointIn"))

    # Inverse adjacency: producer localId -> list of consumer ids.
    consumers: dict[int, list[int]] = {lid: [] for lid in elements_by_id}
    for lid, sources in incoming_by_id.items():
        for src in sources:
            if src in consumers:
                consumers[src].append(lid)

    rungs: list[Rung] = []
    visited: set[int] = set()

    for lid, kind in kind_by_id.items():
        if kind != "leftPowerRail":
            continue
        # Walk the rail's downstream chain.  PLCopen rungs are
        # linear in the common case; we follow the first
        # consumer at each step.
        ops: list = []
        cursor = lid
        while True:
            if cursor in visited:
                break
            visited.add(cursor)
            nxts = consumers.get(cursor, [])
            if not nxts:
                break
            # Prefer the unvisited next consumer that isn't a
            # rightPowerRail (rails are terminators, not links).
            chosen: Optional[int] = None
            for n in nxts:
                if n in visited:
                    continue
                if kind_by_id.get(n) == "rightPowerRail":
                    # Terminator -- record but don't follow further.
                    chosen = n
                    break
                chosen = n
                break
            if chosen is None:
                break
            kind = kind_by_id[chosen]
            elem = elements_by_id[chosen]
            if kind == "contact":
                addr = _parse_ld_variable_operand(elem)
                negated = _parse_bool_attr(elem, "negated")
                # XSD ``edge=`` carries "rising" / "falling" / "none"
                # (default).  IL distinguishes rising / falling via
                # dedicated op classes -- ContactRisingEdge /
                # ContactFallingEdge -- so we dispatch on the edge
                # attr.  Negated edge contacts aren't modelled (the
                # IL classes don't carry a negated flag); we drop
                # negation in that case rather than failing.
                edge = (elem.get("edge") or "none").lower()
                if edge == "rising":
                    ops.append(ContactRisingEdge(addr))
                elif edge == "falling":
                    ops.append(ContactFallingEdge(addr))
                elif negated:
                    ops.append(ContactNC(addr))
                else:
                    ops.append(ContactNO(addr))
                cursor = chosen
                continue
            if kind == "coil":
                addr = _parse_ld_variable_operand(elem)
                storage = elem.get("storage") or ""
                if storage == "set":
                    ops.append(OutSet(addr))
                elif storage == "reset":
                    ops.append(OutReset(addr))
                else:
                    ops.append(OutCoil(addr))
                cursor = chosen
                continue
            if kind == "rightPowerRail":
                visited.add(chosen)
                break
            break
        if ops:
            rungs.append(Rung(ops=ops))

    return rungs


def _parse_body_text(body_elem: ET.Element) -> Optional[list]:
    """Return a ``Subroutine.st_body``-shaped list from a ``<body>``
    element, or ``None`` if the body kind isn't ST.

    Routes the textual ST content through :func:`parse_st_body` so
    the resulting body is a fully structured AST (Assignment,
    IfStatement, ForStatement, ...).  If parsing fails -- which
    can happen for hand-rolled documents that use non-standard
    extensions -- we fall back to wrapping the raw text in a
    single :class:`il.CommentStatement` so the source survives
    the round-trip without crashing the import.
    """
    st_elem = _child(body_elem, "ST")
    if st_elem is None:
        # LD / FBD / SFC bodies are deferred; return None so the
        # caller can decide whether to skip the POU's body, warn,
        # or raise.
        return None
    # The ST body is XHTML-wrapped: <ST><xhtml:pre>...</xhtml:pre></ST>
    # (or <xhtml:p>, or sometimes naked text).  Collect every leaf
    # text node so any embedded XHTML formatting flattens.
    text = "".join(st_elem.itertext()).strip()
    if not text:
        return []
    # Try the structured parser first; on failure, preserve the
    # source as a comment so a partial-import path stays usable.
    from .st_text import StParseError, parse_st_body
    try:
        return parse_st_body(text)
    except StParseError:
        return [CommentStatement(text=text)]


# -----------------------------------------------------------------------------
# POU parsing
# -----------------------------------------------------------------------------


def _parse_pou(pou_elem: ET.Element) -> Subroutine:
    """One ``<pou name="..." pouType="...">`` declaration.

    Schema::

        <pou name="..." pouType="program|function|functionBlock">
          <interface>
            <returnType>...</returnType>?
            <inputVars>...</inputVars>?
            <outputVars>...</outputVars>?
            <inOutVars>...</inOutVars>?
            <localVars>...</localVars>?
            <externalVars>...</externalVars>?
            <tempVars>...</tempVars>?
          </interface>
          <body>
            <ST><xhtml:pre>...</xhtml:pre></ST>
            (or <LD>/<FBD>/<SFC> -- deferred)
          </body>?
          <documentation>...</documentation>?
        </pou>
    """
    name = pou_elem.get("name")
    if not name:
        raise PlcopenParseError("<pou> missing required name=")
    pou_type_attr = pou_elem.get("pouType", "")
    if pou_type_attr not in _POU_TYPE_TO_KIND:
        raise PlcopenParseError(
            f"<pou name={name!r}> has unknown pouType "
            f"{pou_type_attr!r}; expected one of "
            f"{sorted(_POU_TYPE_TO_KIND)}"
        )
    kind = _POU_TYPE_TO_KIND[pou_type_attr]
    main = (kind is PouKind.PROGRAM)

    inputs:     list[Var] = []
    outputs:    list[Var] = []
    in_outs:    list[Var] = []
    local_vars: list[Var] = []
    return_type: Optional[TagType] = None

    interface = _child(pou_elem, "interface")
    if interface is not None:
        rt = _child(interface, "returnType")
        if rt is not None:
            return_type = _parse_type_element(rt)
        for block in interface:
            local = _strip_ns(block.tag)
            if local not in _ELEMENT_TO_DIRECTION:
                # returnType, documentation, addData...
                continue
            vars_ = _parse_var_block(block)
            direction = _ELEMENT_TO_DIRECTION[local]
            if direction is VarDirection.INPUT:
                inputs.extend(vars_)
            elif direction is VarDirection.OUTPUT:
                outputs.extend(vars_)
            elif direction is VarDirection.IN_OUT:
                in_outs.extend(vars_)
            else:
                # LOCAL, EXTERNAL, TEMP all funnel into local_vars
                # -- the IL keeps a single ``local_vars`` slot and
                # discriminates by ``Var.direction`` if needed.
                local_vars.extend(vars_)

    st_body: Optional[list] = None
    fbd_body: Optional[FbdNetwork] = None
    sfc_body: Optional[SfcNetwork] = None
    rungs: list[Rung] = []
    body_elem = _child(pou_elem, "body")
    if body_elem is not None:
        # Body-kind dispatch: prefer the structured form when the
        # inner element identifies it.  Order: FBD, SFC, LD, then
        # ST as the textual fallback.
        fbd_elem = _child(body_elem, "FBD")
        sfc_elem = _child(body_elem, "SFC")
        ld_elem  = _child(body_elem, "LD")
        if fbd_elem is not None:
            fbd_body = _parse_fbd_body(fbd_elem)
        elif sfc_elem is not None:
            sfc_body = _parse_sfc_body(sfc_elem)
        elif ld_elem is not None:
            rungs = _parse_ld_body(ld_elem)
        else:
            st_body = _parse_body_text(body_elem)

    comment = _extract_documentation(pou_elem)

    return Subroutine(
        name=name, kind=kind, main=main, comment=comment,
        inputs=inputs, outputs=outputs, in_outs=in_outs,
        local_vars=local_vars, return_type=return_type,
        rungs=rungs,
        st_body=st_body, fbd_body=fbd_body, sfc=sfc_body,
    )


# -----------------------------------------------------------------------------
# Configuration parsing
# -----------------------------------------------------------------------------


def _parse_task(task_elem: ET.Element) -> TaskSpec:
    """One ``<task>`` element.  TC6 attributes: ``name`` (required),
    ``priority``, ``single`` xor ``interval`` xor ``interrupt``
    (one trigger mode required by IEC §2.7.2)."""
    name = task_elem.get("name")
    if not name:
        raise PlcopenParseError("<task> missing required name=")
    priority_raw = task_elem.get("priority", "1")
    try:
        priority = int(priority_raw)
    except ValueError as exc:
        raise PlcopenParseError(
            f"<task name={name!r}> has non-integer priority "
            f"{priority_raw!r}"
        ) from exc
    return TaskSpec(
        name=name,
        priority=priority,
        interval=task_elem.get("interval"),
        single=task_elem.get("single"),
        interrupt=task_elem.get("interrupt"),
    )


def _parse_pou_instance(pi_elem: ET.Element,
                        task_name: Optional[str] = None) -> PouInstance:
    name = pi_elem.get("name")
    type_name = pi_elem.get("typeName")
    if not name or not type_name:
        raise PlcopenParseError(
            f"<pouInstance> requires both name= and typeName=; "
            f"got name={name!r}, typeName={type_name!r}"
        )
    return PouInstance(name=name, type_name=type_name, task=task_name)


def _parse_resource(resource_elem: ET.Element) -> Resource:
    """One ``<resource>`` element.

    PLCopen nests ``<pouInstance>`` elements *inside* each ``<task>``
    when they're bound to that task; standalone ``<pouInstance>``s
    sit directly inside ``<resource>`` for unbound POUs.  Reverse
    both shapes.
    """
    name = resource_elem.get("name")
    if not name:
        raise PlcopenParseError("<resource> missing required name=")

    tasks: list[TaskSpec] = []
    pou_instances: list[PouInstance] = []
    global_vars: list[Var] = []

    for child in resource_elem:
        local = _strip_ns(child.tag)
        if local == "task":
            tasks.append(_parse_task(child))
            for pi in _children(child, "pouInstance"):
                pou_instances.append(_parse_pou_instance(pi, child.get("name")))
        elif local == "pouInstance":
            pou_instances.append(_parse_pou_instance(child))
        elif local == "globalVars":
            global_vars.extend(_parse_var_block_as_globals(child))

    comment = _extract_documentation(resource_elem)
    return Resource(
        name=name, tasks=tasks, pou_instances=pou_instances,
        global_vars=global_vars, comment=comment,
    )


def _parse_var_block_as_globals(elem: ET.Element) -> list[Var]:
    """``<globalVars>`` uses ``ppx:varList`` (same shape as
    ``localVars``).  We tag each as ``VarDirection.EXTERNAL`` so the
    IL can distinguish them from POU locals on re-emit."""
    return [_parse_variable(v, VarDirection.EXTERNAL)
            for v in _children(elem, "variable")]


def _parse_access_var(av_elem: ET.Element) -> AccessVar:
    alias = av_elem.get("alias")
    path = av_elem.get("instancePathAndName")
    if not alias or not path:
        raise PlcopenParseError(
            f"<accessVariable> requires alias= and "
            f"instancePathAndName=; got alias={alias!r}, "
            f"instancePathAndName={path!r}"
        )
    direction_xml = av_elem.get("direction", "readWrite")
    direction = _XML_ACCESS_DIRECTION.get(direction_xml, "READ_WRITE")
    type_elem = _require_child(av_elem, "type", "<accessVariable>")
    data_type = _parse_type_element(type_elem)
    comment = _extract_documentation(av_elem)
    return AccessVar(
        alias=alias, instance_path=path, data_type=data_type,
        direction=direction, comment=comment,
    )


def _parse_config_var(cv_elem: ET.Element) -> ConfigVar:
    path = cv_elem.get("instancePathAndName")
    if not path:
        raise PlcopenParseError(
            "<configVariable> requires instancePathAndName="
        )
    type_elem = _require_child(cv_elem, "type", "<configVariable>")
    data_type = _parse_type_element(type_elem)
    initial_value = ""
    init = _child(cv_elem, "initialValue")
    if init is not None:
        simple = _child(init, "simpleValue")
        initial_value = (simple.get("value", "") if simple is not None
                          else (init.text or "").strip())
    comment = _extract_documentation(cv_elem)
    return ConfigVar(
        instance_path=path, data_type=data_type,
        initial_value=initial_value, comment=comment,
    )


def _parse_configuration(cfg_elem: ET.Element) -> Configuration:
    name = cfg_elem.get("name")
    if not name:
        raise PlcopenParseError("<configuration> missing required name=")

    resources:    list[Resource] = []
    global_vars:  list[Var] = []
    access_vars:  list[AccessVar] = []
    config_vars:  list[ConfigVar] = []

    for child in cfg_elem:
        local = _strip_ns(child.tag)
        if local == "resource":
            resources.append(_parse_resource(child))
        elif local == "globalVars":
            global_vars.extend(_parse_var_block_as_globals(child))
        elif local == "accessVars":
            access_vars.extend(_parse_access_var(av)
                                for av in _children(child, "accessVariable"))
        elif local == "configVars":
            config_vars.extend(_parse_config_var(cv)
                                for cv in _children(child, "configVariable"))

    return Configuration(
        name=name, resources=resources,
        global_vars=global_vars, access_vars=access_vars,
        config_vars=config_vars,
        comment=_extract_documentation(cfg_elem),
    )


# -----------------------------------------------------------------------------
# Tag-table parsing (synthetic GlobalsHolder POU)
# -----------------------------------------------------------------------------


_GLOBALS_HOLDER_NAME = "GlobalsHolder"


def _is_globals_holder(pou_elem: ET.Element) -> bool:
    """The emitter exports program-level Tags via a synthetic POU
    named ``GlobalsHolder``; on read-back we recognise it and
    repopulate ``Program.tags`` instead of carrying it as a
    POU."""
    return pou_elem.get("name") == _GLOBALS_HOLDER_NAME


def _parse_globals_holder(pou_elem: ET.Element) -> dict[str, Tag]:
    """Extract Tag declarations from the synthetic GlobalsHolder
    POU's ``<localVars>`` block."""
    tags: dict[str, Tag] = {}
    interface = _child(pou_elem, "interface")
    if interface is None:
        return tags
    local_block = _child(interface, "localVars")
    if local_block is None:
        return tags
    for var_elem in _children(local_block, "variable"):
        name = var_elem.get("name")
        if not name:
            continue
        type_child = _require_child(var_elem, "type", "<variable>")
        data_type = _parse_type_element(type_child)
        address = var_elem.get("address")
        tags[name] = Tag(
            name=name,
            data_type=data_type,
            description=_extract_documentation(var_elem),
            address=Address(address) if address else None,
        )
    return tags


# -----------------------------------------------------------------------------
# Top-level parse
# -----------------------------------------------------------------------------


def parse_plcopen_xml(xml: str) -> Program:
    """Parse a PLCopen TC6 XML ``<project>`` document into an IL
    ``Program``.

    Coverage details are in the module docstring.  Bodies that
    aren't ST (LD / FBD / SFC) are silently skipped in V1 -- their
    POU declarations still come through with the right
    interface variables.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise PlcopenParseError(f"malformed XML: {exc}") from exc

    if _strip_ns(root.tag) != "project":
        raise PlcopenParseError(
            f"expected root element <project>, got "
            f"<{_strip_ns(root.tag)}>"
        )

    content_header = _child(root, "contentHeader")
    project_name = ""
    project_comment = ""
    if content_header is not None:
        project_name = content_header.get("name", "") or ""
        # The contentHeader's optional <Comment>/<comment>
        # element carries free-form text.
        comment_elem = _child(content_header, "Comment")
        if comment_elem is None:
            comment_elem = _child(content_header, "comment")
        if comment_elem is not None:
            project_comment = (comment_elem.text or "").strip()

    types = _child(root, "types")
    instances = _child(root, "instances")

    subroutines: list[Subroutine] = []
    tags: dict[str, Tag] = {}
    user_types: list = []

    if types is not None:
        # User-defined types come first so they're available for
        # later type-resolution passes that walk POU variable
        # interfaces and struct member types.
        data_types_elem = _child(types, "dataTypes")
        if data_types_elem is not None:
            for dt in _children(data_types_elem, "dataType"):
                user_types.append(_parse_dataType(dt))

        pous_elem = _child(types, "pous")
        if pous_elem is not None:
            for pou_elem in _children(pous_elem, "pou"):
                if _is_globals_holder(pou_elem):
                    tags.update(_parse_globals_holder(pou_elem))
                else:
                    subroutines.append(_parse_pou(pou_elem))

    configurations: list[Configuration] = []
    if instances is not None:
        configs_elem = _child(instances, "configurations")
        if configs_elem is not None:
            for cfg in _children(configs_elem, "configuration"):
                configurations.append(_parse_configuration(cfg))

    return Program(
        subroutines=subroutines,
        tags=tags,
        user_types=user_types,
        configurations=configurations,
        project_name=project_name,
        comment=project_comment,
    )


def parse_plcopen_xml_file(path: str | Path) -> Program:
    """Read ``path`` from disk and parse via :func:`parse_plcopen_xml`."""
    p = Path(path)
    xml = p.read_text(encoding="utf-8")
    return parse_plcopen_xml(xml)
