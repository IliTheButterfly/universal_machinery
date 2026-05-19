"""Tests for the trampoline + resume-dispatch emitters (commit A).

The scheduled-call rewriter (commit B, still TODO) is what
populates the scheduler state these rungs consume.  Until it
lands, the trampoline is correct-but-inert: no POU body yields,
so the pop / resume paths never fire.  These tests verify the
emitted rung structure regardless.
"""
from universal_machinery.il import (
    Address, PouKind, Program, Rung, Subroutine, TagType, Var, VarDirection,
)
from universal_machinery.il.ops import (
    Call, Compare, ContactNC, Jump, Label, Move, OutReset,
)
from universal_machinery.lowering.click_calling import (
    LoweringConfig, LoweringError, allocate_slots, emit_dispatch_rungs,
    emit_pop_rungs, emit_resume_dispatch, emit_trampoline,
    prepend_trampoline_to_main,
)
import pytest


# -----------------------------------------------------------------------------
# Dispatch table
# -----------------------------------------------------------------------------


def test_dispatch_rungs_one_per_pou_in_alloc():
    """One Compare->Call rung per POU id assigned by the allocator,
    in declaration order."""
    a = Subroutine(name="A")
    b = Subroutine(name="B")
    c = Subroutine(name="C")
    alloc = allocate_slots(Program(subroutines=[a, b, c]))
    cfg = LoweringConfig()
    rungs = emit_dispatch_rungs(alloc, cfg)
    assert len(rungs) == 3
    # Each rung compares sched_next_id against the POU's id and calls it.
    assert rungs[0].ops == [
        Compare(op="==", lhs=Address("DS9800"), rhs="1"),
        Call(target="A"),
    ]
    assert rungs[1].ops == [
        Compare(op="==", lhs=Address("DS9800"), rhs="2"),
        Call(target="B"),
    ]
    assert rungs[2].ops == [
        Compare(op="==", lhs=Address("DS9800"), rhs="3"),
        Call(target="C"),
    ]


# -----------------------------------------------------------------------------
# Pop sequence
# -----------------------------------------------------------------------------


def test_pop_rungs_one_per_depth_level_plus_idle_plus_clear():
    """At stack_depth=N, pop sequence is N+2 rungs:
       N descending pop branches (sp=N -> N-1, ..., sp=1 -> 0)
       1 idle branch (sp=0 -> next_id=0)
       1 yield-flag clear rung
    """
    cfg = LoweringConfig(sched_stack_depth=8)
    rungs = emit_pop_rungs(cfg)
    assert len(rungs) == 8 + 2

    # First pop rung: sp == 8 -> read stack[7] and resume[7]
    assert rungs[0].ops == [
        ContactNC(Address("C1500")),                       # not yielded
        Compare(op="==", lhs=Address("DS9809"), rhs="8"),  # sp == 8
        Move(src=Address("DS9808"), dst=Address("DS9800")),  # stack[7] -> next_id
        Move(src=Address("DS9817"), dst=Address("DS9818")),  # resume[7] -> resume_id
        Move(src="7", dst=Address("DS9809")),               # sp = 7
    ]

    # Last numbered pop: sp == 1 -> read stack[0]
    assert rungs[7].ops == [
        ContactNC(Address("C1500")),
        Compare(op="==", lhs=Address("DS9809"), rhs="1"),
        Move(src=Address("DS9801"), dst=Address("DS9800")),  # stack[0]
        Move(src=Address("DS9810"), dst=Address("DS9818")),  # resume[0]
        Move(src="0", dst=Address("DS9809")),
    ]

    # Idle case (sp == 0)
    assert rungs[8].ops == [
        ContactNC(Address("C1500")),
        Compare(op="==", lhs=Address("DS9809"), rhs="0"),
        Move(src="0", dst=Address("DS9800")),
        Move(src="0", dst=Address("DS9818")),
    ]

    # Clear yield flag
    assert rungs[9].ops == [OutReset(Address("C1500"))]


def test_pop_rungs_scale_with_configurable_stack_depth():
    """Stack depth is configurable per project; rung count scales linearly."""
    cfg4 = LoweringConfig(sched_stack_depth=4)
    cfg16 = LoweringConfig(sched_stack_depth=16)
    assert len(emit_pop_rungs(cfg4)) == 4 + 2
    assert len(emit_pop_rungs(cfg16)) == 16 + 2


# -----------------------------------------------------------------------------
# Trampoline assembly
# -----------------------------------------------------------------------------


def test_trampoline_excludes_main_from_dispatch_table():
    """Main is the dispatcher; it should never appear as a callee
    in its own dispatch table."""
    main = Subroutine(name="Main", main=True, kind=PouKind.PROGRAM)
    a    = Subroutine(name="A")
    b    = Subroutine(name="B")
    prog = Program(subroutines=[main, a, b])
    alloc = allocate_slots(prog)
    rungs = emit_trampoline(prog, alloc)
    # Two dispatch rungs (A, B) + 8 pop rungs + idle + clear = 12
    cfg = LoweringConfig()
    assert len(rungs) == 2 + cfg.sched_stack_depth + 2
    dispatch_calls = [r.ops[-1] for r in rungs[:2]]
    assert dispatch_calls == [Call(target="A"), Call(target="B")]
    # No rung calls Main
    for r in rungs:
        for op in r.ops:
            if isinstance(op, Call):
                assert op.target != "Main"


def test_prepend_trampoline_keeps_main_body_intact():
    """The trampoline rungs are prepended to Main; Main's original
    body follows unchanged."""
    user_rung_a = Rung([Compare(op="==", lhs=Address("DS5"), rhs="0")])
    user_rung_b = Rung([Move(src="42", dst=Address("DS6"))])
    main = Subroutine(name="Main", main=True, kind=PouKind.PROGRAM,
                      rungs=[user_rung_a, user_rung_b])
    helper = Subroutine(name="Helper")
    prog = Program(subroutines=[main, helper])
    alloc = allocate_slots(prog)

    lowered = prepend_trampoline_to_main(prog, alloc)
    lowered_main = lowered.find_subroutine("Main")
    assert lowered_main is not None

    # Trampoline = 1 dispatch (Helper) + 8 pops + 1 idle + 1 clear = 11 rungs
    cfg = LoweringConfig()
    expected_tramp_len = 1 + cfg.sched_stack_depth + 2
    # User rungs follow unchanged at the tail
    assert lowered_main.rungs[expected_tramp_len:] == [user_rung_a, user_rung_b]


def test_prepend_trampoline_does_not_mutate_input_program():
    main = Subroutine(name="Main", main=True, rungs=[
        Rung([Move(src="1", dst=Address("DS1"))]),
    ])
    helper = Subroutine(name="Helper")
    prog = Program(subroutines=[main, helper])
    alloc = allocate_slots(prog)

    orig_main_rungs = list(prog.find_subroutine("Main").rungs)
    prepend_trampoline_to_main(prog, alloc)
    # Input program untouched
    assert prog.find_subroutine("Main").rungs == orig_main_rungs


def test_prepend_trampoline_raises_when_no_main():
    """A program with no main_subroutine has nothing to host the
    trampoline; raise rather than silently dropping it."""
    prog = Program(subroutines=[Subroutine(name="Sub1"),
                                 Subroutine(name="Sub2")])
    alloc = allocate_slots(prog)
    with pytest.raises(LoweringError, match="no main_subroutine"):
        prepend_trampoline_to_main(prog, alloc)


# -----------------------------------------------------------------------------
# Resume dispatch header
# -----------------------------------------------------------------------------


def test_resume_dispatch_empty_when_no_resume_sites():
    """A POU with no nested calls needs no resume dispatch header."""
    cfg = LoweringConfig()
    assert emit_resume_dispatch(0, cfg) == []


def test_resume_dispatch_single_site():
    cfg = LoweringConfig()
    rungs = emit_resume_dispatch(1, cfg)
    # 1 entry-jump + 1 resume-jump + 1 entry-label = 3 rungs
    assert len(rungs) == 3
    assert rungs[0].ops == [
        Compare(op="==", lhs=Address("DS9818"), rhs="0"),
        Jump(label="entry"),
    ]
    assert rungs[1].ops == [
        Compare(op="==", lhs=Address("DS9818"), rhs="1"),
        Jump(label="resume_1"),
    ]
    assert rungs[2].ops == [Label(name="entry")]


def test_resume_dispatch_multiple_sites_in_order():
    cfg = LoweringConfig()
    rungs = emit_resume_dispatch(3, cfg)
    # 1 entry + 3 resume + 1 label = 5 rungs
    assert len(rungs) == 5
    # Resume jumps in id order
    assert rungs[1].ops[-1] == Jump(label="resume_1")
    assert rungs[2].ops[-1] == Jump(label="resume_2")
    assert rungs[3].ops[-1] == Jump(label="resume_3")
    assert rungs[4].ops == [Label(name="entry")]


def test_resume_dispatch_label_names_customizable():
    cfg = LoweringConfig()
    rungs = emit_resume_dispatch(2, cfg,
                                 entry_label="START",
                                 resume_label_prefix="cont_")
    assert rungs[0].ops[-1] == Jump(label="START")
    assert rungs[1].ops[-1] == Jump(label="cont_1")
    assert rungs[2].ops[-1] == Jump(label="cont_2")
    assert rungs[3].ops == [Label(name="START")]


# -----------------------------------------------------------------------------
# Composition with the rest of the lowering pipeline
# -----------------------------------------------------------------------------


def test_trampoline_composes_after_lower_calls_and_lower_pou_bodies():
    """End-to-end shape check: a program goes through callee-body
    rewrite, caller-side marshalling, and trampoline prepending --
    each pass operates on the previous pass's output without
    interfering."""
    from universal_machinery.lowering.click_calling import (
        lower_calls, lower_pou_bodies,
    )
    from universal_machinery.il import TagRef

    f = Subroutine(
        name="F", kind=PouKind.FUNCTION,
        inputs=[Var("a", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("r", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("a"), dst=TagRef("r"))])],
    )
    main = Subroutine(name="Main", main=True, kind=PouKind.PROGRAM,
                      rungs=[Rung([
                          Call(target="F",
                               inputs=(("a", Address("DS10")),),
                               return_to=Address("DS20")),
                      ])])
    prog = Program(subroutines=[main, f])

    after_bodies, alloc = lower_pou_bodies(prog)
    after_calls, _ = lower_calls(after_bodies, alloc=alloc)
    final = prepend_trampoline_to_main(after_calls, alloc)

    # F's body had its TagRef("a") -> DS9000, TagRef("r") -> DS9100
    assert final.find_subroutine("F").rungs[0].ops == [
        Move(src=Address("DS9000"), dst=Address("DS9100")),
    ]

    # Main starts with the trampoline (dispatch for F + 8 pops + idle + clear
    # = 11 rungs), then the user's marshalled rung.
    cfg = LoweringConfig()
    expected_tramp_len = 1 + cfg.sched_stack_depth + 2
    final_main = final.find_subroutine("Main")
    assert len(final_main.rungs) == expected_tramp_len + 1
    user_rung = final_main.rungs[-1]
    assert user_rung.ops == [
        Move(src=Address("DS10"),   dst=Address("DS9000")),
        Call(target="F"),
        Move(src=Address("DS9100"), dst=Address("DS20")),
    ]
