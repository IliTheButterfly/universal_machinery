"""Round-trip tests for the JSON serialisation layer.

Every IL construct should ``to_dict -> from_dict`` (and ``to_json
-> from_json``) without losing data.  This file covers the
common path -- primitives, dataclasses, enums, tuple fields,
Optional fields -- plus error paths (unknown type tag, schema
version skew).
"""
import dataclasses
import json

import pytest

from universal_machinery.builders import (
    abs_, add, alias_type, and_, array_type, call, coil, configuration,
    ctu, enum_type, eq, f_trig, fb, fn, ge, gt, label_, le, limit, lt,
    move, mul, named_type, nc, no, or_, parallel, pou_instance, prog,
    program, r_trig, redge, fedge, reset_, resource, ret, rs, rung, sel,
    set_, sqrt, sr, struct_type, subrange_type, subroutine, sub, tag,
    tag_decl, task_spec, ton, var, var_in, var_inout, var_out, xor_,
)
from universal_machinery.il import (
    Action, Address, AliasType, ArrayType, Configuration, EnumType,
    NamedType, PouInstance, Program, Resource, SfcNetwork, Step,
    StructType, SubrangeType, Subroutine, Tag, TagRef, TagType, TaskSpec,
    Transition, Var, VarDirection, VendorOp,
)
from universal_machinery.serialisation import (
    SCHEMA_VERSION, SerialisationError, from_dict, from_json,
    to_dict, to_json,
)


# -----------------------------------------------------------------------------
# Primitive / leaf round-trips
# -----------------------------------------------------------------------------


def test_address_round_trips():
    a = Address("DS9000")
    assert from_dict(to_dict(a)) == a


def test_tagref_round_trips():
    t = TagRef("motor_speed")
    assert from_dict(to_dict(t)) == t


def test_tagtype_enum_round_trips():
    """Enums encode as {_type, _value} and decode back to the same member."""
    encoded = to_dict(TagType.INT)
    assert encoded == {"_type": "TagType", "_value": "INT"}
    assert from_dict(encoded) is TagType.INT


def test_all_tagtype_members_round_trip():
    """Exhaustive enum coverage."""
    for member in TagType:
        assert from_dict(to_dict(member)) is member


def test_var_with_all_fields_round_trips():
    v = Var(name="x",
            data_type=TagType.INT,
            direction=VarDirection.INPUT,
            initial_value="42",
            address=Address("DS10"),
            comment="documented var")
    assert from_dict(to_dict(v)) == v


def test_tag_with_locked_address_round_trips():
    t = tag_decl("estop", TagType.BOOL, "E-stop input", locked="X101")
    assert from_dict(to_dict(t)) == t


# -----------------------------------------------------------------------------
# Ops -- each variant in the Op union
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("op_factory", [
    lambda: no("X001"),
    lambda: nc("X002"),
    lambda: redge("X003"),
    lambda: fedge("X004"),
    lambda: coil("Y001"),
    lambda: set_("Y002"),
    lambda: reset_("Y003"),
    lambda: ton("T0", 1000, done_bit="C100"),
    lambda: ctu("CT0", preset=100, reset="X005"),
    lambda: eq("DS1", 10),
    lambda: gt("speed", 100),
    lambda: move("DS1", "DS2"),
    lambda: add("a", "b", "c"),
    lambda: sel("X1", "DS1", "DS2", "DS3"),
    lambda: limit(0, "speed", 100, "clamped"),
    lambda: abs_("DS1", "DS2"),
    lambda: ret(),
    lambda: label_("loop_top"),
    lambda: r_trig("C100", "X001", "Y001"),
    lambda: sr("Y010", "X001", "X002"),
    lambda: rs("Y011", "X003", "X004"),
])
def test_op_round_trips(op_factory):
    """Every op family round-trips byte-for-byte."""
    op = op_factory()
    assert from_dict(to_dict(op)) == op


def test_parallel_group_with_nested_branches_round_trips():
    """ParallelGroup has nested tuple-of-tuples branches -- exercises
    the tuple-coercion path."""
    pg = parallel([no("X1"), nc("X2")], [no("X3")])
    assert from_dict(to_dict(pg)) == pg


def test_call_with_inputs_outputs_round_trips():
    """Call.inputs / Call.outputs are tuple[tuple[str, Value], ...]
    -- exercises nested tuple coercion."""
    c = call("PID",
             instance="DB1",
             inputs=[("sp", "DS10"), ("pv", "DS11")],
             outputs=[("out", "DS20")],
             return_to="DS30")
    assert from_dict(to_dict(c)) == c


def test_vendor_op_round_trips():
    v = VendorOp(vendor="click", name="DRUM",
                 operands=(10, "step1"),
                 attributes=(("preset", 1000),),
                 addresses=(Address("DS50"), Address("DS51")))
    assert from_dict(to_dict(v)) == v


def test_stdfunc_round_trips():
    op = limit(0, "speed", 100, "clamped")
    assert from_dict(to_dict(op)) == op


# -----------------------------------------------------------------------------
# User-defined types
# -----------------------------------------------------------------------------


def test_struct_type_round_trips():
    s = struct_type("Point", members=[
        var("x", TagType.INT),
        var("y", TagType.INT),
    ])
    assert from_dict(to_dict(s)) == s


def test_array_type_multidimensional_round_trips():
    """ArrayType.bounds is tuple[tuple[int, int], ...] -- nested tuples."""
    a = array_type("Mat3x3", element_type=TagType.REAL,
                   bounds=[(0, 2), (0, 2)])
    assert from_dict(to_dict(a)) == a


def test_enum_type_round_trips():
    e = enum_type("Color", values=["RED", "GREEN", "BLUE"])
    assert from_dict(to_dict(e)) == e


def test_alias_type_round_trips():
    al = alias_type("Distance", base=TagType.DINT)
    assert from_dict(to_dict(al)) == al


def test_subrange_type_round_trips():
    s = subrange_type("Percent", TagType.UINT, lower=0, upper=100)
    assert from_dict(to_dict(s)) == s


def test_named_type_round_trips():
    n = named_type("Point")
    assert from_dict(to_dict(n)) == n


def test_struct_with_namedtype_member_round_trips():
    """A struct whose member is a NamedType (forward reference to
    another UDT) round-trips correctly."""
    line = struct_type("Line", members=[
        var("start", named_type("Point")),
        var("end",   named_type("Point")),
    ])
    assert from_dict(to_dict(line)) == line


# -----------------------------------------------------------------------------
# Configuration model
# -----------------------------------------------------------------------------


def test_task_spec_round_trips():
    t = task_spec("Fast", priority=1, interval="T#10ms")
    assert from_dict(to_dict(t)) == t


def test_resource_round_trips():
    r = resource("CPU1",
                 tasks=[task_spec("Fast", priority=1, interval="T#10ms")],
                 pou_instances=[pou_instance("MainInst", "Main", task="Fast")],
                 global_vars=[var("counter", TagType.INT)])
    assert from_dict(to_dict(r)) == r


def test_configuration_with_resources_round_trips():
    from universal_machinery.builders import access_var
    cfg = configuration("Default",
                        resources=[
                            resource("CPU1",
                                     tasks=[task_spec("Fast", priority=1,
                                                      interval="T#10ms")]),
                        ],
                        global_vars=[var("sys_state", TagType.INT)],
                        access_vars=[access_var(
                            "hmi_tag",
                            "CPU1.Main.tag", TagType.INT)])
    assert from_dict(to_dict(cfg)) == cfg


# -----------------------------------------------------------------------------
# SFC
# -----------------------------------------------------------------------------


def test_sfc_network_round_trips():
    net = SfcNetwork(
        steps=[
            Step("Idle", initial=True,
                 actions=(Action(qualifier="N", target=Address("Y001")),)),
            Step("Run"),
        ],
        transitions=[
            Transition(from_steps=("Idle",), to_steps=("Run",)),
            Transition(from_steps=("Run",), to_steps=("Idle",)),
        ],
    )
    assert from_dict(to_dict(net)) == net


# -----------------------------------------------------------------------------
# Whole Program (the headline use case)
# -----------------------------------------------------------------------------


def test_empty_program_round_trips():
    assert from_dict(to_dict(Program())) == Program()


def test_realistic_full_program_round_trips_through_json_string():
    """End-to-end JSON string round-trip on a non-trivial Program
    that exercises tags, UDTs, multiple POUs, parameterized calls,
    Configuration, and various op types."""
    p = program(
        project_name="RoundTripDemo",
        comment="end-to-end round-trip test",
        tags=[
            tag_decl("estop", TagType.BOOL, "E-stop input", locked="X101"),
            tag_decl("speed_sp", TagType.INT, "speed setpoint"),
        ],
        user_types=[
            struct_type("Point", members=[
                var("x", TagType.INT), var("y", TagType.INT),
            ]),
            subrange_type("Percent", TagType.UINT, lower=0, upper=100),
            enum_type("State", values=["IDLE", "RUNNING", "DONE"]),
        ],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no("estop"), nc("reset"), coil("running")),
                rung(gt("speed_sp", 100),
                     call("Avg",
                          inputs=[("a", "DS10"), ("b", 5)],
                          return_to="DS20")),
            ]),
            fn("Avg",
               inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
               outputs=[var_out("r", TagType.INT)],
               return_type=TagType.INT,
               rungs=[rung(add(tag("a"), tag("b"), tag("r"))), rung(ret())]),
            fb("Counter",
               inputs=[var_in("clk", TagType.BOOL)],
               in_outs=[var_inout("count", TagType.INT)],
               outputs=[var_out("done", TagType.BOOL)],
               rungs=[rung(no(tag("clk")),
                           add(tag("count"), 1, tag("count")))]),
        ],
        configurations=[
            configuration("Default",
                          resources=[resource("CPU1",
                              tasks=[task_spec("Fast", priority=1,
                                               interval="T#10ms")],
                              pou_instances=[pou_instance("MainInst",
                                                          "Main",
                                                          task="Fast")])]),
        ],
    )
    p2 = from_json(to_json(p))
    assert p == p2


# -----------------------------------------------------------------------------
# Schema versioning + error paths
# -----------------------------------------------------------------------------


def test_top_level_program_has_schema_version():
    """The encoded form stamps a schema-version field on the root
    Program so the decoder can fail fast on version skew."""
    encoded = to_dict(Program())
    assert encoded["_schema"] == SCHEMA_VERSION


def test_unknown_schema_version_raises():
    encoded = to_dict(Program())
    encoded["_schema"] = 999
    with pytest.raises(SerialisationError, match="schema version"):
        from_dict(encoded)


def test_unknown_type_tag_raises():
    with pytest.raises(SerialisationError, match="unknown _type"):
        from_dict({"_type": "NonexistentClass", "foo": 1})


def test_to_json_produces_valid_json_string():
    """``to_json`` output parses with the standard library's json."""
    s = to_json(Program())
    parsed = json.loads(s)
    assert parsed["_type"] == "Program"


def test_to_json_sort_keys_is_deterministic():
    """sort_keys=True makes the output identical across runs for the
    same input -- important for diff-stability in CI."""
    p = program(
        tags=[
            tag_decl("z", TagType.INT),
            tag_decl("a", TagType.INT),
            tag_decl("m", TagType.INT),
        ],
        subroutines=[prog("Main", main=True)],
    )
    s1 = to_json(p, sort_keys=True)
    s2 = to_json(p, sort_keys=True)
    assert s1 == s2


# -----------------------------------------------------------------------------
# Tuple-vs-list discipline (regression: frozen-dataclass hashability)
# -----------------------------------------------------------------------------


def test_tuple_fields_remain_tuples_after_round_trip():
    """JSON has no tuple type, but frozen-dataclass equality + hashing
    depends on tuple fields staying tuples through the round trip."""
    op = call("F", inputs=[("a", "DS1")])
    op2 = from_dict(to_dict(op))
    assert isinstance(op2.inputs, tuple)
    # Two reconstructed Call ops with same input compare equal.
    op3 = from_dict(to_dict(op))
    assert op2 == op3
    assert hash(op2) == hash(op3)


def test_round_tripped_op_is_hashable():
    """Frozen-dataclass ops must remain hashable after reconstruction
    -- they're used in sets / dict keys by some passes."""
    op1 = from_dict(to_dict(no("X1")))
    op2 = from_dict(to_dict(no("X1")))
    s = {op1, op2}
    assert len(s) == 1
