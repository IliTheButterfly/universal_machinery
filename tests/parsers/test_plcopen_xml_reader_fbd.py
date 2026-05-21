"""Tests for FBD body reader coverage in the PLCopen XML parser.

The reader now picks up ``<body><FBD>...</FBD></body>`` and
reverses the connection-graph synthesis produced by the emitter
(PR #10).  Tests pair the existing FBD emitter with the new
reader so any drift between the two surfaces immediately.
"""
from datetime import datetime, timezone

import pytest

from universal_machinery.builders import (
    fb_block, fbd_jump, fbd_label, fbd_network, fbd_return, in_var,
    inout_var, out_var, pin, prog, program, var,
)
from universal_machinery.emitters.plcopen_xml import emit_xml
from universal_machinery.il import (
    BlockPin, Connection, FbBlock, FbdJump, FbdLabel, FbdNetwork,
    FbdReturn, InOutVariable, InVariable, OutVariable, Position, TagType,
)
from universal_machinery.parsers.plcopen_xml import (
    PlcopenParseError, parse_plcopen_xml,
)


_FIXED_TIME = datetime(2026, 5, 21, 12, 0, 0, tzinfo=timezone.utc)


def _round_trip(net, **kwargs):
    p = program(subroutines=[prog("Main", main=True, fbd_body=net,
                                    **kwargs)])
    p2 = parse_plcopen_xml(emit_xml(p, time_now=_FIXED_TIME))
    return p2.find_subroutine("Main").fbd_body


# -----------------------------------------------------------------------------
# Round-trip
# -----------------------------------------------------------------------------


def test_empty_fbd_body_round_trips():
    net = fbd_network()
    out = _round_trip(net)
    assert isinstance(out, FbdNetwork)
    assert out.elements == []


def test_simple_in_to_out_round_trips():
    net = fbd_network(
        in_var(0, "x"),
        out_var(1, "y", source_id=0),
    )
    out = _round_trip(net)
    assert len(out.elements) == 2
    assert isinstance(out.elements[0], InVariable)
    assert out.elements[0].expression == "x"
    assert isinstance(out.elements[1], OutVariable)
    assert out.elements[1].connection == Connection(source_id=0)


def test_add_network_round_trips():
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        fb_block(2, "ADD",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "z", source_id=2, source_pin="OUT"),
    )
    out = _round_trip(net)
    assert len(out.elements) == 4
    block = out.elements[2]
    assert isinstance(block, FbBlock)
    assert block.type_name == "ADD"
    assert [p.formal_parameter for p in block.inputs] == ["IN1", "IN2"]
    assert block.inputs[0].connection == Connection(source_id=0)
    assert block.inputs[1].connection == Connection(source_id=1)
    assert out.elements[3].connection == Connection(source_id=2,
                                                       source_pin="OUT")


def test_fb_instance_name_round_trips():
    net = fbd_network(
        in_var(0, "clk"),
        fb_block(1, "TON",
                 instance_name="tmr1",
                 inputs=[pin("IN", source_id=0), pin("PT")],
                 outputs=[pin("Q"), pin("ET")]),
    )
    out = _round_trip(net)
    block = out.elements[1]
    assert block.type_name == "TON"
    assert block.instance_name == "tmr1"
    assert [p.formal_parameter for p in block.outputs] == ["Q", "ET"]


def test_negated_pin_modifier_round_trips():
    net = fbd_network(
        in_var(0, "a"),
        fb_block(1, "NOT",
                 inputs=[pin("IN", source_id=0, negated=True)],
                 outputs=[pin("OUT")]),
    )
    out = _round_trip(net)
    assert out.elements[1].inputs[0].negated


def test_edge_and_storage_modifiers_round_trip():
    net = fbd_network(
        in_var(0, "clk"),
        fb_block(1, "X",
                 inputs=[pin("IN", source_id=0,
                              edge="rising", storage="set")],
                 outputs=[pin("OUT")]),
    )
    out = _round_trip(net)
    assert out.elements[1].inputs[0].edge == "rising"
    assert out.elements[1].inputs[0].storage == "set"


def test_negated_in_variable_round_trips():
    net = fbd_network(
        in_var(0, "x"),
        # rebuild as a negated InVariable via the AST directly
    )
    # Replace with explicit negated form for the test
    net.elements[0] = InVariable(local_id=0, expression="x", negated=True)
    out = _round_trip(net)
    assert isinstance(out.elements[0], InVariable)
    assert out.elements[0].negated


def test_inout_variable_with_modifiers_round_trips():
    net = fbd_network(
        in_var(0, "delta"),
        inout_var(1, "counter", source_id=0),
    )
    # Inject explicit per-side modifiers
    net.elements[1] = InOutVariable(
        local_id=1, expression="counter",
        connection=Connection(source_id=0),
        negated_in=True, edge_out="rising", storage_in="set",
    )
    out = _round_trip(net)
    inout = out.elements[1]
    assert isinstance(inout, InOutVariable)
    assert inout.negated_in
    assert inout.edge_out == "rising"
    assert inout.storage_in == "set"


def test_explicit_positions_round_trip():
    net = fbd_network(
        in_var(0, "x", position=Position(50, 200)),
        out_var(1, "y", source_id=0, position=Position(500, 200)),
    )
    out = _round_trip(net)
    assert out.elements[0].position == Position(50, 200)
    assert out.elements[1].position == Position(500, 200)


def test_auto_layout_positions_also_round_trip():
    """The emitter auto-lays-out elements without a Position;
    the reader recovers the auto-layout coords on read-back."""
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        in_var(2, "z"),
    )
    out = _round_trip(net)
    # Auto-layout starts at (20, 20) with 200×100 grid; we don't
    # pin the exact constants -- just assert all three positions
    # came back as Position(...) instances.
    assert all(isinstance(e.position, Position) for e in out.elements)


def test_jumps_and_labels_round_trip():
    net = fbd_network(
        in_var(0, "cond"),
        fbd_jump(1, "END_OK", source_id=0),
        fbd_label(2, "END_OK"),
    )
    out = _round_trip(net)
    assert isinstance(out.elements[1], FbdJump)
    assert out.elements[1].label == "END_OK"
    assert out.elements[1].connection == Connection(source_id=0)
    assert isinstance(out.elements[2], FbdLabel)
    assert out.elements[2].label == "END_OK"


def test_return_with_optional_gate_round_trips():
    net = fbd_network(
        in_var(0, "halt"),
        fbd_return(1, source_id=0),
    )
    out = _round_trip(net)
    assert isinstance(out.elements[1], FbdReturn)
    assert out.elements[1].connection == Connection(source_id=0)


def test_return_without_gate_round_trips():
    net = fbd_network(fbd_return(0))
    out = _round_trip(net)
    assert isinstance(out.elements[0], FbdReturn)
    assert out.elements[0].connection is None


def test_execution_order_attribute_round_trips():
    """Optional ``executionOrderId`` per element survives the
    round-trip."""
    net = fbd_network(
        in_var(0, "x", execution_order=1),
        out_var(1, "y", source_id=0, execution_order=2),
    )
    out = _round_trip(net)
    assert out.elements[0].execution_order == 1
    assert out.elements[1].execution_order == 2


# -----------------------------------------------------------------------------
# Error cases
# -----------------------------------------------------------------------------


_BASE = """<?xml version="1.0"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0201">
  <contentHeader name="T"/>
  <types><dataTypes/><pous>
    <pou name="P" pouType="program">
      <interface/>
      <body><FBD>{body}</FBD></body>
    </pou>
  </pous></types>
</project>"""


def test_block_missing_typeName_raises():
    body = '<block localId="0"><inputVariables/><inOutVariables/><outputVariables/></block>'
    with pytest.raises(PlcopenParseError, match="typeName"):
        parse_plcopen_xml(_BASE.format(body=body))


def test_block_missing_localId_raises():
    body = '<block typeName="ADD"><inputVariables/><inOutVariables/><outputVariables/></block>'
    with pytest.raises(PlcopenParseError, match="missing required localId"):
        parse_plcopen_xml(_BASE.format(body=body))


def test_label_missing_label_attr_raises():
    body = '<label localId="0"><position x="0" y="0"/></label>'
    with pytest.raises(PlcopenParseError, match="<label> missing required label"):
        parse_plcopen_xml(_BASE.format(body=body))


def test_jump_missing_label_attr_raises():
    body = '<jump localId="0"><position x="0" y="0"/></jump>'
    with pytest.raises(PlcopenParseError, match="<jump> missing required label"):
        parse_plcopen_xml(_BASE.format(body=body))


def test_connection_non_integer_refLocalId_raises():
    body = (
        '<outVariable localId="1">'
        '  <position x="0" y="0"/>'
        '  <connectionPointIn><connection refLocalId="not-a-number"/></connectionPointIn>'
        '  <expression>y</expression>'
        '</outVariable>'
    )
    with pytest.raises(PlcopenParseError, match="non-integer refLocalId"):
        parse_plcopen_xml(_BASE.format(body=body))


# -----------------------------------------------------------------------------
# Reader picks FBD over ST when both could match
# -----------------------------------------------------------------------------


def test_fbd_body_wins_over_st_when_present_in_xml():
    """The reader's body dispatch tries <FBD> before <ST>."""
    net = fbd_network(in_var(0, "x"))
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    p2 = parse_plcopen_xml(emit_xml(p, time_now=_FIXED_TIME))
    sub = p2.find_subroutine("Main")
    assert sub.fbd_body is not None
    assert sub.st_body is None
