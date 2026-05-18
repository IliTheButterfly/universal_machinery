"""Tests for the callee-body rewriter.

Verifies that ``rewrite_callee_body`` substitutes ``TagRef`` references
to formal parameter names with their allocated slot ``Address``es,
direction-aware (read positions use arg_slot, write positions use
ret_slot), and that unrelated TagRefs (global tags, locals) pass
through unchanged.
"""
import pytest

from universal_machinery.il import (
    Address, PouKind, Program, Rung, Subroutine, TagRef, TagType, Var,
    VarDirection, VendorOp,
)
from universal_machinery.il.ops import (
    BinaryMath, Call, Compare, ContactNC, ContactNO, CTU, CTUD, Move, OutCoil,
    OutReset, OutSet, ParallelGroup, TON,
)
from universal_machinery.lowering.click_calling import (
    allocate_slots, lower_pou_bodies, rewrite_callee_body,
)


# -----------------------------------------------------------------------------
# Basics
# -----------------------------------------------------------------------------


def test_subroutine_without_interface_is_untouched():
    """SUBROUTINE-kind POUs have no slot allocation -- body returns as-is."""
    sub = Subroutine(name="Legacy", rungs=[
        Rung([ContactNO(TagRef("ghost")), OutCoil(Address("Y001"))]),
    ])
    alloc = allocate_slots(Program(subroutines=[sub]))
    rewritten = rewrite_callee_body(sub, alloc)
    assert rewritten == sub                  # frozen op list, structural eq


def test_unrelated_tagrefs_pass_through():
    """TagRefs whose names aren't formal parameters survive unchanged --
    they may reference global Program.tags."""
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([
            ContactNO(TagRef("global_btn")),     # not a formal param
            Move(src=TagRef("a"),                # IS a formal param (input)
                 dst=TagRef("r")),               # IS a formal param (output)
            OutCoil(TagRef("global_lamp")),      # not a formal param
        ])],
    )
    prog = Program(subroutines=[sub])
    alloc = allocate_slots(prog)
    rewritten = rewrite_callee_body(sub, alloc)
    [rung] = rewritten.rungs
    assert rung.ops == [
        ContactNO(TagRef("global_btn")),
        Move(src=Address("DS9000"), dst=Address("DS9100")),
        OutCoil(TagRef("global_lamp")),
    ]


# -----------------------------------------------------------------------------
# Direction awareness: IN_OUT params
# -----------------------------------------------------------------------------


def test_in_out_param_resolves_differently_in_read_vs_write_position():
    """A VAR_IN_OUT named "v" lives at arg_slot["v"] when read and at
    ret_slot["v"] when written.  Same TagRef("v") binds to different
    addresses depending on op position."""
    sub = Subroutine(
        name="Tweak", kind=PouKind.FUNCTION_BLOCK,
        in_outs=[Var("v", TagType.INT, VarDirection.IN_OUT)],
        rungs=[Rung([
            # Read "v" (lhs of compare) AND write "v" (dst of Move)
            Compare(op=">", lhs=TagRef("v"), rhs="100"),
            Move(src=TagRef("v"), dst=TagRef("v")),
            BinaryMath(op="+", lhs=TagRef("v"), rhs="1", dst=TagRef("v")),
        ])],
    )
    prog = Program(subroutines=[sub])
    alloc = allocate_slots(prog)
    rewritten = rewrite_callee_body(sub, alloc)
    [rung] = rewritten.rungs
    assert rung.ops == [
        # Compare reads both lhs and rhs -- "v" resolves to arg_slot
        Compare(op=">", lhs=Address("DS9000"), rhs="100"),
        # Move: src is read (arg_slot), dst is write (ret_slot)
        Move(src=Address("DS9000"), dst=Address("DS9100")),
        # BinaryMath: lhs/rhs are reads, dst is write
        BinaryMath(op="+", lhs=Address("DS9000"), rhs="1", dst=Address("DS9100")),
    ]


def test_input_only_param_skips_write_substitution():
    """A VAR_INPUT shouldn't appear in write positions; if a user does
    write to it (Move dst), the TagRef passes through unrewritten --
    the substitution doesn't blindly use arg_slot for write positions."""
    sub = Subroutine(
        name="ReadOnly", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        rungs=[Rung([
            Move(src=Address("DS5"), dst=TagRef("a")),    # writes to INPUT
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    # "a" isn't in ret_slot -- the dst TagRef survives
    assert rung.ops == [Move(src=Address("DS5"), dst=TagRef("a"))]


def test_output_only_param_skips_read_substitution():
    """A VAR_OUTPUT shouldn't appear in read positions; if a user does
    read from it (Move src), the TagRef passes through unrewritten."""
    sub = Subroutine(
        name="WriteOnly", kind=PouKind.FUNCTION,
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([
            Move(src=TagRef("r"), dst=Address("DS5")),    # reads from OUTPUT
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    assert rung.ops == [Move(src=TagRef("r"), dst=Address("DS5"))]


# -----------------------------------------------------------------------------
# Per-op coverage: contacts, coils, timers, counters, parallels, vendor ops
# -----------------------------------------------------------------------------


def test_contacts_resolve_in_read_position():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.BOOL, VarDirection.INPUT),
                Var("b", TagType.BOOL, VarDirection.INPUT)],
        rungs=[Rung([
            ContactNO(TagRef("a")),
            ContactNC(TagRef("b")),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    assert rung.ops == [
        ContactNO(Address("DS9000")),
        ContactNC(Address("DS9001")),
    ]


def test_coils_resolve_in_write_position():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        outputs=[Var("ok", TagType.BOOL, VarDirection.OUTPUT),
                 Var("err", TagType.BOOL, VarDirection.OUTPUT)],
        rungs=[Rung([
            OutCoil(TagRef("ok")),
            OutSet(TagRef("err")),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    assert rung.ops == [
        OutCoil(Address("DS9100")),
        OutSet(Address("DS9101")),
    ]


def test_timer_address_and_done_bit_resolve_as_writes():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        outputs=[Var("t", TagType.TIME, VarDirection.OUTPUT),
                 Var("done", TagType.BOOL, VarDirection.OUTPUT)],
        rungs=[Rung([
            TON(address=TagRef("t"), preset_ms=1000, done_bit=TagRef("done")),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    [ton] = rung.ops
    assert ton.address == Address("DS9100")
    assert ton.done_bit == Address("DS9101")


def test_ctu_reset_resolves_as_read():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("rst", TagType.BOOL, VarDirection.INPUT)],
        outputs=[Var("c", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([
            CTU(address=TagRef("c"), preset=100, reset=TagRef("rst")),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    [ctu] = rung.ops
    assert ctu.address == Address("DS9100")
    assert ctu.reset == Address("DS9000")


def test_ctud_directions():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION_BLOCK,
        inputs=[Var("up", TagType.BOOL, VarDirection.INPUT),
                Var("down", TagType.BOOL, VarDirection.INPUT)],
        outputs=[Var("at_max", TagType.BOOL, VarDirection.OUTPUT),
                 Var("at_min", TagType.BOOL, VarDirection.OUTPUT)],
        rungs=[Rung([
            CTUD(address=Address("CT0"), preset=100,
                 cu_input=TagRef("up"), cd_input=TagRef("down"),
                 qu=TagRef("at_max"), qd=TagRef("at_min")),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    [ctud] = rung.ops
    assert ctud.cu_input == Address("DS9000")
    assert ctud.cd_input == Address("DS9001")
    assert ctud.qu == Address("DS9100")
    assert ctud.qd == Address("DS9101")


def test_parallel_group_recurses_into_branches():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.BOOL, VarDirection.INPUT),
                Var("b", TagType.BOOL, VarDirection.INPUT)],
        outputs=[Var("ok", TagType.BOOL, VarDirection.OUTPUT)],
        rungs=[Rung([
            ParallelGroup(branches=(
                (ContactNO(TagRef("a")),),
                (ContactNO(TagRef("b")),),
            )),
            OutCoil(TagRef("ok")),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    pg = rung.ops[0]
    assert isinstance(pg, ParallelGroup)
    assert pg.branches == (
        (ContactNO(Address("DS9000")),),
        (ContactNO(Address("DS9001")),),
    )
    assert rung.ops[1] == OutCoil(Address("DS9100"))


def test_vendor_op_addresses_rewritten_best_effort():
    """VendorOp.addresses don't carry direction info; rewriter tries
    the read table first then the write table.  Sufficient for
    non-IN_OUT formal params (which appear in exactly one map)."""
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("in1", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("out1", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([
            VendorOp(vendor="click", name="DRUM",
                     addresses=(TagRef("in1"), TagRef("out1"))),
        ])],
    )
    alloc = allocate_slots(Program(subroutines=[sub]))
    [rung] = rewrite_callee_body(sub, alloc).rungs
    [vop] = rung.ops
    assert vop.addresses == (Address("DS9000"), Address("DS9100"))


# -----------------------------------------------------------------------------
# Nested Call inside a body
# -----------------------------------------------------------------------------


def test_nested_call_inside_body_rewrites_its_arg_bindings_against_current_pou():
    """When F's body contains Call(G, inputs=(("x", TagRef("a")),)),
    the TagRef("a") refers to F's formal parameter -- the rewriter
    substitutes it against F's arg_slot.  The Call op itself remains
    intact; the scheduled-call rewriter (separate pass) is what
    transforms the call into yield/resume form."""
    f = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([
            Call(target="G",
                 inputs=(("x", TagRef("a")),),
                 return_to=TagRef("r")),
        ])],
    )
    g = Subroutine(
        name="G", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("y", TagType.INT, VarDirection.OUTPUT)],
    )
    prog = Program(subroutines=[f, g])
    alloc = allocate_slots(prog)
    [rung] = rewrite_callee_body(f, alloc).rungs
    [call] = rung.ops
    assert isinstance(call, Call)
    assert call.target == "G"
    # F.inputs.a -> DS9000;  F.outputs.r -> DS9100
    assert call.inputs == (("x", Address("DS9000")),)
    assert call.return_to == Address("DS9100")


# -----------------------------------------------------------------------------
# Program-level pass
# -----------------------------------------------------------------------------


def test_lower_pou_bodies_walks_every_subroutine():
    f = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("a"), dst=TagRef("r"))])],
    )
    g = Subroutine(
        name="G", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("y", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("x"), dst=TagRef("y"))])],
    )
    prog = Program(subroutines=[f, g])
    lowered, alloc = lower_pou_bodies(prog)
    # F: slot_base=0;  G: slot_base=1 (each consumes one slot)
    assert lowered.find_subroutine("F").rungs[0].ops == [
        Move(src=Address("DS9000"), dst=Address("DS9100")),
    ]
    assert lowered.find_subroutine("G").rungs[0].ops == [
        Move(src=Address("DS9001"), dst=Address("DS9101")),
    ]


def test_lower_pou_bodies_idempotent_after_one_pass():
    """After one pass, all TagRefs to formal params are resolved; a
    second pass is a no-op (no rewrite happens because the remaining
    references aren't formal-param TagRefs)."""
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("a"), dst=TagRef("r"))])],
    )
    prog = Program(subroutines=[sub])
    once, alloc = lower_pou_bodies(prog)
    twice, _ = lower_pou_bodies(once, alloc=alloc)
    assert once.find_subroutine("F").rungs == twice.find_subroutine("F").rungs


def test_lower_pou_bodies_does_not_mutate_input():
    sub = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("a"), dst=TagRef("r"))])],
    )
    prog = Program(subroutines=[sub])
    original = list(prog.find_subroutine("F").rungs[0].ops)
    lower_pou_bodies(prog)
    assert prog.find_subroutine("F").rungs[0].ops == original


# -----------------------------------------------------------------------------
# Composition: callee-body rewrite + caller-side marshalling
# -----------------------------------------------------------------------------


def test_combined_with_lower_calls_produces_fully_address_program():
    """Run lower_pou_bodies (callee side) then lower_calls (caller
    side) on a program that authors both sides symbolically.  The
    output should have no TagRefs at parameter sites anywhere -- all
    formal-param references on both sides have become Addresses."""
    from universal_machinery.lowering.click_calling import lower_calls

    f = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("a"), dst=TagRef("r"))])],
    )
    main = Subroutine(
        name="Main", main=True, kind=PouKind.PROGRAM,
        rungs=[Rung([
            Call(target="F",
                 inputs=(("a", Address("DS10")),),
                 return_to=Address("DS20")),
        ])],
    )
    prog = Program(subroutines=[main, f])
    # Apply both passes -- order doesn't matter because they target
    # disjoint things (one rewrites callee bodies, the other rewrites
    # call sites).
    after_bodies, alloc = lower_pou_bodies(prog)
    after_calls, _ = lower_calls(after_bodies, alloc=alloc)

    f_body = after_calls.find_subroutine("F").rungs[0].ops
    assert f_body == [Move(src=Address("DS9000"), dst=Address("DS9100"))]

    main_body = after_calls.find_subroutine("Main").rungs[0].ops
    assert main_body == [
        Move(src=Address("DS10"),   dst=Address("DS9000")),
        Call(target="F"),
        Move(src=Address("DS9100"), dst=Address("DS20")),
    ]
