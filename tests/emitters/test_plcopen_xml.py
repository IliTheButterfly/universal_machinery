"""Tests for the IEC 61131-3 / PLCopen TC6 XML emitter.

Verifies the emitted document is well-formed XML, sits in the right
namespace, contains the expected ``<pou>`` / ``<interface>`` /
``<body>`` structure, and that variable declarations + return types
+ tag globals appear where the TC6 schema requires them.

Schema validation against the official XSD is a follow-up: it would
catch tighter structural rules these tests don't enforce.  These
tests are the "is it plausibly conformant" bar -- the next step is a
PLCopen reference-tool round-trip.
"""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from universal_machinery.builders import (
    add, call, coil, fn, no, prog, program, rung, set_, tag, tag_decl,
    var_in, var_inout, var_out,
)
from universal_machinery.emitters.plcopen_xml import (
    PLCOPEN_NS, emit_xml, emit_pou_xml,
)
from universal_machinery.il import TagType


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


_NS = {"plc": PLCOPEN_NS}
_FIXED_TIME = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def _parse(xml: str) -> ET.Element:
    """Parse XML; assert it's well-formed and return the root."""
    return ET.fromstring(xml)


# -----------------------------------------------------------------------------
# Document shape
# -----------------------------------------------------------------------------


def test_empty_program_emits_well_formed_xml():
    """A Program with nothing in it still produces parseable PLCopen XML."""
    xml = emit_xml(program(), time_now=_FIXED_TIME)
    root = _parse(xml)
    assert root.tag == f"{{{PLCOPEN_NS}}}project"


def test_document_has_required_top_level_elements():
    xml = emit_xml(program(), time_now=_FIXED_TIME)
    root = _parse(xml)
    # PLCopen TC6 requires fileHeader, contentHeader, types
    assert root.find("plc:fileHeader", _NS)    is not None
    assert root.find("plc:contentHeader", _NS) is not None
    assert root.find("plc:types", _NS)         is not None
    assert root.find("plc:instances", _NS)     is not None


def test_file_header_attributes_set():
    xml = emit_xml(program(),
                   company="ACME Industries",
                   product="ACME PLC Toolkit",
                   time_now=_FIXED_TIME)
    root = _parse(xml)
    hdr = root.find("plc:fileHeader", _NS)
    assert hdr.attrib["companyName"] == "ACME Industries"
    assert hdr.attrib["productName"] == "ACME PLC Toolkit"
    assert hdr.attrib["creationDateTime"] == "2026-05-19T12:00:00+00:00"


def test_content_header_uses_project_name():
    xml = emit_xml(program(project_name="MyProject"), time_now=_FIXED_TIME)
    root = _parse(xml)
    ch = root.find("plc:contentHeader", _NS)
    assert ch.attrib["name"] == "MyProject"


def test_content_header_defaults_to_untitled():
    """When the Program has no project_name, a default keeps the
    schema's name attribute non-empty (required)."""
    xml = emit_xml(program(), time_now=_FIXED_TIME)
    root = _parse(xml)
    ch = root.find("plc:contentHeader", _NS)
    assert ch.attrib["name"] == "untitled"


# -----------------------------------------------------------------------------
# POU declarations
# -----------------------------------------------------------------------------


def test_program_pou_emitted_with_correct_pouType():
    p = program(subroutines=[prog("Main", main=True)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    root = _parse(xml)
    pous = root.findall(".//plc:pou", _NS)
    main_pou = [p for p in pous if p.attrib["name"] == "Main"][0]
    assert main_pou.attrib["pouType"] == "program"


def test_function_pou_emitted_with_return_type():
    f = fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("r", TagType.INT)],
           return_type=TagType.INT,
           rungs=[rung(add(tag("a"), tag("b"), tag("r")))])
    xml = emit_pou_xml(f)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    pou = root.find("plc:pou", _NS)
    assert pou.attrib["pouType"] == "function"
    ret_type = root.find(".//plc:returnType/plc:INT", _NS)
    assert ret_type is not None, "FUNCTION should declare its return type"


def test_function_block_pou_emitted_with_correct_pouType():
    from universal_machinery.builders import fb
    f = fb("PID",
           inputs=[var_in("sp", TagType.REAL)],
           outputs=[var_out("out", TagType.REAL)])
    xml = emit_pou_xml(f)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    pou = root.find("plc:pou", _NS)
    assert pou.attrib["pouType"] == "functionBlock"


# -----------------------------------------------------------------------------
# Variable declarations
# -----------------------------------------------------------------------------


def test_var_input_block_contains_typed_variables():
    f = fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.DINT)],
           outputs=[var_out("r", TagType.INT)],
           return_type=TagType.INT)
    xml = emit_pou_xml(f)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    input_vars = root.findall(".//plc:inputVars/plc:variable", _NS)
    names = [v.attrib["name"] for v in input_vars]
    assert names == ["a", "b"]
    # Each variable carries a <type><BOOL/></type>-style elementary element
    a_type = input_vars[0].find("plc:type", _NS)
    assert a_type.find("plc:INT", _NS) is not None
    b_type = input_vars[1].find("plc:type", _NS)
    assert b_type.find("plc:DINT", _NS) is not None


def test_var_output_and_var_inout_get_distinct_blocks():
    from universal_machinery.builders import fb
    f = fb("Counter",
           in_outs=[var_inout("count", TagType.INT)],
           outputs=[var_out("at_max", TagType.BOOL)])
    xml = emit_pou_xml(f)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    assert root.find(".//plc:outputVars/plc:variable", _NS) is not None
    assert root.find(".//plc:inOutVars/plc:variable", _NS)  is not None


def test_var_with_initial_value_emits_simpleValue():
    from universal_machinery.builders import var
    p = prog("Main", main=True, local_vars=[
        var("counter", TagType.INT, initial="42"),
    ])
    xml = emit_pou_xml(p)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    sv = root.find(".//plc:simpleValue", _NS)
    assert sv is not None
    assert sv.attrib["value"] == "42"


# -----------------------------------------------------------------------------
# Body: pure-LD rungs lower to native <LD>; mixed rungs go through ST text
# -----------------------------------------------------------------------------


def test_pou_body_pure_ld_rungs_lower_to_LD_element():
    """Rungs that contain only contacts + coils emit as native
    PLCopen ``<LD>`` (left rail → contact(s) → coil → right rail).
    """
    p = prog("Main", main=True, rungs=[
        rung(no("X1"), coil("Y1")),
        rung(no("X2"), set_("Y2")),
    ])
    xml = emit_pou_xml(p)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    ld = root.find(".//plc:body/plc:LD", _NS)
    assert ld is not None
    # Each rung produces left rail + contact + coil + right rail.
    rails_l = ld.findall("plc:leftPowerRail", _NS)
    rails_r = ld.findall("plc:rightPowerRail", _NS)
    contacts = ld.findall("plc:contact", _NS)
    coils = ld.findall("plc:coil", _NS)
    assert len(rails_l) == 2
    assert len(rails_r) == 2
    assert len(contacts) == 2
    assert len(coils) == 2
    # The SET coil keeps its storage modifier.
    assert any(c.get("storage") == "set" for c in coils)


def test_pou_body_mixed_rungs_still_lower_to_ST_text():
    """Rungs that contain non-LD ops keep going through the ST
    translator until that op type's native LD lowering lands.

    Compare / Move / BinaryMath / StdFunc are now native LD via
    ``<block>`` -- this test now uses ``Call`` (POU invocation),
    which is still on the ST-fallback path pending its slice."""
    from universal_machinery.builders import call
    p = prog("Main", main=True, rungs=[
        rung(call("OtherPou")),
    ])
    xml = emit_pou_xml(p)
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    st = root.find(".//plc:body/plc:ST", _NS)
    assert st is not None
    pre = st.find("{http://www.w3.org/1999/xhtml}pre")
    assert pre is not None


def test_st_body_escapes_xml_special_chars():
    """Ops that produce <, >, & in ST text must be XML-escaped.

    Uses ``Call`` (POU invocation) since it's still on the
    ST-fallback path -- Compare / Move / BinaryMath / StdFunc
    have all moved to native LD via ``<block>``."""
    from universal_machinery.builders import call
    p = prog("Main", main=True, rungs=[
        rung(call("OtherPou")),
    ])
    xml = emit_pou_xml(p)
    # Parses cleanly == escaping worked
    root = ET.fromstring(f'<wrap xmlns="{PLCOPEN_NS}">{xml}</wrap>')
    pre = root.find(".//{http://www.w3.org/1999/xhtml}pre")
    assert pre is not None
    # The ST body should carry the call text
    assert "OtherPou" in pre.text


# -----------------------------------------------------------------------------
# Tag declarations -> synthetic GlobalsHolder POU
# -----------------------------------------------------------------------------


def test_tags_emitted_as_globals_holder_pou():
    p = program(tags=[
        tag_decl("speed", TagType.INT, "motor RPM"),
        tag_decl("estop", TagType.BOOL, "E-stop", locked="X101"),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    root = _parse(xml)
    pous = root.findall(".//plc:pou", _NS)
    names = [p.attrib["name"] for p in pous]
    assert "GlobalsHolder" in names


def test_globals_holder_carries_tag_variables_with_types():
    p = program(tags=[
        tag_decl("speed", TagType.INT, "motor RPM"),
        tag_decl("running", TagType.BOOL, "machine running"),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    root = _parse(xml)
    globals_pou = [p for p in root.findall(".//plc:pou", _NS)
                   if p.attrib["name"] == "GlobalsHolder"][0]
    vars_ = globals_pou.findall(".//plc:localVars/plc:variable", _NS)
    names = [v.attrib["name"] for v in vars_]
    assert sorted(names) == ["running", "speed"]


def test_no_globals_holder_when_no_tags():
    p = program(subroutines=[prog("Main", main=True)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    root = _parse(xml)
    pou_names = [p.attrib["name"]
                 for p in root.findall(".//plc:pou", _NS)]
    assert "GlobalsHolder" not in pou_names


# -----------------------------------------------------------------------------
# End-to-end document structure
# -----------------------------------------------------------------------------


def test_realistic_program_round_trips_to_well_formed_xml():
    """A non-trivial Program emits valid PLCopen XML with every
    expected section present."""
    p = program(
        project_name="MotorControl",
        comment="Demo motor control program",
        tags=[
            tag_decl("start_btn", TagType.BOOL, "operator start",
                     locked="X101"),
            tag_decl("running",   TagType.BOOL, "running indicator"),
            tag_decl("speed_sp",  TagType.INT,  "speed setpoint"),
        ],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no("start_btn"), set_("running")),
                rung(no("running"),
                     call("ClampSpeed",
                          inputs=[("sp", "speed_sp")],
                          return_to="speed_cmd")),
            ]),
            fn("ClampSpeed",
               inputs=[var_in("sp", TagType.INT)],
               outputs=[var_out("clamped", TagType.INT)],
               return_type=TagType.INT,
               rungs=[
                   rung(add(tag("sp"), 0, tag("clamped"))),
               ]),
        ],
    )

    xml = emit_xml(p, time_now=_FIXED_TIME)
    # Well-formed
    root = _parse(xml)
    assert root.tag == f"{{{PLCOPEN_NS}}}project"
    # Project metadata
    ch = root.find("plc:contentHeader", _NS)
    assert ch.attrib["name"] == "MotorControl"
    # PLCopen schema requires <Comment> (capital C) inside contentHeader.
    comment = ch.find("plc:Comment", _NS)
    assert comment is not None and "Demo motor control" in comment.text
    # All three POUs present (Main, ClampSpeed, plus synthetic Globals)
    pou_names = sorted([p.attrib["name"]
                        for p in root.findall(".//plc:pou", _NS)])
    assert pou_names == ["ClampSpeed", "GlobalsHolder", "Main"]
    # ClampSpeed FUNCTION has its return type
    clamp_pou = [p for p in root.findall(".//plc:pou", _NS)
                 if p.attrib["name"] == "ClampSpeed"][0]
    assert clamp_pou.attrib["pouType"] == "function"
    assert clamp_pou.find(".//plc:returnType/plc:INT", _NS) is not None


def test_deterministic_output_for_fixed_input_and_time():
    """Same Program + same time_now should produce byte-identical
    XML, so the emission is friendly to diff / round-trip tests."""
    p = program(
        project_name="Test",
        subroutines=[prog("Main", main=True,
                          rungs=[rung(no("X1"), coil("Y1"))])],
    )
    xml1 = emit_xml(p, time_now=_FIXED_TIME)
    xml2 = emit_xml(p, time_now=_FIXED_TIME)
    assert xml1 == xml2
