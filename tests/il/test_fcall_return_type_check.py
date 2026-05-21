"""Tests for FunctionCallExpr return-type inference in the
type-checker.

PR #24 / #26 left ``FunctionCallExpr`` inferring as ``None``
(skip).  This slice resolves the return type for:

  1. User-defined FUNCTION POUs via ``Subroutine.return_type``.
  2. IEC type-conversion convention ``<SRC>_TO_<DST>`` and
     ``<SRC>_TRUNC_<DST>`` -- destination name drives the return.
  3. A fixed-return-type table for ~30 §2.5.2 builtins
     (transcendentals -> REAL, string functions -> STRING / INT,
     time arithmetic -> TIME / TOD / DT, TRUNC -> DINT).

Polymorphic functions (ABS / MIN / MAX / SEL / LIMIT / MUX --
where the result type depends on input types) stay in ``None``
land and skip silently; those need an operand-aware inferrer
landing in a follow-up.
"""
import pytest

from universal_machinery.builders import (
    add_e, alias_type, assign, fb, fcall_expr, fn, gt_e, if_, lit,
    named_type, prog, program, var, var_in,
)
from universal_machinery.il import TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# User-defined FUNCTION return type
# -----------------------------------------------------------------------------


def test_udf_int_return_matches_int_target():
    p = program(
        subroutines=[
            fn("Avg", inputs=[var_in("a", TagType.INT),
                                var_in("b", TagType.INT)],
                       return_type=TagType.INT),
            prog("Main", main=True,
                  local_vars=[var("result", TagType.INT)],
                  st_body=[assign("result",
                                   fcall_expr("Avg",
                                              "result", "result"))]),
        ],
    )
    assert _codes(p) == []


def test_udf_int_return_to_bool_target_raises():
    p = program(
        subroutines=[
            fn("Counter", inputs=[var_in("x", TagType.INT)],
                            return_type=TagType.INT),
            prog("Main", main=True,
                  local_vars=[var("flag", TagType.BOOL)],
                  st_body=[assign("flag",
                                   fcall_expr("Counter", lit(0)))]),
        ],
    )
    assert "st-assignment-type-mismatch" in _codes(p)


def test_udf_real_return_to_real_target():
    p = program(
        subroutines=[
            fn("Avg2", inputs=[var_in("a", TagType.REAL)],
                         return_type=TagType.REAL),
            prog("Main", main=True,
                  local_vars=[var("r", TagType.REAL)],
                  st_body=[assign("r",
                                   fcall_expr("Avg2", lit(1.0)))]),
        ],
    )
    assert _codes(p) == []


def test_udf_with_named_type_return_resolves_to_base():
    """A FUNCTION returning ``Distance`` (alias of REAL) -- the
    return type resolves through the alias chain."""
    p = program(
        user_types=[alias_type("Distance", base=TagType.REAL)],
        subroutines=[
            fn("MeasureDist",
                inputs=[var_in("x", TagType.INT)],
                return_type=named_type("Distance")),
            prog("Main", main=True,
                  local_vars=[var("d", TagType.REAL)],
                  st_body=[assign("d",
                                   fcall_expr("MeasureDist", lit(0)))]),
        ],
    )
    assert _codes(p) == []


def test_udf_without_return_type_stays_silent():
    """A user-defined FUNCTION with no ``return_type`` resolves
    to None -- the type checker can't tell what the call yields
    and skips."""
    p = program(
        subroutines=[
            fn("Doit", inputs=[var_in("x", TagType.INT)],
                         return_type=None),
            prog("Main", main=True,
                  local_vars=[var("flag", TagType.BOOL)],
                  st_body=[assign("flag",
                                   fcall_expr("Doit", lit(0)))]),
        ],
    )
    # Don't raise a false positive
    assert "st-assignment-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Type-conversion convention <SRC>_TO_<DST>
# -----------------------------------------------------------------------------


def test_int_to_real_returns_real():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("r", TagType.REAL)],
        st_body=[assign("r", fcall_expr("INT_TO_REAL", lit(42)))],
    )])
    assert _codes(p) == []


def test_int_to_real_to_bool_target_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("INT_TO_REAL", lit(42)))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_real_to_int_returns_int():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT)],
        st_body=[assign("i", fcall_expr("REAL_TO_INT", lit(3.14)))],
    )])
    assert _codes(p) == []


def test_bool_to_byte_returns_byte():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("b", TagType.BYTE)],
        st_body=[assign("b", fcall_expr("BOOL_TO_BYTE", lit(True)))],
    )])
    assert _codes(p) == []


def test_real_trunc_int_returns_int():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT)],
        st_body=[assign("i", fcall_expr("REAL_TRUNC_INT", lit(3.14)))],
    )])
    assert _codes(p) == []


def test_bogus_to_pattern_not_treated_as_conversion():
    """``MY_TO_VENDOR`` follows the syntactic pattern but the
    destination ``VENDOR`` isn't an IEC elementary type -- the
    resolver returns None and the check skips."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("MY_TO_VENDOR", lit(0)))],
    )])
    # Should NOT raise st-assignment-type-mismatch (false positive avoided)
    assert "st-assignment-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Fixed-return-type table for §2.5.2 builtins
# -----------------------------------------------------------------------------


def test_sqrt_returns_real():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("r", TagType.REAL)],
        st_body=[assign("r", fcall_expr("SQRT", lit(2.0)))],
    )])
    assert _codes(p) == []


def test_sqrt_to_int_target_raises():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.INT)],
        st_body=[assign("i", fcall_expr("SQRT", lit(2.0)))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p)


def test_sin_cos_tan_return_real():
    for name in ("SIN", "COS", "TAN"):
        p = program(subroutines=[prog("Main", main=True,
            local_vars=[var("r", TagType.REAL)],
            st_body=[assign("r", fcall_expr(name, lit(0.0)))],
        )])
        assert _codes(p) == [], f"{name} should return REAL"


def test_len_returns_int():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("n", TagType.INT)],
        st_body=[assign("n", fcall_expr("LEN", lit("'hello'", kind="string")))],
    )])
    assert _codes(p) == []


def test_concat_returns_string():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("s", TagType.STRING)],
        st_body=[assign("s", fcall_expr("CONCAT",
                                          lit("'foo'", kind="string"),
                                          lit("'bar'", kind="string")))],
    )])
    assert _codes(p) == []


def test_add_time_returns_time():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("t", TagType.TIME)],
        st_body=[assign("t", fcall_expr("ADD_TIME",
                                          lit("T#1s", kind="typed"),
                                          lit("T#2s", kind="typed")))],
    )])
    assert _codes(p) == []


def test_trunc_returns_dint():
    """Generic ``TRUNC`` returns DINT; assigning to INT is in
    the same integer family so it should pass.  Assigning to
    BOOL raises."""
    p_ok = program(subroutines=[prog("Main", main=True,
        local_vars=[var("i", TagType.DINT)],
        st_body=[assign("i", fcall_expr("TRUNC", lit(3.14)))],
    )])
    assert _codes(p_ok) == []
    p_bad = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("TRUNC", lit(3.14)))],
    )])
    assert "st-assignment-type-mismatch" in _codes(p_bad)


# -----------------------------------------------------------------------------
# Polymorphic functions skip silently
# -----------------------------------------------------------------------------


def test_abs_skips_silently():
    """``ABS`` is polymorphic -- type depends on input.  V1
    leaves it None so we don't raise false positives on
    ``flag := ABS(x)``."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("ABS", lit(0)))],
    )])
    assert "st-assignment-type-mismatch" not in _codes(p)


def test_min_max_sel_skip_silently():
    for name in ("MIN", "MAX", "SEL", "LIMIT", "MUX"):
        p = program(subroutines=[prog("Main", main=True,
            local_vars=[var("flag", TagType.BOOL)],
            st_body=[assign("flag", fcall_expr(name, lit(0), lit(1)))],
        )])
        assert "st-assignment-type-mismatch" not in _codes(p), name


# -----------------------------------------------------------------------------
# Vendor / unknown function names skip silently
# -----------------------------------------------------------------------------


def test_unknown_function_skips_silently():
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("flag", TagType.BOOL)],
        st_body=[assign("flag", fcall_expr("VendorThing", lit(0)))],
    )])
    assert "st-assignment-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Use in nested expressions
# -----------------------------------------------------------------------------


def test_function_call_in_if_condition():
    """``IF SQRT(x) > 0.0 THEN`` -- the SQRT call resolves to
    REAL, the comparison yields BOOL, and the IF condition is
    BOOL-compatible."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.REAL), var("y", TagType.REAL)],
        st_body=[if_((gt_e(fcall_expr("SQRT", "x"), lit(0.0)),
                        [assign("y", lit(0.0))]))],
    )])
    assert _codes(p) == []


def test_function_call_inside_arith_expression():
    """``count := count + LEN(text)`` -- LEN returns INT,
    addition with INT produces INT, assigned to INT target."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("count", TagType.INT),
                     var("text", TagType.STRING)],
        st_body=[assign("count",
                          add_e("count", fcall_expr("LEN", "text")))],
    )])
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# FUNCTION_BLOCK calls don't go through the FUNCTION return-type
# table (they don't return a single value)
# -----------------------------------------------------------------------------


def test_function_block_call_skips_return_type_lookup():
    """A FB name has no ``return_type`` -- the resolver returns
    None and the check skips, even if the FB has the same name
    as a function.  Calls to FBs typically use named-arg
    syntax to bind outputs."""
    p = program(
        subroutines=[
            fb("Counter"),                           # FB, no return_type
            prog("Main", main=True,
                  local_vars=[var("flag", TagType.BOOL)],
                  st_body=[assign("flag", fcall_expr("Counter"))]),
        ],
    )
    assert "st-assignment-type-mismatch" not in _codes(p)
