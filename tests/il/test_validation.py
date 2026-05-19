"""Tests for the pure-IL validation pass.

Each check has dedicated cases for the clean path (no errors) and
the failure path (specific ``ValidationError.code`` raised with
location info).
"""
import pytest

from universal_machinery.builders import (
    add, alias_type, array_type, call, coil, configuration, enum_type, fb,
    fn, move, named_type, no, pou_instance, prog, program, resource, ret,
    rung, struct_type, tag, tag_decl, task_spec, var, var_in, var_inout,
    var_out,
)
from universal_machinery.il import (
    Action, Address, SfcNetwork, Step, TagType, Transition,
)
from universal_machinery.il.ops import ContactNO
from universal_machinery.validation import (
    ValidationError, is_valid, validate,
)


# -----------------------------------------------------------------------------
# Clean programs return zero errors
# -----------------------------------------------------------------------------


def test_empty_program_validates():
    assert validate(program()) == []
    assert is_valid(program())


def test_simple_well_formed_program():
    p = program(
        tags=[tag_decl("start_btn", TagType.BOOL, "")],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no(tag("start_btn")), coil("Y1")),
            ]),
        ],
    )
    assert validate(p) == []


# -----------------------------------------------------------------------------
# Unresolved TagRefs
# -----------------------------------------------------------------------------


def test_unresolved_tagref_in_rung_op():
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no(tag("undeclared_btn")), coil("Y1")),
            ]),
        ],
    )
    errs = validate(p)
    codes = [e.code for e in errs]
    assert "unresolved-tagref" in codes
    msg = next(e.message for e in errs if e.code == "unresolved-tagref")
    assert "undeclared_btn" in msg


def test_tagref_matching_formal_parameter_is_valid():
    """TagRef('a') inside a POU whose VAR_INPUT is named 'a' is a
    formal-parameter reference, not a global tag lookup -- valid."""
    p = program(
        subroutines=[
            fn("Avg",
               inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
               outputs=[var_out("r", TagType.INT)],
               return_type=TagType.INT,
               rungs=[rung(add(tag("a"), tag("b"), tag("r")))]),
        ],
    )
    errs = validate(p)
    assert [e for e in errs if e.code == "unresolved-tagref"] == []


def test_struct_field_access_validates_base_name():
    """TagRef('axis.position') is a struct field access -- only the
    base name 'axis' needs to resolve."""
    p = program(
        tags=[tag_decl("axis", TagType.INT)],  # type irrelevant for this check
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no(tag("axis.position")), coil("Y1")),
            ]),
        ],
    )
    errs = [e for e in validate(p) if e.code == "unresolved-tagref"]
    assert errs == []


def test_array_element_access_validates_base_name():
    p = program(
        tags=[tag_decl("buf", TagType.INT)],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no(tag("buf[3]")), coil("Y1")),
            ]),
        ],
    )
    errs = [e for e in validate(p) if e.code == "unresolved-tagref"]
    assert errs == []


def test_unresolved_tagref_in_sfc_transition():
    """SFC transition conditions are walked too."""
    p = program(
        subroutines=[
            prog("Seq", main=True, sfc=SfcNetwork(
                steps=[Step("Idle", initial=True)],
                transitions=[Transition(
                    from_steps=("Idle",), to_steps=("Idle",),
                    condition=(ContactNO(tag("ghost_btn")),),
                )],
            )),
        ],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "unresolved-tagref"]
    assert len(bad) == 1
    assert "SFC transition" in bad[0].location


# -----------------------------------------------------------------------------
# Unresolved NamedType
# -----------------------------------------------------------------------------


def test_unresolved_named_type_in_var_declaration():
    p = program(
        subroutines=[
            prog("Main", main=True,
                 local_vars=[var("p", named_type("Ghost"))]),
        ],
    )
    errs = validate(p)
    codes = [e.code for e in errs]
    assert "unresolved-named-type" in codes


def test_named_type_declared_validates_cleanly():
    p = program(
        user_types=[struct_type("Point", members=[var("x", TagType.INT)])],
        subroutines=[
            prog("Main", main=True,
                 local_vars=[var("p", named_type("Point"))]),
        ],
    )
    assert [e for e in validate(p)
            if e.code == "unresolved-named-type"] == []


def test_unresolved_named_type_in_struct_member():
    """A struct member referencing an undeclared UDT is flagged."""
    p = program(
        user_types=[
            struct_type("Line", members=[
                var("start", named_type("Point")),  # Point not declared
            ]),
        ],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "unresolved-named-type"]
    assert len(bad) == 1
    assert "Point" in bad[0].message


def test_unresolved_named_type_in_array_element_type():
    p = program(
        user_types=[
            array_type("Buf", element_type=named_type("Ghost"),
                       bounds=[(0, 9)]),
        ],
    )
    errs = [e for e in validate(p) if e.code == "unresolved-named-type"]
    assert len(errs) == 1


def test_unresolved_named_type_in_alias_base():
    p = program(
        user_types=[alias_type("D", base=named_type("Ghost"))],
    )
    errs = [e for e in validate(p) if e.code == "unresolved-named-type"]
    assert len(errs) == 1


# -----------------------------------------------------------------------------
# Unknown Call targets
# -----------------------------------------------------------------------------


def test_parameterized_call_to_unknown_target():
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(call("Ghost",
                          inputs=[("a", "DS1")],
                          return_to="DS2")),
            ]),
        ],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "unknown-call-target"]
    assert len(bad) == 1
    assert "Ghost" in bad[0].message


def test_bare_call_to_unknown_target_is_not_flagged():
    """Bare unparameterised Calls may target hand-authored vendor
    subroutines -- we only flag parameterised ones."""
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[rung(call("VendorSub"))]),
        ],
    )
    errs = [e for e in validate(p) if e.code == "unknown-call-target"]
    assert errs == []


# -----------------------------------------------------------------------------
# Call parameter-binding mismatches
# -----------------------------------------------------------------------------


def test_call_with_unknown_input_name():
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(call("Avg",
                          inputs=[("typo_a", "DS1")],
                          return_to="DS2")),
            ]),
            fn("Avg",
               inputs=[var_in("a", TagType.INT)],
               outputs=[var_out("r", TagType.INT)],
               return_type=TagType.INT),
        ],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "bad-input-binding"]
    assert len(bad) == 1
    assert "typo_a" in bad[0].message


def test_call_with_unknown_output_name():
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(call("PID",
                          instance="DB1",
                          outputs=[("typo_out", "DS2")])),
            ]),
            fb("PID",
               inputs=[var_in("sp", TagType.REAL)],
               outputs=[var_out("out", TagType.REAL)]),
        ],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "bad-output-binding"]
    assert len(bad) == 1


def test_call_return_to_with_no_outputs():
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(call("Action",
                          inputs=[("x", "DS1")],
                          return_to="DS2")),
            ]),
            fn("Action",
               inputs=[var_in("x", TagType.INT)]),
        ],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "return-to-no-outputs"]
    assert len(bad) == 1


def test_valid_call_parameter_bindings_pass():
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(call("Avg",
                          inputs=[("a", "DS1"), ("b", "DS2")],
                          return_to="DS3")),
            ]),
            fn("Avg",
               inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
               outputs=[var_out("r", TagType.INT)],
               return_type=TagType.INT),
        ],
    )
    bad = [e for e in validate(p)
           if e.code in ("bad-input-binding", "bad-output-binding",
                         "return-to-no-outputs")]
    assert bad == []


# -----------------------------------------------------------------------------
# Call-graph cycles
# -----------------------------------------------------------------------------


def test_self_recursion_detected():
    """A POU calling itself is a cycle of length 1."""
    p = program(subroutines=[
        prog("Self", rungs=[rung(call("Self"))]),
    ])
    errs = validate(p)
    bad = [e for e in errs if e.code == "call-graph-cycle"]
    assert len(bad) == 1
    assert "Self" in bad[0].message


def test_two_pou_cycle_detected():
    p = program(subroutines=[
        prog("A", rungs=[rung(call("B"))]),
        prog("B", rungs=[rung(call("A"))]),
    ])
    errs = validate(p)
    bad = [e for e in errs if e.code == "call-graph-cycle"]
    assert len(bad) == 1


def test_acyclic_call_graph_passes():
    p = program(subroutines=[
        prog("Main", main=True, rungs=[rung(call("A"))]),
        prog("A", rungs=[rung(call("B"))]),
        prog("B", rungs=[rung(call("C"))]),
        prog("C", rungs=[rung(coil("Y1"))]),
    ])
    bad = [e for e in validate(p) if e.code == "call-graph-cycle"]
    assert bad == []


# -----------------------------------------------------------------------------
# PouInstance task / type references
# -----------------------------------------------------------------------------


def test_pou_instance_with_unknown_task():
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                tasks=[task_spec("Fast", priority=1, interval="T#10ms")],
                pou_instances=[pou_instance("MainInst",
                                            type_name="Main",
                                            task="Ghost")])])],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "unknown-task"]
    assert len(bad) == 1
    assert "Ghost" in bad[0].message


def test_pou_instance_with_unknown_type():
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                pou_instances=[pou_instance("GhostInst",
                                            type_name="GhostPou")])])],
    )
    errs = validate(p)
    bad = [e for e in errs if e.code == "unknown-pou-type"]
    assert len(bad) == 1
    assert "GhostPou" in bad[0].message


def test_pou_instance_without_task_is_valid():
    """Unbound POU instance (no task binding) -- valid (runs in the
    resource's default slot)."""
    p = program(
        subroutines=[prog("Helper", main=False)],
        configurations=[configuration("Default",
            resources=[resource("CPU1",
                pou_instances=[pou_instance("Helper1",
                                            type_name="Helper")])])],
    )
    bad = [e for e in validate(p)
           if e.code in ("unknown-task", "unknown-pou-type")]
    assert bad == []


# -----------------------------------------------------------------------------
# SFC well-formedness
# -----------------------------------------------------------------------------


def test_sfc_missing_initial_step_flagged():
    p = program(subroutines=[
        prog("Seq", main=True, sfc=SfcNetwork(
            steps=[Step("A"), Step("B")],  # no initial!
            transitions=[Transition(from_steps=("A",), to_steps=("B",))],
        )),
    ])
    errs = validate(p)
    bad = [e for e in errs if e.code == "sfc-issue"]
    assert any("initial" in e.message for e in bad)


def test_sfc_undeclared_step_flagged():
    p = program(subroutines=[
        prog("Seq", main=True, sfc=SfcNetwork(
            steps=[Step("A", initial=True)],
            transitions=[Transition(from_steps=("A",), to_steps=("Ghost",))],
        )),
    ])
    errs = validate(p)
    bad = [e for e in errs if e.code == "sfc-issue"]
    assert any("Ghost" in e.message for e in bad)


def test_well_formed_sfc_passes():
    p = program(subroutines=[
        prog("Seq", main=True, sfc=SfcNetwork(
            steps=[Step("Idle", initial=True), Step("Run")],
            transitions=[
                Transition(from_steps=("Idle",), to_steps=("Run",)),
                Transition(from_steps=("Run",), to_steps=("Idle",)),
            ],
        )),
    ])
    bad = [e for e in validate(p) if e.code == "sfc-issue"]
    assert bad == []


# -----------------------------------------------------------------------------
# Composition: a program with multiple problems at once
# -----------------------------------------------------------------------------


def test_multiple_problems_aggregated():
    """validate() collects all issues; it doesn't stop at the first."""
    p = program(
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no(tag("undeclared")), coil("Y1")),         # bad-tagref
                rung(call("Ghost",
                          inputs=[("typo", "DS1")])),            # bad-target
            ]),
        ],
    )
    errs = validate(p)
    codes = {e.code for e in errs}
    assert "unresolved-tagref" in codes
    assert "unknown-call-target" in codes


def test_is_valid_convenience_wrapper():
    good = program(subroutines=[prog("Main", main=True)])
    bad = program(subroutines=[
        prog("Self", rungs=[rung(call("Self"))]),
    ])
    assert is_valid(good) is True
    assert is_valid(bad) is False


# -----------------------------------------------------------------------------
# ValidationError shape
# -----------------------------------------------------------------------------


def test_validation_error_has_code_message_location():
    p = program(subroutines=[
        prog("Main", main=True, rungs=[
            rung(no(tag("undeclared")), coil("Y1")),
        ]),
    ])
    err = validate(p)[0]
    assert err.code == "unresolved-tagref"
    assert err.message
    assert "Main" in err.location
    assert "rung 0" in err.location
