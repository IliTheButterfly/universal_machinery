"""Tests for the Function Block Diagram body (IEC 61131-3 §6.7).

Covers four layers:

  - AST construction (lookups, default values)
  - Builder DSL (smart-wiring shortcuts, position omission)
  - ST emitter fallback (marker comment when body is FBD)
  - JSON round-trip
  - Validation (body-kind mutex, local_id uniqueness, connection
    resolution, unknown source pin, unknown jump label, method
    fbd_body)
"""
import pytest

from universal_machinery.builders import (
    fb, fb_block, fbd_jump, fbd_label, fbd_network, fbd_return, in_var,
    inout_var, method, out_var, pin, prog, program, var, var_in,
)
from universal_machinery.emitters.st import emit_pou
from universal_machinery.il import (
    BlockPin, Connection, FbBlock, FbdJump, FbdLabel, FbdNetwork,
    FbdReturn, InOutVariable, InVariable, OutVariable, Position, TagType,
)
from universal_machinery.serialisation import from_json, to_json
from universal_machinery.validation import is_valid, validate


# -----------------------------------------------------------------------------
# Dataclass construction + lookups
# -----------------------------------------------------------------------------


def test_position_defaults_match_xsd_decimal_type():
    p = Position(x=10.5, y=20.0)
    assert p.x == 10.5
    assert p.y == 20.0


def test_fbd_network_next_local_id_starts_at_zero():
    net = FbdNetwork()
    assert net.next_local_id() == 0


def test_fbd_network_next_local_id_after_inserts():
    net = FbdNetwork(elements=[
        InVariable(local_id=0, expression="x"),
        InVariable(local_id=5, expression="y"),
        InVariable(local_id=2, expression="z"),
    ])
    assert net.next_local_id() == 6


def test_fbd_network_find_by_local_id():
    a = InVariable(local_id=0, expression="x")
    b = InVariable(local_id=3, expression="y")
    net = FbdNetwork(elements=[a, b])
    assert net.find(0) is a
    assert net.find(3) is b
    assert net.find(99) is None


def test_fbd_network_find_label():
    lbl_a = FbdLabel(local_id=0, label="START")
    lbl_b = FbdLabel(local_id=1, label="END")
    net = FbdNetwork(elements=[lbl_a, lbl_b])
    assert net.find_label("START") is lbl_a
    assert net.find_label("END") is lbl_b
    assert net.find_label("MISSING") is None


# -----------------------------------------------------------------------------
# Builder DSL
# -----------------------------------------------------------------------------


def test_pin_shortcut_creates_connection():
    p = pin("IN1", source_id=5, source_pin="OUT")
    assert isinstance(p, BlockPin)
    assert p.connection == Connection(source_id=5, source_pin="OUT")


def test_pin_without_source_has_no_connection():
    p = pin("OUT")
    assert p.connection is None


def test_pin_modifier_flags():
    p = pin("IN1", source_id=0, negated=True, edge="rising", storage="set")
    assert p.negated
    assert p.edge == "rising"
    assert p.storage == "set"


def test_fbd_network_collects_elements_in_order():
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        fb_block(2, "ADD",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "z", source_id=2, source_pin="OUT"),
    )
    assert len(net.elements) == 4
    assert isinstance(net.elements[0], InVariable)
    assert isinstance(net.elements[2], FbBlock)
    assert isinstance(net.elements[3], OutVariable)


def test_out_var_shortcut_wires_source():
    o = out_var(3, "z", source_id=2, source_pin="OUT")
    assert o.connection == Connection(source_id=2, source_pin="OUT")


def test_inout_var_shortcut_wires_source():
    v = inout_var(5, "counter", source_id=4, source_pin="OUT")
    assert v.connection == Connection(source_id=4, source_pin="OUT")


def test_fbd_jump_and_return_with_gate():
    j = fbd_jump(10, "END", source_id=9, source_pin="OUT")
    assert j.label == "END"
    assert j.connection == Connection(source_id=9, source_pin="OUT")
    r = fbd_return(11, source_id=9, source_pin="OUT")
    assert isinstance(r, FbdReturn)
    assert r.connection == Connection(source_id=9, source_pin="OUT")


# -----------------------------------------------------------------------------
# ST emitter fallback
# -----------------------------------------------------------------------------


def test_st_emitter_marker_comment_for_fbd_body():
    net = fbd_network(
        in_var(0, "x"),
        out_var(1, "y", source_id=0),
    )
    sub = prog("Main", main=True, fbd_body=net)
    txt = emit_pou(sub)
    assert "PROGRAM Main" in txt
    assert "FBD body not emitted in ST" in txt
    assert "see PLCopen XML <FBD>" in txt
    assert "END_PROGRAM" in txt


# -----------------------------------------------------------------------------
# JSON round-trip
# -----------------------------------------------------------------------------


def test_fbd_round_trips_through_json():
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        fb_block(2, "AND",
                 inputs=[pin("IN1", source_id=0, negated=True),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "result", source_id=2, source_pin="OUT"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])

    js = to_json(p)
    p2 = from_json(js)

    n2 = p2.subroutines[0].fbd_body
    assert len(n2.elements) == 4
    assert n2.elements[2].type_name == "AND"
    assert n2.elements[2].inputs[0].negated  # modifier preserved
    assert n2.elements[3].connection.source_id == 2


def test_jump_and_label_round_trip():
    net = fbd_network(
        in_var(0, "cond"),
        fbd_jump(1, "END", source_id=0),
        fbd_label(2, "END"),
        fbd_return(3),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])

    p2 = from_json(to_json(p))
    n2 = p2.subroutines[0].fbd_body
    assert n2.find_label("END") is not None
    assert isinstance(n2.elements[1], FbdJump)
    assert isinstance(n2.elements[3], FbdReturn)


def test_inout_variable_modifier_round_trip():
    """InOutVariable carries per-side negated/edge/storage; check
    they survive serialisation."""
    net = fbd_network(
        in_var(0, "x"),
        inout_var(1, "counter", source_id=0),
    )
    # Re-create with explicit modifiers
    inout = InOutVariable(local_id=1, expression="counter",
                           connection=Connection(source_id=0),
                           negated_in=True, edge_out="rising",
                           storage_in="set")
    net.elements[1] = inout
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    p2 = from_json(to_json(p))
    n2 = p2.subroutines[0].fbd_body
    assert n2.elements[1].negated_in
    assert n2.elements[1].edge_out == "rising"
    assert n2.elements[1].storage_in == "set"


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def test_clean_fbd_program_validates():
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
    assert is_valid(p), validate(p)


def test_multiple_body_kinds_with_fbd_flagged():
    net = fbd_network(in_var(0, "x"))
    bad = prog("Main", st_body=[], fbd_body=net,
                rungs=[])  # rungs/st_body empty, fbd_body set...
    # Need actual content in two body kinds to trigger
    from universal_machinery.builders import assign, lit, rung, coil
    bad = prog("Main",
                rungs=[rung(coil("Y1"))],
                fbd_body=net)
    p = program(subroutines=[bad])
    codes = [e.code for e in validate(p)]
    assert "multiple-body-kinds" in codes


def test_duplicate_local_id_flagged():
    net = FbdNetwork(elements=[
        InVariable(local_id=0, expression="x"),
        InVariable(local_id=0, expression="y"),  # duplicate
    ])
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    codes = [e.code for e in validate(p)]
    assert "fbd-duplicate-local-id" in codes


def test_unresolved_connection_flagged():
    net = fbd_network(
        in_var(0, "x"),
        out_var(1, "z", source_id=99),  # 99 doesn't exist
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    codes = [e.code for e in validate(p)]
    assert "fbd-unresolved-connection" in codes


def test_unknown_source_pin_flagged():
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "NOT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        # source_pin "BOGUS" doesn't exist on block 1
        out_var(2, "y", source_id=1, source_pin="BOGUS"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    codes = [e.code for e in validate(p)]
    assert "fbd-unknown-source-pin" in codes


def test_known_source_pin_passes():
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "NOT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        out_var(2, "y", source_id=1, source_pin="OUT"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    codes = [e.code for e in validate(p)]
    assert "fbd-unknown-source-pin" not in codes


def test_unknown_jump_label_flagged():
    net = fbd_network(
        in_var(0, "cond"),
        fbd_jump(1, "MISSING", source_id=0),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    codes = [e.code for e in validate(p)]
    assert "fbd-unknown-jump-label" in codes


def test_known_jump_label_passes():
    net = fbd_network(
        in_var(0, "cond"),
        fbd_jump(1, "END", source_id=0),
        fbd_label(2, "END"),
    )
    p = program(subroutines=[prog("Main", main=True, fbd_body=net)])
    codes = [e.code for e in validate(p)]
    assert "fbd-unknown-jump-label" not in codes


def test_method_fbd_body_validates():
    """An FB method authored in FBD goes through the same checks."""
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "NOT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        out_var(2, "y", source_id=1, source_pin="OUT"),
    )
    p = program(subroutines=[
        fb("Inverter",
           methods=[method("Compute",
                           inputs=[var_in("x", TagType.BOOL)],
                           fbd_body=net)]),
    ])
    assert is_valid(p), validate(p)


def test_method_fbd_body_bad_connection_flagged():
    """Bad references in a method's fbd_body should still be caught."""
    bad_net = fbd_network(
        in_var(0, "x"),
        out_var(1, "y", source_id=42),  # dangling
    )
    p = program(subroutines=[
        fb("Owner",
           methods=[method("Bad", fbd_body=bad_net)]),
    ])
    codes = [e.code for e in validate(p)]
    assert "fbd-unresolved-connection" in codes
