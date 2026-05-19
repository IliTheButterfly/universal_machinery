"""Tests for the IEC 61131-3 §3 Structured Text emitter."""
import pytest

from universal_machinery.builders import (
    abs_, add, and_, call, coil, eq, f_trig, fb, fn, gt, jump, label_, ge, le,
    limit, lt, mod, move, mul, nc, ne, no, or_, parallel, prog, program,
    r_trig, redge, fedge, reset_, ret, rs, rung, sel, set_, sqrt, sr,
    subroutine, sub, tag, tag_decl, ton, var, var_in, var_inout, var_out, xor_,
)
from universal_machinery.il import (
    Address, PouKind, Rung, TagRef, TagType,
)
from universal_machinery.emitters.st import (
    emit_program, emit_pou, emit_rung,
)


# -----------------------------------------------------------------------------
# Rung emission: gate + outputs
# -----------------------------------------------------------------------------


def test_simple_no_to_coil():
    r = rung(no("X1"), coil("Y1"))
    assert emit_rung(r) == ["Y1 := X1;"]


def test_no_and_nc_series_gate():
    """Series contacts AND together."""
    r = rung(no("X1"), nc("X2"), coil("Y1"))
    assert emit_rung(r) == ["Y1 := X1 AND NOT X2;"]


def test_unconditional_coil_is_TRUE_assignment():
    """A rung with no input prefix has gate=TRUE; OutCoil assigns TRUE."""
    r = rung(coil("Y1"))
    assert emit_rung(r) == ["Y1 := TRUE;"]


def test_set_coil_wraps_in_if_then():
    r = rung(no("X1"), set_("Y1"))
    assert emit_rung(r) == ["IF X1 THEN Y1 := TRUE; END_IF;"]


def test_reset_coil_wraps_in_if_then():
    r = rung(no("X1"), reset_("Y1"))
    assert emit_rung(r) == ["IF X1 THEN Y1 := FALSE; END_IF;"]


def test_unconditional_set_no_if_wrap():
    """No gate -> bare assignment (no IF wrap needed)."""
    r = rung(set_("Y1"))
    assert emit_rung(r) == ["Y1 := TRUE;"]


def test_compare_in_gate_becomes_boolean_subexpr():
    r = rung(gt("speed", 100), set_("over_limit"))
    assert emit_rung(r) == [
        "IF (speed > 100) THEN over_limit := TRUE; END_IF;",
    ]


def test_parallel_group_becomes_OR_expression():
    """A ParallelGroup of single-contact branches renders as a
    parenthesised OR of those contacts."""
    r = rung(no("X1"), parallel([no("X2")], [no("X3")]), coil("Y1"))
    assert emit_rung(r) == ["Y1 := X1 AND (X2 OR X3);"]


def test_parallel_group_with_multi_contact_branch():
    """Each branch is an AND of its contacts; the group ORs branches."""
    r = rung(
        parallel([no("X1"), nc("X2")], [no("X3")]),
        coil("Y1"),
    )
    assert emit_rung(r) == ["Y1 := (X1 AND NOT X2 OR X3);"]


# -----------------------------------------------------------------------------
# Move / Math / Compare outputs
# -----------------------------------------------------------------------------


def test_move_unconditional():
    r = rung(move("DS10", "DS20"))
    assert emit_rung(r) == ["DS20 := DS10;"]


def test_move_gated():
    r = rung(no("X1"), move("DS10", "DS20"))
    assert emit_rung(r) == ["IF X1 THEN DS20 := DS10; END_IF;"]


def test_binary_math_gated():
    r = rung(no("X1"), add("DS10", "DS11", "DS12"))
    assert emit_rung(r) == ["IF X1 THEN DS12 := DS10 + DS11; END_IF;"]


def test_move_with_tagref_and_literal():
    r = rung(move(42, tag("scratch")))
    assert emit_rung(r) == ["scratch := 42;"]


def test_compare_only_in_gate_no_output():
    """A rung that's pure gate (no outputs) emits no statements --
    the comparison is evaluated but its result drives nothing."""
    r = rung(gt("DS10", 5))
    assert emit_rung(r) == []


# -----------------------------------------------------------------------------
# Calls
# -----------------------------------------------------------------------------


def test_bare_call():
    r = rung(call("Sub1"))
    assert emit_rung(r) == ["Sub1();"]


def test_function_call_with_inputs_and_return_to():
    r = rung(call("Avg",
                  inputs=[("a", "DS10"), ("b", "DS11")],
                  return_to="DS20"))
    assert emit_rung(r) == [
        "DS20 := Avg(a := DS10, b := DS11);",
    ]


def test_function_block_call_with_instance_and_outputs():
    """FB calls use the instance variable as the invocation target."""
    r = rung(call("PID",
                  instance="PID_Loop1",
                  inputs=[("SP", "setpoint"), ("PV", "process_val")],
                  outputs=[("CV", "control_val")]))
    assert emit_rung(r) == [
        "PID_Loop1(SP := setpoint, PV := process_val, CV => control_val);",
    ]


def test_call_gated_by_contact():
    r = rung(no("X1"), call("Sub1"))
    assert emit_rung(r) == ["IF X1 THEN Sub1(); END_IF;"]


# -----------------------------------------------------------------------------
# Standard library functions
# -----------------------------------------------------------------------------


def test_std_func_emits_iec_call_form():
    r = rung(abs_("DS10", "DS11"))
    assert emit_rung(r) == ["DS11 := ABS(DS10);"]


def test_std_func_multi_input():
    r = rung(sel("X1", "DS10", "DS20", "DS30"))
    assert emit_rung(r) == ["DS30 := SEL(X1, DS10, DS20);"]


def test_std_func_with_literals():
    r = rung(limit(0, "speed", 1000, "speed_clamped"))
    assert emit_rung(r) == [
        "speed_clamped := LIMIT(0, speed, 1000);",
    ]


def test_std_func_gated():
    r = rung(no("enable"), sqrt("DS10", "DS11"))
    assert emit_rung(r) == [
        "IF enable THEN DS11 := SQRT(DS10); END_IF;",
    ]


# -----------------------------------------------------------------------------
# Control flow
# -----------------------------------------------------------------------------


def test_return_gated():
    r = rung(no("err"), ret())
    assert emit_rung(r) == ["IF err THEN RETURN; END_IF;"]


def test_unconditional_return():
    r = rung(ret())
    assert emit_rung(r) == ["RETURN;"]


def test_jump_and_label():
    r1 = rung(no("skip"), jump("loop_top"))
    r2 = rung(label_("loop_top"))
    assert emit_rung(r1) == ["IF skip THEN GOTO loop_top; END_IF;"]
    assert emit_rung(r2) == ["loop_top:"]


# -----------------------------------------------------------------------------
# Stateful FBs emit a placeholder comment (first cut; instance
# synthesis is a follow-up pass)
# -----------------------------------------------------------------------------


def test_ton_emits_placeholder_comment():
    r = rung(no("X1"), ton("T0", 1000))
    stmts = emit_rung(r)
    assert len(stmts) == 1
    assert "TON" in stmts[0]
    assert "T0" in stmts[0]
    assert "1000ms" in stmts[0]


def test_r_trig_emits_placeholder_comment():
    r = rung(r_trig("C100", "X001", "Y001"))
    stmts = emit_rung(r)
    assert len(stmts) == 1
    assert "R_TRIG" in stmts[0]
    assert "X001" in stmts[0]


def test_sr_emits_placeholder_comment():
    r = rung(sr("Y010", "X001", "X002"))
    stmts = emit_rung(r)
    assert len(stmts) == 1
    assert "SR" in stmts[0] and "Y010" in stmts[0]


# -----------------------------------------------------------------------------
# Comments
# -----------------------------------------------------------------------------


def test_rung_comment_emitted_as_ST_comment():
    r = Rung(ops=[no("X1"), coil("Y1")], comment="motor start logic")
    assert emit_rung(r) == [
        "(* motor start logic *)",
        "Y1 := X1;",
    ]


# -----------------------------------------------------------------------------
# POU emission: interface + body
# -----------------------------------------------------------------------------


def test_function_pou_emits_full_iec_form():
    f = fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("result", TagType.INT)],
           return_type=TagType.INT,
           rungs=[rung(add(tag("a"), tag("b"), tag("result")))])
    text = emit_pou(f)
    assert "FUNCTION Avg : INT" in text
    assert "VAR_INPUT" in text
    assert "a : INT;" in text
    assert "b : INT;" in text
    assert "VAR_OUTPUT" in text
    assert "result : INT;" in text
    assert "result := a + b;" in text
    assert text.endswith("END_FUNCTION")


def test_function_block_pou_emits_FB_keyword():
    f = fb("PID",
           inputs=[var_in("sp", TagType.REAL), var_in("pv", TagType.REAL)],
           outputs=[var_out("out", TagType.REAL)])
    text = emit_pou(f)
    assert text.startswith("FUNCTION_BLOCK PID")
    assert text.endswith("END_FUNCTION_BLOCK")


def test_program_pou_emits_PROGRAM_keyword():
    p = prog("Main", main=True, rungs=[rung(no("X1"), coil("Y1"))])
    text = emit_pou(p)
    assert text.startswith("PROGRAM Main")
    assert text.endswith("END_PROGRAM")


def test_subroutine_kind_falls_back_to_PROGRAM_for_main():
    s = subroutine("Legacy", main=True, rungs=[rung(coil("Y1"))])
    text = emit_pou(s)
    # SUBROUTINE has no IEC equivalent; main=True maps to PROGRAM
    assert text.startswith("PROGRAM Legacy")


def test_subroutine_kind_falls_back_to_FB_for_non_main():
    s = subroutine("Helper", rungs=[rung(coil("Y1"))])
    text = emit_pou(s)
    assert text.startswith("FUNCTION_BLOCK Helper")


def test_var_in_out_section_emitted():
    f = fb("Counter",
           in_outs=[var_inout("count", TagType.INT)],
           local_vars=[var("scratch", TagType.INT)])
    text = emit_pou(f)
    assert "VAR_IN_OUT" in text
    assert "count : INT;" in text
    assert "VAR" in text
    assert "scratch : INT;" in text


# -----------------------------------------------------------------------------
# Program emission: globals + POUs
# -----------------------------------------------------------------------------


def test_program_emits_var_global_for_tags():
    p = program(tags=[
        tag_decl("speed", TagType.INT, "commanded RPM"),
        tag_decl("estop", TagType.BOOL, "E-stop", locked="X101"),
    ])
    text = emit_program(p)
    assert "VAR_GLOBAL" in text
    assert "speed : INT;" in text
    assert "estop : BOOL" in text
    # Locked tag annotated with its physical address
    assert "X101" in text


def test_program_emits_pou_per_subroutine():
    p = program(subroutines=[
        prog("Main", main=True, rungs=[rung(no("X1"), coil("Y1"))]),
        fn("Avg",
           inputs=[var_in("a", TagType.INT), var_in("b", TagType.INT)],
           outputs=[var_out("r", TagType.INT)],
           return_type=TagType.INT,
           rungs=[rung(add(tag("a"), tag("b"), tag("r")))]),
    ])
    text = emit_program(p)
    assert "PROGRAM Main" in text
    assert "FUNCTION Avg : INT" in text


# -----------------------------------------------------------------------------
# User-defined type emission
# -----------------------------------------------------------------------------


def test_enum_type_emits_paren_list():
    from universal_machinery.builders import enum_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    e = enum_type("State", values=["IDLE", "RUNNING", "DONE"])
    text = _fmt_user_type_decl(e)
    assert "TYPE" in text
    assert "State : (IDLE, RUNNING, DONE);" in text
    assert "END_TYPE" in text


def test_struct_type_emits_struct_block():
    from universal_machinery.builders import struct_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    s = struct_type("Point", members=[
        var("x", TagType.INT),
        var("y", TagType.INT),
    ])
    text = _fmt_user_type_decl(s)
    assert "Point :" in text
    assert "STRUCT" in text
    assert "x : INT;" in text
    assert "y : INT;" in text
    assert "END_STRUCT;" in text
    assert "END_TYPE" in text


def test_struct_with_initial_values():
    from universal_machinery.builders import struct_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    s = struct_type("Config", members=[
        var("max_speed", TagType.INT,  initial="1000"),
        var("enabled",   TagType.BOOL, initial="TRUE"),
    ])
    text = _fmt_user_type_decl(s)
    assert "max_speed : INT := 1000;" in text
    assert "enabled : BOOL := TRUE;" in text


def test_array_type_emits_bounds_and_element_type():
    from universal_machinery.builders import array_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    a = array_type("Vector10", element_type=TagType.INT, bounds=[(0, 9)])
    text = _fmt_user_type_decl(a)
    assert "Vector10 : ARRAY [0..9] OF INT;" in text


def test_multidimensional_array_emits_comma_separated_bounds():
    from universal_machinery.builders import array_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    a = array_type("Matrix3x3", element_type=TagType.REAL,
                   bounds=[(0, 2), (0, 2)])
    text = _fmt_user_type_decl(a)
    assert "Matrix3x3 : ARRAY [0..2, 0..2] OF REAL;" in text


def test_array_of_user_type_renders_named_element():
    from universal_machinery.builders import array_type, named_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    a = array_type("PointBuf",
                   element_type=named_type("Point"),
                   bounds=[(0, 99)])
    text = _fmt_user_type_decl(a)
    assert "ARRAY [0..99] OF Point;" in text


def test_alias_of_elementary():
    from universal_machinery.builders import alias_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    d = alias_type("Distance", base=TagType.DINT)
    text = _fmt_user_type_decl(d)
    assert "Distance : DINT;" in text


def test_subrange_emits_iec_paren_range():
    from universal_machinery.builders import subrange_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    t = subrange_type("SmallInt", TagType.INT, lower=-100, upper=100)
    text = _fmt_user_type_decl(t)
    assert "SmallInt : INT (-100..100);" in text


def test_subrange_unsigned_emits_unsigned_base():
    from universal_machinery.builders import subrange_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    t = subrange_type("Percent", TagType.UINT, lower=0, upper=100)
    text = _fmt_user_type_decl(t)
    assert "Percent : UINT (0..100);" in text


def test_alias_of_user_type():
    from universal_machinery.builders import alias_type, named_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    d = alias_type("BigPoint", base=named_type("Point"))
    text = _fmt_user_type_decl(d)
    assert "BigPoint : Point;" in text


def test_struct_member_with_named_type_reference():
    from universal_machinery.builders import struct_type, named_type
    from universal_machinery.emitters.st import _fmt_user_type_decl
    line = struct_type("Line", members=[
        var("start", named_type("Point")),
        var("end",   named_type("Point")),
    ])
    text = _fmt_user_type_decl(line)
    assert "start : Point;" in text
    assert "end : Point;" in text


def test_emit_program_emits_user_types_before_pous():
    """User-defined types are declared first so subsequent VAR
    sections can reference them by name."""
    from universal_machinery.builders import struct_type, enum_type, named_type
    p = program(
        user_types=[
            enum_type("State", values=["IDLE", "RUNNING"]),
            struct_type("Point", members=[
                var("x", TagType.INT),
                var("y", TagType.INT),
            ]),
        ],
        subroutines=[prog("Main", main=True)],
    )
    text = emit_program(p)
    type_pos = text.index("TYPE")
    pou_pos  = text.index("PROGRAM Main")
    assert type_pos < pou_pos


def test_emit_program_with_no_user_types_omits_TYPE_block():
    """A program with no UDTs produces no TYPE...END_TYPE blocks."""
    p = program(subroutines=[prog("Main", main=True)])
    text = emit_program(p)
    # Should NOT have any "TYPE\n" header at the start of a section
    assert "\nTYPE\n" not in text
    assert not text.startswith("TYPE\n")


# -----------------------------------------------------------------------------
# CONFIGURATION / RESOURCE / TASK emission
# -----------------------------------------------------------------------------


def test_task_cyclic_emits_interval_and_priority():
    from universal_machinery.builders import task_spec
    from universal_machinery.emitters.st import _fmt_task
    t = task_spec("Fast", priority=1, interval="T#10ms")
    assert _fmt_task(t) == "        TASK Fast(INTERVAL := T#10ms, PRIORITY := 1);"


def test_task_single_shot_emits_single_attribute():
    from universal_machinery.builders import task_spec
    from universal_machinery.emitters.st import _fmt_task
    t = task_spec("OnEStop", priority=0, single="emergency_btn")
    assert "SINGLE := emergency_btn" in _fmt_task(t)
    assert "PRIORITY := 0" in _fmt_task(t)


def test_task_interrupt_driven_emits_interrupt_attribute():
    from universal_machinery.builders import task_spec
    from universal_machinery.emitters.st import _fmt_task
    t = task_spec("OnTimer", priority=2, interrupt="INT_TIMER0")
    assert "INTERRUPT := INT_TIMER0" in _fmt_task(t)


def test_pou_instance_with_task_uses_WITH_syntax():
    from universal_machinery.builders import pou_instance
    from universal_machinery.emitters.st import _fmt_pou_instance
    inst = pou_instance("MainProg", type_name="Main", task="Fast")
    assert _fmt_pou_instance(inst) == "        PROGRAM MainProg WITH Fast : Main;"


def test_pou_instance_without_task_omits_WITH():
    from universal_machinery.builders import pou_instance
    from universal_machinery.emitters.st import _fmt_pou_instance
    inst = pou_instance("Helper", type_name="Helper")
    assert _fmt_pou_instance(inst) == "        PROGRAM Helper : Helper;"


def test_resource_block_full_structure():
    from universal_machinery.builders import (
        resource, task_spec, pou_instance,
    )
    from universal_machinery.emitters.st import _fmt_resource
    r = resource(
        "CPU1",
        tasks=[task_spec("Fast", priority=1, interval="T#10ms")],
        pou_instances=[pou_instance("Main1", type_name="Main", task="Fast")],
        global_vars=[var("counter", TagType.INT)],
    )
    text = _fmt_resource(r)
    assert text.startswith("    RESOURCE CPU1 ON PLC")
    assert text.endswith("    END_RESOURCE")
    assert "VAR_GLOBAL" in text
    assert "counter : INT;" in text
    assert "END_VAR" in text
    assert "TASK Fast(INTERVAL := T#10ms, PRIORITY := 1);" in text
    assert "PROGRAM Main1 WITH Fast : Main;" in text


def test_configuration_block_full_structure():
    from universal_machinery.builders import configuration, resource, task_spec
    from universal_machinery.emitters.st import _fmt_configuration
    cfg = configuration(
        "Default",
        global_vars=[var("system_state", TagType.INT)],
        access_vars=[var("hmi_tag", TagType.INT)],
        resources=[
            resource("CPU1",
                     tasks=[task_spec("Fast", priority=1, interval="T#10ms")]),
        ],
    )
    text = _fmt_configuration(cfg)
    assert text.startswith("CONFIGURATION Default")
    assert text.endswith("END_CONFIGURATION")
    assert "VAR_GLOBAL" in text
    assert "system_state : INT;" in text
    assert "VAR_ACCESS" in text
    assert "hmi_tag : INT;" in text
    assert "RESOURCE CPU1 ON PLC" in text


def test_emit_program_emits_configurations_after_pous():
    """POU type declarations come first so the Configuration's
    PROGRAM bindings can reference them by name."""
    from universal_machinery.builders import (
        configuration, pou_instance, resource, task_spec,
    )
    p = program(
        subroutines=[prog("Main", main=True)],
        configurations=[configuration("Default", resources=[
            resource("CPU1",
                     tasks=[task_spec("Fast", priority=1, interval="T#10ms")],
                     pou_instances=[pou_instance("MainProg",
                                                 type_name="Main",
                                                 task="Fast")]),
        ])],
    )
    text = emit_program(p)
    main_pos = text.index("PROGRAM Main")
    cfg_pos  = text.index("CONFIGURATION Default")
    assert main_pos < cfg_pos


def test_emit_program_with_no_configurations_omits_block():
    p = program(subroutines=[prog("Main", main=True)])
    text = emit_program(p)
    assert "CONFIGURATION" not in text
    assert "END_CONFIGURATION" not in text


def test_realistic_full_program_round_trips_to_ST():
    """End-to-end shape check: a non-trivial program emits as
    well-formed ST.  We don't try to parse it back (no ST parser
    yet), but we verify every expected section is present."""
    p = program(
        cpu_model="C2-01CPU",
        tags=[
            tag_decl("start_btn", TagType.BOOL, "start button",
                     locked="X101"),
            tag_decl("running",   TagType.BOOL, "running indicator"),
            tag_decl("speed_sp",  TagType.INT,  "speed setpoint"),
        ],
        subroutines=[
            prog("Main", main=True, rungs=[
                rung(no("start_btn"), set_("running")),
                rung(no("running"),
                     call("ClampSpeed",
                          inputs=[("sp", "speed_sp")],
                          return_to="speed_cmd")),
            ]),
            fn("ClampSpeed",
               inputs=[var_in("sp", TagType.INT)],
               outputs=[var_out("clamped", TagType.INT)],
               return_type=TagType.INT,
               rungs=[
                   rung(limit(0, tag("sp"), 1000, tag("clamped"))),
                   rung(ret()),
               ]),
        ],
    )

    text = emit_program(p)
    # Tags exported as VAR_GLOBAL
    assert "VAR_GLOBAL" in text
    assert "start_btn : BOOL" in text
    assert "X101" in text                              # locked address annotation
    # Main: PROGRAM with the two rungs
    assert "PROGRAM Main" in text
    assert "running := TRUE" in text                   # set_ via IF wrap
    assert "speed_cmd := ClampSpeed(sp := speed_sp);"  in text
    # ClampSpeed: FUNCTION with LIMIT call
    assert "FUNCTION ClampSpeed : INT" in text
    assert "clamped := LIMIT(0, sp, 1000);" in text
    assert "RETURN;" in text
    assert text.endswith("END_FUNCTION\n")
