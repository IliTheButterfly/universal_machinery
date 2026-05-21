"""Tests for operand-aware return-type inference of polymorphic
IEC §2.5.2 builtins (ABS / MIN / MAX / SEL / LIMIT / MUX).

PR #27 left these inferring as ``None`` because their return
type depends on the input types.  This slice walks the
controlling argument and inherits its type:

  - ABS(x)                 -> type of x
  - MIN(a, b, ...)         -> type of a (first input)
  - MAX(a, b, ...)         -> type of a (first input)
  - SEL(g, in0, in1)       -> type of in0  (IEC requires
                              in0 / in1 to match anyway)
  - LIMIT(lo, value, hi)   -> type of value
  - MUX(k, in0, in1, ...)  -> type of in0

When the controlling arg can't be inferred (literal, function-
call expression, etc.), the call's return type falls back to
``None`` and the check skips silently.
"""
import pytest

from universal_machinery.builders import (
    add_e, assign, fcall_expr, lit, prog, program, var,
)
from universal_machinery.il import TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# ABS
# -----------------------------------------------------------------------------


def test_abs_of_int_var_returns_int():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.INT), var("r", TagType.INT)],
        st_body=[assign("r", fcall_expr("ABS", "x"))],
    )])
    assert _codes(p) == []


def test_abs_of_int_var_to_bool_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.INT), var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("ABS", "x"))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_abs_of_real_var_returns_real():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.REAL), var("r", TagType.REAL)],
        st_body=[assign("r", fcall_expr("ABS", "x"))],
    )])
    assert _codes(p) == []


def test_abs_of_lreal_var_returns_lreal_compatible():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.LREAL), var("r", TagType.LREAL)],
        st_body=[assign("r", fcall_expr("ABS", "x"))],
    )])
    assert _codes(p) == []


def test_abs_of_int_literal_returns_int():
    """``ABS(42)`` -- the literal infers as INT, so the return
    is INT.  Assigning to a BOOL target raises."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("ABS", lit(42)))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# MIN / MAX
# -----------------------------------------------------------------------------


def test_min_inherits_first_arg_type():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("a", TagType.REAL), var("b", TagType.REAL),
                     var("r", TagType.REAL)],
        st_body=[assign("r", fcall_expr("MIN", "a", "b"))],
    )])
    assert _codes(p) == []


def test_max_inherits_first_arg_type():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("a", TagType.INT), var("b", TagType.INT),
                     var("r", TagType.INT)],
        st_body=[assign("r", fcall_expr("MAX", "a", "b"))],
    )])
    assert _codes(p) == []


def test_min_int_var_to_bool_target_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("a", TagType.INT), var("b", TagType.INT),
                     var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("MIN", "a", "b"))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_min_with_more_than_two_args_still_uses_first():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("a", TagType.REAL),
                     var("b", TagType.REAL),
                     var("c", TagType.REAL),
                     var("r", TagType.REAL)],
        st_body=[assign("r", fcall_expr("MIN", "a", "b", "c"))],
    )])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# SEL / LIMIT / MUX
# -----------------------------------------------------------------------------


def test_sel_returns_in0_type():
    """SEL(g, in0, in1) -- result tracks in0 (index 1).
    g is BOOL, in0/in1 are INTs, result is INT."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("g", TagType.BOOL),
                     var("lo", TagType.INT),
                     var("hi", TagType.INT),
                     var("r", TagType.INT)],
        st_body=[assign("r", fcall_expr("SEL", "g", "lo", "hi"))],
    )])
    assert _codes(p) == []


def test_sel_int_to_bool_target_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("g", TagType.BOOL),
                     var("lo", TagType.INT),
                     var("hi", TagType.INT),
                     var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("SEL", "g", "lo", "hi"))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_limit_returns_value_type():
    """LIMIT(lo, value, hi) -- result tracks value (index 1).
    REAL value -> REAL result."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("lo", TagType.REAL),
                     var("value", TagType.REAL),
                     var("hi", TagType.REAL),
                     var("r", TagType.REAL)],
        st_body=[assign("r",
                          fcall_expr("LIMIT", "lo", "value", "hi"))],
    )])
    assert _codes(p) == []


def test_mux_returns_first_input_type():
    """MUX(k, in0, in1, in2) -- result tracks in0 (index 1)."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("k", TagType.INT),
                     var("a", TagType.REAL),
                     var("b", TagType.REAL),
                     var("c", TagType.REAL),
                     var("r", TagType.REAL)],
        st_body=[assign("r",
                          fcall_expr("MUX", "k", "a", "b", "c"))],
    )])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Polymorphic call used inside an arithmetic expression
# -----------------------------------------------------------------------------


def test_abs_in_arith_expression():
    """``count := count + ABS(delta)`` -- ABS yields delta's
    type (INT); the INT chain is consistent with INT target."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("count", TagType.INT),
                     var("delta", TagType.INT)],
        st_body=[assign("count",
                          add_e("count", fcall_expr("ABS", "delta")))],
    )])
    assert _codes(p) == []


def test_abs_in_arith_expr_with_real_operand():
    """ABS preserves the family: REAL operand -> REAL result."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("total", TagType.REAL),
                     var("delta", TagType.REAL)],
        st_body=[assign("total",
                          add_e("total", fcall_expr("ABS", "delta")))],
    )])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Unresolvable operand: skip silently
# -----------------------------------------------------------------------------


def test_abs_of_unknown_var_skips_silently():
    """If the operand can't be resolved (unknown name), ABS
    falls back to ``None`` and the surrounding assignment's
    type check skips -- no false positive for unresolved
    operand chains."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("ABS", "missing_var"))],
    )])
    assert "st-assignment-type-mismatch" not in _codes(p)


def test_abs_of_polymorphic_call_chains():
    """``r := ABS(MIN(a, b))`` -- nested polymorphic call.
    MIN inherits from a (REAL), ABS inherits from MIN (REAL),
    result REAL matches r."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("a", TagType.REAL),
                     var("b", TagType.REAL),
                     var("r", TagType.REAL)],
        st_body=[assign("r",
                          fcall_expr("ABS",
                                      fcall_expr("MIN", "a", "b")))],
    )])
    assert _codes(p) == []
