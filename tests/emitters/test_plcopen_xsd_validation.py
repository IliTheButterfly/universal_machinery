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
