"""XSD validation tests for the PLCopen TC6 XML emitter.

These tests run the emitter's output through the official PLCopen
TC6 v2.01 XSD (bundled under ``universal_machinery/emitters/
schemas/``).  They're the cert verification loop's first checkpoint:
emit -> validate against the canonical schema -> assert no
conformance errors.

Skipped automatically if the ``xmlschema`` package isn't installed.
"""
from datetime import datetime, timezone

import pytest

# Skip the whole module if the validator isn't available.
xmlschema = pytest.importorskip("xmlschema")

from universal_machinery.builders import (
    abs_, add, and_, call, coil, fb, fn, gt, le, limit, move, nc, no,
    parallel, prog, program, reset_, ret, rung, sel, set_, sub, tag, tag_decl,
    var, var_in, var_inout, var_out,
)
from universal_machinery.il import TagType
from universal_machinery.emitters.plcopen_xml import (
    XMLSchemaError, bundled_xsd_path, emit_xml, is_valid_plcopen_xml,
    validate_plcopen_xml,
)


_FIXED_TIME = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Bundled schema availability
# -----------------------------------------------------------------------------


def test_bundled_xsd_is_present_and_loadable():
    """The TC6 XSD ships with the package and is loadable by xmlschema."""
    path = bundled_xsd_path()
    assert path.exists()
    schema = xmlschema.XMLSchema(str(path))
    # Root element is "project" in the PLCopen namespace
    assert any(e.local_name == "project" for e in schema.elements.values())


# -----------------------------------------------------------------------------
# Validation: simple programs
# -----------------------------------------------------------------------------


def test_empty_program_validates():
    """A trivially-empty Program produces XSD-valid XML."""
    xml = emit_xml(program(), time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)         # raises on failure


def test_single_program_with_one_rung_validates():
    p = program(subroutines=[
        prog("Main", main=True, rungs=[rung(no("X1"), coil("Y1"))]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_function_with_return_type_validates():
    p = program(subroutines=[
        fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("r", TagType.INT)],
           return_type=TagType.INT,
           rungs=[rung(add(tag("a"), tag("b"), tag("r")))]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_function_block_with_in_out_validates():
    p = program(subroutines=[
        fb("Counter",
           inputs=[var_in("clk", TagType.BOOL)],
           in_outs=[var_inout("count", TagType.INT)],
           outputs=[var_out("at_max", TagType.BOOL)],
           local_vars=[var("scratch", TagType.INT, initial="0")],
           rungs=[
               rung(no(tag("clk")), add(tag("count"), 1, tag("count"))),
               rung(le(tag("count"), 100), coil(tag("at_max"))),
           ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_program_with_tags_emits_globals_holder_that_validates():
    p = program(
        tags=[
            tag_decl("start_btn", TagType.BOOL, "operator start",
                     locked="X101"),
            tag_decl("running",   TagType.BOOL, "running indicator"),
            tag_decl("speed_sp",  TagType.INT,  "speed setpoint"),
        ],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_program_with_comment_validates():
    """contentHeader.Comment is optional but conformantly-cased."""
    p = program(
        project_name="Test",
        comment="Demo project description",
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


# -----------------------------------------------------------------------------
# Validation: complex programs exercising many op types
# -----------------------------------------------------------------------------


def test_complex_program_with_many_ops_validates():
    """Comprehensive shape: every supported op family in one program."""
    p = program(
        project_name="ComplexDemo",
        comment="Exercises many op types end-to-end",
        tags=[
            tag_decl("start_btn", TagType.BOOL, "operator start",
                     locked="X101"),
            tag_decl("running",   TagType.BOOL, "running indicator"),
            tag_decl("speed_sp",  TagType.INT,  "speed setpoint"),
        ],
        subroutines=[
            prog("Main", main=True, comment="entry point", rungs=[
                rung(no("start_btn"), nc("estop"), set_("running")),
                rung(no("running"), gt("speed_sp", 100),
                     reset_("low_speed")),
                rung(parallel([no("btn_a")], [no("btn_b")]),
                     coil("any_btn")),
                rung(no("running"),
                     call("Avg",
                          inputs=[("a", "speed_sp"), ("b", 100)],
                          return_to="avg_result")),
                rung(no("compute_enable"),
                     limit(0, "avg_result", 1000, "clamped")),
                rung(abs_("signed_val", "unsigned_val")),
                rung(ret()),
            ]),
            fn("Avg",
               inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
               outputs=[var_out("r", TagType.INT)],
               return_type=TagType.INT,
               rungs=[
                   rung(add(tag("a"), tag("b"), tag("r"))),
                   rung(ret()),
               ]),
            fb("Counter",
               inputs=[var_in("clk", TagType.BOOL)],
               in_outs=[var_inout("count", TagType.INT)],
               outputs=[var_out("at_max", TagType.BOOL)],
               local_vars=[var("scratch", TagType.INT, initial="0")],
               rungs=[
                   rung(no(tag("clk")),
                        add(tag("count"), 1, tag("count"))),
                   rung(le(tag("count"), 100), coil(tag("at_max"))),
               ]),
        ],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_program_with_xml_special_chars_in_strings_validates():
    """Ops whose ST text contains <, >, & should be escaped so the
    resulting XML remains valid."""
    p = program(subroutines=[
        prog("Main", main=True, rungs=[
            rung(gt("DS5", 100), set_("over")),
            rung(le("DS6", 0), reset_("nonzero")),
        ]),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


# -----------------------------------------------------------------------------
# Negative cases
# -----------------------------------------------------------------------------


def test_validate_raises_on_malformed_xml():
    with pytest.raises(XMLSchemaError):
        validate_plcopen_xml("<not a project document/>")


def test_is_valid_returns_false_on_bad_xml():
    assert is_valid_plcopen_xml("<not a project document/>") is False


def test_is_valid_returns_true_on_good_xml():
    xml = emit_xml(program(), time_now=_FIXED_TIME)
    assert is_valid_plcopen_xml(xml) is True


# -----------------------------------------------------------------------------
# Round-trip determinism
# -----------------------------------------------------------------------------


def test_validation_is_idempotent():
    """Validation never mutates the input or its result."""
    xml = emit_xml(
        program(subroutines=[prog("Main", main=True)]),
        time_now=_FIXED_TIME,
    )
    validate_plcopen_xml(xml)
    validate_plcopen_xml(xml)         # second pass also clean


# -----------------------------------------------------------------------------
# User-defined types -- every variant must produce XSD-valid output
# -----------------------------------------------------------------------------


def test_enum_type_validates():
    from universal_machinery.builders import enum_type
    p = program(
        user_types=[enum_type("Color", values=["RED", "GREEN", "BLUE"])],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_struct_type_validates():
    from universal_machinery.builders import struct_type
    p = program(
        user_types=[struct_type("Point", members=[
            var("x", TagType.INT), var("y", TagType.INT),
        ])],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_struct_with_initial_values_validates():
    from universal_machinery.builders import struct_type
    p = program(
        user_types=[struct_type("Config", members=[
            var("max_speed", TagType.INT,  initial="1000"),
            var("enabled",   TagType.BOOL, initial="TRUE"),
        ])],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_array_type_single_dimension_validates():
    from universal_machinery.builders import array_type
    p = program(
        user_types=[array_type("Vector10",
                                element_type=TagType.INT,
                                bounds=[(0, 9)])],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_array_type_multidimensional_validates():
    from universal_machinery.builders import array_type
    p = program(
        user_types=[array_type("Matrix3x3",
                                element_type=TagType.REAL,
                                bounds=[(0, 2), (0, 2)])],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_array_of_user_type_validates():
    from universal_machinery.builders import array_type, named_type, struct_type
    p = program(
        user_types=[
            struct_type("Point", members=[var("x", TagType.INT)]),
            array_type("PointBuffer",
                        element_type=named_type("Point"),
                        bounds=[(0, 99)]),
        ],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_alias_of_elementary_validates():
    from universal_machinery.builders import alias_type
    p = program(
        user_types=[alias_type("Distance", base=TagType.DINT)],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_alias_of_user_type_validates():
    from universal_machinery.builders import alias_type, named_type, struct_type
    p = program(
        user_types=[
            struct_type("Point", members=[var("x", TagType.INT)]),
            alias_type("BigPoint", base=named_type("Point")),
        ],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_nested_struct_via_named_type_validates():
    from universal_machinery.builders import struct_type, named_type
    p = program(
        user_types=[
            struct_type("Point", members=[
                var("x", TagType.INT), var("y", TagType.INT),
            ]),
            struct_type("Line", members=[
                var("start", named_type("Point")),
                var("end",   named_type("Point")),
            ]),
        ],
        subroutines=[prog("Main", main=True)],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_var_typed_with_user_type_validates():
    """A POU local declared with a UDT type renders as
    <type><derived name="..."/></type> and validates."""
    from universal_machinery.builders import struct_type, named_type
    p = program(
        user_types=[struct_type("Point", members=[
            var("x", TagType.INT), var("y", TagType.INT),
        ])],
        subroutines=[
            prog("Main", main=True,
                 local_vars=[var("origin", named_type("Point"))]),
        ],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


# -----------------------------------------------------------------------------
# CONFIGURATION / RESOURCE / TASK -- IEC §2.7 + XSD <instances><configurations>
# -----------------------------------------------------------------------------


def test_empty_configuration_validates():
    from universal_machinery.builders import configuration
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default")],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_configuration_with_resource_validates():
    from universal_machinery.builders import configuration, resource
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("CPU1")])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_configuration_with_cyclic_task_validates():
    from universal_machinery.builders import (
        configuration, resource, task_spec,
    )
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                tasks=[task_spec("Fast", priority=1, interval="T#10ms")])])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_configuration_with_single_shot_task_validates():
    from universal_machinery.builders import (
        configuration, resource, task_spec,
    )
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                tasks=[task_spec("OnButton", priority=0,
                                 single="start_btn")])])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_pou_instance_bound_to_task_nested_under_task_element():
    """PLCopen schema places task-bound POU instances under
    ``<task>``, not at the resource level."""
    from universal_machinery.builders import (
        configuration, pou_instance, resource, task_spec,
    )
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                tasks=[task_spec("Fast", priority=1, interval="T#10ms")],
                pou_instances=[pou_instance("MainProg",
                                            type_name="Main",
                                            task="Fast")])])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # And the instance lives inside the task element.
    import xml.etree.ElementTree as ET
    from universal_machinery.emitters.plcopen_xml import PLCOPEN_NS
    NS = {"plc": PLCOPEN_NS}
    root = ET.fromstring(xml)
    task_inst = root.find(".//plc:task/plc:pouInstance", NS)
    assert task_inst is not None
    assert task_inst.attrib["name"] == "MainProg"
    assert task_inst.attrib["typeName"] == "Main"


def test_resource_level_pou_instance_no_task_binding():
    """Unbound POU instances appear at the resource level, not under
    any task."""
    from universal_machinery.builders import (
        configuration, pou_instance, resource,
    )
    p = program(
        subroutines=[prog("Helper", main=False)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                pou_instances=[pou_instance("Helper1",
                                            type_name="Helper")])])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_configuration_with_global_vars_at_both_scopes():
    from universal_machinery.builders import configuration, resource
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            global_vars=[var("system_state", TagType.INT)],
            resources=[resource("CPU1",
                global_vars=[var("cpu_temp", TagType.REAL)])])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_configuration_with_access_vars_validates():
    """accessVariable has required ``alias`` and ``instancePathAndName``
    attributes per the schema; our emitter populates both from
    Var.name."""
    from universal_machinery.builders import configuration
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            access_vars=[var("hmi_state", TagType.INT),
                         var("hmi_speed", TagType.REAL)])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_multi_resource_configuration_validates():
    """A multi-PLC Configuration has multiple Resources -- the
    project-vision multi-PLC arc starts here."""
    from universal_machinery.builders import configuration, resource
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("CellController",
            resources=[
                resource("PLC_A"),
                resource("PLC_B"),
                resource("PLC_C"),
            ])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_comprehensive_configuration_validates():
    """Real-shape Configuration with multiple tasks, multiple bound
    + unbound POU instances, globals at both scopes, and access
    variables -- end-to-end XSD validation."""
    from universal_machinery.builders import (
        configuration, pou_instance, resource, task_spec,
    )
    p = program(
        project_name="ProductionLine",
        subroutines=[
            prog("MotionControl", main=False),
            prog("ProcessControl", main=False),
            prog("Diagnostics", main=False),
            prog("EmergencyStop", main=False),
            prog("BackgroundJobs", main=False),
        ],
        configurations=[configuration("ProductionLine",
            global_vars=[
                var("recipe_id", TagType.INT),
                var("system_ok", TagType.BOOL),
            ],
            access_vars=[
                var("hmi_recipe", TagType.INT),
                var("hmi_running", TagType.BOOL),
            ],
            resources=[resource("MainController",
                tasks=[
                    task_spec("FastControl",  priority=1, interval="T#10ms"),
                    task_spec("MediumControl", priority=2, interval="T#100ms"),
                    task_spec("SlowControl",  priority=3, interval="T#1s"),
                    task_spec("OnEmergency", priority=0,
                              single="emergency_btn"),
                ],
                pou_instances=[
                    pou_instance("Motion", type_name="MotionControl",
                                 task="FastControl"),
                    pou_instance("Process", type_name="ProcessControl",
                                 task="MediumControl"),
                    pou_instance("Diag", type_name="Diagnostics",
                                 task="SlowControl"),
                    pou_instance("EStop", type_name="EmergencyStop",
                                 task="OnEmergency"),
                    pou_instance("Background",
                                 type_name="BackgroundJobs"),  # unbound
                ],
                global_vars=[
                    var("cpu_temp",     TagType.REAL),
                    var("scan_time_ms", TagType.INT),
                ])])],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)


def test_comprehensive_udt_program_validates():
    """Mixed program with every UDT variant + UDT-typed POU
    variables + nested types -- single end-to-end XSD check."""
    from universal_machinery.builders import (
        alias_type, array_type, enum_type, named_type, struct_type,
    )
    p = program(
        project_name="UDTDemo",
        user_types=[
            enum_type("MachineState",
                       values=["IDLE", "STARTING", "RUNNING", "STOPPING"]),
            struct_type("AxisConfig", members=[
                var("max_velocity", TagType.REAL, initial="100.0"),
                var("max_accel",    TagType.REAL, initial="500.0"),
            ]),
            struct_type("MachineConfig", members=[
                var("axis_x", named_type("AxisConfig")),
                var("axis_y", named_type("AxisConfig")),
                var("state",  named_type("MachineState")),
            ]),
            array_type("RecipeBuffer",
                        element_type=TagType.INT,
                        bounds=[(0, 99)]),
            alias_type("Distance", base=TagType.DINT),
        ],
        subroutines=[
            prog("Main", main=True,
                 local_vars=[
                     var("config", named_type("MachineConfig")),
                     var("recipes", named_type("RecipeBuffer")),
                 ]),
        ],
    )
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
