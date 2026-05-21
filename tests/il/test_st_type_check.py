"""Type-checking tests for ST AST bodies (IEC §3 / §6.5).

The previous slice landed type checks on rung ops; this one
extends the same compatibility rules into ``Subroutine.st_body``
and ``Method.st_body``.  Four new error codes are emitted:

  - ``st-assignment-type-mismatch``  : Assignment target ↔ value
  - ``st-condition-not-bool``        : IF / WHILE / REPEAT cond
  - ``st-for-index-not-numeric``     : FOR index var (must be int)
  - ``st-for-bound-not-numeric``     : FOR start / end / step

Expression type inference is best-effort: literals, variable
references, unary, and binary operations are covered.  Function
calls, field access, and index access skip silently so callers
get false-positive-free results.
"""
import pytest

from universal_machinery.builders import (
    abs_, add_e, alias_type, and_e, assign, case_, case_clause, eq_e, fb,
    for_, gt_e, if_, lit, lt_e, method, mul_e, named_type, not_e, or_e,
    prog, program, repeat_, ret_st, sub_e, subrange_type, tag_decl, var,
    var_in, while_,
)
from universal_machinery.il import TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# Clean ST bodies
# -----------------------------------------------------------------------------


def test_clean_integer_assignment():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("count", TagType.INT)],
        st_body=[assign("count", lit(42))],
    )])
    assert _codes(p) == []


def test_clean_bool_assignment_from_comparison():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("speed", TagType.INT),
                     var("ok", TagType.BOOL)],
        st_body=[assign("ok", gt_e("speed", lit(0)))],
    )])
    assert _codes(p) == []


def test_clean_for_loop():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT), var("sum", TagType.INT)],
        st_body=[
            assign("sum", lit(0)),
            for_("i", lit(1), lit(10),
                  [assign("sum", add_e("sum", "i"))]),
        ],
    )])
    assert _codes(p) == []


def test_clean_while_loop_with_bool_condition():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT), var("done", TagType.BOOL)],
        st_body=[while_(not_e("done"),
                          [assign("i", add_e("i", lit(1)))])],
    )])
    # Should have no condition-not-bool error
    assert "st-condition-not-bool" not in _codes(p)


def test_clean_if_elsif_else_with_bool_conditions():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.INT), var("y", TagType.INT)],
        st_body=[if_(
            (gt_e("x", lit(10)), [assign("y", lit(1))]),
            (gt_e("x", lit(0)),  [assign("y", lit(2))]),
            else_=[assign("y", lit(0))],
        )],
    )])
    assert _codes(p) == []


def test_clean_repeat_loop():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT)],
        st_body=[repeat_([assign("i", sub_e("i", lit(1)))],
                           gt_e("i", lit(0)))],
    )])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Assignment type mismatch
# -----------------------------------------------------------------------------


def test_int_to_bool_assignment_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", lit(42))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_bool_to_int_assignment_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("count", TagType.INT)],
        st_body=[assign("count", lit(True))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_int_to_real_assignment_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("r", TagType.REAL)],
        st_body=[assign("r", lit(42))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_clean_integer_promotion_within_bucket():
    """UINT -> INT within the integer family is fine."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("u", TagType.UINT), var("i", TagType.INT)],
        st_body=[assign("i", "u")],
    )])
    assert _codes(p) == []


def test_assignment_with_binary_expr_value_checks_result_type():
    """``count := count + 1`` -- the RHS infers as INT (lhs's
    type), which matches the LHS."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("count", TagType.INT)],
        st_body=[assign("count", add_e("count", lit(1)))],
    )])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Condition checks (IF / WHILE / REPEAT)
# -----------------------------------------------------------------------------


def test_if_with_int_condition_raises():
    """``IF counter THEN ...`` is non-BOOL."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("counter", TagType.INT)],
        st_body=[if_(("counter", [assign("counter", lit(0))]))],
    )])
    assert "st-condition-not-bool" in _codes(p)


def test_while_with_int_condition_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("counter", TagType.INT)],
        st_body=[while_("counter",
                          [assign("counter", sub_e("counter", lit(1)))])],
    )])
    assert "st-condition-not-bool" in _codes(p)


def test_repeat_with_int_condition_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("counter", TagType.INT)],
        st_body=[repeat_([assign("counter", add_e("counter", lit(1)))],
                           "counter")],
    )])
    assert "st-condition-not-bool" in _codes(p)


def test_if_with_comparison_passes():
    """``IF x > 0 THEN`` -- comparison expressions infer as BOOL."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.INT)],
        st_body=[if_((gt_e("x", lit(0)), [assign("x", lit(0))]))],
    )])
    assert "st-condition-not-bool" not in _codes(p)


def test_if_with_logical_chain_passes():
    """``IF a AND NOT b OR c THEN`` -- logical operators preserve
    the BOOL family."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("a", TagType.BOOL), var("b", TagType.BOOL),
                     var("c", TagType.BOOL), var("x", TagType.BOOL)],
        st_body=[if_((or_e(and_e("a", not_e("b")), "c"),
                       [assign("x", lit(True))]))],
    )])
    assert "st-condition-not-bool" not in _codes(p)


# -----------------------------------------------------------------------------
# FOR loop checks
# -----------------------------------------------------------------------------


def test_for_index_real_raises_non_integer():
    """IEC §3.3.2.4 requires the loop variable to be an integer
    type; REAL is rejected even though it's numeric."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("rate", TagType.REAL)],
        st_body=[for_("rate", lit(1), lit(10), [])],
    )])
    assert "st-for-index-not-numeric" in _codes(p)


def test_for_index_bool_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[for_("flag", lit(1), lit(10), [])],
    )])
    assert "st-for-index-not-numeric" in _codes(p)


def test_for_index_uint_passes():
    """Unsigned integer is fine for FOR index."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.UINT)],
        st_body=[for_("i", lit(0), lit(100), [])],
    )])
    assert "st-for-index-not-numeric" not in _codes(p)


def test_for_bound_with_bool_raises():
    """FOR i := flag TO 10 DO ... -- flag is BOOL, not numeric."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT), var("flag", TagType.BOOL)],
        st_body=[for_("i", "flag", lit(10), [])],
    )])
    assert "st-for-bound-not-numeric" in _codes(p)


def test_for_step_with_bool_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT), var("flag", TagType.BOOL)],
        st_body=[for_("i", lit(1), lit(10), [], step="flag")],
    )])
    assert "st-for-bound-not-numeric" in _codes(p)


# -----------------------------------------------------------------------------
# CASE statement descends into clause bodies
# -----------------------------------------------------------------------------


def test_case_clause_body_typechecked():
    """An assignment inside a CASE clause body still gets
    checked (descent through CaseStatement.clauses works)."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("mode", TagType.INT),
                     var("flag", TagType.BOOL)],
        st_body=[case_("mode",
                         case_clause([lit(0)],
                                      [assign("flag", lit(42))])),  # BOOL := INT
                ],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# UDT resolution still works in ST type checks
# -----------------------------------------------------------------------------


def test_assignment_to_alias_typed_var_passes():
    """``Distance`` is an alias for REAL; assigning a REAL to
    a Distance-typed variable is fine."""
    p = program(
        user_types=[alias_type("Distance", base=TagType.REAL)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("d", named_type("Distance"))],
            st_body=[assign("d", lit(3.14))],
        )],
    )
    assert _codes(p) == []


def test_subrange_resolves_for_assignment():
    p = program(
        user_types=[subrange_type("Percent",
                                     base=TagType.UINT, lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Percent"))],
            st_body=[assign("p", lit(50))],
        )],
    )
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Method bodies (FB ST methods) get the same checks
# -----------------------------------------------------------------------------


def test_method_st_body_assignment_mismatch_flagged():
    p = program(subroutines=[
        fb("Inverter",
           methods=[method("Bad",
                            local_vars=[var("flag", TagType.BOOL)],
                            st_body=[assign("flag", lit(42))])])
    ])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_method_st_body_clean_with_method_locals():
    p = program(subroutines=[
        fb("FB1",
           methods=[method("Compute",
                            inputs=[var_in("x", TagType.INT)],
                            local_vars=[var("y", TagType.INT)],
                            st_body=[assign("y", add_e("x", lit(1)))])])
    ])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Function-call expressions skip type inference (no false positives)
# -----------------------------------------------------------------------------


def test_function_call_expression_doesnt_raise_false_positive():
    """The inferrer returns None for FunctionCallExpr, so a
    ``y := someCall(x)`` assignment passes regardless of the
    LHS type."""
    from universal_machinery.builders import fcall_expr
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("result", TagType.INT)],
        st_body=[assign("result", fcall_expr("DoIt", "x"))],
    )])
    # Should not raise st-assignment-type-mismatch even though
    # the inferrer can't determine the call's return type
    assert "st-assignment-type-mismatch" not in _codes(p)
