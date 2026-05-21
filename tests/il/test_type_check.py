"""Tests for semantic type checking on rung ops (IEC §6.5).

The validator now runs a type-compatibility pass over every
``Move`` / ``BinaryMath`` / ``Compare`` / ``OutCoil`` /
``OutSet`` / ``OutReset`` in each POU's rungs, using the merged
type env from ``Program.tags`` + per-POU variable interfaces +
the UDT registry.

Five new error codes are emitted:

  - ``move-type-mismatch``
  - ``binary-math-non-numeric``
  - ``binary-math-type-mismatch``
  - ``compare-type-mismatch``
  - ``coil-target-not-bool``

The compatibility rules cluster types into IEC §6.5 buckets:
integer family (signed + unsigned), real family (REAL/LREAL),
bit-string family (BYTE/WORD/DWORD).  Cross-bucket usage raises;
unknown operand types (literals, unresolved tags) skip the check
so the structural validator's ``unresolved-tagref`` stays
canonical for naming issues.
"""
import pytest

from universal_machinery.builders import (
    add, alias_type, coil, eq, gt, move, named_type, no, prog, program,
    reset_, rung, set_, sub, tag_decl, var, var_in,
)
from universal_machinery.il import Address, TagRef, TagType
from universal_machinery.validation import validate


def _codes(prog):
    return [e.code for e in validate(prog)]


# -----------------------------------------------------------------------------
# Clean programs (no type errors)
# -----------------------------------------------------------------------------


def test_clean_bool_coil_with_bool_contact():
    p = program(
        tags=[tag_decl("start", TagType.BOOL),
               tag_decl("running", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("start"), coil("running")),
        ])],
    )
    assert _codes(p) == []


def test_clean_integer_math():
    p = program(
        tags=[tag_decl("a", TagType.INT),
               tag_decl("b", TagType.INT),
               tag_decl("c", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(add("a", "b", "c")),
        ])],
    )
    assert _codes(p) == []


def test_clean_real_math():
    p = program(
        tags=[tag_decl("x", TagType.REAL),
               tag_decl("y", TagType.REAL),
               tag_decl("z", TagType.REAL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(add("x", "y", "z")),
        ])],
    )
    assert _codes(p) == []


def test_signed_unsigned_integers_implicitly_compatible():
    """IEC §6.5 + real-world vendor practice: signed/unsigned
    integers of any width sit in one compatibility bucket."""
    p = program(
        tags=[tag_decl("u", TagType.UINT),
               tag_decl("i", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(move("u", "i")),
        ])],
    )
    assert _codes(p) == []


def test_real_lreal_compatible():
    p = program(
        tags=[tag_decl("r32", TagType.REAL),
               tag_decl("r64", TagType.LREAL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(move("r32", "r64")),
        ])],
    )
    assert _codes(p) == []


def test_bit_string_int_cross_compatible():
    """BYTE/WORD/DWORD and integers freely interconvert."""
    p = program(
        tags=[tag_decl("w", TagType.WORD),
               tag_decl("i", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(move("w", "i")),
        ])],
    )
    assert _codes(p) == []


def test_local_vars_take_precedence_over_global_tags():
    """A POU-local INT shadows a global BOOL of the same name."""
    p = program(
        tags=[tag_decl("count", TagType.BOOL)],   # global BOOL
        subroutines=[prog("Main", main=True,
            local_vars=[var("count", TagType.INT)],   # local INT shadows
            rungs=[rung(add("count", 1, "count"))],
        )],
    )
    # Math on the local INT is fine; the BOOL tag isn't seen
    assert "binary-math-non-numeric" not in _codes(p)


# -----------------------------------------------------------------------------
# Move type-mismatch
# -----------------------------------------------------------------------------


def test_bool_to_int_move_raises():
    p = program(
        tags=[tag_decl("flag", TagType.BOOL),
               tag_decl("counter", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(move("flag", "counter")),
        ])],
    )
    assert "move-type-mismatch" in _codes(p)


def test_real_to_bool_move_raises():
    p = program(
        tags=[tag_decl("temp", TagType.REAL),
               tag_decl("flag", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(move("temp", "flag")),
        ])],
    )
    assert "move-type-mismatch" in _codes(p)


def test_int_to_real_move_raises():
    """INT and REAL sit in different buckets per V1 -- explicit
    INT_TO_REAL conversion required."""
    p = program(
        tags=[tag_decl("i", TagType.INT),
               tag_decl("r", TagType.REAL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(move("i", "r")),
        ])],
    )
    assert "move-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# BinaryMath checks
# -----------------------------------------------------------------------------


def test_math_with_bool_operand_raises_non_numeric():
    p = program(
        tags=[tag_decl("flag", TagType.BOOL),
               tag_decl("count", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(add("flag", 1, "count")),
        ])],
    )
    assert "binary-math-non-numeric" in _codes(p)


def test_math_with_string_operand_raises_non_numeric():
    p = program(
        tags=[tag_decl("text", TagType.STRING),
               tag_decl("count", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(add("text", "count", "count")),
        ])],
    )
    assert "binary-math-non-numeric" in _codes(p)


def test_math_dst_mismatched_bucket_raises():
    """INT ADD INT -> REAL crosses a bucket boundary."""
    p = program(
        tags=[tag_decl("a", TagType.INT),
               tag_decl("b", TagType.INT),
               tag_decl("r", TagType.REAL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(add("a", "b", "r")),
        ])],
    )
    assert "binary-math-type-mismatch" in _codes(p)


# -----------------------------------------------------------------------------
# Compare checks
# -----------------------------------------------------------------------------


def test_compare_bool_to_int_raises():
    p = program(
        tags=[tag_decl("flag", TagType.BOOL),
               tag_decl("count", TagType.INT),
               tag_decl("out", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(eq("flag", "count"), coil("out")),
        ])],
    )
    assert "compare-type-mismatch" in _codes(p)


def test_compare_string_to_int_raises():
    p = program(
        tags=[tag_decl("s", TagType.STRING),
               tag_decl("n", TagType.INT),
               tag_decl("out", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(gt("s", "n"), coil("out")),
        ])],
    )
    assert "compare-type-mismatch" in _codes(p)


def test_compare_against_literal_skipped():
    """Comparing a tag against a literal value doesn't raise --
    the literal's type is unknown, so the check skips."""
    p = program(
        tags=[tag_decl("speed", TagType.INT),
               tag_decl("out", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(gt("speed", 100), coil("out")),
        ])],
    )
    assert "compare-type-mismatch" not in _codes(p)


# -----------------------------------------------------------------------------
# Coil-target checks
# -----------------------------------------------------------------------------


def test_coil_to_int_raises():
    p = program(
        tags=[tag_decl("speed", TagType.INT),
               tag_decl("start", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("start"), coil("speed")),
        ])],
    )
    assert "coil-target-not-bool" in _codes(p)


def test_set_coil_to_real_raises():
    p = program(
        tags=[tag_decl("temp", TagType.REAL),
               tag_decl("start", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("start"), set_("temp")),
        ])],
    )
    assert "coil-target-not-bool" in _codes(p)


def test_reset_coil_to_word_raises():
    p = program(
        tags=[tag_decl("flags", TagType.WORD),
               tag_decl("start", TagType.BOOL)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("start"), reset_("flags")),
        ])],
    )
    assert "coil-target-not-bool" in _codes(p)


def test_coil_to_unresolved_name_doesnt_raise_type_error():
    """Unresolved names are caught by the structural pass with
    ``unresolved-tagref``; the type checker stays silent so the
    user sees one canonical complaint."""
    p = program(
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("X1"), coil("Y1")),
        ])],
    )
    # Address values without matching tag entries are unresolved
    # -> type check skips, no false positive
    codes = _codes(p)
    assert "coil-target-not-bool" not in codes


# -----------------------------------------------------------------------------
# UDT resolution
# -----------------------------------------------------------------------------


def test_alias_of_int_resolves_for_type_check():
    """A Var typed as ``Distance`` (alias of REAL) should resolve
    to REAL in the type-env, so REAL math against it works
    without complaint."""
    p = program(
        user_types=[alias_type("Distance", base=TagType.REAL)],
        tags=[],
        subroutines=[prog("Main", main=True,
            local_vars=[var("d1", named_type("Distance")),
                          var("d2", named_type("Distance")),
                          var("d3", named_type("Distance"))],
            rungs=[rung(add("d1", "d2", "d3"))],
        )],
    )
    assert _codes(p) == []


def test_subrange_resolves_to_base_int():
    """SUBRANGE Percent (UINT 0..100) -- math on Percent values
    is integer math."""
    from universal_machinery.builders import subrange_type
    p = program(
        user_types=[subrange_type("Percent",
                                     base=TagType.UINT,
                                     lower=0, upper=100)],
        subroutines=[prog("Main", main=True,
            local_vars=[var("p1", named_type("Percent")),
                          var("p2", named_type("Percent")),
                          var("p3", named_type("Percent"))],
            rungs=[rung(add("p1", "p2", "p3"))],
        )],
    )
    assert _codes(p) == []


# -----------------------------------------------------------------------------
# Location annotation
# -----------------------------------------------------------------------------


def test_error_location_carries_subroutine_and_rung_index():
    p = program(
        tags=[tag_decl("flag", TagType.BOOL),
               tag_decl("count", TagType.INT)],
        subroutines=[prog("Main", main=True, rungs=[
            rung(no("flag"), coil("count")),
        ])],
    )
    errs = validate(p)
    coil_err = next(e for e in errs if e.code == "coil-target-not-bool")
    assert "Main" in coil_err.location
    assert "rung 0" in coil_err.location
