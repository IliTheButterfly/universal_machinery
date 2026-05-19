"""Tests for the FBD → ST lowering pass.

Covers the lowering core (topological sort, producer-expression
resolution, statement synthesis) plus integration with the ST
emitter so authored FBD bodies render as real ST text rather than
a marker comment.
"""
import pytest

from universal_machinery.builders import (
    fb_block, fbd_jump, fbd_label, fbd_network, fbd_return, in_var,
    inout_var, out_var, pin, prog, program, var,
)
from universal_machinery.emitters.st import emit_pou, emit_st_body
from universal_machinery.il import (
    Assignment, BinaryExpr, BinaryOp, CommentStatement, FbdNetwork,
    FunctionCallExpr, FunctionCallStatement, IfStatement, InVariable,
    Literal, OutVariable, ReturnStatement, TagRef, TagType, UnaryExpr,
    UnaryOp, VarRef,
)
from universal_machinery.lowering import LoweringError, lower_fbd_to_st


# -----------------------------------------------------------------------------
# Topological sort
# -----------------------------------------------------------------------------


def test_empty_network_lowers_to_nothing():
    result = lower_fbd_to_st(FbdNetwork())
    assert result.statements == []
    assert result.temp_vars == []


def test_simple_assignment_chain_lowers_to_assignment():
    net = fbd_network(
        in_var(0, "x"),
        out_var(1, "y", source_id=0),
    )
    result = lower_fbd_to_st(net)
    assert len(result.statements) == 1
    stmt = result.statements[0]
    assert isinstance(stmt, Assignment)
    assert isinstance(stmt.target, VarRef)
    assert stmt.target.ref.name == "y"


def test_topo_sort_handles_out_of_order_declaration():
    """Declaring the OutVariable before its source must still
    produce a valid lowered output (sink fires after producer)."""
    net = fbd_network(
        out_var(0, "y", source_id=1),
        in_var(1, "x"),
    )
    result = lower_fbd_to_st(net)
    # The InVariable doesn't emit; OutVariable becomes ``y := x;``
    assert len(result.statements) == 1
    txt = emit_st_body(result.statements, level=0)
    assert txt == ["y := x;"]


def test_cycle_detected_raises_lowering_error():
    """An FBD network with a wire cycle is unlowerable: each
    element depends on the other's output."""
    # Two OutVariables referencing each other -- pathological but
    # structurally a cycle for the topo sort.  Note this also
    # violates IEC semantics; the validator should catch it, but
    # the lowering pass must not loop forever.
    bad = FbdNetwork(elements=[
        OutVariable(local_id=0, expression="a",
                     connection=__import__(
                         "universal_machinery.il", fromlist=["Connection"]
                     ).Connection(source_id=1)),
        OutVariable(local_id=1, expression="b",
                     connection=__import__(
                         "universal_machinery.il", fromlist=["Connection"]
                     ).Connection(source_id=0)),
    ])
    with pytest.raises(LoweringError):
        lower_fbd_to_st(bad)


# -----------------------------------------------------------------------------
# Inline BinaryExpr lowering (algebraic identities)
# -----------------------------------------------------------------------------


def test_add_block_lowers_to_binary_plus():
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        fb_block(2, "ADD",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "z", source_id=2, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    # Stateless ADD with single output + exactly 2 inputs lowers
    # inline -- no temp vars needed.
    assert result.temp_vars == []
    assert emit_st_body(result.statements, level=0) == ["z := x + y;"]


def test_and_block_lowers_to_binary_and():
    net = fbd_network(
        in_var(0, "a"),
        in_var(1, "b"),
        fb_block(2, "AND",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "out", source_id=2, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    assert emit_st_body(result.statements, level=0) == ["out := a AND b;"]


def test_comparison_block_lowers_to_binary_op():
    net = fbd_network(
        in_var(0, "speed"),
        in_var(1, "100"),
        fb_block(2, "GT",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "fast", source_id=2, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    assert emit_st_body(result.statements, level=0) == ["fast := speed > 100;"]


def test_negated_input_pin_wraps_with_NOT():
    net = fbd_network(
        in_var(0, "a"),
        in_var(1, "b"),
        fb_block(2, "AND",
                 inputs=[pin("IN1", source_id=0, negated=True),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "out", source_id=2, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    assert emit_st_body(result.statements, level=0) == ["out := NOT a AND b;"]


def test_chained_binary_ops_compose_inline():
    """(x + y) * z -- two stateless binary blocks chained, all
    inline (no temps)."""
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        in_var(2, "z"),
        fb_block(3, "ADD",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        fb_block(4, "MUL",
                 inputs=[pin("IN1", source_id=3, source_pin="OUT"),
                          pin("IN2", source_id=2)],
                 outputs=[pin("OUT")]),
        out_var(5, "r", source_id=4, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    assert result.temp_vars == []
    # ADD has lower precedence than MUL so the precedence walker
    # parenthesises -- this exercises the BinaryExpr expression
    # emitter's precedence handling too.
    assert emit_st_body(result.statements, level=0) == ["r := (x + y) * z;"]


# -----------------------------------------------------------------------------
# Stateless function call -> temp variable
# -----------------------------------------------------------------------------


def test_stateless_function_assigned_to_temp():
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "SQRT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        out_var(2, "r", source_id=1, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    # SQRT isn't in the BinaryExpr-eligible set; routes through a temp.
    assert len(result.temp_vars) == 1
    temp_name = result.temp_vars[0].name
    txt = emit_st_body(result.statements, level=0)
    assert txt[0] == f"{temp_name} := SQRT(x);"
    assert txt[1] == f"r := {temp_name};"


def test_unary_not_block_routes_through_temp():
    """``NOT`` block has one input; doesn't qualify for inline
    BinaryExpr (we treat NOT specifically via UnaryExpr).  For
    now the lowering uses ``NOT(x)`` as a function call to a temp.
    """
    net = fbd_network(
        in_var(0, "flag"),
        fb_block(1, "NOT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        out_var(2, "inv", source_id=1, source_pin="OUT"),
    )
    result = lower_fbd_to_st(net)
    txt = emit_st_body(result.statements, level=0)
    assert "NOT(flag)" in txt[0]


# -----------------------------------------------------------------------------
# FB instance calls
# -----------------------------------------------------------------------------


def test_fb_call_emits_named_arg_call_and_dot_access():
    net = fbd_network(
        in_var(0, "start_clk"),
        in_var(1, "T#100ms"),
        fb_block(2, "TON",
                 instance_name="tmr1",
                 inputs=[pin("IN", source_id=0),
                          pin("PT", source_id=1)],
                 outputs=[pin("Q"), pin("ET")]),
        out_var(3, "done", source_id=2, source_pin="Q"),
        out_var(4, "elapsed", source_id=2, source_pin="ET"),
    )
    result = lower_fbd_to_st(net)
    txt = emit_st_body(result.statements, level=0)
    assert txt[0] == "tmr1(IN := start_clk, PT := T#100ms);"
    # The order of the two output assignments is implementation
    # detail (depends on topo tiebreaker), but both must appear.
    assert "done := tmr1.Q;" in txt
    assert "elapsed := tmr1.ET;" in txt


def test_fb_call_with_single_output_pin_use():
    """An FB whose output is consumed by exactly one downstream
    OutVariable still emits the call + dot-access form (not
    inlined)."""
    net = fbd_network(
        in_var(0, "clk"),
        fb_block(1, "R_TRIG",
                 instance_name="re",
                 inputs=[pin("CLK", source_id=0)],
                 outputs=[pin("Q")]),
        out_var(2, "pulse", source_id=1, source_pin="Q"),
    )
    result = lower_fbd_to_st(net)
    txt = emit_st_body(result.statements, level=0)
    assert "re(CLK := clk);" in txt
    assert "pulse := re.Q;" in txt


# -----------------------------------------------------------------------------
# InOutVariable
# -----------------------------------------------------------------------------


def test_inout_variable_lowers_to_assignment_and_propagates():
    """An InOutVariable acts as both sink (assignment target) and
    source (its current value flows downstream)."""
    net = fbd_network(
        in_var(0, "delta"),
        inout_var(1, "counter", source_id=0),
        # Read the inout's downstream value into a second OutVariable
        out_var(2, "snapshot", source_id=1),
    )
    result = lower_fbd_to_st(net)
    txt = emit_st_body(result.statements, level=0)
    # The InOut assigns first, then snapshot reads counter
    assert txt == ["counter := delta;", "snapshot := counter;"]


# -----------------------------------------------------------------------------
# Return + jump/label fallback
# -----------------------------------------------------------------------------


def test_return_with_gate_lowers_to_if_return():
    net = fbd_network(
        in_var(0, "halt"),
        fbd_return(1, source_id=0),
    )
    result = lower_fbd_to_st(net)
    assert len(result.statements) == 1
    s = result.statements[0]
    assert isinstance(s, IfStatement)
    assert len(s.branches) == 1
    cond, body = s.branches[0]
    assert len(body) == 1
    assert isinstance(body[0], ReturnStatement)


def test_return_without_gate_lowers_to_unconditional_return():
    net = fbd_network(
        fbd_return(0),
    )
    result = lower_fbd_to_st(net)
    assert len(result.statements) == 1
    assert isinstance(result.statements[0], ReturnStatement)


def test_jump_and_label_lower_to_comment_marker():
    """Jumps/labels currently lower to ``CommentStatement`` markers
    until the ST AST grows first-class Goto/Label statements."""
    net = fbd_network(
        in_var(0, "cond"),
        fbd_jump(1, "END_OK", source_id=0),
        fbd_label(2, "END_OK"),
    )
    result = lower_fbd_to_st(net)
    # Find the CommentStatement markers in the lowered output
    flat: list = []
    def _walk(stmts):
        for s in stmts:
            flat.append(s)
            if isinstance(s, IfStatement):
                for _c, body in s.branches:
                    _walk(body)
                if s.else_branch is not None:
                    _walk(s.else_branch)
    _walk(result.statements)
    comments = [s for s in flat if isinstance(s, CommentStatement)]
    assert any("FBD jump" in c.text for c in comments)
    assert any("FBD label" in c.text for c in comments)


# -----------------------------------------------------------------------------
# Integration with emit_pou
# -----------------------------------------------------------------------------


def test_emit_pou_lowers_fbd_body_to_real_st_text():
    """Authored FBD body renders as ST in the POU output."""
    net = fbd_network(
        in_var(0, "x"),
        in_var(1, "y"),
        fb_block(2, "ADD",
                 inputs=[pin("IN1", source_id=0),
                          pin("IN2", source_id=1)],
                 outputs=[pin("OUT")]),
        out_var(3, "z", source_id=2, source_pin="OUT"),
    )
    sub = prog("Main", main=True,
                local_vars=[var("x", TagType.INT), var("y", TagType.INT),
                             var("z", TagType.INT)],
                fbd_body=net)
    txt = emit_pou(sub)
    assert "PROGRAM Main" in txt
    assert "z := x + y;" in txt
    assert "FBD body not emitted" not in txt   # old marker is gone
    assert "END_PROGRAM" in txt


def test_emit_pou_injects_lowering_temps_as_var_block():
    """When the lowering allocates temps, the POU's body gets a
    synthetic ``VAR ... END_VAR`` block declaring them."""
    net = fbd_network(
        in_var(0, "x"),
        fb_block(1, "SQRT",
                 inputs=[pin("IN", source_id=0)],
                 outputs=[pin("OUT")]),
        out_var(2, "r", source_id=1, source_pin="OUT"),
    )
    sub = prog("Main", main=True, fbd_body=net)
    txt = emit_pou(sub)
    # The synthetic VAR section appears after the user's variable
    # blocks (none here) and before the lowered statements.
    assert "VAR  (* FBD lowering temporaries *)" in txt
    assert "_t0" in txt  # temp name
    assert "END_VAR" in txt
    assert "SQRT(x)" in txt
