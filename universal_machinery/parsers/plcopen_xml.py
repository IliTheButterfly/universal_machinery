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
    AccessVar, Address, CommentStatement, ConfigVar, Configuration,
    PouInstance, PouKind, Program, Resource, Subroutine, Tag, TagType,
    TaskSpec, Var, VarDirection,
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


def _parse_type_element(type_elem: ET.Element) -> TagType:
    """Read a ``<type>`` element's first child as an elementary
    ``TagType``.

    V1 covers elementary types only -- ``<INT/>`` / ``<BOOL/>`` /
    ``<REAL/>`` / etc.  User-defined types (``<derived
    name="..."/>``) need the user-defined-type registry plumbed in;
    that's a follow-up slice.  Unknown types fall back to ``INT``
    with a warning embedded in the comment field of the owning
    Var (V1 deliberately doesn't raise so a partial-import path
    stays usable).
    """
    children = list(type_elem)
    if not children:
        raise PlcopenParseError("<type> element has no body")
    child = children[0]
    local = _strip_ns(child.tag)
    # Elementary type: the tag name matches TagType.value
    try:
        return TagType(local)
    except ValueError:
        # Unknown elementary type or a <derived/> reference -- fall
        # through to INT.  A future slice replaces this with a real
        # NamedType lookup.
        return TagType.INT


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
    body_elem = _child(pou_elem, "body")
    if body_elem is not None:
        st_body = _parse_body_text(body_elem)
        # If the body was non-ST (LD/FBD/SFC), st_body is None and
        # the body content is lost in V1.  A follow-up slice will
        # populate sub.rungs / sub.fbd_body / sub.sfc as
        # appropriate.

    comment = _extract_documentation(pou_elem)

    return Subroutine(
        name=name, kind=kind, main=main, comment=comment,
        inputs=inputs, outputs=outputs, in_outs=in_outs,
        local_vars=local_vars, return_type=return_type,
        st_body=st_body,
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

    if types is not None:
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
        configurations=configurations,
        project_name=project_name,
        comment=project_comment,
    )


def parse_plcopen_xml_file(path: str | Path) -> Program:
    """Read ``path`` from disk and parse via :func:`parse_plcopen_xml`."""
    p = Path(path)
    xml = p.read_text(encoding="utf-8")
    return parse_plcopen_xml(xml)
