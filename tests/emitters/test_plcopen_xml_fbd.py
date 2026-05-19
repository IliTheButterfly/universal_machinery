"""PLCopen XML emission of Function Block Diagram bodies.

Each test emits a complete project XML and re-validates against
the bundled PLCopen TC6 v2.01 XSD so the new ``<FBD>`` body path
is part of the conformant-output guarantee.
"""
from datetime import datetime, timezone

import pytest

xmlschema = pytest.importorskip("xmlschema")

from universal_machinery.builders import (
    fb_block, fbd_jump, fbd_label, fbd_network, fbd_return, in_var,
    inout_var, out_var, pin, prog, program, var,
)
from universal_machinery.emitters.plcopen_xml import (
    emit_xml, validate_plcopen_xml,
)
from universal_machinery.il import Position, TagType


_FIXED_TIME = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


# -----------------------------------------------------------------------------
# Basic shapes
# -----------------------------------------------------------------------------


def test_empty_fbd_body_validates():
    p = program(subroutines=[
        prog("Main", main=True, fbd_body=fbd_network()),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<FBD" in xml


def test_simple_add_fbd_validates():
    """Two inVariables -> ADD block -> outVariable."""
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        fb_block(2, "ADD",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "z", source_id=2, source_pin="OUT"),
    )
    p = program(subroutines=[
        prog("Main", main=True,
             local_vars=[var("x", TagType.INT), var("y", TagType.INT),
                          var("z", TagType.INT)],
             fbd_body=net),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<inVariable localId="0"' in xml
    assert '<block localId="2" typeName="ADD"' in xml
    assert '<outVariable localId="3"' in xml


# -----------------------------------------------------------------------------
# Auto-layout vs explicit positions
# -----------------------------------------------------------------------------


def test_auto_layout_emits_required_position():
    """Elements without an explicit position get auto-laid out
    on a coarse grid; the XSD requires a <position> on every
    FBD element so this verifies the XML is well-formed."""
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        in_var(2, "z"),
    )
    p = program(subroutines=[
        prog("Main", main=True, fbd_body=net),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    # Three distinct positions, each in the auto-layout grid
    assert xml.count("<position x=") == 3
    assert 'x="20"' in xml
    assert 'x="220"' in xml
    assert 'x="420"' in xml


def test_explicit_positions_preserved():
    net = fbd_network(
        in_var(0, "x", position=Position(50, 200)),
        out_var(1, "y", source_id=0, position=Position(500, 200)),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'x="50" y="200"' in xml
    assert 'x="500" y="200"' in xml


# -----------------------------------------------------------------------------
# Pin modifiers (negated / edge / storage)
# -----------------------------------------------------------------------------


def test_negated_pin_emits_attribute():
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "AND",
                 inputs=[pin("IN1", source_id=0, negated=True)],
                 outputs=[pin("OUT")]),
        out_var(2, "y", source_id=1, source_pin="OUT"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'negated="true"' in xml


def test_edge_modifier_emits_attribute():
    net = fbd_network(
        in_var(0, "clk"),
        fb_block(1, "TON",
                 inputs=[pin("IN", source_id=0, edge="rising")],
                 outputs=[pin("Q")]),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'edge="rising"' in xml


def test_storage_modifier_emits_attribute():
    net = fbd_network(
        in_var(0, "trigger"),
        fb_block(1, "AND",
                 inputs=[pin("IN1", source_id=0, storage="set")],
                 outputs=[pin("OUT")]),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'storage="set"' in xml


# -----------------------------------------------------------------------------
# FB instance call sites
# -----------------------------------------------------------------------------


def test_fb_instance_name_emits_attribute():
    """FB call sites carry an instanceName so the runtime knows
    which per-instance state DataBlock to read/write."""
    net = fbd_network(
        in_var(0, "start_clk"),
        fb_block(1, "TON",
                 instance_name="tmr1",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("Q"), pin("ET")]),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert 'typeName="TON"' in xml
    assert 'instanceName="tmr1"' in xml


# -----------------------------------------------------------------------------
# InOutVariable
# -----------------------------------------------------------------------------


def test_inout_variable_emits_both_connection_points():
    net = fbd_network(
        in_var(0, "delta"),
        inout_var(1, "counter", source_id=0),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<inOutVariable" in xml
    # The schema requires connectionPointIn AND connectionPointOut on inOutVariable
    assert "<connectionPointIn>" in xml
    assert "<connectionPointOut/>" in xml


# -----------------------------------------------------------------------------
# Jumps / labels / returns
# -----------------------------------------------------------------------------


def test_jump_and_label_render_correctly():
    net = fbd_network(
        in_var(0, "cond"),
        fbd_jump(1, "END_OK", source_id=0),
        fbd_label(2, "END_OK"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<jump localId="1" label="END_OK"' in xml
    assert '<label localId="2" label="END_OK"' in xml


def test_return_renders_with_optional_gate():
    net = fbd_network(
        in_var(0, "halt"),
        fbd_return(1, source_id=0),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<return localId="1"' in xml


# -----------------------------------------------------------------------------
# Connection back-references
# -----------------------------------------------------------------------------


def test_connection_carries_formal_parameter_when_block_output():
    """When the source of a wire is a block, the connection
    records its formalParameter so the wire knows which output
    pin it came from."""
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "NOT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        out_var(2, "y", source_id=1, source_pin="OUT"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<connection refLocalId="1" formalParameter="OUT"/>' in xml


def test_connection_omits_formal_parameter_for_in_variable():
    """When the source is an inVariable (single implicit output
    pin), the connection omits formalParameter -- matches the
    XSD's optional formalParameter attribute."""
    net = fbd_network(
        in_var(0, "x"),
        out_var(1, "y", source_id=0),       # no source_pin
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert '<connection refLocalId="0"/>' in xml


# -----------------------------------------------------------------------------
# st_body / fbd_body coexistence dispatch
# -----------------------------------------------------------------------------


def test_fbd_body_chosen_over_other_bodies_in_xml():
    """When fbd_body is set, emit FBD; not ST."""
    net = fbd_network(in_var(0, "x"))
    p = program(subroutines=[
        prog("Main", main=True, fbd_body=net),
    ])
    xml = emit_xml(p, time_now=_FIXED_TIME)
    validate_plcopen_xml(xml)
    assert "<FBD" in xml
    assert "<ST>" not in xml
