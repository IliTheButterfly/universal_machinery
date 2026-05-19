"""Tests for the parameterized-POU / DataBlock / SFC additions.

Exercises:
  - FUNCTION POU with VAR_INPUT / VAR_OUTPUT and a Call with arg bindings
  - FUNCTION_BLOCK POU + instance DataBlock + Call(..., instance=...)
  - DataBlock lookup helpers (find / fb_instances_of)
  - SfcNetwork structural validation
  - Program.referenced_addresses() walks Call bindings + SFC bodies
"""

from universal_machinery.il import (
    Action, Address, DataBlock, PouKind, Program, Rung, SfcNetwork,
    Step, Subroutine, TagType, Transition, Var, VarDirection,
)
from universal_machinery.il.ops import (
    Call, Compare, ContactNO, OutCoil, addresses_of,
)


# -----------------------------------------------------------------------------
# FUNCTION POU
# -----------------------------------------------------------------------------


def test_function_pou_with_arg_bindings():
    avg = Subroutine(
        name="Average",
        kind=PouKind.FUNCTION,
        return_type=TagType.INT,
        inputs=[
            Var("a", TagType.INT, VarDirection.INPUT),
            Var("b", TagType.INT, VarDirection.INPUT),
        ],
        outputs=[Var("result", TagType.INT, VarDirection.OUTPUT)],
    )
    main = Subroutine(
        name="Main", main=True,
        rungs=[Rung([Call(
            target="Average",
            inputs=(("a", Address("DS10")), ("b", Address("DS11"))),
            return_to=Address("DS12"),
        )])],
    )
    prog = Program(subroutines=[main, avg])

    # The Function POU shape is intact
    assert avg.kind is PouKind.FUNCTION
    assert avg.return_type is TagType.INT
    assert avg.find_var("a").direction is VarDirection.INPUT
    assert avg.find_var("result").direction is VarDirection.OUTPUT
    assert avg.find_var("zzz") is None

    # Call bindings flow through referenced_addresses
    refs = {a.raw for a in prog.referenced_addresses()}
    assert refs == {"DS10", "DS11", "DS12"}


# -----------------------------------------------------------------------------
# FUNCTION_BLOCK + instance DB
# -----------------------------------------------------------------------------


def test_function_block_with_instance_db():
    pid_fb = Subroutine(
        name="PID",
        kind=PouKind.FUNCTION_BLOCK,
        inputs=[
            Var("SP", TagType.REAL, VarDirection.INPUT),
            Var("PV", TagType.REAL, VarDirection.INPUT),
        ],
        outputs=[Var("OUT", TagType.REAL, VarDirection.OUTPUT)],
        local_vars=[
            Var("integral",    TagType.REAL),
            Var("last_error",  TagType.REAL),
        ],
    )
    pid_inst1 = DataBlock(
        name="PID_loop1",
        fb_template="PID",
        base_address=Address("DB100"),
        members=[
            Var("integral",   TagType.REAL),
            Var("last_error", TagType.REAL),
        ],
    )
    pid_inst2 = DataBlock(name="PID_loop2", fb_template="PID",
                          base_address=Address("DB110"))
    main = Subroutine(
        name="Main", main=True,
        rungs=[Rung([Call(
            target="PID",
            instance=Address("DB100"),
            inputs=(("SP", Address("DS50")), ("PV", Address("DS51"))),
            outputs=(("OUT", Address("DS52")),),
        )])],
    )
    prog = Program(subroutines=[main, pid_fb], data_blocks=[pid_inst1, pid_inst2])

    assert pid_fb.kind is PouKind.FUNCTION_BLOCK
    # DataBlock lookups
    assert prog.find_data_block("PID_loop1") is pid_inst1
    assert prog.find_data_block("nope") is None
    inst_names = [db.name for db in prog.fb_instances_of("PID")]
    assert inst_names == ["PID_loop1", "PID_loop2"]
    assert pid_inst1.find("integral").data_type is TagType.REAL

    # Instance address is collected
    refs = {a.raw for a in prog.referenced_addresses()}
    assert refs == {"DS50", "DS51", "DS52", "DB100"}


# -----------------------------------------------------------------------------
# Legacy unparameterized SUBROUTINE Call still works (back-compat)
# -----------------------------------------------------------------------------


def test_legacy_subroutine_call_unchanged():
    # `Call(target="X")` with no kwargs is the legacy form; addresses_of
    # must still return an empty set for it.
    op = Call(target="X")
    assert op.inputs == () and op.outputs == ()
    assert op.instance is None and op.return_to is None
    assert addresses_of(op) == set()


# -----------------------------------------------------------------------------
# SFC (grafcet)
# -----------------------------------------------------------------------------


def test_sfc_network_structure_and_validation():
    sfc = SfcNetwork(
        steps=[
            Step("Idle", initial=True,
                 actions=(Action(qualifier="N", target=Address("Y001")),)),
            Step("Filling",
                 actions=(Action(qualifier="N", target=Address("Y002")),)),
            Step("Done"),
        ],
        transitions=[
            Transition(
                from_steps=("Idle",), to_steps=("Filling",),
                condition=(ContactNO(Address("X001")),),
            ),
            Transition(
                from_steps=("Filling",), to_steps=("Done",),
                condition=(Compare(op=">=", lhs=Address("DS30"), rhs="100"),),
            ),
        ],
    )
    assert sfc.validate() == []
    assert sfc.step_names() == {"Idle", "Filling", "Done"}
    assert [s.name for s in sfc.initial_steps()] == ["Idle"]

    # Catch undeclared target + missing initial step
    broken = SfcNetwork(
        steps=[Step("A"), Step("A")],   # dup + no initial
        transitions=[Transition(from_steps=("A",), to_steps=("Ghost",))],
    )
    issues = broken.validate()
    assert any("duplicate step names" in i for i in issues)
    assert any("undeclared step 'Ghost'" in i for i in issues)
    assert any("no initial step" in i for i in issues)


def test_sfc_body_in_pou_contributes_addresses():
    """Program.referenced_addresses() must walk SFC transitions and
    coil-style actions, not just rung ops."""
    pou = Subroutine(
        name="Sequence",
        kind=PouKind.PROGRAM,
        main=True,
        sfc=SfcNetwork(
            steps=[
                Step("S0", initial=True,
                     actions=(Action(qualifier="N", target=Address("Y010")),)),
                Step("S1",
                     actions=(Action(qualifier="S", target=Address("Y011")),)),
            ],
            transitions=[
                Transition(
                    from_steps=("S0",), to_steps=("S1",),
                    condition=(
                        ContactNO(Address("X100")),
                        Compare(op="==", lhs=Address("DS40"), rhs="5"),
                    ),
                ),
            ],
        ),
    )
    prog = Program(subroutines=[pou])
    refs = {a.raw for a in prog.referenced_addresses()}
    assert refs == {"Y010", "Y011", "X100", "DS40"}


# -----------------------------------------------------------------------------
# Var dataclass
# -----------------------------------------------------------------------------


def test_var_defaults_and_explicit_address():
    v = Var(name="speed", data_type=TagType.INT,
            direction=VarDirection.INPUT, address=Address("DS9001"))
    assert v.direction is VarDirection.INPUT
    assert v.address == Address("DS9001")

    # frozen: cannot rebind
    import dataclasses
    try:
        v.name = "other"
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("Var must be frozen")
