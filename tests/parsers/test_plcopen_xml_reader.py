"""Tests for the PLCopen TC6 XML reader.

Three coverage layers:

  - Round-trip: emit a Program -> parse the XML -> assert the
    structural shape survives.  This is the load-bearing test --
    it covers the common case and catches any drift between the
    emitter and reader.
  - Targeted parsing: feed the reader hand-crafted XML snippets
    for each schema area (interface, configuration, access vars,
    config vars, comments, addresses, initial values) and check
    the resulting IL shape.
  - Error cases: malformed XML, missing required attributes,
    unknown pouType, etc.
"""
from datetime import datetime, timezone

import pytest

from universal_machinery.builders import (
    access_var, assign, config_var, configuration, fb, fn, lit, pou_instance,
    prog, program, resource, task_spec, tag_decl, var, var_in, var_inout,
    var_out,
)
from universal_machinery.emitters.plcopen_xml import emit_xml
from universal_machinery.il import (
    AccessVar, Address, CommentStatement, ConfigVar, Configuration, PouKind,
    Program, Subroutine, TagType, VarDirection,
)
from universal_machinery.parsers.plcopen_xml import (
    PlcopenParseError, parse_plcopen_xml,
)


_FIXED_TIME = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


def _round_trip(p: Program) -> Program:
    return parse_plcopen_xml(emit_xml(p, time_now=_FIXED_TIME))


def test_round_trip_empty_program():
    p = program()
    p2 = _round_trip(p)
    assert p2.subroutines == []
    assert p2.tags == {}
    assert p2.configurations == []


def test_round_trip_preserves_project_name_and_comment():
    p = program(project_name="MyProject", comment="demo")
    p2 = _round_trip(p)
    assert p2.project_name == "MyProject"
    assert p2.comment == "demo"


def test_round_trip_program_with_inputs_outputs_locals():
    p = program(subroutines=[
        prog("Main", main=True,
              inputs=[var_in("a", TagType.INT),
                       var_in("b", TagType.REAL)],
              outputs=[var_out("done", TagType.BOOL)],
              local_vars=[var("count", TagType.INT, initial="42")]),
    ])
    p2 = _round_trip(p)
    sub = p2.find_subroutine("Main")
    assert sub is not None
    assert sub.kind is PouKind.PROGRAM
    assert sub.main
    assert [(v.name, v.data_type) for v in sub.inputs] == [
        ("a", TagType.INT), ("b", TagType.REAL),
    ]
    assert sub.outputs[0].name == "done"
    assert sub.local_vars[0].initial_value == "42"


def test_round_trip_function_preserves_return_type():
    p = program(subroutines=[
        fn("Avg",
           inputs=[var_in("x", TagType.INT), var_in("y", TagType.INT)],
           return_type=TagType.INT),
    ])
    sub = _round_trip(p).find_subroutine("Avg")
    assert sub.kind is PouKind.FUNCTION
    assert sub.return_type is TagType.INT


def test_round_trip_function_block_with_in_out():
    p = program(subroutines=[
        fb("Counter",
           inputs=[var_in("clk", TagType.BOOL)],
           in_outs=[var_inout("count", TagType.INT)],
           outputs=[var_out("done", TagType.BOOL)]),
    ])
    sub = _round_trip(p).find_subroutine("Counter")
    assert sub.kind is PouKind.FUNCTION_BLOCK
    assert sub.in_outs[0].name == "count"
    assert sub.in_outs[0].direction is VarDirection.IN_OUT


def test_round_trip_tags_via_globals_holder():
    """Program.tags get exported as a synthetic GlobalsHolder POU
    on emit -- the reader recognises this and repopulates
    Program.tags rather than emitting a real POU.

    Note: only IEC §2.4.1 direct-representation addresses
    (``%I``/``%Q``/``%M``...) survive the round-trip via the
    schema's ``address`` attribute.  CLICK-style vendor addresses
    (``X001``, ``DS9000``) are emitted as XML-comment
    annotations, which ElementTree strips on read.
    """
    p = program(
        tags=[
            tag_decl("start_btn", TagType.BOOL, "operator start",
                     locked="%I0.0"),
            tag_decl("speed_sp", TagType.INT, "setpoint"),
        ],
        subroutines=[prog("Main", main=True)],
    )
    p2 = _round_trip(p)
    assert set(p2.tags) == {"start_btn", "speed_sp"}
    assert p2.tags["start_btn"].data_type is TagType.BOOL
    assert p2.tags["start_btn"].address == Address("%I0.0")
    # The GlobalsHolder POU itself should NOT appear in subroutines
    assert all(s.name != "GlobalsHolder" for s in p2.subroutines)


def test_round_trip_configuration_with_task_and_pou_instance():
    p = program(
        subroutines=[prog("Main", main=False)],
        configurations=[configuration("Default",
            resources=[resource("R1",
                tasks=[task_spec("Fast", interval="T#10ms", priority=2)],
                pou_instances=[pou_instance("MainProg",
                                              type_name="Main",
                                              task="Fast")])])],
    )
    p2 = _round_trip(p)
    cfg = p2.configurations[0]
    assert cfg.name == "Default"
    r = cfg.resources[0]
    assert r.name == "R1"
    assert r.tasks[0].name == "Fast"
    assert r.tasks[0].interval == "T#10ms"
    assert r.tasks[0].priority == 2
    pi = r.pou_instances[0]
    assert pi.name == "MainProg"
    assert pi.type_name == "Main"
    assert pi.task == "Fast"


def test_round_trip_access_vars_and_config_vars():
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("R1", pou_instances=[
                pou_instance("MainProg", type_name="Main")])],
            access_vars=[
                access_var("hmi_state", "R1.MainProg.state",
                            TagType.BOOL, direction="READ_ONLY"),
                access_var("hmi_speed", "R1.MainProg.speed",
                            TagType.INT, direction="READ_WRITE",
                            comment="setpoint to operator"),
            ],
            config_vars=[
                config_var("R1.MainProg.threshold", TagType.INT,
                            initial="100"),
            ])],
    )
    p2 = _round_trip(p)
    cfg = p2.configurations[0]
    aliases = [(a.alias, a.instance_path, a.direction)
               for a in cfg.access_vars]
    assert aliases == [
        ("hmi_state", "R1.MainProg.state", "READ_ONLY"),
        ("hmi_speed", "R1.MainProg.speed", "READ_WRITE"),
    ]
    assert cfg.access_vars[1].comment == "setpoint to operator"
    assert cfg.config_vars[0].instance_path == "R1.MainProg.threshold"
    assert cfg.config_vars[0].initial_value == "100"


def test_round_trip_st_body_parsed_to_structured_ast():
    """ST bodies round-trip back into structured AST via the ST
    text parser -- the emit/parse pair preserves the statement
    list shape.  An ``Assignment`` should re-emerge as an
    ``Assignment``, not as a ``CommentStatement``."""
    from universal_machinery.il import Assignment as _Assignment
    p = program(subroutines=[
        prog("Main", main=True,
              local_vars=[var("count", TagType.INT)],
              st_body=[assign("count", lit(42))]),
    ])
    p2 = _round_trip(p)
    sub = p2.find_subroutine("Main")
    assert sub.st_body is not None
    assert len(sub.st_body) == 1
    assert isinstance(sub.st_body[0], _Assignment)
    assert sub.st_body[0].target.ref.name == "count"
    assert sub.st_body[0].value.value == "42"


def test_round_trip_pou_comment_preserved():
    p = program(subroutines=[
        prog("Main", main=True, comment="entry point of the program"),
    ])
    sub = _round_trip(p).find_subroutine("Main")
    assert sub.comment == "entry point of the program"


# -----------------------------------------------------------------------------
# Targeted parsing
# -----------------------------------------------------------------------------


_BASE_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201"
         xmlns:xhtml="http://www.w3.org/1999/xhtml"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <fileHeader companyName="x" productName="y" productVersion="0"
              creationDateTime="2026-05-20T00:00:00+00:00"/>
  <contentHeader name="{name}" modificationDateTime="2026-05-20T00:00:00+00:00">
    <coordinateInfo>
      <pageSize x="1024" y="768"/>
      <fbd><scaling x="1" y="1"/></fbd>
      <ld><scaling x="1" y="1"/></ld>
      <sfc><scaling x="1" y="1"/></sfc>
    </coordinateInfo>
  </contentHeader>
  <types><dataTypes/><pous>{pous}</pous></types>
  <instances><configurations>{configs}</configurations></instances>
</project>"""


def _wrap_doc(*, name: str = "T", pous: str = "", configs: str = "") -> str:
    return _BASE_DOC.format(name=name, pous=pous, configs=configs)


def test_namespace_aware_parsing_with_canonical_ns():
    """Document with the canonical TC6 namespace parses cleanly."""
    p = parse_plcopen_xml(_wrap_doc(
        name="NSTest",
        pous='<pou name="Foo" pouType="program"><interface/></pou>',
    ))
    assert p.project_name == "NSTest"
    assert p.find_subroutine("Foo") is not None


def test_namespace_agnostic_parsing_without_ns():
    """Bare-tag-name (no namespace) document parses too -- some
    hand-rolled tools emit this shape."""
    xml = (
        '<?xml version="1.0"?>'
        '<project>'
        '<contentHeader name="BareNS"/>'
        '<types><pous>'
        '<pou name="Foo" pouType="program">'
        '<interface/>'
        '</pou>'
        '</pous></types>'
        '</project>'
    )
    p = parse_plcopen_xml(xml)
    assert p.project_name == "BareNS"
    assert p.find_subroutine("Foo") is not None


def test_initial_value_simpleValue_attribute_extracted():
    """initialValue wraps a simpleValue carrying the literal."""
    pou = (
        '<pou name="P" pouType="program"><interface><localVars>'
        '<variable name="x"><type><INT/></type>'
        '<initialValue><simpleValue value="42"/></initialValue>'
        '</variable></localVars></interface></pou>'
    )
    p = parse_plcopen_xml(_wrap_doc(pous=pou))
    sub = p.find_subroutine("P")
    assert sub.local_vars[0].initial_value == "42"


def test_variable_address_attribute_propagates_to_il_Address():
    pou = (
        '<pou name="P" pouType="program"><interface><localVars>'
        '<variable name="x" address="%I0.0"><type><BOOL/></type>'
        '</variable></localVars></interface></pou>'
    )
    p = parse_plcopen_xml(_wrap_doc(pous=pou))
    sub = p.find_subroutine("P")
    assert sub.local_vars[0].address == Address("%I0.0")


def test_documentation_extracted_as_comment_text():
    pou = (
        '<pou name="P" pouType="program">'
        '<interface/>'
        '<documentation>'
        '<xhtml:p xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        'POU-level note</xhtml:p>'
        '</documentation>'
        '</pou>'
    )
    p = parse_plcopen_xml(_wrap_doc(pous=pou))
    assert p.find_subroutine("P").comment == "POU-level note"


def test_pou_instance_under_task_inherits_task_name():
    """PLCopen nests <pouInstance> inside <task> when bound;
    the reader propagates the task name onto the IL PouInstance."""
    cfg = (
        '<configuration name="Default">'
        '<resource name="R1">'
        '<task name="Fast" interval="T#10ms" priority="1">'
        '<pouInstance name="MainProg" typeName="Main"/>'
        '</task>'
        '</resource>'
        '</configuration>'
    )
    p = parse_plcopen_xml(_wrap_doc(configs=cfg))
    pi = p.configurations[0].resources[0].pou_instances[0]
    assert pi.task == "Fast"


# -----------------------------------------------------------------------------
# Error cases
# -----------------------------------------------------------------------------


def test_malformed_xml_raises_plcopen_parse_error():
    with pytest.raises(PlcopenParseError, match="malformed XML"):
        parse_plcopen_xml("<<<not xml>>>")


def test_wrong_root_element_raises():
    with pytest.raises(PlcopenParseError, match="expected root element"):
        parse_plcopen_xml(
            '<?xml version="1.0"?><notProject/>'
        )


def test_pou_missing_pouType_attribute_raises():
    pou = '<pou name="Foo"><interface/></pou>'
    with pytest.raises(PlcopenParseError, match="unknown pouType"):
        parse_plcopen_xml(_wrap_doc(pous=pou))


def test_pou_unknown_pouType_raises():
    pou = '<pou name="Foo" pouType="bogus"><interface/></pou>'
    with pytest.raises(PlcopenParseError, match="unknown pouType"):
        parse_plcopen_xml(_wrap_doc(pous=pou))


def test_variable_missing_name_raises():
    pou = (
        '<pou name="P" pouType="program"><interface><localVars>'
        '<variable><type><INT/></type></variable>'
        '</localVars></interface></pou>'
    )
    with pytest.raises(PlcopenParseError, match="missing required name"):
        parse_plcopen_xml(_wrap_doc(pous=pou))


def test_variable_missing_type_raises():
    pou = (
        '<pou name="P" pouType="program"><interface><localVars>'
        '<variable name="x"/>'
        '</localVars></interface></pou>'
    )
    with pytest.raises(PlcopenParseError, match="missing required child"):
        parse_plcopen_xml(_wrap_doc(pous=pou))


def test_access_variable_missing_alias_raises():
    cfg = (
        '<configuration name="Default">'
        '<accessVars>'
        '<accessVariable instancePathAndName="R1.M.x">'
        '<type><INT/></type></accessVariable>'
        '</accessVars>'
        '</configuration>'
    )
    with pytest.raises(PlcopenParseError, match="alias="):
        parse_plcopen_xml(_wrap_doc(configs=cfg))


def test_task_non_integer_priority_raises():
    cfg = (
        '<configuration name="Default">'
        '<resource name="R1">'
        '<task name="Bad" priority="not-a-number"/>'
        '</resource>'
        '</configuration>'
    )
    with pytest.raises(PlcopenParseError, match="non-integer priority"):
        parse_plcopen_xml(_wrap_doc(configs=cfg))
