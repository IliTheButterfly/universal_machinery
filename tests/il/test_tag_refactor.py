"""Tests for the Tag refactor + TagRef symbolic-reference support.

Exercises:
  - Tag declaration shape (name, data_type, description, optional address)
  - static vs dynamic tag classification (Program.static_tags / dynamic_tags)
  - Program.tags keyed by name; find_tag lookup
  - TagRef as an op operand wherever Address was accepted
  - addresses_of skips TagRef; tags_of returns TagRef names
  - Program.referenced_tags walks rung ops + SFC bodies
  - Mixed Address + TagRef in the same program
"""
from universal_machinery.il import (
    Action, Address, PouKind, Program, Rung, SfcNetwork, Step, Subroutine,
    Tag, TagRef, TagType, Transition,
)
from universal_machinery.il.ops import (
    Call, Compare, ContactNO, Move, OutCoil, addresses_of, tags_of,
)


# -----------------------------------------------------------------------------
# Tag dataclass shape
# -----------------------------------------------------------------------------


def test_tag_minimal_three_field_form():
    """Declaring just name + type + description -- address defaults None
    (dynamic; allocator picks)."""
    t = Tag(name="motor_speed", data_type=TagType.INT,
            description="commanded RPM")
    assert t.name == "motor_speed"
    assert t.data_type is TagType.INT
    assert t.description == "commanded RPM"
    assert t.address is None


def test_tag_locked_static_form():
    """Setting address pins the tag to that location (static)."""
    t = Tag(name="estop_btn", data_type=TagType.BOOL,
            description="hardwired E-stop input on slot 1",
            address=Address("X101"))
    assert t.address == Address("X101")


def test_static_vs_dynamic_classification():
    prog = Program(tags={
        "speed":     Tag(name="speed", data_type=TagType.INT),
        "estop":     Tag(name="estop", data_type=TagType.BOOL,
                         address=Address("X101")),
        "hmi_state": Tag(name="hmi_state", data_type=TagType.INT,
                         address=Address("DS200")),
        "scratch":   Tag(name="scratch", data_type=TagType.INT),
    })
    static_names = {t.name for t in prog.static_tags()}
    dynamic_names = {t.name for t in prog.dynamic_tags()}
    assert static_names == {"estop", "hmi_state"}
    assert dynamic_names == {"speed", "scratch"}


def test_program_tags_keyed_by_name():
    prog = Program(tags={
        "motor_speed": Tag(name="motor_speed", data_type=TagType.INT),
    })
    found = prog.find_tag("motor_speed")
    assert found is not None and found.data_type is TagType.INT
    assert prog.find_tag("ghost") is None


# -----------------------------------------------------------------------------
# TagRef in rung ops
# -----------------------------------------------------------------------------


def test_tagref_usable_anywhere_an_address_was():
    """Rung ops accept TagRef interchangeably with Address."""
    rung = Rung([
        ContactNO(TagRef("start_btn")),
        Move(src=TagRef("setpoint"), dst=TagRef("active_setpoint")),
        Compare(op=">", lhs=TagRef("temperature"), rhs="100"),
        OutCoil(TagRef("heater_relay")),
    ])
    # The dataclasses construct without complaint
    assert len(rung.ops) == 4


def test_addresses_of_skips_tagref():
    """addresses_of returns only concrete Addresses -- TagRefs are
    unresolved symbolic references."""
    op = Move(src=TagRef("setpoint"), dst=Address("DS20"))
    addrs = {a.raw for a in addresses_of(op)}
    assert addrs == {"DS20"}


def test_tags_of_returns_tagref_names():
    op = Move(src=TagRef("setpoint"), dst=Address("DS20"))
    assert tags_of(op) == {"setpoint"}


def test_mixed_address_and_tagref_in_one_op():
    op = Compare(op="==", lhs=TagRef("speed"), rhs=Address("DS5"))
    assert {a.raw for a in addresses_of(op)} == {"DS5"}
    assert tags_of(op) == {"speed"}


def test_parallel_group_recurses_for_tagrefs():
    from universal_machinery.il.ops import ParallelGroup
    pg = ParallelGroup(branches=(
        (ContactNO(TagRef("button_a")),),
        (ContactNO(Address("X002")), ContactNO(TagRef("button_b"))),
    ))
    assert tags_of(pg) == {"button_a", "button_b"}
    assert {a.raw for a in addresses_of(pg)} == {"X002"}


def test_call_inputs_outputs_accept_tagrefs():
    call = Call(
        target="PID",
        inputs=(("sp", TagRef("setpoint")), ("pv", Address("DS50"))),
        outputs=(("cv", TagRef("control_value")),),
        instance=TagRef("pid_loop_1"),
    )
    assert tags_of(call) == {"setpoint", "control_value", "pid_loop_1"}
    assert {a.raw for a in addresses_of(call)} == {"DS50"}


# -----------------------------------------------------------------------------
# Program-level walks
# -----------------------------------------------------------------------------


def test_program_referenced_tags_collects_from_rungs():
    prog = Program(
        subroutines=[
            Subroutine(name="Main", main=True, kind=PouKind.PROGRAM, rungs=[
                Rung([
                    ContactNO(TagRef("start")),
                    Move(src=TagRef("setpoint"), dst=Address("DS10")),
                    OutCoil(TagRef("running")),
                ]),
            ]),
        ],
        tags={
            "start":     Tag(name="start",     data_type=TagType.BOOL),
            "setpoint":  Tag(name="setpoint",  data_type=TagType.INT),
            "running":   Tag(name="running",   data_type=TagType.BOOL),
        },
    )
    assert prog.referenced_tags() == {"start", "setpoint", "running"}
    # Addresses walk still works
    assert {a.raw for a in prog.referenced_addresses()} == {"DS10"}


def test_program_referenced_tags_walks_sfc_bodies():
    """SFC transition guards + step actions that use TagRefs are
    walked too, mirroring referenced_addresses' SFC traversal."""
    prog = Program(
        subroutines=[
            Subroutine(
                name="Seq", main=True, kind=PouKind.PROGRAM,
                sfc=SfcNetwork(
                    steps=[
                        Step("Idle", initial=True,
                             actions=(Action(qualifier="N",
                                             target=TagRef("idle_lamp")),)),
                        Step("Run",
                             actions=(Action(qualifier="N",
                                             target=Address("Y010")),)),
                    ],
                    transitions=[
                        Transition(from_steps=("Idle",), to_steps=("Run",),
                                   condition=(ContactNO(TagRef("start")),)),
                    ],
                ),
            ),
        ],
    )
    assert prog.referenced_tags() == {"idle_lamp", "start"}
    # Action target Address still goes through referenced_addresses
    assert {a.raw for a in prog.referenced_addresses()} == {"Y010"}


def test_undeclared_tagref_visible_via_set_diff():
    """A backend's pre-emit check can detect unresolved TagRefs by
    diffing referenced_tags against declared tag names."""
    prog = Program(
        subroutines=[
            Subroutine(name="Main", main=True, rungs=[
                Rung([ContactNO(TagRef("declared")),
                      ContactNO(TagRef("missing"))]),
            ]),
        ],
        tags={"declared": Tag(name="declared", data_type=TagType.BOOL)},
    )
    referenced = prog.referenced_tags()
    declared = set(prog.tags)
    assert referenced - declared == {"missing"}


# -----------------------------------------------------------------------------
# Frozen / hash semantics survive the refactor
# -----------------------------------------------------------------------------


def test_tag_and_tagref_frozen_hashable():
    import dataclasses
    t = Tag(name="x", data_type=TagType.INT)
    r = TagRef("x")
    # Hashable
    assert hash(t) == hash(Tag(name="x", data_type=TagType.INT))
    assert hash(r) == hash(TagRef("x"))
    # Frozen
    try:
        r.name = "y"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("TagRef must be frozen")
