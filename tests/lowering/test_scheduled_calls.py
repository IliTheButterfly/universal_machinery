"""Tests for the scheduled-call rewriter (commit B).

The rewriter transforms bare CLICK Call ops inside non-Main POU
bodies into the cooperative-yield sequence the trampoline (commit
A) drives.  Main's Calls to non-leaf POUs are rewritten to a
scheduler-dispatch sequence so the trampoline picks them up.
"""
from universal_machinery.il import (
    Address, PouKind, Program, Rung, Subroutine, TagType, Var, VarDirection,
)
from universal_machinery.il.ops import (
    BinaryMath, Call, Compare, ContactNO, Jump, Label, Move, OutCoil, OutSet,
    Return,
)
from universal_machinery.lowering.click_calling import (
    LoweringConfig, _classify_leaves, _emit_yield_rungs, _gate_prefix,
    allocate_slots, lower_calls, lower_pou_bodies, lower_scheduled_calls,
    prepend_trampoline_to_main,
)


# -----------------------------------------------------------------------------
# Leaf classification
# -----------------------------------------------------------------------------


def test_classify_leaves_empty_body_is_leaf():
    """A POU with no Calls anywhere in its body is a leaf."""
    a = Subroutine(name="A", rungs=[Rung([OutCoil(Address("Y001"))])])
    prog = Program(subroutines=[a])
    assert _classify_leaves(prog) == {"A"}


def test_classify_leaves_pou_with_bare_call_is_not_leaf():
    """A POU body containing any bare Call is a non-leaf."""
    a = Subroutine(name="A", rungs=[Rung([Call(target="B")])])
    b = Subroutine(name="B", rungs=[Rung([OutCoil(Address("Y001"))])])
    prog = Program(subroutines=[a, b])
    assert _classify_leaves(prog) == {"B"}     # B has no Calls; A does


def test_classify_leaves_all_leaves_when_no_calls_anywhere():
    a = Subroutine(name="A")
    b = Subroutine(name="B")
    prog = Program(subroutines=[a, b])
    assert _classify_leaves(prog) == {"A", "B"}


# -----------------------------------------------------------------------------
# _gate_prefix helper
# -----------------------------------------------------------------------------


def test_gate_prefix_extracts_contact_compare_run():
    ops = [
        ContactNO(Address("X001")),
        Compare(op=">", lhs=Address("DS5"), rhs="0"),
        Move(src="1", dst=Address("DS6")),         # not gate
        OutCoil(Address("Y001")),                  # not gate
    ]
    assert _gate_prefix(ops) == ops[:2]


def test_gate_prefix_stops_at_first_non_input_op():
    ops = [
        ContactNO(Address("X001")),
        Move(src="0", dst=Address("DS1")),
        ContactNO(Address("X002")),                # mixed in -- not picked up
    ]
    assert _gate_prefix(ops) == ops[:1]


def test_gate_prefix_empty_when_rung_starts_with_output():
    ops = [Move(src="1", dst=Address("DS1"))]
    assert _gate_prefix(ops) == []


# -----------------------------------------------------------------------------
# Yield-rung emitter
# -----------------------------------------------------------------------------


def test_yield_rungs_count_equals_stack_depth():
    cfg = LoweringConfig(sched_stack_depth=4)
    rungs = _emit_yield_rungs(
        gate=[ContactNO(Address("X001"))],
        caller_id=2, resume_id=1, callee_id=3,
        marshal_in=[],
        config=cfg,
    )
    # No marshal_in, so just the depth-many push rungs
    assert len(rungs) == 4


def test_yield_rungs_with_marshal_in_prepends_marshal_rung():
    cfg = LoweringConfig(sched_stack_depth=4)
    marshal = [Move(src=Address("DS10"), dst=Address("DS9000"))]
    rungs = _emit_yield_rungs(
        gate=[ContactNO(Address("X001"))],
        caller_id=2, resume_id=1, callee_id=3,
        marshal_in=marshal,
        config=cfg,
    )
    # 1 marshal rung + 4 push rungs
    assert len(rungs) == 5
    assert rungs[0].ops == [ContactNO(Address("X001"))] + marshal


def test_yield_rungs_push_sequence_shape():
    """Each push rung tests sp==k, writes caller_id+resume_id at the
    matching stack slot, increments sp, sets next_id+yield flag, returns."""
    cfg = LoweringConfig(sched_stack_depth=4)
    rungs = _emit_yield_rungs(
        gate=[ContactNO(Address("X001"))],
        caller_id=2, resume_id=1, callee_id=3,
        marshal_in=[],
        config=cfg,
    )
    # First push rung: sp==0, push at stack_base+0
    assert rungs[0].ops == [
        ContactNO(Address("X001")),                          # gate
        Compare(op="==", lhs=Address("DS9809"), rhs="0"),    # sp == 0
        Move(src="2", dst=Address("DS9801")),                # caller_id -> stack[0]
        Move(src="1", dst=Address("DS9810")),                # resume_id -> resume[0]
        Move(src="1", dst=Address("DS9809")),                # sp = 1
        Move(src="3", dst=Address("DS9800")),                # callee_id -> next_id
        OutSet(Address("C1500")),                            # yield flag
        Return(),
    ]
    # Last push rung: sp==3, push at stack_base+3
    assert rungs[3].ops[2] == Move(src="2", dst=Address("DS9804"))
    assert rungs[3].ops[3] == Move(src="1", dst=Address("DS9813"))
    assert rungs[3].ops[4] == Move(src="4", dst=Address("DS9809"))


# -----------------------------------------------------------------------------
# Non-Main POU rewriting
# -----------------------------------------------------------------------------


def _two_pou_program_with_nested_call():
    """A calls B (a -> nested call); B is a leaf.

    A's body: [NO(X1), Call(B), OutCoil(Y1)]  -- a single nested call.
    B's body: [OutCoil(Y2)]  -- pure leaf.
    Main: calls A from its top rung.
    """
    a = Subroutine(name="A", rungs=[Rung([
        ContactNO(Address("X001")),
        Call(target="B"),
        OutCoil(Address("Y001")),
    ])])
    b = Subroutine(name="B", rungs=[Rung([OutCoil(Address("Y002"))])])
    main = Subroutine(name="Main", main=True, kind=PouKind.PROGRAM,
                      rungs=[Rung([Call(target="A")])])
    return Program(subroutines=[main, a, b])


def test_non_main_body_call_becomes_yield_sequence_plus_continuation():
    prog = _two_pou_program_with_nested_call()
    alloc = allocate_slots(prog)
    rewritten, _ = lower_scheduled_calls(prog, alloc=alloc)
    a = rewritten.find_subroutine("A")
    assert a is not None

    # A's new body shape, with stack_depth=8:
    #   resume-dispatch header: 1 entry-jump + 1 resume-jump + 1 entry-label
    #   yield rungs: 8 push rungs (no marshal_in since no parameterized Call)
    #   continuation: 1 rung with Label(resume_1) + gate + OutCoil(Y001)
    # Total: 3 + 8 + 1 = 12 rungs
    cfg = LoweringConfig()
    assert len(a.rungs) == 3 + cfg.sched_stack_depth + 1

    # First rung is the resume-dispatch entry-jump
    assert a.rungs[0].ops[-1] == Jump(label="entry")
    assert a.rungs[1].ops[-1] == Jump(label="resume_1")
    assert a.rungs[2].ops == [Label(name="entry")]

    # Push rungs all carry the original gate
    for i in range(cfg.sched_stack_depth):
        push = a.rungs[3 + i]
        assert push.ops[0] == ContactNO(Address("X001"))    # gate preserved
        assert push.ops[-1] == Return()
        assert push.ops[-2] == OutSet(Address("C1500"))     # yield flag

    # Continuation rung has the resume label + the gate + post-Call op
    cont = a.rungs[-1]
    assert cont.ops == [
        Label(name="resume_1"),
        ContactNO(Address("X001")),       # gate re-applied
        OutCoil(Address("Y001")),         # post-Call op
    ]


def test_pou_without_calls_is_passed_through_unchanged():
    """A POU body with no bare Calls needs no rewrite -- not even a
    resume-dispatch header."""
    leaf = Subroutine(name="Leaf", rungs=[
        Rung([ContactNO(Address("X001")), OutCoil(Address("Y001"))]),
    ])
    main = Subroutine(name="Main", main=True, rungs=[])
    prog = Program(subroutines=[main, leaf])
    rewritten, _ = lower_scheduled_calls(prog)
    out = rewritten.find_subroutine("Leaf")
    assert out.rungs == leaf.rungs


def test_pou_with_two_nested_calls_gets_two_resume_sites():
    """Multiple Calls in a POU body get distinct resume ids."""
    a = Subroutine(name="A", rungs=[
        Rung([Call(target="B")]),
        Rung([Call(target="C")]),
    ])
    b = Subroutine(name="B")
    c = Subroutine(name="C")
    main = Subroutine(name="Main", main=True)
    prog = Program(subroutines=[main, a, b, c])
    rewritten, _ = lower_scheduled_calls(prog)
    a_out = rewritten.find_subroutine("A")

    # Resume header now has 2 sites
    # Rung 0: entry jump (id 0)
    # Rung 1: resume_1 jump
    # Rung 2: resume_2 jump
    # Rung 3: entry label
    assert a_out.rungs[0].ops[-1] == Jump(label="entry")
    assert a_out.rungs[1].ops[-1] == Jump(label="resume_1")
    assert a_out.rungs[2].ops[-1] == Jump(label="resume_2")
    assert a_out.rungs[3].ops == [Label(name="entry")]

    # Look for the two continuation Labels somewhere later in the body
    labels = [op for r in a_out.rungs for op in r.ops if isinstance(op, Label)]
    label_names = [l.name for l in labels]
    assert "entry" in label_names
    assert "resume_1" in label_names
    assert "resume_2" in label_names


# -----------------------------------------------------------------------------
# Main body rewriting
# -----------------------------------------------------------------------------


def test_main_call_to_leaf_pou_remains_native_click_call():
    leaf = Subroutine(name="Leaf", rungs=[Rung([OutCoil(Address("Y001"))])])
    main = Subroutine(name="Main", main=True, rungs=[
        Rung([ContactNO(Address("X001")), Call(target="Leaf")]),
    ])
    prog = Program(subroutines=[main, leaf])
    rewritten, _ = lower_scheduled_calls(prog)
    main_out = rewritten.find_subroutine("Main")
    [rung] = main_out.rungs
    assert rung.ops == [ContactNO(Address("X001")), Call(target="Leaf")]


def test_main_call_to_non_leaf_pou_becomes_scheduler_dispatch():
    """Main's Call(P) where P has nested calls becomes:
       Move(P.id -> sched_next_id)
       Move("0" -> sched_resume_id)
    so the trampoline picks P up on the next dispatch cycle."""
    non_leaf = Subroutine(name="NonLeaf", rungs=[
        Rung([Call(target="Inner")]),       # has a Call -> non-leaf
    ])
    inner = Subroutine(name="Inner", rungs=[Rung([OutCoil(Address("Y001"))])])
    main = Subroutine(name="Main", main=True, rungs=[
        Rung([ContactNO(Address("X001")), Call(target="NonLeaf")]),
    ])
    prog = Program(subroutines=[main, non_leaf, inner])
    rewritten, alloc = lower_scheduled_calls(prog)
    main_out = rewritten.find_subroutine("Main")
    [rung] = main_out.rungs
    non_leaf_id = alloc.pou_id["NonLeaf"]
    assert rung.ops == [
        ContactNO(Address("X001")),
        Move(src=str(non_leaf_id), dst=Address("DS9800")),
        Move(src="0", dst=Address("DS9818")),
    ]


# -----------------------------------------------------------------------------
# Full-pipeline composition
# -----------------------------------------------------------------------------


def test_full_pipeline_produces_legal_click_program():
    """End-to-end: a Program with parameterized calls + nested calls
    runs cleanly through all four passes (callee body rewrite,
    caller marshalling, scheduled-call rewrite, trampoline
    prepend).  The result has no bare Calls inside non-Main POU
    bodies (they've become yields), and Main has the trampoline
    plus its rewritten user body."""
    from universal_machinery.il import TagRef

    # B is a leaf FUNCTION
    b = Subroutine(
        name="B", kind=PouKind.FUNCTION,
        inputs=[Var("x", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("y", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([Move(src=TagRef("x"), dst=TagRef("y"))])],
    )
    # A is a FUNCTION that calls B internally (non-leaf)
    a = Subroutine(
        name="A", kind=PouKind.FUNCTION,
        inputs=[Var("a_in", TagType.INT, VarDirection.INPUT)],
        outputs=[Var("a_out", TagType.INT, VarDirection.OUTPUT)],
        rungs=[Rung([
            Call(target="B",
                 inputs=(("x", TagRef("a_in")),),
                 return_to=TagRef("a_out")),
        ])],
    )
    main = Subroutine(name="Main", main=True, kind=PouKind.PROGRAM,
                      rungs=[Rung([
                          Call(target="A",
                               inputs=(("a_in", Address("DS10")),),
                               return_to=Address("DS20")),
                      ])])
    prog = Program(subroutines=[main, a, b])

    # Pipeline
    p, alloc = lower_pou_bodies(prog)
    p, _ = lower_calls(p, alloc=alloc)
    p_pre_trampoline, _ = lower_scheduled_calls(p, alloc=alloc)
    final = prepend_trampoline_to_main(p_pre_trampoline, alloc)

    # A is non-leaf (after lower_calls it had a bare Call inside);
    # its body now has no bare Calls (rewritten to yields).
    a_out = final.find_subroutine("A")
    bare_calls_in_a = [op for r in a_out.rungs for op in r.ops
                       if isinstance(op, Call)]
    assert bare_calls_in_a == []

    # Main's USER body (pre-trampoline) had its Call(A) rewritten to
    # a scheduler dispatch.  We check before prepending so the
    # trampoline's dispatch table doesn't confuse the assertion.
    main_user_body = p_pre_trampoline.find_subroutine("Main").rungs
    bare_calls_in_user = [op for r in main_user_body for op in r.ops
                          if isinstance(op, Call) and op.target == "A"]
    assert bare_calls_in_user == []

    # The trampoline's dispatch table SHOULD contain Call(A) and Call(B)
    # -- that's how POUs are dispatched.  Verify it's there in the
    # final program.
    main_out = final.find_subroutine("Main")
    dispatch_calls = [
        op for r in main_out.rungs for op in r.ops
        if isinstance(op, Call) and op.target in {"A", "B"}
    ]
    targets = sorted({c.target for c in dispatch_calls})
    assert targets == ["A", "B"]


def test_input_program_not_mutated():
    prog = _two_pou_program_with_nested_call()
    original_a_rungs = list(prog.find_subroutine("A").rungs)
    original_main_rungs = list(prog.find_subroutine("Main").rungs)
    lower_scheduled_calls(prog)
    assert prog.find_subroutine("A").rungs == original_a_rungs
    assert prog.find_subroutine("Main").rungs == original_main_rungs
