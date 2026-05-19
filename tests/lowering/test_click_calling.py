"""Tests for the CLICK calling-convention lowering.

Two passes are exercised:

  - allocate_slots: bump-allocator for per-POU argument/return slot
    bases, FB-instance pointer slots, and scheduler POU ids.
  - marshal_call / lower_calls: caller-side expansion of a
    parameterized Call into [Move..., bare Call, Move...].
"""
import pytest

from universal_machinery.il import (
    Address, DataBlock, PouKind, Program, Rung, Subroutine, TagRef, TagType,
    Var, VarDirection,
)
from universal_machinery.il.ops import (
    Call, ContactNO, Move, OutCoil,
)
from universal_machinery.lowering.click_calling import (
    IDLE_POU_ID, LoweringConfig, LoweringError, allocate_slots, lower_calls,
    marshal_call,
)


# -----------------------------------------------------------------------------
# Slot allocator
# -----------------------------------------------------------------------------


def test_empty_program_allocates_nothing():
    alloc = allocate_slots(Program())
    assert alloc.per_pou == {}
    assert alloc.fb_instance_slot == {}
    assert alloc.pou_id == {}


def test_subroutine_kind_gets_id_but_no_slots():
    """Legacy SUBROUTINE POUs (no formal interface) get a scheduler
    id but no argument/return slot allocation."""
    sub = Subroutine(name="Legacy")
    prog = Program(subroutines=[sub])
    alloc = allocate_slots(prog)
    assert alloc.pou_id == {"Legacy": 1}
    assert "Legacy" not in alloc.per_pou
    assert alloc.fb_instance_slot == {}


def test_function_pou_slot_layout():
    fn = Subroutine(
        name="Avg", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT),
                Var("b", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("result", TagType.INT, VarDirection.OUTPUT)],
        return_type=TagType.INT,
    )
    alloc = allocate_slots(Program(subroutines=[fn]))
    slots = alloc.per_pou["Avg"]
    assert slots.slot_base == 0
    assert slots.arg_slot["a"]      == Address("DS9000")
    assert slots.arg_slot["b"]      == Address("DS9001")
    assert slots.ret_slot["result"] == Address("DS9100")
    assert slots.output_order == ("result",)


def test_two_pous_get_non_overlapping_slot_bases():
    f1 = Subroutine(
        name="F1", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT),
                Var("b", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
    )
    f2 = Subroutine(
        name="F2", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("y", TagType.INT, VarDirection.OUTPUT)],
    )
    alloc = allocate_slots(Program(subroutines=[f1, f2]))
    assert alloc.per_pou["F1"].slot_base == 0
    assert alloc.per_pou["F1"].width == 2          # max(2 inputs, 1 output)
    assert alloc.per_pou["F2"].slot_base == 2
    assert alloc.per_pou["F2"].arg_slot["x"] == Address("DS9002")
    assert alloc.per_pou["F2"].ret_slot["y"] == Address("DS9102")


def test_in_out_param_occupies_arg_and_ret_slots():
    sub = Subroutine(
        name="Tweak", kind=PouKind.FUNCTION_BLOCK,
        in_outs=[Var("level", TagType.INT, VarDirection.IN_OUT)],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    slots = alloc.per_pou["Tweak"]
    assert slots.arg_slot["level"] == Address("DS9000")
    assert slots.ret_slot["level"] == Address("DS9100")


def test_function_block_gets_instance_slot():
    pid  = Subroutine(name="PID",  kind=PouKind.FUNCTION_BLOCK)
    ramp = Subroutine(name="Ramp", kind=PouKind.FUNCTION_BLOCK)
    alloc = allocate_slots(Program(subroutines=[pid, ramp]))
    assert alloc.fb_instance_slot == {"PID": 0, "Ramp": 1}


def test_pou_ids_dense_and_skip_zero():
    """ID 0 is the scheduler's idle sentinel; assigned ids start at 1."""
    a = Subroutine(name="A")
    b = Subroutine(name="B")
    c = Subroutine(name="C")
    alloc = allocate_slots(Program(subroutines=[a, b, c]))
    assert IDLE_POU_ID == 0
    assert alloc.pou_id == {"A": 1, "B": 2, "C": 3}
    assert 0 not in alloc.pou_id.values()


def test_explicit_var_address_is_honored():
    """Vars with a manually-set address bypass auto-allocation and
    do not consume slot width."""
    sub = Subroutine(
        name="Manual", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT,
                    address=Address("DS5000"))],
        outputs=[Var("y", TagType.INT, VarDirection.OUTPUT)],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    slots = alloc.per_pou["Manual"]
    assert slots.arg_slot["x"] == Address("DS5000")
    assert slots.ret_slot["y"] == Address("DS9100")
    assert slots.width == 1                  # only y was auto-allocated


def test_lowering_config_overrides_bases():
    cfg = LoweringConfig(arg_base=Address("DS500"), ret_base=Address("DS600"))
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
    )
    alloc = allocate_slots(Program(subroutines=[sub]), cfg)
    assert alloc.per_pou["F"].arg_slot["a"] == Address("DS500")
    assert alloc.per_pou["F"].ret_slot["r"] == Address("DS600")


def test_address_arithmetic_handles_arbitrary_prefix():
    """C-bit base for POU-active flags is used by the scheduler;
    arithmetic over it must preserve the 'C' prefix."""
    cfg = LoweringConfig(arg_base=Address("C500"), ret_base=Address("C600"))
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.BOOL, VarDirection.INPUT),
                Var("b", TagType.BOOL, VarDirection.INPUT)],
    )
    alloc = allocate_slots(Program(subroutines=[sub]), cfg)
    assert alloc.per_pou["F"].arg_slot["a"] == Address("C500")
    assert alloc.per_pou["F"].arg_slot["b"] == Address("C501")


# -----------------------------------------------------------------------------
# Marshalling
# -----------------------------------------------------------------------------


def _avg_fn() -> Subroutine:
    """Reusable FUNCTION POU: result := avg(a, b)."""
    return Subroutine(
        name="Avg", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT),
                Var("b", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("result", TagType.INT, VarDirection.OUTPUT)],
        return_type=TagType.INT,
    )


def test_bare_call_passes_through_unchanged():
    """A Call with no inputs/outputs/instance/return_to is the legacy
    vendor-native form -- it bypasses the allocator entirely."""
    alloc = allocate_slots(Program())
    op = Call(target="Whatever")
    assert marshal_call(op, alloc) == [op]


def test_function_call_marshalled_to_move_call_move_returnto():
    prog = Program(subroutines=[_avg_fn()])
    alloc = allocate_slots(prog)
    call = Call(target="Avg",
                inputs=(("a", Address("DS10")), ("b", "5")),    # mix addr + literal
                return_to=Address("DS20"))
    expanded = marshal_call(call, alloc)
    assert expanded == [
        Move(src=Address("DS10"),   dst=Address("DS9000")),
        Move(src="5",               dst=Address("DS9001")),
        Call(target="Avg"),
        Move(src=Address("DS9100"), dst=Address("DS20")),
    ]


def test_function_block_call_marshals_inputs_then_instance_then_call_then_outputs():
    pid = Subroutine(
        name="PID", kind=PouKind.FUNCTION_BLOCK,
        inputs=[Var("sp", TagType.REAL, VarDirection.INPUT),
                Var("pv", TagType.REAL, VarDirection.INPUT)],
        outputs=[Var("out", TagType.REAL, VarDirection.OUTPUT)],
    )
    prog = Program(subroutines=[pid])
    alloc = allocate_slots(prog)
    call = Call(target="PID",
                instance=Address("DB100"),
                inputs=(("sp", Address("DS50")), ("pv", Address("DS51"))),
                outputs=(("out", Address("DS52")),))
    expanded = marshal_call(call, alloc)
    assert expanded == [
        Move(src=Address("DS50"),   dst=Address("DS9000")),     # input sp
        Move(src=Address("DS51"),   dst=Address("DS9001")),     # input pv
        Move(src=Address("DB100"), dst=Address("DS9200")),      # instance ptr
        Call(target="PID"),
        Move(src=Address("DS9100"), dst=Address("DS52")),       # output out
    ]


def test_in_out_param_marshals_before_and_after_call():
    """An IN_OUT param emits both an input-side Move (before CALL)
    and an output-side Move (after CALL) when the caller binds both
    sides."""
    sub = Subroutine(
        name="Twiddle", kind=PouKind.FUNCTION_BLOCK,
        in_outs=[Var("v", TagType.INT, VarDirection.IN_OUT)],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    call = Call(target="Twiddle",
                inputs=(("v", Address("DS10")),),
                outputs=(("v", Address("DS10")),))
    expanded = marshal_call(call, alloc)
    assert expanded == [
        Move(src=Address("DS10"),   dst=Address("DS9000")),
        Call(target="Twiddle"),
        Move(src=Address("DS9100"), dst=Address("DS10")),
    ]


def test_tagref_source_flows_through_to_move():
    """TagRefs as Call input sources are forwarded verbatim into the
    Move's src; a downstream tag resolver binds them later."""
    prog = Program(subroutines=[_avg_fn()])
    alloc = allocate_slots(prog)
    call = Call(target="Avg",
                inputs=(("a", TagRef("temperature")), ("b", Address("DS11"))))
    expanded = marshal_call(call, alloc)
    assert expanded == [
        Move(src=TagRef("temperature"), dst=Address("DS9000")),
        Move(src=Address("DS11"),       dst=Address("DS9001")),
        Call(target="Avg"),
    ]


def test_unknown_target_raises():
    alloc = allocate_slots(Program())
    with pytest.raises(LoweringError, match="unknown target"):
        marshal_call(
            Call(target="Ghost", inputs=(("a", Address("DS1")),)),
            alloc,
        )


def test_unknown_formal_param_raises():
    alloc = allocate_slots(Program(subroutines=[_avg_fn()]))
    with pytest.raises(LoweringError, match="VAR_INPUT/VAR_IN_OUT"):
        marshal_call(
            Call(target="Avg", inputs=(("typo", Address("DS1")),)),
            alloc,
        )


def test_instance_on_non_fb_target_raises():
    """Avg is FUNCTION; passing instance= is a typo / mistake."""
    alloc = allocate_slots(Program(subroutines=[_avg_fn()]))
    with pytest.raises(LoweringError, match="not a FUNCTION_BLOCK"):
        marshal_call(Call(target="Avg", instance=Address("DB1")), alloc)


def test_return_to_on_outputless_pou_raises():
    sub = Subroutine(
        name="Action", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT)],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    with pytest.raises(LoweringError, match="declares no VAR_OUTPUT"):
        marshal_call(
            Call(target="Action",
                 inputs=(("x", Address("DS1")),),
                 return_to=Address("DS2")),
            alloc,
        )


# -----------------------------------------------------------------------------
# Program-level pass
# -----------------------------------------------------------------------------


def test_lower_calls_preserves_contact_prefix():
    """Same-rung packing: the contact prefix gating the original Call
    must still gate every Move + the bare Call in the lowered rung."""
    avg = _avg_fn()
    main = Subroutine(
        name="Main", main=True, kind=PouKind.PROGRAM,
        rungs=[Rung([
            ContactNO(Address("X001")),
            Call(target="Avg",
                 inputs=(("a", Address("DS10")), ("b", Address("DS11"))),
                 return_to=Address("DS12")),
        ])],
    )
    prog = Program(subroutines=[main, avg])
    lowered, _ = lower_calls(prog)
    lowered_main = lowered.find_subroutine("Main")
    assert lowered_main is not None
    [rung] = lowered_main.rungs
    assert rung.ops == [
        ContactNO(Address("X001")),
        Move(src=Address("DS10"),   dst=Address("DS9000")),
        Move(src=Address("DS11"),   dst=Address("DS9001")),
        Call(target="Avg"),
        Move(src=Address("DS9100"), dst=Address("DS12")),
    ]


def test_lower_calls_returns_new_program_does_not_mutate_input():
    avg = _avg_fn()
    main = Subroutine(
        name="Main", main=True,
        rungs=[Rung([Call(target="Avg",
                          inputs=(("a", Address("DS10")),),
                          return_to=Address("DS20"))])],
    )
    prog = Program(subroutines=[main, avg])
    original_ops = list(prog.find_subroutine("Main").rungs[0].ops)
    lowered, _ = lower_calls(prog)
    # Input untouched
    assert prog.find_subroutine("Main").rungs[0].ops == original_ops
    # Lowered diverges and is a fresh instance
    assert prog is not lowered
    assert len(lowered.find_subroutine("Main").rungs[0].ops) > 1


def test_rungs_without_calls_pass_through_unchanged():
    rung_ops = [ContactNO(Address("X001")), OutCoil(Address("Y001"))]
    main = Subroutine(name="Main", main=True, rungs=[Rung(rung_ops)])
    prog = Program(subroutines=[main])
    lowered, _ = lower_calls(prog)
    assert lowered.find_subroutine("Main").rungs[0].ops == rung_ops


def test_multiple_calls_in_one_rung_each_expanded_in_order():
    """A rung carrying two parameterized Calls gets both expanded in
    place, preserving order."""
    f = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("y", TagType.INT, VarDirection.OUTPUT)],
    )
    main = Subroutine(
        name="Main", main=True,
        rungs=[Rung([
            ContactNO(Address("X001")),
            Call(target="F", inputs=(("x", Address("DS1")),),
                 return_to=Address("DS2")),
            Call(target="F", inputs=(("x", Address("DS3")),),
                 return_to=Address("DS4")),
        ])],
    )
    prog = Program(subroutines=[main, f])
    lowered, _ = lower_calls(prog)
    ops = lowered.find_subroutine("Main").rungs[0].ops
    assert ops[0] == ContactNO(Address("X001"))
    # First Call's expansion
    assert Move(src=Address("DS1"),   dst=Address("DS9000")) in ops
    assert Move(src=Address("DS9100"), dst=Address("DS2")) in ops
    # Second Call's expansion
    assert Move(src=Address("DS3"),   dst=Address("DS9000")) in ops
    assert Move(src=Address("DS9100"), dst=Address("DS4")) in ops
    # Both bare Calls survive
    bare = [op for op in ops if isinstance(op, Call)]
    assert bare == [Call(target="F"), Call(target="F")]


def test_lower_calls_accepts_precomputed_allocation():
    """Callers can pass in a SlotAllocation built earlier (e.g. so the
    same allocation drives multiple lowering passes)."""
    avg = _avg_fn()
    prog = Program(subroutines=[
        Subroutine(name="Main", main=True,
                   rungs=[Rung([Call(target="Avg",
                                     inputs=(("a", Address("DS10")),))])]),
        avg,
    ])
    alloc = allocate_slots(prog)
    lowered, returned_alloc = lower_calls(prog, alloc=alloc)
    assert returned_alloc is alloc
    ops = lowered.find_subroutine("Main").rungs[0].ops
    assert ops[0] == Move(src=Address("DS10"), dst=Address("DS9000"))
    assert ops[1] == Call(target="Avg")
