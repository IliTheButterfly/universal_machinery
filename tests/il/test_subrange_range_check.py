"""Tests for SUBRANGE literal-bounds checks.

Last gap from the deferred-list of the type-checker series: when
a target's declared type is a ``SubrangeType`` and the RHS is an
integer literal, the validator verifies the literal sits in
``[lower, upper]``.  Non-literal RHS (variable references,
arithmetic expressions, function calls) can't be checked without
value-flow analysis -- those skip silently.

The check supports:

  - Direct SubrangeType-typed variable assignment
  - AliasType chain that bottoms out at SubrangeType
  - SUBRANGE struct members
  - SUBRANGE array elements
  - Both signed (``SINT`` / ``INT`` / ``DINT`` / ``LINT`` base) and
    unsigned (``USINT`` / ``UINT`` / ``UDINT`` / ``ULINT`` base)
  - Boolean-literal coercion (TRUE = 1, FALSE = 0)
  - Based-int literals (``16#FF`` = 255)

New error code: ``subrange-out-of-range``.
"""
import pytest

from universal_machinery.builders import (
    alias_type, array_type, assign, field_, index_, lit, named_type,
    prog, program, struct_type, subrange_type, var,
)
from universal_machinery.il import Literal, TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# Clean (in-range)
# -----------------------------------------------------------------------------


def test_unsigned_subrange_in_range_passes():
    p = program(
        user_types=[subrange_type("Percent",
                                     base=TagType.UINT,
                                     lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Percent"))],
            st_body=[assign("p", lit(50))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


def test_signed_subrange_in_range_passes():
    p = program(
        user_types=[subrange_type("Centered",
                                     base=TagType.INT,
                                     lower=-100, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("x", named_type("Centered"))],
            st_body=[assign("x", lit(-50))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


def test_value_equal_to_lower_passes():
    """``lower <= value <= upper`` is inclusive at both bounds."""
    p = program(
        user_types=[subrange_type("Pct",
                                     base=TagType.UINT, lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Pct"))],
            st_body=[assign("p", lit(0))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


def test_value_equal_to_upper_passes():
    p = program(
        user_types=[subrange_type("Pct",
                                     base=TagType.UINT, lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Pct"))],
            st_body=[assign("p", lit(100))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


# -----------------------------------------------------------------------------
# Out-of-range raises
# -----------------------------------------------------------------------------


def test_unsigned_subrange_above_upper_raises():
    p = program(
        user_types=[subrange_type("Pct",
                                     base=TagType.UINT, lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Pct"))],
            st_body=[assign("p", lit(150))],
        )],
    )
    codes = _codes(p)
    assert "subrange-out-of-range" in codes
    # Error message names the offending value + bounds
    err = next(e for e in validate(p)
                if e.code == "subrange-out-of-range")
    assert "150" in err.message
    assert "[0, 100]" in err.message
    assert "Pct" in err.message


def test_signed_subrange_below_lower_raises():
    p = program(
        user_types=[subrange_type("Small",
                                     base=TagType.INT,
                                     lower=-100, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("x", named_type("Small"))],
            st_body=[assign("x", lit(-200))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


def test_value_one_above_upper_raises():
    """Edge: ``upper + 1`` is the first out-of-range value."""
    p = program(
        user_types=[subrange_type("Pct",
                                     base=TagType.UINT, lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Pct"))],
            st_body=[assign("p", lit(101))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


def test_value_one_below_lower_raises():
    p = program(
        user_types=[subrange_type("Sm",
                                     base=TagType.INT, lower=0, upper=10)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("x", named_type("Sm"))],
            st_body=[assign("x", lit(-1))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


# -----------------------------------------------------------------------------
# AliasType chains to SubrangeType still trigger range check
# -----------------------------------------------------------------------------


def test_alias_of_subrange_still_range_checks():
    """Alias chains shouldn't hide the underlying subrange's
    bounds."""
    p = program(
        user_types=[
            subrange_type("Pct", base=TagType.UINT, lower=0, upper=100),
            alias_type("Level", base=named_type("Pct")),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Level"))],
            st_body=[assign("p", lit(150))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


# -----------------------------------------------------------------------------
# Struct member is a subrange
# -----------------------------------------------------------------------------


def test_struct_member_subrange_out_of_range_raises():
    p = program(
        user_types=[
            subrange_type("Pct", base=TagType.UINT, lower=0, upper=100),
            struct_type("Mix", [var("level", named_type("Pct"))]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("m", named_type("Mix"))],
            st_body=[assign(field_("m", "level"), lit(150))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


def test_struct_member_subrange_in_range_passes():
    p = program(
        user_types=[
            subrange_type("Pct", base=TagType.UINT, lower=0, upper=100),
            struct_type("Mix", [var("level", named_type("Pct"))]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("m", named_type("Mix"))],
            st_body=[assign(field_("m", "level"), lit(75))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


# -----------------------------------------------------------------------------
# Array element is a subrange
# -----------------------------------------------------------------------------


def test_array_element_subrange_out_of_range_raises():
    p = program(
        user_types=[
            subrange_type("Byte", base=TagType.UINT, lower=0, upper=255),
            array_type("Buf",
                         element_type=named_type("Byte"),
                         bounds=[(0, 7)]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("b", named_type("Buf"))],
            st_body=[assign(index_("b", lit(0)), lit(300))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


def test_array_element_subrange_in_range_passes():
    p = program(
        user_types=[
            subrange_type("Byte", base=TagType.UINT, lower=0, upper=255),
            array_type("Buf",
                         element_type=named_type("Byte"),
                         bounds=[(0, 7)]),
        ],
        subroutines=[prog("Main", main=True,
            local_vars=[var("b", named_type("Buf"))],
            st_body=[assign(index_("b", lit(0)), lit(255))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


# -----------------------------------------------------------------------------
# Non-literal RHS skips silently (no false positive)
# -----------------------------------------------------------------------------


def test_non_literal_value_skips_range_check():
    """A variable on the RHS can't be range-checked without
    value-flow analysis; the validator stays silent."""
    p = program(
        user_types=[subrange_type("Pct",
                                     base=TagType.UINT,
                                     lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Pct")),
                         var("source", TagType.INT)],
            st_body=[assign("p", "source")],
        )],
    )
    # Type compat may still complain (or not) depending on
    # bucket; the key is that subrange-out-of-range doesn't fire.
    assert "subrange-out-of-range" not in _codes(p)


# -----------------------------------------------------------------------------
# Based-int literal value extraction
# -----------------------------------------------------------------------------


def test_based_int_literal_parses_and_validates():
    """``16#FF`` = 255; assigning to a [0..255] subrange passes."""
    p = program(
        user_types=[subrange_type("Byte",
                                     base=TagType.UINT,
                                     lower=0, upper=255)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("b", named_type("Byte"))],
            st_body=[assign("b",
                              Literal(value="16#FF", kind="typed"))],
        )],
    )
    assert "subrange-out-of-range" not in _codes(p)


def test_based_int_literal_above_range_raises():
    """``16#100`` = 256; one over a [0..255] subrange."""
    p = program(
        user_types=[subrange_type("Byte",
                                     base=TagType.UINT,
                                     lower=0, upper=255)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("b", named_type("Byte"))],
            st_body=[assign("b",
                              Literal(value="16#100", kind="typed"))],
        )],
    )
    assert "subrange-out-of-range" in _codes(p)


# -----------------------------------------------------------------------------
# Non-integer types don't trigger the check (no false positives)
# -----------------------------------------------------------------------------


def test_real_literal_to_subrange_doesnt_range_check():
    """A REAL literal can't be parsed into an integer value;
    the range check skips.  Bucket mismatch may fire separately."""
    p = program(
        user_types=[subrange_type("Pct",
                                     base=TagType.UINT,
                                     lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p", named_type("Pct"))],
            st_body=[assign("p", lit(3.14))],
        )],
    )
    # subrange-out-of-range should NOT fire on a REAL literal
    assert "subrange-out-of-range" not in _codes(p)


# -----------------------------------------------------------------------------
# Plain (non-subrange) target -- check doesn't accidentally fire
# -----------------------------------------------------------------------------


def test_plain_int_target_no_subrange_check():
    """No SUBRANGE in the target chain -> no range check."""
    p = program(subroutines=[prog("Main", main=True,
        local_vars=[var("x", TagType.INT)],
        st_body=[assign("x", lit(99999))],
    )])
    assert "subrange-out-of-range" not in _codes(p)
